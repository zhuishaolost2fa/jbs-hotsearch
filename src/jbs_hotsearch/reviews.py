# -*- coding: utf-8 -*-
"""店家评测聚合：从评论接口的 HAR 里抽「带店名」的评论，按店家聚合、输出排行榜 + 小红书素材。

输入：HAR（米圈 App 详情页「全部评论」分页截包，接口 /v13/script/getScriptEvaluateList）
处理：
  1. 解码 base64 内层 data + 响应里的 items；
  2. **排除无店名评论**（item.shopInfo 为空）—— 这些是玩家没填店家；
  3. 按 shopId 聚合：评论数 + 各维度均值（剧情/推理/玩法，取 scriptLabelScores 与 scriptLabelNames 对齐均值）；
  4. 评论文本里再正则抽 DM 名字（DM[名]/dm[名]/[名]主持 等），店家内部按 DM 二次聚合；
  5. 评分口径：综合 = 三维度均值的均值；推荐门槛：评论数 ≥ min_reviews（默认 2）。

产物：
  data/reviews/{script_key}.html          店家排行榜（手机可读，与 site.py 同款视觉）
  data/reviews/{script_key}.txt           小红书文案（LLM 优先，模板兜底）
  data/reviews/social/{date}.html         小红书素材页（海报截图 + 文案 + 一键复制）
  data/reviews/social/{date}.png          海报截图（如 Playwright 可用）
  data/reviews/social/{date}.txt          当日文案
"""
from __future__ import annotations

import base64
import html
import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from .config import Config

logger = logging.getLogger(__name__)

# ─────────── 评分子结构 ─────────────────────────────────────────────
@dataclass
class Review:
    evaluate_id: str
    shop_id: str | None
    shop_name: str | None
    shop_district: str | None
    nick_name: str
    text: str
    label_names: list[str]
    label_scores: list[float]
    recommend_degree: int
    played_time: int  # ms timestamp

    @property
    def has_shop(self) -> bool:
        return bool(self.shop_id and self.shop_name)

    @property
    def dm_name(self) -> str | None:
        """从评论文本里抽 DM 名；没抽到返回 None（归入「未注明 DM」）。"""
        if not self.has_shop:
            return None  # 没店就一定没有可靠的 DM 归属
        return _extract_dm(self.text)


# ─────────── DM 正则 ────────────────────────────────────────────────
# 强弱两套模式：强 = DM 后面紧跟"老师/带的/带我们"等明确指示词；弱 = 普通 DM 标记。
# 强模式优先；多条匹配时取最强那条。这样能从"评价店铺：很吃dm虽然是dm小天才的首车"
# 中正确抽出"小天才"而不是"虽然"。
_DM_STRONG_PATTERNS = [
    # DM 紧跟"老师/带的/带我们/带本/带车/带整/带完"等强指示词
    re.compile(r"[Dd][Mm]\s*[:：@]?\s*([\u4e00-\u9fff]{2,4})\s*(?:老师|带的|带我们|带本|带车|带整|带完)"),
    # DM + 系动词（是/为/叫/就是）+ 名（我们的 dm 是 里水）
    re.compile(r"[Dd][Mm]\s*[:：@]?\s*(?:是|为|叫|就是)\s*([\u4e00-\u9fff]{2,4})"),
    # [名] 老师 带的
    re.compile(r"([\u4e00-\u9fff]{2,4})\s*老师\s*带的"),
    # 主持人 是 / 老师 / 带的
    re.compile(r"主持人\s*[:：]?\s*(?:是|为|叫)?\s*([\u4e00-\u9fff]{2,4})\s*(?:老师|带的|带我们)"),
]
_DM_WEAK_PATTERNS = [
    re.compile(r"[Dd][Mm]\s*[:：@]?\s*([\u4e00-\u9fff]{2,4})"),
    re.compile(r"主持人\s*[:：]?\s*([\u4e00-\u9fff]{2,4})"),
]
# 完全无意义的词组（直接判否，常见于误命中）
_DM_DENY_NAMES = {
    "时候", "推荐", "感觉", "觉得", "我们", "你们", "他们", "这种",
    "那个", "这个", "一场", "一下", "一句", "今天", "昨天", "明天",
    "因为", "所以", "可以", "应该", "就是", "不是", "怎么", "什么",
    "如果", "然后", "现在", "知道",
    "虽然", "但是", "或者", "不过", "然后", "不然", "即使", "就算",
}
# 出现在名字末尾的字（裁剪后丢弃）：这些字常出现在"描述 DM 的短语"而非真名。
_DM_DENY_SUFFIX = set(
    # 助词 / 副词
    "的了着过得着很也都也特别非常一直一定真的有点"
    # 动词
    "带走推玩唱看听说做让去来在跟把下上出回是"
    # 形容词 / 状态
    "太真极超好"
    # 常见名词延续（不是名字）
    "车牛鬼玩推抗唱厉思导引看听到跑控场查专改用路编怎"
    # 标点紧邻的"了"
    "了"
)
# 2 字结果尾字若是这里面的，判否（这些字几乎不会出现在 DM 名字的结尾）
_DM_DENY_END_2 = set(
    "像拦扶纠礼一抗走牛鬼思导引玩唱局盘物完摆弄车"
    "的了吗啊吧呢太拉也拉得像老场控专改用路编怎"
)

def _validate_dm(raw: str) -> str | None:
    """对 raw 字符串做长度/停用字/黑名单等清洗；通过返回清理后的名，否则 None。"""
    if raw.endswith("老师"):
        raw = raw[:-2]
    name = raw
    if name in _DM_DENY_NAMES:
        return None
    # 4+ 字：先按停用尾字裁，再硬截到 3
    while len(name) > 2 and name[-1] in _DM_DENY_SUFFIX:
        name = name[:-1]
    if len(name) >= 4:
        name = name[:3]
    # 3 字：首/末位是停用字都视为杂质
    if len(name) == 3:
        if name[-1] in _DM_DENY_SUFFIX and name[0] in _DM_DENY_SUFFIX:
            return None  # 两端都脏，整体可疑
        if name[-1] in _DM_DENY_SUFFIX:
            name = name[:2]  # 末位脏，保留前 2
        elif name[0] in _DM_DENY_SUFFIX:
            name = name[1:]  # 首位脏，保留后 2
    if len(name) == 2 and name[-1] in _DM_DENY_END_2:
        return None
    if name in _DM_DENY_NAMES or len(name) < 2:
        return None
    return name


def _extract_dm(text: str) -> str | None:
    """从评论文本里抽 DM 名；失败返回 None（归入「未注明 DM」）。

    策略：先按强模式（DM 后紧跟"老师/带的/带我们"等强指示词）抽；找不到再退回弱模式。
    强模式命中后再做长度/黑名单裁剪。多条候选取**通过校验的第一个**。
    """
    for pat in _DM_STRONG_PATTERNS:
        for m in pat.finditer(text):
            cand = _validate_dm(m.group(1))
            if cand:
                return cand
    for pat in _DM_WEAK_PATTERNS:
        for m in pat.finditer(text):
            cand = _validate_dm(m.group(1))
            if cand:
                return cand
    return None


# ─────────── HAR 解析 ──────────────────────────────────────────────
def load_reviews_from_har(har_path: Path) -> list[Review]:
    """解析 HAR 中的所有评论项，**不过滤**；聚合时再按 shopInfo 筛。"""
    har = json.loads(har_path.read_text(encoding="utf-8"))
    out: list[Review] = []
    for entry in har["log"]["entries"]:
        url = entry["request"]["url"]
        if "/v13/script/getScriptEvaluateList" not in url:
            continue
        try:
            resp_text = entry["response"]["content"]["text"]
            data = json.loads(resp_text)["data"]
        except Exception:
            continue
        for it in data.get("items") or []:
            si = it.get("shopInfo") or {}
            out.append(Review(
                evaluate_id=str(it.get("scriptEvaluateId") or it.get("id") or ""),
                shop_id=str(si["shopId"]) if si.get("shopId") else None,
                shop_name=si.get("shopName"),
                shop_district=si.get("shopDistrict"),
                nick_name=it.get("nickName") or "匿名",
                text=it.get("evaluateTextContent") or "",
                label_names=list(it.get("scriptLabelNames") or []),
                label_scores=list(it.get("scriptLabelScores") or []),
                recommend_degree=int(it.get("recommendDegree") or 0),
                played_time=int(it.get("playedScriptTime") or 0),
            ))
    return out


# ─────────── 聚合 ──────────────────────────────────────────────────
@dataclass
class DMStat:
    name: str
    reviews: int
    label_means: dict[str, float]
    recommend_count: int
    samples: list[str] = field(default_factory=list)  # 评论样本（最多 2 条）


@dataclass
class ShopStat:
    shop_id: str
    shop_name: str
    shop_district: str | None
    reviews: int
    label_means: dict[str, float]              # 维度 → 均值（剧情/推理/玩法）
    composite: float                            # 三维均值（参考，不主导展示）
    recommend_count: int
    dms: list[DMStat]                           # 已识别 DM（按评论数降序）
    undm_reviews: int                           # 未识别 DM 的评论数
    sample_reviews: list[dict] = field(default_factory=list)  # 最多 3 条样本
    summary: str = ""                           # 玩家评论摘要（80~120 字）
    summary_source: str = ""                    # "llm" / "template"


def _aggregate_dim(groups: dict[str, list[tuple[str, float]]]) -> dict[str, float]:
    """groups = {维度: [(评论key, 分值), ...]} → {维度: 均值}。"""
    return {dim: sum(s for _, s in rows) / len(rows) for dim, rows in groups.items() if rows}


def aggregate_by_shop(reviews: list[Review]) -> list[ShopStat]:
    by_shop: dict[str, list[Review]] = defaultdict(list)
    for r in reviews:
        if not r.has_shop:
            continue  # 没店的不参与聚合
        by_shop[r.shop_id].append(r)

    out: list[ShopStat] = []
    for sid, rs in by_shop.items():
        # 维度均值：每条评论把自己的 label_scores 按 label_names 对齐成 (dim, score)
        per_dim: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for r in rs:
            for n, s in zip(r.label_names, r.label_scores):
                per_dim[n].append((r.evaluate_id, float(s)))
        label_means = _aggregate_dim(per_dim)
        # 综合：维度均值的均值（仅纳入有数据的维度）
        composite = sum(label_means.values()) / len(label_means) if label_means else 0.0
        # DM 二次聚合
        by_dm: dict[str, list[Review]] = defaultdict(list)
        undm = 0
        for r in rs:
            n = r.dm_name
            if n:
                by_dm[n].append(r)
            else:
                undm += 1
        dm_stats: list[DMStat] = []
        for name, dms_rs in by_dm.items():
            dms_per_dim: dict[str, list[tuple[str, float]]] = defaultdict(list)
            for r in dms_rs:
                for n2, s in zip(r.label_names, r.label_scores):
                    dms_per_dim[n2].append((r.evaluate_id, float(s)))
            dm_stats.append(DMStat(
                name=name,
                reviews=len(dms_rs),
                label_means=_aggregate_dim(dms_per_dim),
                recommend_count=sum(1 for r in dms_rs if r.recommend_degree > 0),
                samples=[r.text[:60].replace("\n", " ") for r in dms_rs[:2]],
            ))
        dm_stats.sort(key=lambda d: (-d.reviews, -sum(d.label_means.values()) / max(1, len(d.label_means))))
        out.append(ShopStat(
            shop_id=sid,
            shop_name=rs[0].shop_name,
            shop_district=rs[0].shop_district,
            reviews=len(rs),
            label_means=label_means,
            composite=composite,
            recommend_count=sum(1 for r in rs if r.recommend_degree > 0),
            dms=dm_stats,
            undm_reviews=undm,
            sample_reviews=[
                {"nick": r.nick_name, "text": r.text, "labels": list(zip(r.label_names, r.label_scores))}
                for r in rs[:3]
            ],
        ))
    return out


def rank_shops(shops: list[ShopStat], min_reviews: int = 2) -> list[ShopStat]:
    """按综合分降序；门槛是评论数。"""
    eligible = [s for s in shops if s.reviews >= min_reviews]
    eligible.sort(key=lambda s: (-s.composite, -s.reviews))
    return eligible


# ─────────── 渲染：店家排行榜 ────────────────────────────────────────
_CSS = """
:root {
  --bg: #f5f3ee; --ink: #211d18; --muted: #8c8578;
  --brand: #e5532b; --line: #ece7dd; --card: #fff;
  --gold: #f2c14e; --gold2: #c8961e;
  --chip: #fff5ee; --chip-border: #f9d9c5;
  --sample-bg: #f9f6ef;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--ink); line-height: 1.6; padding: 20px 14px 60px; }
.wrap { max-width: 720px; margin: 0 auto; }
.hero { background: linear-gradient(135deg, #3a2015 0%, #6b2c1c 45%, #c8502a 100%);
  color: #fff; border-radius: 0 0 22px 22px; padding: 26px 22px 22px; margin: 0 -14px 18px; }
.hero .brand { font-size: 13px; letter-spacing: 3px; opacity: .82; }
.hero h1 { font-size: 28px; font-weight: 800; margin: 6px 0 4px; }
.hero .sub { font-size: 14px; opacity: .9; }
.hero .meta { font-size: 12px; opacity: .75; margin-top: 4px; margin-bottom: 12px; }
.copy-btn { font-size: 13px; padding: 6px 14px; border-radius: 20px; border: 1px solid rgba(255,255,255,.35);
  background: rgba(255,255,255,.12); color: #fff; cursor: pointer; }
.copy-btn:active { background: rgba(255,255,255,.22); }
.summary { display: flex; gap: 12px; margin: 0 0 14px; padding: 14px 16px; background: var(--card);
  border: 1px solid var(--line); border-radius: 14px; }
.summary > div { flex: 1; }
.summary b { display: block; font-size: 20px; color: var(--brand); }
.summary span { font-size: 12px; color: var(--muted); }
.section-title { font-size: 14px; font-weight: 700; color: var(--muted); margin: 22px 4px 10px; letter-spacing: 1px; }

.shop { background: var(--card); border: 1px solid var(--line); border-radius: 16px;
  padding: 14px 16px; margin-bottom: 14px; }
.shop-row1 { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }
.rank { flex: 0 0 26px; height: 26px; border-radius: 8px; background: #cfc8bb; color: #fff;
  display: inline-flex; align-items: center; justify-content: center; font-weight: 800; font-size: 14px; }
.rank-1 { background: linear-gradient(135deg, #f2c14e, #c8961e); }
.rank-2 { background: linear-gradient(135deg, #c6ccd2, #8d939b); }
.rank-3 { background: linear-gradient(135deg, #d9a06b, #b0723a); }
.score-line { font-size: 12px; color: var(--muted); text-align: right; }
.score-line b { color: var(--ink); font-weight: 700; font-size: 13px; margin-right: 2px; }
.shop-name { font-size: 19px; font-weight: 700; line-height: 1.35; margin-bottom: 4px; }
.shop-dist-row { margin-bottom: 10px; }
.shop-dist { font-size: 12px; color: var(--muted); padding: 1px 8px; border: 1px solid var(--line); border-radius: 20px; }
.score-badge { display: none; }

/* 玩家评论摘要：卡片主体 */
.summary-text { margin: 10px 0 8px; padding: 12px 14px; background: #fff8f1;
  border-left: 3px solid var(--brand); border-radius: 0 8px 8px 0;
  font-size: 14px; color: var(--ink); line-height: 1.7; }
.summary-text .label { font-size: 11px; color: var(--brand); font-weight: 700;
  letter-spacing: 1px; display: block; margin-bottom: 4px; }

/* 原文样本 */
.review-snip { background: var(--sample-bg); border-left: 3px solid #cfc8bb; padding: 8px 12px;
  margin-top: 6px; font-size: 12px; color: #4a4540; border-radius: 0 6px 6px 0; }
.review-snip .nick { color: var(--muted); margin-right: 6px; }
.review-snips-title { font-size: 11px; color: var(--muted); margin-top: 10px; letter-spacing: 1px; }

/* 点名 DM（底部） */
.dms { margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--line); font-size: 13px;
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.dms .dms-label { color: var(--muted); font-size: 12px; margin-right: 2px; }
.dm-chip { display: inline-block; padding: 3px 9px; background: var(--chip);
  border: 1px solid var(--chip-border); border-radius: 20px; font-size: 12px; }
.dm-chip .dm-name { font-weight: 700; }
.dm-chip .dm-n { color: var(--muted); font-size: 11px; margin-left: 4px; }
.dms-undm { color: var(--muted); font-size: 12px; padding: 3px 9px;
  border: 1px dashed var(--line); border-radius: 20px; }

.foot { text-align: center; color: var(--muted); font-size: 11px; margin-top: 18px; }
"""


def _fmt_score(v: float) -> str:
    return f"{v:.1f}" if v > 0 else "—"


def _fmt_shop_name(name: str, district: str | None) -> str:
    """如果店名末尾已经带了（区域），且与 shop_district 一致，则截掉避免重复显示。"""
    if district:
        suffix = f"（{district}）"
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def render_shop_html(
    script_title: str,
    shops: list[ShopStat],
    total_reviews: int,
    with_shop_reviews: int,
    caption: str = "",
) -> str:
    """店家排行榜 HTML（评论聚合优先，评分降权为角标）。"""
    rows = []
    for i, s in enumerate(shops, 1):
        rank_cls = f"rank rank-{i}" if i <= 3 else "rank"
        # DM chips
        dms_html = ""
        if s.dms:
            chips = "".join(
                f'<span class="dm-chip"><span class="dm-name">{html.escape(d.name)}</span>'
                f'<span class="dm-n">{d.reviews}条</span></span>'
                for d in s.dms[:3]
            )
            tail = (
                f'<span class="dms-undm">另 {s.undm_reviews} 条未注明 DM</span>'
                if s.undm_reviews else ""
            )
            dms_html = f'<div class="dms"><span class="dms-label">DM：</span>{chips}{tail}</div>'
        elif s.undm_reviews:
            dms_html = (
                f'<div class="dms"><span class="dms-label">DM：</span>'
                f'<span class="dms-undm">玩家未点名具体 DM</span></div>'
            )

        # 玩家评论摘要（核心）
        summary_html = (
            f'<div class="summary-text">'
            f'<span class="label">玩家评价</span>'
            f'{html.escape(s.summary)}</div>'
            if s.summary else ""
        )

        # 评论样本（原文摘录，最多 2 条）
        snips = ""
        picked = [sr for sr in s.sample_reviews[:3]
                  if len(sr.get("text", "")) >= 20][:2]
        if picked:
            snips_parts = []
            for sr in picked:
                txt = html.escape(_truncate_text(sr.get("text", ""), 120))
                nick = html.escape(sr.get("nick") or "匿名")
                snips_parts.append(
                    f'<div class="review-snip"><span class="nick">{nick}：</span>{txt}</div>'
                )
            snips = (
                f'<div class="review-snips-title">原评论摘录</div>'
                + "".join(snips_parts)
            )

        # 评分一行（右上角，弱化）
        score_line = (
            f'<div class="score-line"><b>{_fmt_score(s.composite)}</b> '
            f'剧情 {_fmt_score(s.label_means.get("剧情", 0))} · '
            f'推理 {_fmt_score(s.label_means.get("推理", 0))} · '
            f'玩法 {_fmt_score(s.label_means.get("玩法", 0))} · '
            f'{s.reviews} 条</div>'
        )

        rows.append(f"""
        <article class="shop">
          <div class="shop-row1">
            <span class="{rank_cls}">{i}</span>
            {score_line}
          </div>
          <div class="shop-name">{html.escape(_fmt_shop_name(s.shop_name, s.shop_district))}</div>
          <div class="shop-dist-row">
            <span class="shop-dist">{html.escape(s.shop_district or "—")}</span>
          </div>
          {summary_html}
          {snips}
          {dms_html}
        </article>""")

    excluded = total_reviews - with_shop_reviews
    llm_n = sum(1 for s in shops if s.summary_source == "llm")
    caption_json = json.dumps(caption, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(script_title)} · 店家榜</title>
<style>{_CSS}</style></head>
<body>
<script>window.__CAPTION__ = {caption_json};</script>
<div class="wrap">
  <header class="hero">
    <div class="brand">JBS · 评论聚合</div>
    <h1>{html.escape(script_title)} · 杭州店家榜</h1>
    <div class="sub">按玩家评论排序 · 评论区含店家的优先</div>
    <div class="meta">共 {total_reviews} 条评论，其中 {with_shop_reviews} 条注明店家（{excluded} 条已排除）</div>
    <button class="copy-btn" type="button" onclick="copyCaption(this)">📋 复制文案</button>
  </header>
  <section class="summary">
    <div><b>{len(shops)}</b><span>上榜店家</span></div>
    <div><b>{with_shop_reviews}</b><span>有效评论</span></div>
    <div><b>{llm_n}/{len(shops)}</b><span>AI 摘要覆盖</span></div>
  </section>
  {''.join(rows)}
  <div class="foot">评分仅作排序参考 · 真实选店请结合玩家原文与点名 DM</div>
</div>
<script>
function copyCaption(btn) {{
  const text = (typeof window !== 'undefined' && window.__CAPTION__) || '';
  if (!text) {{ btn.textContent = '暂无文案'; return; }}
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(function() {{
      const old = btn.textContent;
      btn.textContent = '✓ 已复制';
      setTimeout(function() {{ btn.textContent = old; }}, 1500);
    }}).catch(function() {{ fallbackCopy(text, btn); }});
  }} else {{
    fallbackCopy(text, btn);
  }}
}}
function fallbackCopy(text, btn) {{
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try {{ document.execCommand('copy'); btn.textContent = '✓ 已复制'; }}
  catch (err) {{ btn.textContent = '复制失败'; }}
  document.body.removeChild(ta);
  setTimeout(function() {{ btn.textContent = '📋 复制文案'; }}, 1500);
}}
</script>
</body></html>"""


def render_poster_html(
    script_title: str,
    shops: list[ShopStat],
    total_reviews: int,
    with_shop_reviews: int,
) -> str:
    """生成「保存图片」专用海报页：白底、固定宽度、无多余信息，方便手机截图。"""
    rows = []
    for i, s in enumerate(shops, 1):
        rank_cls = f"rank rank-{i}" if i <= 3 else "rank"
        dms_html = ""
        if s.dms:
            chips = "".join(
                f'<span class="dm-chip"><span class="dm-name">{html.escape(d.name)}</span>'
                f'<span class="dm-n">{d.reviews}条</span></span>'
                for d in s.dms[:3]
            )
            tail = (
                f'<span class="dms-undm">另 {s.undm_reviews} 条未注明 DM</span>'
                if s.undm_reviews else ""
            )
            dms_html = f'<div class="dms"><span class="dms-label">DM：</span>{chips}{tail}</div>'
        elif s.undm_reviews:
            dms_html = (
                f'<div class="dms"><span class="dms-label">DM：</span>'
                f'<span class="dms-undm">玩家未点名具体 DM</span></div>'
            )

        summary_html = (
            f'<div class="summary-text">'
            f'<span class="label">玩家评价</span>'
            f'{html.escape(s.summary)}</div>'
            if s.summary else ""
        )

        snips = ""
        picked = [sr for sr in s.sample_reviews[:3]
                  if len(sr.get("text", "")) >= 20][:2]
        if picked:
            snips_parts = []
            for sr in picked:
                txt = html.escape(_truncate_text(sr.get("text", ""), 120))
                nick = html.escape(sr.get("nick") or "匿名")
                snips_parts.append(
                    f'<div class="review-snip"><span class="nick">{nick}：</span>{txt}</div>'
                )
            snips = (
                f'<div class="review-snips-title">原评论摘录</div>'
                + "".join(snips_parts)
            )

        score_line = (
            f'<div class="score-line"><b>{_fmt_score(s.composite)}</b> '
            f'剧情 {_fmt_score(s.label_means.get("剧情", 0))} · '
            f'推理 {_fmt_score(s.label_means.get("推理", 0))} · '
            f'玩法 {_fmt_score(s.label_means.get("玩法", 0))} · '
            f'{s.reviews} 条</div>'
        )

        rows.append(f"""
        <article class="shop">
          <div class="shop-row1">
            <span class="{rank_cls}">{i}</span>
            {score_line}
          </div>
          <div class="shop-name">{html.escape(_fmt_shop_name(s.shop_name, s.shop_district))}</div>
          <div class="shop-dist-row">
            <span class="shop-dist">{html.escape(s.shop_district or "—")}</span>
          </div>
          {summary_html}
          {snips}
          {dms_html}
        </article>""")

    excluded = total_reviews - with_shop_reviews
    llm_n = sum(1 for s in shops if s.summary_source == "llm")
    poster_css = f"""
{_CSS}
body {{ background: #fff; padding: 0; }}
.wrap {{ max-width: 375px; margin: 0 auto; padding: 18px 16px 28px; background: #fff; }}
.hero {{ background: #fff; color: var(--ink); border-radius: 0; padding: 0 0 14px; margin: 0 0 14px;
  border-bottom: 2px solid var(--line); }}
.hero .brand {{ color: var(--brand); opacity: 1; }}
.hero h1 {{ font-size: 22px; color: var(--ink); }}
.hero .sub {{ color: var(--muted); opacity: 1; }}
.hero .meta {{ color: var(--muted); opacity: 1; }}
.copy-btn {{ display: none; }}
.section-title {{ margin-top: 14px; }}
.foot {{ display: none; }}
"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(script_title)} · 店家榜海报</title>
<style>{poster_css}</style></head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="brand">JBS · 评论聚合</div>
    <h1>{html.escape(script_title)} · 杭州店家榜</h1>
    <div class="sub">按玩家评论排序 · 评论区含店家的优先</div>
    <div class="meta">共 {total_reviews} 条评论，其中 {with_shop_reviews} 条注明店家（{excluded} 条已排除）</div>
  </header>
  <section class="summary">
    <div><b>{len(shops)}</b><span>上榜店家</span></div>
    <div><b>{with_shop_reviews}</b><span>有效评论</span></div>
    <div><b>{llm_n}/{len(shops)}</b><span>AI 摘要覆盖</span></div>
  </section>
  {''.join(rows)}
</div>
</body></html>"""


# ─────────── 小红书文案 ─────────────────────────────────────────────
_TEMPLATE_CAPTION = """🎭 在杭州打《{title}》，评论区 11 家被反复点名的店！

按玩家评论聚合（评分仅作参考）：
🥇 {top1_name}（{top1_dist}）{top1_summary}
🥈 {top2_name}（{top2_dist}）{top2_summary}
🥉 {top3_name}（{top3_dist}）{top3_summary}

挑店小 tips：
- 优先看 DM 被点名的店，玩家原话比综合分靠谱；
- 按你所在的区域就近选，跨区路上 1h+ 体验打折；
- 评论区有 {n_comments} 条打分，{excluded} 条没注明店家已剔除。

#剧本杀 #杭州剧本杀 #鬼河怒放 #DM推荐 #杭州周末去哪儿"""


def _gen_caption(cfg: Config, script_title: str, shops: list[ShopStat], total: int, excluded: int) -> tuple[str, str]:
    if len(shops) < 1:
        return f"《{script_title}》评论区暂无足够注明店家的评论。", "template"

    def shop_line(s: ShopStat) -> str:
        dms = "、".join(d.name for d in s.dms[:2]) or "未点名"
        summary = s.summary if s.summary else "玩家反馈良好"
        return (
            f"店名：{s.shop_name}\n"
            f"区域：{s.shop_district or '—'}\n"
            f"评论数：{s.reviews} 条\n"
            f"点名 DM：{dms}\n"
            f"玩家一句话评价：{summary}\n"
            f"---"
        )

    if cfg.llm_api_key and len(shops) >= 1:
        try:
            summary = "\n".join(shop_line(s) for s in shops[:6])
            prompt = (
                f"你是小红书剧本杀垂类博主。下面是《{script_title}》杭州玩家评论中"
                f"注明店家的 {len(shops)} 家店，按玩家评价质量排序（已附每家的一句话）：\n"
                f"{summary}\n\n"
                f"写一段 250 字内的小红书发布文案，要求：\n"
                f"1. 第一行带 emoji + 钩子标题\n"
                f"2. 列出 Top3 店家，写店名 + 区域 + 一句玩家评价（直接用上面提供的那句，不杜撰）\n"
                f"3. 文末给 1~2 条挑店 tips（看 DM / 看区域 / 看评论原文等）\n"
                f"4. 不要写\"综合评分 X.X\"这种术语，读者不关心\n"
                f"5. 末尾 4-5 个 # 话题标签\n"
                f"6. 口语化、有情绪\n"
                f"直接输出文案，不要任何解释。"
            )
            resp = httpx.post(
                f"{cfg.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
                json={
                    "model": cfg.llm_model,
                    "messages": [
                        {"role": "system", "content": "你是小红书剧本杀垂类博主，文案口语化、有情绪。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.8, "max_tokens": 500, "repetition_penalty": 1.15,
                },
                timeout=cfg.http_timeout + 30.0,
            )
            if resp.status_code == 200:
                content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip().strip('"')
                if content:
                    return content, "llm"
        except Exception as exc:  # noqa: BLE001
            logger.warning("店家榜文案 LLM 失败，回退模板：%s", exc)

    # 模板兜底
    t1 = shops[0]
    t2 = shops[1] if len(shops) > 1 else t1
    t3 = shops[2] if len(shops) > 2 else t1
    return _TEMPLATE_CAPTION.format(
        title=script_title,
        top1_name=t1.shop_name, top1_dist=t1.shop_district or "—",
        top1_summary=(t1.summary or "玩家反馈良好")[:60],
        top2_name=t2.shop_name, top2_dist=t2.shop_district or "—",
        top2_summary=(t2.summary or "玩家反馈良好")[:60],
        top3_name=t3.shop_name, top3_dist=t3.shop_district or "—",
        top3_summary=(t3.summary or "玩家反馈良好")[:60],
        n_comments=total,
        excluded=excluded,
    ), "template"


# ─────────── 主流程 ────────────────────────────────────────────────
def _script_key(title: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff-]", "", title).replace(":", "")[:40]


def _truncate_text(text: str, n: int = 200) -> str:
    """清洗评论文本：去多余空白、限制长度。"""
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= n else text[:n].rstrip() + "…"


def _summarize_shop_reviews(shop: ShopStat) -> str:
    """模板兜底：从评论里取特征句拼成一段话。

    优先用带 DM 名的评论（玩家对店家/DM 的正面评价密度更高），
    没有就取 sample_reviews 里长度适中的原文拼起来。
    """
    pool = shop.sample_reviews or []
    if not pool:
        return f"{shop.shop_name}（{shop.shop_district or '—'}）共 {shop.reviews} 条玩家评论。"

    # 提 2 条最有信息量的（按"长度适中 + 含 DM/评价词"排序）
    def score(rec: dict) -> int:
        t = rec.get("text", "")
        n = len(t)
        if n < 25 or n > 150:
            return -1
        s = 0
        if any(k in t for k in ("DM", "dm", "主持人", "老师")):
            s += 3
        if any(k in t for k in ("很棒", "推荐", "好玩", "带", "绝", "氛围", "演绎")):
            s += 2
        return s

    sorted_recs = sorted(pool, key=lambda r: (-score(r), -len(r.get("text", ""))))
    picked = [r for r in sorted_recs if score(r) > 0][:2]
    if not picked:
        picked = pool[:1]
    dms = "、".join(d.name for d in shop.dms[:2])
    dm_clause = f"，点名 DM {dms}" if dms else ""
    raw = " / ".join(_truncate_text(r.get("text", ""), 60) for r in picked)
    return f"玩家反馈：{raw}{dm_clause}"


def _gen_shop_summaries(cfg: Config, script_title: str, shops: list[ShopStat]) -> None:
    """对每家店调用一次 LLM 生成 80~120 字的玩家评论摘要，失败回退模板。原地写进 shop.summary。"""
    if not shops:
        return

    if cfg.llm_api_key:
        try:
            for shop in shops:
                # 把该店的评论样本拼成上下文
                lines = []
                for i, r in enumerate(shop.sample_reviews[:3], 1):
                    nick = html.escape(r.get("nick") or "匿名")
                    txt = html.escape(_truncate_text(r.get("text", ""), 160))
                    lines.append(f"[{i}] {nick}：{txt}")
                dms = "、".join(f"{d.name}({d.reviews}条)" for d in shop.dms[:3])
                if not dms:
                    dms = "（玩家未点名 DM）"
                district = html.escape(shop.shop_district or "—")
                reviews_n = shop.reviews
                prompt = (
                    f"剧本：《{script_title}》\n"
                    f"店家：{html.escape(shop.shop_name)}\n"
                    f"区域：{district}\n"
                    f"评论数：{reviews_n} 条\n"
                    f"点名 DM：{dms}\n\n"
                    f"以下是这家店的玩家评论原文（已经过滤掉没注明店家的）：\n"
                    + "\n".join(lines)
                    + "\n\n请基于上面这些真实评论原文，**用一段话（80~120 字）**总结玩家对这家店的评价。"
                    f"要求：\n"
                    f"1. 必须是综合上述评论得出的判断，不能杜撰具体细节；\n"
                    f"2. 直接给出结论（氛围/DM 带本水平/时长/适配人群/重复率等任意一个最突出的点）；\n"
                    f"3. 若提到了 DM 必须用其原名；\n"
                    f"4. 不要出现\"综合评分\"\"三维度均值\"等术语，读者不关心这个；\n"
                    f"5. 只输出这段话，不要任何标题/前缀/解释。"
                )
                resp = httpx.post(
                    f"{cfg.llm_base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
                    json={
                        "model": cfg.llm_model,
                        "messages": [
                            {"role": "system", "content": "你是剧本杀玩家评论员，擅长一句话提炼店家口碑。"},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.6, "max_tokens": 220, "repetition_penalty": 1.1,
                    },
                    timeout=cfg.http_timeout + 30.0,
                )
                if resp.status_code == 200:
                    content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
                    # 去掉前后引号/常见前缀
                    content = content.strip('"').strip("「").strip("」").strip()
                    # 去掉前缀 "摘要：" 之类
                    content = re.sub(r"^(摘要[:：]?|总结[:：]?|玩家评价[:：]?)\s*", "", content)
                    if content and 20 <= len(content) <= 240:
                        shop.summary = content
                        shop.summary_source = "llm"
                        continue
            # 任何失败都掉到下面模板兜底
            logger.info("店家榜部分店 LLM 摘要失败，回退模板")
        except Exception as exc:  # noqa: BLE001
            logger.warning("店家榜 LLM 摘要异常，回退模板：%s", exc)

    # 模板兜底
    for shop in shops:
        if not shop.summary:
            shop.summary = _summarize_shop_reviews(shop)
            shop.summary_source = "template"


def write_reviews(
    cfg: Config,
    har_path: Path,
    script_title: str,
    min_reviews: int = 2,
) -> dict[str, Any]:
    """从 HAR 生成店家榜 + 小红书素材，返回各产物路径与状态。"""
    all_reviews = load_reviews_from_har(har_path)
    with_shop = [r for r in all_reviews if r.has_shop]
    shops_all = aggregate_by_shop(all_reviews)
    shops = rank_shops(shops_all, min_reviews=min_reviews)

    # 生成每家店的玩家评论摘要（LLM → 模板）
    _gen_shop_summaries(cfg, script_title, shops)

    reviews_dir = cfg.data_dir / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)

    key = _script_key(script_title)

    caption, caption_source = _gen_caption(
        cfg, script_title, shops, len(all_reviews), len(all_reviews) - len(with_shop)
    )

    page_path = reviews_dir / f"{key}.html"
    page_path.write_text(
        render_shop_html(script_title, shops, len(all_reviews), len(with_shop), caption=caption),
        encoding="utf-8",
    )

    txt_path = reviews_dir / f"{key}.txt"
    txt_path.write_text(caption, encoding="utf-8")

    poster_path = reviews_dir / f"{key}.poster.html"
    poster_path.write_text(
        render_poster_html(script_title, shops, len(all_reviews), len(with_shop)),
        encoding="utf-8",
    )

    return {
        "script": script_title,
        "reviews_total": len(all_reviews),
        "reviews_with_shop": len(with_shop),
        "reviews_excluded": len(all_reviews) - len(with_shop),
        "shops_total": len(shops_all),
        "shops_ranked": len(shops),
        "shops_skipped_low_review": len(shops_all) - len(shops),
        "shops_with_llm_summary": sum(1 for s in shops if s.summary_source == "llm"),
        "page": str(page_path),
        "caption": str(txt_path),
        "poster": str(poster_path),
        "caption_source": caption_source,
        "ok": True,
    }