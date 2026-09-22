# -*- coding: utf-8 -*-
"""小红书素材生成：3:4 竖版海报截图 + LLM 文案 + 素材页。

产物（每天出榜后自动生成）：
  data/social/YYYY-MM-DD.png   海报截图（1080×1440 标准 3:4；内容多时自动切多张 -2/-3…）
  data/social/YYYY-MM-DD.txt   小红书文案（含话题标签）
  data/social/poster.html      最新一张海报的 HTML（调试/复刻用）
  data/social/index.html       素材页（内嵌最新图 + 文案，手机可存图/复制）
  data/social/miniapp-qr.png   小程序码独立文件（海报已印 + 素材页可单独保存）

设计原则：
  - 海报复用榜单页的暖橙视觉语言，独立 1080×1440 竖版布局；
  - Playwright 延迟 import，未装浏览器时只降级「截图失败」，不拖垮出榜；
  - 文案 LLM 失败时回退到模板，保证每天都有可用文案；
  - 小程序码是打包资产，缺文件时海报自动退回「不带码」，绝不因为一张图挂掉出榜。
"""
from __future__ import annotations

import base64
import html
import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

from .caption_recipes import gen_daily_caption, log_run
from .config import Config
from .models import DailyBoard, RankedScript
from .shot import screenshot_html as _screenshot_html
from .shot import screenshot_slices as _screenshot_slices

logger = logging.getLogger(__name__)


def _escape(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


# ─────────── 小程序码（打包资产） ────────────────────────────────────
# 码图随包分发（src/jbs_hotsearch/assets/miniapp_qr.png）：
#   - 海报 HTML 里直接 base64 内嵌 —— 截图走 file://、素材页走公网静态托管，
#     不需要考虑相对路径和目录布局，一份字节三种场景通吃；
#   - 素材页另存一份独立文件，发布时想单独发图 / 换图都方便。
# 文件本体是一次性转好的 430×430 PNG（原微信导出是 JPEG 装在 .png 名里），
# 更换码图时直接覆盖这个文件即可，代码不用动（MIME 按字节头嗅探）。
_QR_ASSET = Path(__file__).parent / "assets" / "miniapp_qr.png"


def _qr_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    return "application/octet-stream"


@lru_cache(maxsize=1)
def _qr_data_uri() -> str:
    """小程序码的 data URI；资产缺失返回空串，海报退回不带码。"""
    try:
        raw = _QR_ASSET.read_bytes()
    except OSError:
        logger.warning("小程序码资产缺失（%s），海报不带码", _QR_ASSET)
        return ""
    return f"data:{_qr_mime(raw)};base64,{base64.b64encode(raw).decode('ascii')}"


def copy_qr_asset(dest_dir: Path) -> str:
    """把小程序码落成素材目录里的独立文件，返回文件名；失败返回空串。"""
    try:
        raw = _QR_ASSET.read_bytes()
    except OSError:
        return ""
    ext = {"image/png": ".png", "image/jpeg": ".jpg"}.get(_qr_mime(raw), ".png")
    name = f"miniapp-qr{ext}"
    try:
        (dest_dir / name).write_bytes(raw)
    except OSError as exc:
        logger.warning("小程序码复制到素材目录失败：%s", exc)
        return ""
    return name


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


def render_qr_page(
    qr_uri: str,
    width: int = 1080,
    height: int = 1440,
    board_date: str = "",
) -> str:
    """独立 3:4 码页（HS_SOCIAL_QR=tail 时追加在最后一张）。

    为什么单独一张而不是印在榜单图上：小红书对含码图片是按**整篇笔记**判「站外引流」
    限流，主图干净至少保住封面曝光；这张附图要不要发，由人自己权衡（不发就删掉）。

    这页是给站外 / 私域 / 线下用的（朋友圈、门店物料、私信），所以文案可以直说微信。
    """
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
.page {{ width: 100%; height: 100%; display: flex; flex-direction: column;
  align-items: center; justify-content: center; padding: 0 60px; text-align: center; }}
.card {{
  background: #fff; border: 1px solid #ece7dd; border-radius: 28px;
  padding: 46px 40px 40px; width: 100%;
}}
.card img {{ width: 430px; height: 430px; }}
.card h2 {{ font-size: 44px; font-weight: 800; margin-top: 26px; letter-spacing: 2px; }}
.card p {{ font-size: 26px; color: #8c8578; margin-top: 12px; line-height: 1.5; }}
.tail {{ margin-top: 34px; font-size: 22px; color: #a89e8e; }}
</style>
</head>
<body>
<div class="page">
  <div class="card">
    <img src="{qr_uri}" alt="小程序码">
    <h2>微信扫码 · 进小程序</h2>
    <p>剧本杀热度榜每日更新<br>看完整榜单 · 查剧本详情</p>
  </div>
  {('<div class="tail">' + _escape(board_date) + '</div>') if board_date else ""}
</div>
</body>
</html>"""


def render_poster_html(
    board: DailyBoard,
    board_date: str,
    width: int = 1080,
    height: int = 1440,
    show_parsed: bool = True,
    parsed_limit: int = 6,
    qr_uri: str = "",
) -> str:
    """渲染竖版海报 HTML（body 尺寸随内容变化，一屏即完整海报）。

    qr_uri 传空 = 不渲染小程序码区块（默认 off / tail 模式 / 资产缺失）。
    """
    items = board.items
    cards = "".join(_poster_item(it) for it in items)
    filtered = board.filtered_items if show_parsed else []
    parsed_block = _filtered_chips(filtered, parsed_limit)
    sub = "米圈杭州拼场 · 近 3 天真实组局"
    qr_block = ""
    if qr_uri:
        qr_block = (
            '\n  <section class="qrbox">'
            f'\n    <img src="{qr_uri}" alt="小程序码">'
            '\n    <div class="qt"><b>微信扫码 · 进小程序</b>'
            "\n      <span>剧本杀热度榜 · 每日更新</span></div>"
            "\n  </section>"
        )
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

/* 小程序码：白卡横条，码 + 两行引导。切片锚点带 .qrbox，整块永不被拦腰切 */
.qrbox {{
  margin-top: 14px; display: flex; align-items: center; gap: 22px;
  background: #fff; border: 1px solid #ece7dd; border-radius: 16px; padding: 15px 20px;
}}
.qrbox img {{ width: 124px; height: 124px; border-radius: 14px; flex: 0 0 auto; }}
.qrbox .qt b {{ display: block; font-size: 27px; font-weight: 800; letter-spacing: 1px; }}
.qrbox .qt span {{ display: block; font-size: 19px; color: #8c8578; margin-top: 7px; }}
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
  <ol class="list">{cards}</ol>{parsed_block}{qr_block}
  <div class="footer">热度由近 3 天真实组局计算 · 每日更新</div>
</div>
</body>
</html>"""


_TEMPLATE_CAPTION = """📊 杭州剧本杀热度榜 · {date}

今日榜首《{top1}》，杭州近 3 天真实拼场人气第一🔥

完整 Top10：
{summary}
{parsed_line}

#剧本杀 #杭州剧本杀 #周末去哪玩 #热门剧本杀 #剧本杀推荐"""


def _template_caption(board: DailyBoard, caption_parsed: list[RankedScript] | None = None) -> str:
    top1 = board.items[0].title if board.items else "——"
    summary = "\n".join(f"{it.rank}. {it.title}" for it in board.items)
    parsed_line = ""
    if caption_parsed:
        names = "、".join(f"《{it.title}》" for it in caption_parsed)
        # 只陈述状态，不做任何请求（「扣1」「码住」「求赞」= 诱导互动，小红书违规）
        parsed_line = (
            f"\n📚 这几本攻略已整理好，热度够高但本期未入榜：{names}"
        )
    return _TEMPLATE_CAPTION.format(
        date=board.board_date.isoformat(), top1=top1, summary=summary, parsed_line=parsed_line
    )


def _gen_caption(cfg: Config, board: DailyBoard):
    """按配方生成文案，返回 CaptionResult（含 .caption / .source / .recipe）。

    prompt 与采样参数全部来自 Supabase 的 caption_recipes 表，按日期哈希轮换，
    改文案不用动代码也不用重新部署；取不到配方时自动回退内置那套。
    """
    return gen_daily_caption(cfg, board, meta_fn=_item_meta, template_fn=_template_caption)


def _render_material_page(
    board_date: str,
    png_names: list[str],
    caption: str,
    caption_source: str,
    recipe: object | None = None,
    qr_file: str = "",
    qr_tail: bool = False,
) -> str:
    """素材页：海报切片（两列 + 序号 + 大图预览 + 一键保存）+ 文案 + 本次配方。

    切片排版跟周报保持一致：两列网格、每张独立卡片带「第 N 张」角标，
    用户一眼能看出每张的边界，不用再对着一整条长图猜哪里断开。
    qr_file 传素材目录里的码图文件名，空 = 不展示小程序码卡片。
    """
    caption_escaped = _escape(caption)
    # A/B 实验留痕：真人得知道今天这条文案是哪套配方出的，才好评判
    recipe_html = ""
    if recipe is not None and getattr(recipe, "name", ""):
        meta = recipe.as_meta() if hasattr(recipe, "as_meta") else {}
        bits = [f'配方 {_escape(meta.get("name") or "")}']
        if not meta.get("is_builtin"):
            bits.append(f'id={_escape(meta.get("id") or "")}')
        bits.append(f'来源 {"AI 生成" if caption_source == "llm" else "模板兜底"}')
        if meta.get("model"):
            bits.append(_escape(meta["model"]))
        if meta.get("temperature") is not None:
            bits.append(f'temp {meta["temperature"]}')
        recipe_html = f'<div class="recipe">A/B 实验 · {" · ".join(bits)}</div>'
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
        if qr_tail:
            tip += (
                "。<b style=\"color:#a12a2a\">最后一张是小程序码</b>：小红书机器审核能识别图片里的码，"
                "命中后按整篇笔记限流 —— 主图已是干净的，怕限流就别发那一张"
            )
    else:
        shot = ""
        tip = "海报没生成，先复制文案"
    qr_html = ""
    if qr_file:
        qr_html = (
            '<div class="qr-card">'
            f'<img src="{_escape(qr_file)}" alt="小程序码">'
            '<div class="qr-info"><h2>小程序码</h2>'
            "<p>点按钮下载，或长按图片保存原图。</p>"
            "<p style=\"margin-top:6px\"><b style=\"color:#a12a2a\">别印进小红书主图</b>："
            "机器审核识别到码会按整篇笔记限流。这张给站外 / 私域 / 线下用。</p>"
            f'<a class="qr-save" href="{_escape(qr_file)}" '
            f'download="{_escape(qr_file)}">⬇️ 保存小程序码</a>'
            "</div></div>"
        )
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
.date {{ color: #8c8578; font-size: 13px; margin-bottom: 6px; }}
.recipe {{
  display: inline-block; background: #fff5ee; border: 1px solid #f0c9b9; color: #a8451f;
  font-size: 12px; padding: 4px 10px; border-radius: 20px; margin-bottom: 16px;
}}
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
/* 小程序码卡片：码图 + 说明 + 单独保存按钮 */
.qr-card {{
  display: flex; gap: 14px; align-items: center; background: #fff;
  border: 1px solid #ece7dd; border-radius: 16px; padding: 14px; margin-bottom: 16px;
}}
.qr-card img {{ width: 110px; height: 110px; border-radius: 12px; border: 1px solid #ece7dd; flex: 0 0 auto; }}
.qr-card h2 {{ font-size: 15px; margin-bottom: 4px; }}
.qr-card p {{ font-size: 12px; color: #8c8578; line-height: 1.6; }}
.qr-save {{
  display: inline-block; margin-top: 8px; font-size: 12px; font-weight: 600;
  color: #e5532b; background: #fff5ee; border: 1px solid #f0c9b9;
  padding: 6px 14px; border-radius: 20px; text-decoration: none;
}}
.qr-save:active {{ opacity: .85; }}
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
  <div class="date">{_escape(board_date)}</div>
  {recipe_html}
  {shot}
  <div class="hint">{tip}</div>
  {qr_html}
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

    # 小程序码：off（默认，海报不带码）/ tail（主图干净 + 码单独一张附图）/ all（印在海报底部）
    # 素材目录那份独立文件三种模式都落 —— 海报不带码 ≠ 不要码，私域/站外照样要用
    qr_mode = cfg.social_qr
    qr_uri = _qr_data_uri()
    qr_file = copy_qr_asset(social_dir)
    result["qr_mode"] = qr_mode
    result["qr"] = bool(qr_uri)

    poster_html = social_dir / "poster.html"
    poster_html.write_text(
        render_poster_html(
            board,
            board_date,
            width,
            height,
            show_parsed,
            parsed_limit,
            qr_uri=qr_uri if qr_mode == "all" else "",
        ),
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
            # .qrbox 也要当锚点：多页切片时整块码不会从中间被切开
            anchors=".hero,.item,.parsed,.qrbox,.footer",
        )
        if slices:
            result["png"] = str(slices[0])
            if len(slices) > 1:
                result["pngs"] = [str(s) for s in slices]
    except Exception as exc:  # noqa: BLE001
        logger.warning("海报截图失败（Playwright/浏览器未就绪？）：%s", exc)
        result["png_error"] = str(exc)

    # 2.5) tail 模式：码单独做一张 3:4 附图，追加在切片末尾（主图保持干净）
    #      —— 是否发布这张由人决定，不发就在素材页里删掉它
    qr_tail_name = ""
    if qr_mode == "tail" and qr_uri:
        qr_page = social_dir / "poster-qr.html"
        qr_png = social_dir / f"{board_date}-qr.png"
        try:
            qr_page.write_text(
                render_qr_page(qr_uri, width, height, board_date), encoding="utf-8"
            )
            if _screenshot_html(qr_page, qr_png, width=width, height=height, scale=scale):
                qr_tail_name = qr_png.name
                slices = list(slices or []) + [qr_png]
                result["png"] = str(slices[0])
                result["pngs"] = [str(s) for s in slices]
                result["qr_png"] = str(qr_png)
        except Exception as exc:  # noqa: BLE001 - 码页失败不该影响主图已出好的产物
            logger.warning("小程序码页截图失败：%s", exc)

    # 3) 文案（按 caption_recipes 配方生成，A/B 轮换）
    cap = _gen_caption(cfg, board)
    caption_path = social_dir / f"{board_date}.txt"
    caption_path.write_text(cap.caption, encoding="utf-8")
    result["caption"] = str(caption_path)
    result["caption_source"] = cap.source
    result["recipe"] = cap.recipe.as_meta()
    result["recipe_id"] = cap.recipe.id

    # 配方留痕：哪天用了哪套 + 出了什么，几周后回看对比用
    recipe_json = social_dir / f"{board_date}.caption.json"
    recipe_json.write_text(
        json.dumps(
            {
                "board_date": board_date,
                "recipe": cap.recipe.as_meta(),
                "source": cap.source,
                "model": cap.model,
                "caption_len": len(cap.caption),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    result["recipe_json"] = str(recipe_json)
    log_run(cfg, board.board_date, cap)

    # 4) 素材页（只有 PNG 成功才内嵌图片，否则退化为纯文案页）
    material = social_dir / "index.html"
    material.write_text(
        _render_material_page(
            board_date,
            [Path(p).name for p in result.get("pngs", [result["png"]])] if "png" in result else [],
            cap.caption,
            cap.source,
            cap.recipe,
            qr_file=qr_file,
            qr_tail=bool(qr_tail_name),
        ),
        encoding="utf-8",
    )
    result["material_page"] = str(material)
    result["ok"] = "png" in result and bool(cap.caption)
    return result
