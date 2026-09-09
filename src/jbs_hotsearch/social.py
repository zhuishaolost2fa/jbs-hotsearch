# -*- coding: utf-8 -*-
"""小红书素材生成：3:4 竖版海报截图 + LLM 文案 + 素材页。

产物（每天出榜后自动生成）：
  data/social/YYYY-MM-DD.png   海报截图（1080×1440，实际 2x = 2160×2880）
  data/social/YYYY-MM-DD.txt   小红书文案（含话题标签）
  data/social/poster.html      最新一张海报的 HTML（调试/复刻用）
  data/social/index.html       素材页（内嵌最新图 + 文案，手机可存图/复制）

设计原则：
  - 海报复用榜单页的暖橙视觉语言，独立 1080×1440 竖版布局；
  - Playwright 延迟 import，未装浏览器时只降级「截图失败」，不拖垮出榜；
  - 文案 LLM 失败时回退到模板，保证每天都有可用文案。
"""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .models import DailyBoard, RankedScript

logger = logging.getLogger(__name__)


def _escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _rank_class(rank: int) -> str:
    if rank == 1:
        return "rank-gold"
    if rank == 2:
        return "rank-silver"
    if rank == 3:
        return "rank-bronze"
    return "rank-plain"


def _item_meta(item: RankedScript) -> str:
    """条目副文案：优先「近N天 X家店 · Y场组局」，退化为评分。"""
    grp = item.source_detail.get("miquan_group") or {}
    shops = grp.get("shop_count")
    groups = grp.get("group_count")
    days = grp.get("window_days")
    span = f"近 {int(days)} 天" if days else "近期"
    bits: list[str] = []
    if shops:
        bits.append(f"{span} {int(shops)} 家店")
    if groups:
        bits.append(f"{int(groups)} 场组局")
    if not bits and item.rating:
        bits.append(f"评分 {item.rating:.1f}")
    return " · ".join(bits) if bits else ""


def _poster_item(item: RankedScript) -> str:
    rank = int(item.rank)
    title = _escape(item.title)
    score = f"{item.hot_score:.0f}" if item.hot_score is not None else "—"
    meta = _escape(_item_meta(item))
    badge = ""
    if item.is_new:
        badge = '<span class="badge badge-new">新</span>'
    elif item.rank_change is not None:
        if item.rank_change > 0:
            badge = f'<span class="badge badge-up">↑{item.rank_change}</span>'
        elif item.rank_change < 0:
            badge = f'<span class="badge badge-down">↓{abs(item.rank_change)}</span>'
    return f"""
    <li class="item">
      <div class="rank {_rank_class(rank)}">{rank}</div>
      <div class="body">
        <div class="row"><span class="title">{title}</span>{badge}</div>
        {('<div class="meta">' + meta + '</div>') if meta else ""}
      </div>
      <div class="score">{score}<span class="unit">热度</span></div>
    </li>"""


def render_poster_html(board: DailyBoard, board_date: str, width: int = 1080, height: int = 1440) -> str:
    """渲染竖版海报 HTML（body 固定尺寸，一屏即完整海报）。"""
    items = board.items
    cards = "".join(_poster_item(it) for it in items)
    sub = "米圈杭州拼场 · 近 3 天真实组局"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ width: {width}px; height: {height}px; overflow: hidden; }}
body {{
  font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
    "Noto Sans CJK SC", "Source Han Sans SC", sans-serif;
  background: #f5f3ee; color: #211d18;
}}
.poster {{ width: 100%; height: 100%; padding: 0 48px 26px; display: flex; flex-direction: column; }}
.hero {{
  background: linear-gradient(135deg, #3a2015 0%, #6b2c1c 45%, #c8502a 100%);
  color: #fff; border-radius: 0 0 26px 26px;
  margin: 0 -48px; padding: 36px 48px 28px;
}}
.hero .brand {{ font-size: 21px; letter-spacing: 4px; opacity: .82; }}
.hero h1 {{ font-size: 56px; font-weight: 800; margin: 8px 0 6px; letter-spacing: 2px; }}
.hero .sub {{ font-size: 25px; opacity: .9; }}
.hero .date {{ font-size: 23px; opacity: .78; margin-top: 5px; letter-spacing: 1px; }}

.list {{ list-style: none; margin-top: 12px; }}
.item {{
  display: flex; align-items: center; gap: 14px;
  background: #fff; border: 1px solid #ece7dd; border-radius: 13px;
  padding: 10px 16px; margin-bottom: 7px;
}}
.rank {{
  flex: 0 0 46px; height: 46px; border-radius: 13px;
  display: flex; align-items: center; justify-content: center;
  font-size: 24px; font-weight: 800; color: #fff; background: #cfc8bb;
}}
.rank-gold {{ background: linear-gradient(135deg, #f2c14e, #c8961e); }}
.rank-silver {{ background: linear-gradient(135deg, #c6ccd2, #8d939b); }}
.rank-bronze {{ background: linear-gradient(135deg, #d9a06b, #b0723a); }}
.body {{ flex: 1; min-width: 0; }}
.row {{ display: flex; align-items: center; gap: 12px; }}
.title {{ font-size: 30px; font-weight: 700; line-height: 1.2; }}
.badge {{ font-size: 17px; padding: 2px 9px; border-radius: 20px; font-weight: 600; white-space: nowrap; }}
.badge-new {{ background: #ffe9dd; color: #b83d1a; }}
.badge-up {{ background: #ffe1d8; color: #b83d1a; }}
.badge-down {{ background: #e3f0e4; color: #2e7d32; }}
.meta {{ font-size: 21px; color: #8c8578; margin-top: 1px; }}
.score {{ flex: 0 0 auto; display: flex; flex-direction: column; align-items: center; color: #e5532b; font-size: 36px; font-weight: 800; line-height: 1; }}
.score .unit {{ font-size: 15px; font-weight: 500; color: #8c8578; margin-top: 2px; }}

.footer {{ text-align: center; color: #8c8578; font-size: 19px; margin-top: 2px; letter-spacing: 1px; }}
</style>
</head>
<body>
<div class="poster">
  <header class="hero">
    <div class="brand">JBS · 每日更新</div>
    <h1>杭州剧本杀热度榜</h1>
    <div class="sub">{sub}</div>
    <div class="date">{_escape(board_date)}</div>
  </header>
  <ol class="list">{cards}</ol>
  <div class="footer">热度由近 3 天真实组局计算 · 每日更新</div>
</div>
</body>
</html>"""


def _screenshot_png(html_path: Path, png_path: Path, width: int, height: int, scale: int) -> None:
    """用 Playwright 无头浏览器把海报 HTML 截成 PNG。"""
    from playwright.sync_api import sync_playwright

    png_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"]
        )
        try:
            page = browser.new_page(
                viewport={"width": width, "height": height}, device_scale_factor=scale
            )
            page.goto(html_path.as_uri())
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(png_path), full_page=False)
        finally:
            browser.close()


def _board_summary(board: DailyBoard) -> str:
    lines = []
    for it in board.items:
        meta = _item_meta(it)
        lines.append(f"{it.rank}. {it.title}（热度 {it.hot_score:.0f}" + (f"，{meta}" if meta else "") + "）")
    return "\n".join(lines)


_TEMPLATE_CAPTION = """📊 杭州剧本杀热度榜 · {date}

今日榜首《{top1}》，杭州近 3 天真实拼场人气第一🔥

完整 Top10：
{summary}

#剧本杀 #杭州剧本杀 #周末去哪玩 #热门剧本杀 #剧本杀推荐"""


def _caption_via_llm(cfg: Config, board: DailyBoard) -> str | None:
    """调 SiliconFlow 生成小红书文案；失败返回 None 走模板。"""
    if not cfg.llm_api_key:
        return None
    summary = _board_summary(board)
    prompt = (
        f"以下是今天（{board.board_date.isoformat()}）的杭州剧本杀热度榜 Top{len(board.items)}：\n"
        f"{summary}\n\n"
        "请以小红书剧本杀垂类博主的语气写一段发布文案，要求：\n"
        "1. 第一行是标题，带 1-2 个 emoji，要有钩子（点出榜首或最大黑马）\n"
        "2. 正文 2-4 句话，口语化，点出 1-3 个值得关注的点（榜首、上升快、新上榜）\n"
        "3. 结尾 3-5 个话题标签，如 #剧本杀 #杭州剧本杀 #周末去哪儿\n"
        "直接输出文案，不要任何解释或前后缀。"
    )
    try:
        resp = httpx.post(
            f"{cfg.llm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
            json={
                "model": cfg.llm_model,
                "messages": [
                    {"role": "system", "content": "你是小红书剧本杀垂类博主，文案口语化、有情绪、有钩子。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.8,
                "max_tokens": 400,
            },
            timeout=cfg.http_timeout + 30.0,
        )
        if resp.status_code != 200:
            logger.warning("文案 LLM 返回 %s：%s", resp.status_code, resp.text[:200])
            return None
        content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        content = (content or "").strip().strip('"').strip()
        return content or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("文案 LLM 调用失败，回退模板：%s", exc)
        return None


def _template_caption(board: DailyBoard) -> str:
    top1 = board.items[0].title if board.items else "——"
    summary = "\n".join(f"{it.rank}. {it.title}" for it in board.items)
    return _TEMPLATE_CAPTION.format(
        date=board.board_date.isoformat(), top1=top1, summary=summary
    )


def _gen_caption(cfg: Config, board: DailyBoard) -> tuple[str, str]:
    """返回 (文案, 来源)；来源 = llm / template。"""
    llm_text = _caption_via_llm(cfg, board)
    if llm_text:
        return llm_text, "llm"
    return _template_caption(board), "template"


def _render_material_page(board_date: str, png_name: str, caption: str, caption_source: str) -> str:
    """素材页：内嵌最新海报 + 文案，手机可长按存图、一键复制文案。"""
    caption_escaped = _escape(caption)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>小红书素材 · {_escape(board_date)}</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
  background: #f5f3ee; color: #211d18; line-height: 1.6; padding: 20px 16px 40px;
}}
.wrap {{ max-width: 560px; margin: 0 auto; }}
h1 {{ font-size: 22px; margin-bottom: 4px; }}
.date {{ color: #8c8578; font-size: 13px; margin-bottom: 16px; }}
.poster {{ width: 100%; border-radius: 16px; box-shadow: 0 6px 20px rgba(60,30,15,.18); display: block; }}
.hint {{ text-align: center; color: #8c8578; font-size: 12px; margin: 10px 0 20px; }}
.caption-box {{ background: #fff; border: 1px solid #ece7dd; border-radius: 16px; padding: 16px; }}
.caption-box h2 {{ font-size: 15px; margin-bottom: 10px; }}
.caption-box pre {{
  white-space: pre-wrap; word-break: break-word; font-family: inherit;
  font-size: 14px; line-height: 1.7; color: #3a3530;
}}
.copy-btn {{
  display: block; width: 100%; margin-top: 12px; padding: 12px;
  background: #e5532b; color: #fff; border: none; border-radius: 12px;
  font-size: 15px; font-weight: 600; cursor: pointer;
}}
.copy-btn:active {{ opacity: .85; }}
.src {{ text-align: center; color: #8c8578; font-size: 12px; margin-top: 20px; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>今日小红书素材</h1>
  <div class="date">{_escape(board_date)} · 文案来源 {'AI 生成' if caption_source == 'llm' else '模板'}</div>
  <img class="poster" src="{_escape(png_name)}" alt="杭州剧本杀热度榜海报">
  <div class="hint">长按图片保存到相册</div>
  <div class="caption-box">
    <h2>发布文案</h2>
    <pre id="caption">{caption_escaped}</pre>
    <button class="copy-btn" id="copyBtn" type="button">复制文案</button>
  </div>
  <div class="src">数据来源：米圈杭州拼场 · 每日更新</div>
</div>
<script>
(function(){{
  var b = document.getElementById('copyBtn'), t = document.getElementById('caption');
  if (b && t) {{
    b.addEventListener('click', function(){{
      var text = t.innerText || t.textContent;
      function done(){{ b.textContent = '已复制'; setTimeout(function(){{ b.textContent = '复制文案'; }}, 1500); }}
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text).then(done, done);
      }} else {{
        var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta);
        ta.select(); try{{ document.execCommand('copy'); }}catch(e){{}} document.body.removeChild(ta); done();
      }}
    }});
  }}
}})();
</script>
</body>
</html>"""


def write_social(cfg: Config, board: DailyBoard) -> dict[str, Any]:
    """生成当天小红书素材，返回各产物路径与状态。任何子步骤失败都不抛异常。"""
    board_date = board.board_date.isoformat()
    social_dir = cfg.data_dir / "social"
    social_dir.mkdir(parents=True, exist_ok=True)

    result: dict[str, Any] = {"board_date": board_date, "ok": False}

    width = max(320, int(cfg.social_poster_width))
    height = max(480, int(cfg.social_poster_height))
    scale = max(1, int(cfg.social_poster_scale))

    # 1) 海报 HTML
    poster_html = social_dir / "poster.html"
    poster_html.write_text(render_poster_html(board, board_date, width, height), encoding="utf-8")
    result["poster_html"] = str(poster_html)

    # 2) 截图 PNG
    png_name = f"{board_date}.png"
    png_path = social_dir / png_name
    try:
        _screenshot_png(poster_html, png_path, width, height, scale)
        result["png"] = str(png_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("海报截图失败（Playwright/浏览器未就绪？）：%s", exc)
        result["png_error"] = str(exc)

    # 3) 文案
    caption, source = _gen_caption(cfg, board)
    caption_path = social_dir / f"{board_date}.txt"
    caption_path.write_text(caption, encoding="utf-8")
    result["caption"] = str(caption_path)
    result["caption_source"] = source

    # 4) 素材页（只有 PNG 成功才内嵌图片，否则退化为纯文案页）
    material = social_dir / "index.html"
    material.write_text(
        _render_material_page(board_date, png_name if "png" in result else "", caption, source),
        encoding="utf-8",
    )
    result["material_page"] = str(material)
    result["ok"] = "png" in result and bool(caption)
    return result
