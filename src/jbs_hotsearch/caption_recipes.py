# -*- coding: utf-8 -*-
"""小红书文案配方（A/B 实验）。

设计目标：改文案不用改代码、不用重新部署。
配方（prompt + 采样参数 + 已解析本筛选口径）存在 Supabase 的 caption_recipes 表，
服务每天出榜时**按 board_date 稳定哈希**挑一套，并把「当天用了哪套 + 出了什么」
写进 caption_runs，几周后回看即可对比各配方的实际表现。

三条兜底，保证任何时候都不会出空文案：
  1. 表不存在 / 查不到 / 全部 disabled → 回退内置配方（= 改造前那套 prompt 与参数）
  2. 配方里的 prompt 占位符写错导致 render 失败 → 回退内置模板
  3. LLM 调用失败 → 回退固定模板文案

轮换为什么用「日期哈希」而不是随机：
  同一天重跑（重试 / 手动补跑）必须命中同一套，否则同一份榜单会拿到两条不同文案，
  实验归因就废了。哈希输入只有 board_date，与进程、时区、调用次数无关。
  注意：增删配方或改权重会让之后的分流重算，历史归属一律以 caption_runs 记录为准。
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

import httpx

from .config import Config
from .models import DailyBoard, RankedScript

logger = logging.getLogger(__name__)

RECIPE_TABLE = "caption_recipes"
RUN_TABLE = "caption_runs"

# 内置默认值：配方里对应字段留空时用这些
DEFAULT_TEMPERATURE = 0.8
DEFAULT_MAX_TOKENS = 400
DEFAULT_REPETITION_PENALTY = 1.15
DEFAULT_MAX_LEN = 600

# LLM 偶发「重复退化」：结尾刷出上百个相同 emoji（如 😉😉😉…）。
# 这里只压缩「非中文/非字母数字」符号的连续重复（3 个及以上压成 1 个），
# 因此「哈哈哈」「！！！」这类合法中文表达不会被误伤。
_REPEAT_RUN = re.compile(r"([^\s\w\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\1{2,}")

BUILTIN_SYSTEM = "你是小红书剧本杀垂类博主，文案口语化、有情绪、有钩子。"

BUILTIN_USER = (
    "以下是今天（{date}）的杭州剧本杀热度榜 Top{top_n}：\n"
    "{summary}\n\n"
    "请以小红书剧本杀垂类博主的语气写一段发布文案，要求：\n"
    "1. 第一行是标题，带 1-2 个 emoji，要有钩子（点出榜首或最大黑马）\n"
    "2. 正文 2-4 句话，口语化，点出 1-3 个值得关注的点（榜首、上升快、新上榜）\n"
    "3. 结尾 3-5 个话题标签，如 #剧本杀 #杭州剧本杀 #周末去哪儿\n"
    "{parsed_hint}"
    "直接输出文案，不要任何解释或前后缀。"
)


@dataclass
class Recipe:
    """一套文案配方。字段留空表示「用内置默认」。"""

    id: str
    name: str
    enabled: bool = True
    weight: int = 1
    system_prompt: str = ""
    user_prompt: str = ""
    model: str = ""
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    repetition_penalty: float | None = None
    max_len: int | None = None
    include_parsed: bool = True
    parsed_ratio: float | None = None
    parsed_max: int | None = None
    note: str = ""
    is_builtin: bool = False

    def as_meta(self) -> dict[str, Any]:
        """给素材页 / caption.json 用的摘要（不含 prompt 全文，避免把配方泄到公网页）。"""
        return {
            "id": self.id,
            "name": self.name,
            "is_builtin": self.is_builtin,
            "model": self.model or "",
            "temperature": self.temperature if self.temperature is not None else DEFAULT_TEMPERATURE,
            "max_tokens": self.max_tokens or DEFAULT_MAX_TOKENS,
            "note": self.note,
        }


@dataclass
class CaptionResult:
    caption: str
    source: str  # llm / template
    recipe: Recipe
    model: str
    error: str | None = None


def builtin_recipe() -> Recipe:
    """内置配方：与改造前 social.py 的行为完全一致，作为所有异常的兜底。"""
    return Recipe(
        id="builtin",
        name="内置默认（未走配方表）",
        system_prompt=BUILTIN_SYSTEM,
        user_prompt=BUILTIN_USER,
        is_builtin=True,
    )


# ─────────── 读配方 ────────────────────────────────────────────────
def _supabase_get(cfg: Config, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    base = f"{cfg.supabase_url.rstrip('/')}/rest/v1"
    headers = {
        "apikey": cfg.supabase_service_role_key,
        "Authorization": f"Bearer {cfg.supabase_service_role_key}",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=cfg.http_timeout, headers=headers) as client:
        resp = client.get(f"{base}/{table}", params=params)
    if resp.status_code >= 400:
        body = resp.text[:300]
        if "PGRST205" in body or "does not exist" in body:
            raise RuntimeError(
                f"表不存在：Supabase 里还没有 public.{table}。"
                f"去 Dashboard -> SQL Editor 执行一次 sql/caption_recipes.sql"
            )
        raise RuntimeError(f"Supabase 读取 {table} 失败 {resp.status_code}: {body}")
    return resp.json() or []


def _row_to_recipe(row: dict[str, Any]) -> Recipe:
    def f(key: str) -> float | None:
        v = row.get(key)
        return float(v) if v is not None else None

    def i(key: str) -> int | None:
        v = row.get(key)
        return int(v) if v is not None else None

    return Recipe(
        id=str(row.get("id") or ""),
        name=str(row.get("name") or row.get("id") or ""),
        enabled=bool(row.get("enabled", True)),
        weight=max(1, int(row.get("weight") or 1)),
        system_prompt=str(row.get("system_prompt") or ""),
        user_prompt=str(row.get("user_prompt") or ""),
        model=str(row.get("model") or ""),
        temperature=f("temperature"),
        top_p=f("top_p"),
        max_tokens=i("max_tokens"),
        repetition_penalty=f("repetition_penalty"),
        max_len=i("max_len"),
        include_parsed=bool(row.get("include_parsed", True)),
        parsed_ratio=f("parsed_ratio"),
        parsed_max=i("parsed_max"),
        note=str(row.get("note") or ""),
    )


def load_recipes(cfg: Config) -> list[Recipe]:
    """读出全部配方（含 disabled，供 CLI 展示）。取不到就只返回内置配方。"""
    if not getattr(cfg, "social_caption_recipes", True):
        return [builtin_recipe()]
    if not (cfg.supabase_url and cfg.supabase_service_role_key):
        logger.info("未配置 Supabase，文案走内置配方")
        return [builtin_recipe()]
    try:
        rows = _supabase_get(
            cfg, RECIPE_TABLE, {"select": "*", "order": "id.asc", "limit": "200"}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取文案配方失败，走内置配方：%s", exc)
        return [builtin_recipe()]
    recipes = [r for r in (_row_to_recipe(x) for x in rows) if r.id]
    if not recipes:
        logger.info("caption_recipes 表为空，文案走内置配方")
        return [builtin_recipe()]
    return recipes


def pick_recipe(recipes: list[Recipe], board_date: date) -> Recipe:
    """按 board_date 稳定哈希挑一套启用中的配方（加权）。

    同一 board_date + 同一批配方 → 必然同一结果，重跑不会串味。
    """
    pool = [r for r in recipes if r.enabled and not r.is_builtin] or [r for r in recipes if r.enabled]
    if not pool:
        return builtin_recipe()
    if len(pool) == 1:
        return pool[0]
    total = sum(r.weight for r in pool)
    digest = hashlib.md5(board_date.isoformat().encode("utf-8")).hexdigest()
    cursor = int(digest, 16) % total
    acc = 0
    for r in pool:
        acc += r.weight
        if cursor < acc:
            return r
    return pool[-1]


# ─────────── 上下文与渲染 ───────────────────────────────────────────
def _parsed_for_caption(board: DailyBoard, ratio: float, max_n: int) -> list[RankedScript]:
    """挑出值得写进文案的已解析剧本：原热度 ≥ 榜首热度 × ratio，按热度降序取前 max_n 本。

    为什么按「相对榜首」而不是绝对分数：每天热度尺度会漂移（有时榜首 100、有时 60），
    绝对值阈值不稳定；相对榜首能稳定表达「这个本够不够格跟榜首相提并论」。
    """
    if not board.items or not board.filtered_items:
        return []
    top = float(board.items[0].hot_score or 0)
    if ratio <= 0:  # 0 = 不筛选
        picked = list(board.filtered_items)
    else:
        threshold = top * ratio
        picked = [it for it in board.filtered_items if float(it.hot_score or 0) >= threshold]
    picked.sort(key=lambda it: -float(it.hot_score or 0))
    return picked[: max(1, max_n)]


def build_context(
    cfg: Config, board: DailyBoard, recipe: Recipe, meta_fn: Callable[[RankedScript], str]
) -> dict[str, Any]:
    """把榜单 + 配方口径拼成 prompt 占位符上下文。

    配方里 ratio / max 留空时沿用 config 的全局默认（HS_SOCIAL_CAPTION_PARSED_*），
    这样既能全局调，也能单个 variant 单独调。
    """
    ratio = (
        float(recipe.parsed_ratio)
        if recipe.parsed_ratio is not None
        else float(cfg.social_caption_parsed_ratio)
    )
    max_n = (
        int(recipe.parsed_max)
        if recipe.parsed_max is not None
        else int(cfg.social_caption_parsed_max)
    )
    caption_parsed = _parsed_for_caption(board, ratio, max_n) if recipe.include_parsed else []

    lines = []
    for it in board.items:
        meta = meta_fn(it)
        suffix = f"，{meta}" if meta else ""
        lines.append(f"{it.rank}. {it.title}（热度 {it.hot_score:.0f}{suffix}）")

    parsed_names = "、".join(it.title for it in caption_parsed)
    if caption_parsed:
        parsed_detail = "、".join(
            f"{it.title}（原热度 {it.hot_score:.0f}）" for it in caption_parsed
        )
        lines.append(f"已解析（攻略已上线，未入榜）：{parsed_detail}")
        parsed_hint = (
            f"4. 文末必须点名提到这些已解析剧本：{parsed_names}。"
            "说明它们热度够高但攻略已整理好（DM 手册已入库），并引导读者私信或看主页获取攻略；"
            "注意不要把它们写成榜内排名，它们是「已解析未入榜」\n"
        )
    else:
        parsed_hint = ""

    return {
        "date": board.board_date.isoformat(),
        "top_n": str(len(board.items)),
        "top1": board.items[0].title if board.items else "——",
        "summary": "\n".join(lines),
        "parsed_hint": parsed_hint,
        "parsed_names": parsed_names,
        "_parsed": caption_parsed,  # 供模板兜底使用，不参与 format
    }


def render_prompt(recipe: Recipe, ctx: dict[str, Any]) -> str:
    """渲染 user prompt；占位符写错时回退内置模板，不让 LLM 收到半截模板。"""
    tpl = recipe.user_prompt or BUILTIN_USER
    payload = {k: v for k, v in ctx.items() if not k.startswith("_")}
    try:
        return tpl.format(**payload)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("配方 %s 的 prompt 渲染失败（%s），回退内置模板", recipe.id, exc)
        return BUILTIN_USER.format(**payload)


# ─────────── 清洗 / LLM ────────────────────────────────────────────
def sanitize_caption(text: str, max_len: int | None = None) -> str:
    """清洗 LLM 文案：压掉重复符号串、去空行、超长截断。"""
    if not text:
        return ""
    limit = max_len or DEFAULT_MAX_LEN
    cleaned = _REPEAT_RUN.sub(r"\1", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) > limit:
        cut = cleaned[:limit].rstrip()
        # 尽量在句子边界截断，避免半句话
        for sep in ("\n", "。", "！", "？"):
            pos = cut.rfind(sep)
            if pos > limit * 0.6:
                return cut[: pos + 1]
        return cut + "…"
    return cleaned


def _call_llm(cfg: Config, recipe: Recipe, system: str, user: str) -> str | None:
    if not cfg.llm_api_key:
        return None
    payload: dict[str, Any] = {
        "model": recipe.model or cfg.llm_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": recipe.temperature if recipe.temperature is not None else DEFAULT_TEMPERATURE,
        "max_tokens": recipe.max_tokens or DEFAULT_MAX_TOKENS,
        # 从源头抑制「重复退化」（曾出现过结尾刷上百个 😉 的情况）
        "repetition_penalty": (
            recipe.repetition_penalty
            if recipe.repetition_penalty is not None
            else DEFAULT_REPETITION_PENALTY
        ),
    }
    if recipe.top_p is not None:
        payload["top_p"] = recipe.top_p
    try:
        resp = httpx.post(
            f"{cfg.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json=payload,
            timeout=cfg.http_timeout + 30.0,
        )
        if resp.status_code != 200:
            logger.warning("文案 LLM 返回 %s：%s", resp.status_code, resp.text[:200])
            return None
        content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        content = sanitize_caption((content or "").strip().strip('"'), recipe.max_len)
        return content or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("文案 LLM 调用失败，回退模板：%s", exc)
        return None


# ─────────── 对外主入口 ────────────────────────────────────────────
def gen_daily_caption(
    cfg: Config,
    board: DailyBoard,
    meta_fn: Callable[[RankedScript], str],
    template_fn: Callable[[DailyBoard, list[RankedScript]], str],
) -> CaptionResult:
    """按配方生成日报文案，返回 (文案, 来源, 配方, 模型)。任何环节失败都不会抛异常。"""
    recipes = load_recipes(cfg)
    recipe = pick_recipe(recipes, board.board_date)
    ctx = build_context(cfg, board, recipe, meta_fn)
    parsed = ctx.get("_parsed") or []

    model = recipe.model or cfg.llm_model
    system = recipe.system_prompt or BUILTIN_SYSTEM
    text = _call_llm(cfg, recipe, system, render_prompt(recipe, ctx))
    if text:
        return CaptionResult(caption=text, source="llm", recipe=recipe, model=model)

    fallback = template_fn(board, parsed)
    return CaptionResult(caption=fallback, source="template", recipe=recipe, model=model)


def log_run(cfg: Config, board_date: date, result: CaptionResult, kind: str = "daily") -> None:
    """把「哪天用了哪套 + 出了什么」写进 caption_runs，供几周后回看对比。

    用 (board_date, kind) 唯一键做 upsert，重跑覆盖当天记录而不是堆重复行。
    写失败只告警，绝不影响出榜。
    """
    if not (cfg.supabase_url and cfg.supabase_service_role_key):
        return
    payload = {
        "board_date": board_date.isoformat(),
        "kind": kind,
        "recipe_id": None if result.recipe.is_builtin else result.recipe.id,
        "recipe_name": result.recipe.name,
        "source": result.source,
        "model": result.model or None,
        "temperature": (
            result.recipe.temperature
            if result.recipe.temperature is not None
            else DEFAULT_TEMPERATURE
        ),
        "caption": result.caption,
        "caption_len": len(result.caption),
        "error": result.error,
    }
    try:
        base = f"{cfg.supabase_url.rstrip('/')}/rest/v1"
        headers = {
            "apikey": cfg.supabase_service_role_key,
            "Authorization": f"Bearer {cfg.supabase_service_role_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        with httpx.Client(timeout=cfg.http_timeout, headers=headers) as client:
            resp = client.post(
                f"{base}/{RUN_TABLE}",
                params={"on_conflict": "board_date,kind"},
                json=payload,
            )
        if resp.status_code >= 400:
            logger.warning("caption_runs 写入失败 %s：%s", resp.status_code, resp.text[:200])
    except Exception as exc:  # noqa: BLE001
        logger.warning("caption_runs 写入异常：%s", exc)
