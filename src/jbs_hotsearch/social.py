# -*- coding: utf-8 -*-
"""小红书素材生成：3:4 竖版海报截图 + LLM 文案 + 素材页。

产物（每天出榜后自动生成）：
  data/social/YYYY-MM-DD.png   海报截图（1080×1440 标准 3:4；内容多时自动切多张 -2/-3…）
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
import logging
import re
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .models import DailyBoard, RankedScript
from .shot import screenshot_slices as _screenshot_slices

logger = logging.getLogger(__name__)

# LLM 偶发「重复退化」：结尾刷出上百个相同 emoji（如 😉😉😉…）。
# 这里只压缩「非中文/非字母数字」符号的连续重复（3 个及以上压成 1 个），
# 因此「哈哈哈」「！！！」这类合法中文表达不会被误伤。
_REPEAT_RUN = re.compile(r"([^\s\w\u4e00-\u9fff\u3000-\u303f\uff00-\uffef])\1{2,}")
_CAPTION_MAX_LEN = 600


def _sanitize_caption(text: str) -> str:
    """清洗 LLM 文案：压掉重复符号串、去空行、超长截断。"""
    if not text:
        return ""
    cleaned = _REPEAT_RUN.sub(r"\1", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    if len(cleaned) > _CAPTION_MAX_LEN:
        cut = cleaned[:_CAPTION_MAX_LEN].rstrip()
        # 尽量在句子边界截断，避免半句话
        for sep in ("\n", "。", "！", "？"):
            pos = cut.rfind(sep)
            if pos > _CAPTION_MAX_LEN * 0.6:
                return cut[: pos + 1]
        return cut + "…"
    return cleaned


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


def _filtered_chips(filtered: list[RankedScript], limit: int) -> str:
    """已解析区块：两列紧凑 chips（删除线 + 原热度），超出 limit 折叠成「等 N 本」。"""
    if not filtered:
        return ""
    shown = filtered[:limit]
    overflow = len(filtered) - len(shown)
    chips = "".join(
        f'<li class="chip"><span class="strike">{_escape(it.title)}</span>'
        f'<span class="fscore">{it.hot_score:.0f}</span></li>'
        for it in shown
    )
    if overflow > 0:
        chips += f'<li class="chip chip-more">等 {overflow} 本</li>'
    return f"""
  <section class="parsed">
    <h3>已解析 · 未入榜</h3>
    <p class="phint">热度够进榜，但 DM 手册已在库，本轮剔除</p>
    <ul class="chips">{chips}</ul>
  </section>"""


def render_poster_html(
    board: DailyBoard,
    board_date: str,
    width: int = 1080,
    height: int = 1440,
    show_parsed: bool = True,
    parsed_limit: int = 6,
) -> str:
    """渲染竖版海报 HTML（body 尺寸随内容变化，一屏即完整海报）。"""
    items = board.items
    cards = "".join(_poster_item(it) for it in items)
    filtered = board.filtered_items if show_parsed else []
    parsed_block = _filtered_chips(filtered, parsed_limit)
    sub = "米圈杭州拼场 · 近 3 天真实组局"
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
/* 高度不写死：视口给保底，内容更高时由截图器量出来后放大视口（只放大不收缩，单调收敛） */
html, body {{ width: {width}px; overflow: hidden; }}
body {{
  font-family: "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei",
    "Noto Sans CJK SC", "Source Han Sans SC", sans-serif;
  background: #f5f3ee; color: #211d18;
}}
.poster {{ width: 100%; padding: 0 48px 26px; display: flex; flex-direction: column; }}
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

.parsed {{ margin-top: 14px; }}
.parsed h3 {{ font-size: 24px; font-weight: 700; color: #6b6257; margin-bottom: 2px; }}
.parsed .phint {{ font-size: 18px; color: #9c9488; margin-bottom: 8px; }}
.chips {{ list-style: none; display: flex; flex-wrap: wrap; gap: 8px; }}
.chip {{
  display: flex; align-items: center; gap: 8px;
  background: #efece5; border: 1px dashed #d6cfc3; border-radius: 20px;
  padding: 7px 14px; font-size: 21px; color: #7d7466;
}}
.chip .strike {{ text-decoration: line-through; text-decoration-thickness: 2px; }}
.chip .fscore {{ font-size: 17px; color: #a89e8e; }}
.chip-more {{ background: #f7f5f0; color: #a89e8e; font-style: italic; }}
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
  <ol class="list">{cards}</ol>{parsed_block}
  <div class="footer">热度由近 3 天真实组局计算 · 每日更新</div>
</div>
</body>
</html>"""


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


def _board_summary(board: DailyBoard, caption_parsed: list[RankedScript] | None = None) -> str:
    lines = []
    for it in board.items:
        meta = _item_meta(it)
        lines.append(f"{it.rank}. {it.title}（热度 {it.hot_score:.0f}" + (f"，{meta}" if meta else "") + "）")
    if caption_parsed:
        parsed = "、".join(f"{it.title}（原热度 {it.hot_score:.0f}）" for it in caption_parsed)
        lines.append(f"已解析（攻略已上线，未入榜）：{parsed}")
    return "\n".join(lines)


_TEMPLATE_CAPTION = """📊 杭州剧本杀热度榜 · {date}

今日榜首《{top1}》，杭州近 3 天真实拼场人气第一🔥

完整 Top10：
{summary}
{parsed_line}

#剧本杀 #杭州剧本杀 #周末去哪玩 #热门剧本杀 #剧本杀推荐"""


def _caption_via_llm(cfg: Config, board: DailyBoard) -> str | None:
    """调 SiliconFlow 生成小红书文案；失败返回 None 走模板。"""
    if not cfg.llm_api_key:
        return None
    # 已解析本按热度筛选后再喂给 LLM，避免冷门本占用文案篇幅
    caption_parsed = _parsed_for_caption(
        board, cfg.social_caption_parsed_ratio, cfg.social_caption_parsed_max
    )
    summary = _board_summary(board, caption_parsed)
    parsed_names = "、".join(it.title for it in caption_parsed)
    parsed_hint = (
        f"4. 文末必须点名提到这些已解析剧本：{parsed_names}。"
        "说明它们热度够高但攻略已整理好（DM 手册已入库），并引导读者私信或看主页获取攻略；"
        "注意不要把它们写成榜内排名，它们是「已解析未入榜」\n"
        if parsed_names
        else ""
    )
    prompt = (
        f"以下是今天（{board.board_date.isoformat()}）的杭州剧本杀热度榜 Top{len(board.items)}：\n"
        f"{summary}\n\n"
        "请以小红书剧本杀垂类博主的语气写一段发布文案，要求：\n"
        "1. 第一行是标题，带 1-2 个 emoji，要有钩子（点出榜首或最大黑马）\n"
        "2. 正文 2-4 句话，口语化，点出 1-3 个值得关注的点（榜首、上升快、新上榜）\n"
        "3. 结尾 3-5 个话题标签，如 #剧本杀 #杭州剧本杀 #周末去哪儿\n"
        f"{parsed_hint}"
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
                # 从源头抑制「重复退化」（曾出现过结尾刷上百个 😉 的情况）
                "repetition_penalty": 1.15,
            },
            timeout=cfg.http_timeout + 30.0,
        )
        if resp.status_code != 200:
            logger.warning("文案 LLM 返回 %s：%s", resp.status_code, resp.text[:200])
            return None
        content = (resp.json().get("choices") or [{}])[0].get("message", {}).get("content", "")
        content = _sanitize_caption((content or "").strip().strip('"'))
        return content or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("文案 LLM 调用失败，回退模板：%s", exc)
        return None


def _template_caption(board: DailyBoard, caption_parsed: list[RankedScript] | None = None) -> str:
    top1 = board.items[0].title if board.items else "——"
    summary = "\n".join(f"{it.rank}. {it.title}" for it in board.items)
    parsed_line = ""
    if caption_parsed:
        names = "、".join(f"《{it.title}》" for it in caption_parsed)
        parsed_line = f"\n📚 已解析攻略已上线（热度够但未入榜）：{names}，私信获取～"
    return _TEMPLATE_CAPTION.format(
        date=board.board_date.isoformat(), top1=top1, summary=summary, parsed_line=parsed_line
    )


def _gen_caption(cfg: Config, board: DailyBoard) -> tuple[str, str]:
    """返回 (文案, 来源)；来源 = llm / template。"""
    caption_parsed = _parsed_for_caption(
        board, cfg.social_caption_parsed_ratio, cfg.social_caption_parsed_max
    )
    llm_text = _caption_via_llm(cfg, board)
    if llm_text:
        return llm_text, "llm"
    return _template_caption(board, caption_parsed), "template"


def _render_material_page(
    board_date: str, png_names: list[str], caption: str, caption_source: str
) -> str:
    """素材页：海报切片（两列 + 序号 + 大图预览 + 一键保存）+ 文案。

    切片排版跟周报保持一致：两列网格、每张独立卡片带「第 N 张」角标，
    用户一眼能看出每张的边界，不用再对着一整条长图猜哪里断开。
    """
    caption_escaped = _escape(caption)
    if png_names:
        total = len(png_names)
        single = " single" if total == 1 else ""
        imgs = "".join(
            f'<figure class="slice" onclick="openLb({i - 1})">'
            f'<img src="{_escape(p)}" data-file="{_escape(p)}" '
            f'alt="杭州剧本杀热度榜海报 {i}/{total}">'
            f'<figcaption class="no">第 {i} 张</figcaption>'
            f"</figure>"
            for i, p in enumerate(png_names, 1)
        )
        if total == 1:
            # 单张时「一键保存全部（1 张）」听着别扭，也不需要一个专门的预览入口
            acts = (
                '<button class="slice-btn primary" type="button" '
                'onclick="saveAllSlices(this)">⬇️ 保存这张海报</button>'
                '<button class="slice-btn" type="button" onclick="openLb(0)">🔍 放大预览</button>'
            )
        else:
            acts = (
                f'<button class="slice-btn primary" type="button" '
                f'onclick="saveAllSlices(this)">⬇️ 一键保存全部（{total} 张）</button>'
                f'<button class="slice-btn" type="button" onclick="openLb(0)">🔍 逐张预览保存</button>'
            )
        shot = f'<div class="slices{single}">{imgs}</div><div class="slice-acts">{acts}</div>'
        tip = (
            f'共 {total} 张，每张都是 3:4，可直接发小红书；'
            f'点任意一张可放大，长按存进相册。安卓「一键保存」会依次下载，iPhone 请用「逐张预览」长按保存'
            if total > 1
            else "点图片可放大，长按存进相册"
        )
    else:
        shot = ""
        tip = "海报没生成，先复制文案"
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
/* 切片：两列网格 + 独立卡片 + 序号，边界一眼看得出 */
.slices {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }}
.slices.single {{ grid-template-columns: 1fr; }}
.slice {{
  position: relative; background: #fff; border: 1px solid #ece7dd;
  border-radius: 14px; padding: 6px; cursor: zoom-in;
}}
.slice img {{ display: block; width: 100%; height: auto; border-radius: 10px; border: 1px solid #ece7dd; }}
.slice .no {{
  position: absolute; left: 12px; top: 12px; background: rgba(33,29,24,.74); color: #fff;
  font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 20px; letter-spacing: .5px;
}}
.slice-acts {{ display: flex; gap: 8px; margin-top: 12px; }}
.slice-btn {{
  flex: 1; padding: 11px 6px; border-radius: 12px; font-size: 13px; font-weight: 600;
  border: 1px solid #f0c9b9; background: #fff5ee; color: #e5532b; cursor: pointer;
}}
.slice-btn.primary {{ background: #e5532b; border-color: #e5532b; color: #fff; }}
.slice-btn:active {{ opacity: .85; }}
.slice-btn:disabled {{ opacity: .6; }}
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
/* 大图预览：手机上长按这张大图即可存入相册 */
.lightbox {{
  position: fixed; inset: 0; z-index: 99; display: none; flex-direction: column;
  align-items: center; justify-content: center; padding: 18px; background: rgba(18,12,8,.94);
}}
.lightbox.on {{ display: flex; }}
.lightbox img {{ max-width: 100%; max-height: 70vh; border-radius: 12px; background: #fff; }}
.lightbox .lb-no {{ color: #fff; font-size: 13px; margin-top: 14px; }}
.lightbox .lb-nav {{ display: flex; gap: 26px; align-items: center; margin-top: 12px; color: #fff; font-size: 14px; }}
.lightbox .lb-nav span {{ padding: 6px 16px; border: 1px solid rgba(255,255,255,.35); border-radius: 20px; cursor: pointer; }}
.lightbox .lb-save {{
  margin-top: 14px; padding: 10px 22px; border-radius: 24px;
  background: #e5532b; color: #fff; font-size: 14px; font-weight: 600;
}}
.lightbox .lb-close {{
  position: absolute; right: 16px; top: 14px; color: #fff; font-size: 28px;
  line-height: 1; padding: 6px 10px; cursor: pointer;
}}
</style>
</head>
<body>
<div class="wrap">
  <h1>今日小红书素材</h1>
  <div class="date">{_escape(board_date)} · 文案来源 {'AI 生成' if caption_source == 'llm' else '模板'}</div>
  {shot}
  <div class="hint">{tip}</div>
  <div class="caption-box">
    <h2>发布文案</h2>
    <pre id="caption">{caption_escaped}</pre>
    <button class="copy-btn" id="copyBtn" type="button">复制文案</button>
  </div>
  <div class="src">数据来源：米圈杭州拼场 · 每日更新</div>
</div>
<div class="lightbox" id="lb" onclick="closeLb()">
  <div class="lb-close">×</div>
  <img id="lbImg" src="" alt="海报大图" onclick="event.stopPropagation()">
  <div class="lb-no" id="lbNo"></div>
  <div class="lb-save" onclick="event.stopPropagation(); saveOne(lbIdx)">⬇️ 保存本张</div>
  <div class="lb-nav">
    <span onclick="event.stopPropagation(); lbStep(-1)">← 上一张</span>
    <span onclick="event.stopPropagation(); lbStep(1)">下一张 →</span>
  </div>
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

/* ---- 海报切片：大图预览 + 一键保存 ---- */
var lbIdx = 0;
function lbImages() {{ return [].slice.call(document.querySelectorAll('.slice img')); }}
function openLb(i) {{
  var imgs = lbImages();
  if (!imgs.length) return;
  lbIdx = i;
  document.getElementById('lbImg').src = imgs[i].src;
  document.getElementById('lbNo').textContent = '第 ' + (i + 1) + ' / ' + imgs.length + ' 张 · 长按图片保存到相册';
  document.getElementById('lb').classList.add('on');
  document.body.style.overflow = 'hidden';
}}
function lbStep(d) {{
  var imgs = lbImages();
  if (!imgs.length) return;
  openLb((lbIdx + d + imgs.length) % imgs.length);
}}
function closeLb() {{
  document.getElementById('lb').classList.remove('on');
  document.body.style.overflow = '';
}}
function saveOne(i) {{
  var imgs = lbImages();
  if (!imgs[i]) return;
  var a = document.createElement('a');
  a.href = imgs[i].src;
  a.download = imgs[i].getAttribute('data-file') || ('slice-' + (i + 1) + '.png');
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
}}
/* 一键保存：逐个触发 <a download>。安卓会依次下载到「下载」目录，
   iOS Safari 不支持连续下载（只会打开一张），所以提示 iPhone 用预览长按保存。 */
async function saveAllSlices(btn) {{
  var imgs = lbImages();
  if (!imgs.length) return;
  var label = btn.textContent;
  btn.disabled = true;
  for (var i = 0; i < imgs.length; i++) {{
    btn.textContent = '正在保存 ' + (i + 1) + '/' + imgs.length + ' …';
    saveOne(i);
    await new Promise(function(r) {{ setTimeout(r, 600); }});
  }}
  btn.textContent = '✓ 已触发 ' + imgs.length + ' 张，去相册/下载看看';
  setTimeout(function() {{ btn.textContent = label; btn.disabled = false; }}, 2500);
}}
document.addEventListener('keydown', function(e) {{
  var lb = document.getElementById('lb');
  if (!lb || !lb.classList.contains('on')) return;
  if (e.key === 'Escape') closeLb();
  if (e.key === 'ArrowLeft') lbStep(-1);
  if (e.key === 'ArrowRight') lbStep(1);
}});
/* 大图里左右滑动切换 */
(function(){{
  var x0 = null, lb = document.getElementById('lb');
  if (!lb) return;
  lb.addEventListener('touchstart', function(e) {{ x0 = e.touches[0].clientX; }}, {{passive:true}});
  lb.addEventListener('touchend', function(e) {{
    if (x0 === null) return;
    var dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 45) lbStep(dx < 0 ? 1 : -1);
    x0 = null;
  }});
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

    # 1) 海报 HTML（底部「已解析·未入榜」区块受 social_show_parsed / social_parsed_limit 控制；
    #    内容更高时截图器会自动加长视口，无需在此估算高度）
    show_parsed = bool(cfg.social_show_parsed)
    parsed_limit = max(1, int(cfg.social_parsed_limit))
    parsed_section = _filtered_chips(board.filtered_items, parsed_limit) if show_parsed else ""

    poster_html = social_dir / "poster.html"
    poster_html.write_text(
        render_poster_html(board, board_date, width, height, show_parsed, parsed_limit),
        encoding="utf-8",
    )
    result["poster_html"] = str(poster_html)
    result["parsed_count"] = len(board.filtered_items) if show_parsed else 0
    result["parsed_in_poster"] = bool(parsed_section)

    # 2) 截图 PNG（按小红书 3:4：内容一页内→单张标准 3:4；内容更多→按块边界切成多张）
    png_name = f"{board_date}.png"
    png_path = social_dir / png_name
    try:
        slices = _screenshot_slices(
            poster_html,
            png_path,
            width=width,
            scale=scale,
            page_height=height,
            anchors=".hero,.item,.parsed,.footer",
        )
        if slices:
            result["png"] = str(slices[0])
            if len(slices) > 1:
                result["pngs"] = [str(s) for s in slices]
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
        _render_material_page(
            board_date,
            [Path(p).name for p in result.get("pngs", [result["png"]])] if "png" in result else [],
            caption,
            source,
        ),
        encoding="utf-8",
    )
    result["material_page"] = str(material)
    result["ok"] = "png" in result and bool(caption)
    return result
