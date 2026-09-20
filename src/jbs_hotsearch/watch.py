# -*- coding: utf-8 -*-
"""监听大盘的 Web 服务：`python -m jbs_hotsearch watch`。

同样只用标准库 —— 监控自己不该有依赖风险。
路由：
    GET /                       大盘 HTML（服务端渲染，禁 JS 也能看）
    GET /api/status.json        同一份数据的 JSON（给脚本 / 别的面板接）
    GET /healthz                今天出榜成功 → 200，否则 503（可直接拿去做容器健康检查）
    GET /reviews/               评论聚合列表页（from data/reviews/*.html）
    GET /reviews/<file>         评论聚合某本的页面或配套文案（鬼河怒放.html / .txt）
    GET /reviews/<file>/poster  某本的照片页（鬼河怒放.html/poster -> 鬼河怒放.poster.html）
                                里面是一张可长按保存的 PNG + 复制文案按钮
    GET /weekly/                每周热度周报列表页（from data/weekly/*.html）
    GET /weekly/<file>          周报页 / 海报 PNG / 文案（.html/.png/.txt，不给 .json）
    GET /ab/                    文案 A/B 实验：回填发布后的数据 + 分配方效果对比
    POST /ab/save               把表单里的互动数据写回 Supabase caption_runs.metrics
"""
from __future__ import annotations

import html
import json
import logging
import re as _re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config
from .dashboard import render, render_json
from .watchdog import (
    ALERT_LEVELS,
    LEVEL_BAD,
    LEVEL_IDLE,
    LEVEL_MISSING,
    LEVEL_OK,
    LEVEL_PENDING,
    LEVEL_RUNNING,
    LEVEL_STUCK,
    LEVEL_UNKNOWN,
    Snapshot,
    build_snapshot,
)

# /healthz 里算「还活着」的等级：
#   未到点 / 无任务 / 进行中 都是正常的；探测失败（unknown）不算任务失败，
#   否则网络一抖就误报 503；可疑（warn）留给页面去看，不触发健康检查失败。
HEALTHY_LEVELS = (
    LEVEL_OK,
    LEVEL_IDLE,
    LEVEL_PENDING,
    LEVEL_RUNNING,
    LEVEL_UNKNOWN,
)

# 单个任务的健康与否，由它今天（asset 类则由产物）的等级决定
BREAKING_LEVELS = (LEVEL_BAD, LEVEL_MISSING, LEVEL_STUCK)

logger = logging.getLogger(__name__)


class _State:
    """快照缓存 + 告警去重。多线程读写，用锁兜住。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.snapshot: Snapshot | None = None
        self.fetched_at: float = 0.0
        self.last_alert_sig: str | None = None

    def fresh(self, ttl: float) -> Snapshot | None:
        with self.lock:
            if self.snapshot and (time.monotonic() - self.fetched_at) < ttl:
                return self.snapshot
        return None


def _alert_signature(snap: Snapshot) -> str:
    """告警集合的指纹：只有「谁出问题了」发生变化才重新推送，避免每分钟刷屏。"""
    parts: list[str] = []
    for board in snap.boards:
        for item in board.items:
            if item.level in ALERT_LEVELS:
                parts.append(
                    f"{board.key}|{item.date}|{item.level}|{(item.error or item.note or '')[:80]}"
                )
        for asset in board.assets:
            if asset.level == LEVEL_BAD:
                parts.append(f"{board.key}|asset|{asset.path}|{asset.note[:60]}")
    return "|".join(parts)


def _post_webhook(url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info("告警已推送 %s -> %s", url, resp.status)
    except urllib.error.HTTPError as exc:
        logger.warning("告警推送失败 %s：HTTP %s", url, exc.code)
    except OSError as exc:  # URLError / 超时都归到这里，告警失败绝不能影响大盘
        logger.warning("告警推送失败 %s：%s", url, exc)


# ----------------------------------------------------------------------------
# 评论聚合（reviews）：挂载 data/reviews/*.html 与 *.txt
# ----------------------------------------------------------------------------
def _reviews_dir(cfg: Config) -> Path:
    return cfg.data_dir / "reviews"


def _list_reviews(reviews_dir: Path) -> list[dict[str, Any]]:
    """扫 reviews 目录：返回每本（*.html 一项，附同名 *.txt 文案内容）。"""
    out: list[dict[str, Any]] = []
    if not reviews_dir.is_dir():
        return out
    for html_path in sorted(reviews_dir.glob("*.html")):
        # 海报页和截图底稿都是详情页的附属，不在列表里单独展示
        if html_path.name.endswith(".poster.html") or html_path.name.endswith(".sheet.html"):
            continue
        title = html_path.stem  # 例: 鬼河怒放
        txt_path = reviews_dir / f"{title}.txt"
        poster_path = reviews_dir / f"{title}.poster.html"
        try:
            stat = html_path.stat()
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError:
            size = 0
            mtime = 0.0
        # 列表摘要：从 hero 段后面抓一段纯文本（去掉所有 HTML 标签）
        summary = ""
        try:
            raw = html_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            raw = ""
        if "</header>" in raw:
            tail = raw.split("</header>", 1)[1][:600]
            text = _re.sub(r"<[^>]+>", " ", tail)
            text = _re.sub(r"\s+", " ", text).strip()
            summary = text[:140]
        caption = ""
        if txt_path.is_file():
            try:
                caption = txt_path.read_text(encoding="utf-8", errors="ignore")[:2000]
            except OSError:
                caption = ""
        out.append({
            "title": title,
            "html": html_path.name,
            "txt": txt_path.name if txt_path.is_file() else None,
            "poster": poster_path.name if poster_path.is_file() else None,
            "caption": caption,
            "mtime": mtime,
            "size": size,
            "summary": summary,
        })
    out.sort(key=lambda r: (-r["mtime"], r["title"]))
    return out


_REVIEWS_LIST_CSS = """
:root { --bg:#f5f3ee; --card:#fff; --ink:#211d18; --muted:#8c8578; --line:#ece7dd; --brand:#e5532b; }
* { box-sizing:border-box; margin:0; padding:0; }
body { font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--ink); padding:24px 16px 60px; }
.wrap { max-width:720px; margin:0 auto; }
.hero { background:linear-gradient(135deg,#3a2015,#6b2c1c 45%,#c8502a); color:#fff;
  border-radius:0 0 22px 22px; padding:26px 22px 22px; margin:0 -16px 18px; }
.hero .brand { font-size:13px; letter-spacing:3px; opacity:.82; }
.hero h1 { font-size:26px; font-weight:800; margin:6px 0 4px; }
.hero .sub { font-size:14px; opacity:.9; }
.hero .meta { font-size:12px; opacity:.75; margin-top:4px; }
nav.top { font-size:12px; color:var(--muted); margin:0 0 12px; }
nav.top a { color:var(--brand); text-decoration:none; }
.row { background:var(--card); border:1px solid var(--line); border-radius:14px;
  padding:14px 16px; margin-bottom:12px; display:flex; gap:12px; align-items:flex-start; }
.row .body { flex:1; min-width:0; }
.row .title { font-size:17px; font-weight:700; }
.row .meta { font-size:12px; color:var(--muted); margin-top:2px; }
.row .summary { font-size:12px; color:#5b554a; margin-top:6px; line-height:1.5; }
.row .acts { margin-left:auto; display:flex; flex-direction:column; gap:6px; flex-shrink:0; }
.row .acts a, .row .acts button { font-size:12px; padding:5px 11px; border-radius:20px; text-decoration:none;
  background:#fff5ee; border:1px solid #f9d9c5; color:var(--brand); font-weight:600;
  text-align:center; white-space:nowrap; cursor:pointer; }
.row .acts a.txt { background:#f3f0e9; border-color:#e4e0d6; color:#6b6255; }
.row .acts button { background:var(--brand); border-color:var(--brand); color:#fff; }
.empty { background:var(--card); border:1px dashed var(--line); border-radius:14px;
  padding:40px 20px; text-align:center; color:var(--muted); }
.empty b { display:block; color:var(--ink); font-size:16px; margin-bottom:6px; }
.empty code { background:#f1f0ea; padding:1px 6px; border-radius:4px; font-size:12px; }
.foot { text-align:center; color:var(--muted); font-size:11px; margin-top:24px; }
"""


def _render_reviews_index(rows: list[dict[str, Any]]) -> str:
    if not rows:
        body = """
        <div class="empty">
          <b>暂无评论聚合</b>
          还没有生成任何剧本的店家榜。<br>
          步骤：导出某剧本的剧评 HAR（米圈 App 详情页「全部评论」分页截包），
          然后跑 <code>python -m jbs_hotsearch reviews --har x.har --script 剧本名</code>。
        </div>"""
    else:
        items = []
        for r in rows:
            ts = time.strftime("%m-%d %H:%M", time.localtime(r["mtime"])) if r["mtime"] else "—"
            acts = (
                f'<a href="/reviews/{urllib.parse.quote(r["html"])}" target="_blank" rel="noopener">打开页面</a>'
            )
            if r["caption"]:
                caption_attr = html.escape(r["caption"]).replace("\n", "&#10;").replace("\r", "")
                acts += (
                    f'<button type="button" class="copy-btn" data-caption="{caption_attr}" '
                    f'onclick="copyRowCaption(this)">复制文案</button>'
                )
            if r["poster"]:
                acts += (
                    f'<a class="txt" href="/reviews/{urllib.parse.quote(r["poster"])}" '
                    f'target="_blank" rel="noopener">保存图片</a>'
                )
            summary = html.escape(r["summary"][:140]) if r["summary"] else ""
            items.append(
                f'<div class="row"><div class="body">'
                f'<div class="title">{html.escape(r["title"])}</div>'
                f'<div class="meta">更新 {ts} · {int(r["size"]/1024)+1} KB</div>'
                + (f'<div class="summary">{summary}</div>' if summary else "")
                + f'</div><div class="acts">{acts}</div></div>'
            )
        body = "".join(items)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>评论聚合 · 店家榜</title>
<style>{_REVIEWS_LIST_CSS}</style></head>
<body><div class="wrap">
  <header class="hero">
    <div class="brand">JBS · 评论聚合</div>
    <h1>店家评测榜</h1>
    <div class="sub">按玩家评论聚合（评分仅作排序参考）</div>
    <div class="meta">共 {len(rows)} 部 · 数据源：米圈 /v13/script/getScriptEvaluateList</div>
  </header>
  <nav class="top"><a href="/">← 回到大盘</a></nav>
  {body}
  <div class="foot">gen by jbs-hotsearch · 评论聚合</div>
</div>
<script>
function copyRowCaption(btn) {{
  const text = (btn.getAttribute('data-caption') || '').replace(/&#10;/g, '\n');
  if (!text) {{ btn.textContent = '暂无'; return; }}
  const ok = function() {{ btn.textContent = '✓ 已复制'; setTimeout(function(){{ btn.textContent = '复制文案'; }}, 1500); }};
  const fail = function() {{ btn.textContent = '失败'; setTimeout(function(){{ btn.textContent = '复制文案'; }}, 1500); }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(ok).catch(fail);
  }} else {{
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try {{ document.execCommand('copy'); ok(); }} catch (e) {{ fail(); }}
    document.body.removeChild(ta);
  }}
}}
</script>
</body></html>"""


def _resolve_reviews_file(cfg: Config, name: str):
    """校验文件名后返回 (abs_path, mime)；不安全（穿越 / 非白名单）返回 (None, None)。

    允许 .html / .txt / .png；解析后的绝对路径必须仍在 reviews 目录下（防 ../ 越权）。
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None, None
    if not (name.endswith(".html") or name.endswith(".txt") or name.endswith(".png")):
        return None, None
    reviews_dir = _reviews_dir(cfg).resolve()
    candidate = (reviews_dir / name).resolve()
    try:
        candidate.relative_to(reviews_dir)
    except ValueError:
        return None, None
    if not candidate.is_file():
        return None, None
    if name.endswith(".png"):
        mime = "image/png"
    elif name.endswith(".html"):
        mime = "text/html; charset=utf-8"
    else:
        mime = "text/plain; charset=utf-8"
    return candidate, mime


class _ReviewsCache:
    """reviews 列表缓存：reviews 文件少、扫盘轻，没必要每请求 IO。"""

    def __init__(self, ttl: float = 30.0) -> None:
        self.ttl = ttl
        self.lock = threading.Lock()
        self.rows: list[dict[str, Any]] = []
        self.fetched_at: float = 0.0

    def get(self, cfg: Config) -> list[dict[str, Any]]:
        if (time.monotonic() - self.fetched_at) < self.ttl:
            return self.rows
        with self.lock:
            if (time.monotonic() - self.fetched_at) < self.ttl:
                return self.rows
            self.rows = _list_reviews(_reviews_dir(cfg))
            self.fetched_at = time.monotonic()
            return self.rows


# ----------------------------------------------------------------------------
# 每周周报（weekly）：挂载 data/weekly/*
# ----------------------------------------------------------------------------
def _weekly_dir(cfg: Config) -> Path:
    return cfg.data_dir / "weekly"


def _list_weekly(weekly_dir: Path) -> list[dict[str, Any]]:
    """扫周报目录：*.html 一项（跳过 .sheet.html 底稿），元数据从同名 .json 读。"""
    out: list[dict[str, Any]] = []
    if not weekly_dir.is_dir():
        return out
    for html_path in sorted(weekly_dir.glob("*.html"), reverse=True):
        if html_path.name.endswith(".sheet.html"):
            continue
        title = html_path.stem  # 例: 2026-09-12（统计周期的起始日 = 周六）
        try:
            stat = html_path.stat()
            mtime = stat.st_mtime
        except OSError:
            mtime = 0.0
        meta: dict[str, Any] = {}
        meta_path = weekly_dir / f"{title}.json"
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        caption = ""
        txt_path = weekly_dir / f"{title}.txt"
        if txt_path.is_file():
            try:
                caption = txt_path.read_text(encoding="utf-8", errors="ignore")[:3000]
            except OSError:
                caption = ""
        png_path = weekly_dir / f"{title}.png"
        items = meta.get("items") or []
        out.append({
            "title": title,
            "html": html_path.name,
            "png": png_path.name if png_path.is_file() else None,
            "range_text": meta.get("range_text") or title,
            "range_cn": meta.get("range_cn") or "",
            "iso_week": meta.get("iso_week") or "",
            "covered_days": meta.get("covered_days"),
            "unique_scripts": meta.get("unique_scripts"),
            "top1": (items[0].get("title") if items else None),
            "caption": caption,
            "mtime": mtime,
        })
    return out


_WEEKLY_LIST_CSS = _REVIEWS_LIST_CSS + """
.row .range { font-size:13px; color:var(--ink); font-weight:600; margin-top:2px; }
.row .stats { font-size:12px; color:var(--muted); margin-top:2px; }
.row .stats b { color:var(--brand); }
"""


def _render_weekly_index(rows: list[dict[str, Any]]) -> str:
    if not rows:
        body = """
        <div class="empty">
          <b>暂无周报</b>
          周报每周五自动生成上一周期（周六~周五）的总结。<br>
          也可以手动跑：<code>python -m jbs_hotsearch weekly</code>
        </div>"""
    else:
        items = []
        for r in rows:
            covered = r.get("covered_days")
            unique = r.get("unique_scripts")
            top1 = r.get("top1")
            stats_bits = []
            if covered is not None:
                stats_bits.append(f"覆盖 <b>{covered}/7</b> 天")
            if unique is not None:
                stats_bits.append(f"<b>{unique}</b> 本上榜")
            if top1:
                stats_bits.append(f"周冠军 <b>{html.escape(str(top1))}</b>")
            stats = " · ".join(stats_bits)
            acts = (
                f'<a href="/weekly/{urllib.parse.quote(r["html"])}" target="_blank" '
                f'rel="noopener">打开周报</a>'
            )
            if r["caption"]:
                caption_attr = html.escape(r["caption"]).replace("\n", "&#10;").replace("\r", "")
                acts += (
                    f'<button type="button" class="copy-btn" data-caption="{caption_attr}" '
                    f'onclick="copyRowCaption(this)">复制文案</button>'
                )
            if r["png"]:
                acts += (
                    f'<a class="txt" href="/weekly/{urllib.parse.quote(r["png"])}" '
                    f'target="_blank" rel="noopener">保存图片</a>'
                )
            items.append(
                f'<div class="row"><div class="body">'
                f'<div class="range">{html.escape(r["range_text"])}'
                + (f' <span style="font-weight:400;color:var(--muted)">（{html.escape(r["iso_week"])}）</span>' if r["iso_week"] else "")
                + "</div>"
                + (f'<div class="stats">{stats}</div>' if stats else "")
                + f"</div><div class=\"acts\">{acts}</div></div>"
            )
        body = "".join(items)
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>热度周报</title>
<style>{_WEEKLY_LIST_CSS}</style></head>
<body><div class="wrap">
  <header class="hero">
    <div class="brand">JBS · 热度周报</div>
    <h1>每周热度总结</h1>
    <div class="sub">统计口径：周六 ~ 周五（7 天）· 周均热度 × 上榜天数</div>
    <div class="meta">每周五 10:00 自动生成上周六~本周五总结 · 也可手动跑 weekly 命令</div>
  </header>
  <nav class="top"><a href="/">← 回到大盘</a></nav>
  {body}
  <div class="foot">gen by jbs-hotsearch · 热度周报</div>
</div>
<script>
function copyRowCaption(btn) {{
  const text = (btn.getAttribute('data-caption') || '').replace(/&#10;/g, '\n');
  if (!text) {{ btn.textContent = '暂无'; return; }}
  const ok = function() {{ btn.textContent = '✓ 已复制'; setTimeout(function(){{ btn.textContent = '复制文案'; }}, 1500); }};
  const fail = function() {{ btn.textContent = '失败'; setTimeout(function(){{ btn.textContent = '复制文案'; }}, 1500); }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(ok).catch(fail);
  }} else {{
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try {{ document.execCommand('copy'); ok(); }} catch (e) {{ fail(); }}
    document.body.removeChild(ta);
  }}
}}
</script>
</body></html>"""


def _resolve_weekly_file(cfg: Config, name: str):
    """周报文件白名单：.html / .png / .txt（不给 .json，元数据是内部的）。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None, None
    if name.endswith(".sheet.html") or name.endswith(".json"):
        return None, None
    if not (name.endswith(".html") or name.endswith(".png") or name.endswith(".txt")):
        return None, None
    weekly_dir = _weekly_dir(cfg).resolve()
    candidate = (weekly_dir / name).resolve()
    try:
        candidate.relative_to(weekly_dir)
    except ValueError:
        return None, None
    if not candidate.is_file():
        return None, None
    if name.endswith(".png"):
        mime = "image/png"
    elif name.endswith(".html"):
        mime = "text/html; charset=utf-8"
    else:
        mime = "text/plain; charset=utf-8"
    return candidate, mime


# ----------------------------------------------------------------------------
# 文案 A/B 实验（/ab/）：回填发布数据 + 分配方效果对比
# ----------------------------------------------------------------------------
# 为什么要这个页面：caption_runs 里只记录了「哪天用了哪套配方、出了什么文案」，
# 但小红书的浏览/点赞/转发/收藏要等发完才知道。之前只能手写 SQL update，
# 现在做成表单直接写回 metrics jsonb，判定优化结果不用再碰 SQL。
#
# 刻意用 urllib 而不是 httpx：watch 镜像（Dockerfile.watch）为了「构建只要几秒」
# 和「依赖装不上也要能看大盘」，一个第三方包都没装。这里必须守住那条约束。

AB_TABLE = "caption_runs"
AB_KIND = "daily"

# 要填的互动字段（顺序 = 表单顺序）。key 就是 metrics jsonb 里的 key，
# 和 sql/caption_ab_report.sql 保持一致，改这里要连 SQL 一起改。
AB_FIELDS = (
    ("views", "浏览量"),
    ("likes", "点赞"),
    ("shares", "转发"),
    ("collects", "收藏"),
    ("comments", "评论"),
)
AB_NUM_KEYS = tuple(k for k, _ in AB_FIELDS)
AB_NOTE_KEY = "note"          # 备注存同一个 jsonb 里，便于记「几点发的 / 标题改过没」
AB_PAGE_LIMIT = 60            # 页面最多列多少天
_DATE_RE = _re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _ab_int(value: Any) -> int | None:
    """表单/数据库里的数字清洗：空串与非数字一律当「没填」，负数截为 0。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        n = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None
    return max(0, n)


def _sb_call(
    cfg: Config,
    method: str,
    table: str,
    query: dict[str, str],
    payload: Any = None,
) -> tuple[bool, int, str]:
    """Supabase REST 的最小封装（stdlib urllib）。返回 (是否成功, HTTP 码, 响应体)。"""
    headers = {
        "apikey": cfg.supabase_service_role_key,
        "Authorization": f"Bearer {cfg.supabase_service_role_key}",
        "Accept": "application/json",
    }
    data: bytes | None = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    url = f"{cfg.supabase_url.rstrip('/')}/rest/v1/{table}?{urllib.parse.urlencode(query)}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=cfg.http_timeout) as resp:
            return True, resp.status, resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        return False, exc.code, exc.read().decode("utf-8", "ignore")[:300]
    except OSError as exc:  # URLError / 超时 / DNS 都在这
        return False, 0, str(exc)


def _fetch_ab_runs(cfg: Config) -> tuple[list[dict[str, Any]], str]:
    """取最近 AB_PAGE_LIMIT 天的日报文案记录。返回 (rows, 错误文案)。"""
    if not (cfg.supabase_url and cfg.supabase_service_role_key):
        return [], "未配置 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY，读不到文案记录。"
    ok, status, text = _sb_call(
        cfg,
        "GET",
        AB_TABLE,
        {
            "select": "board_date,kind,recipe_id,recipe_name,source,caption,caption_len,metrics",
            "kind": f"eq.{AB_KIND}",
            "order": "board_date.desc",
            "limit": str(AB_PAGE_LIMIT),
        },
    )
    if not ok:
        if "PGRST205" in text or "does not exist" in text:
            return [], (
                f"Supabase 里还没有 public.{AB_TABLE}。"
                "去 Dashboard → SQL Editor 执行一次 sql/caption_recipes.sql。"
            )
        return [], f"读取 caption_runs 失败（HTTP {status}）：{text}"
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        return [], "caption_runs 返回的不是合法 JSON"
    return [r for r in rows if _DATE_RE.match(str(r.get("board_date") or ""))], ""


def _save_ab_metrics(cfg: Config, date: str, metrics: dict[str, Any] | None) -> tuple[bool, str]:
    """把某一天的互动数据写回 metrics。metrics 为 None/空 = 清空（= 这条不参与统计）。"""
    ok, status, text = _sb_call(
        cfg,
        "PATCH",
        AB_TABLE,
        {"board_date": f"eq.{date}", "kind": f"eq.{AB_KIND}"},
        {"metrics": metrics},
    )
    if ok:
        return True, ""
    return False, f"{date}: HTTP {status} {text}"


def _ab_metrics_of(row: dict[str, Any]) -> dict[str, Any]:
    m = row.get("metrics")
    return m if isinstance(m, dict) else {}


def _agg_ab(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按配方聚合「已回填」的行，给对比用的均值与比率。

    两层过滤缺一不可：
      - 没回填的行不能进，否则等于拿一堆 0 拉低均值，实验直接失真；
      - source <> 'llm' 的行（LLM 挂了掉回模板兜底）也要排除，
        那天文案压根不是配方写的，算进来只会污染对比 —— 与 sql/caption_ab_report.sql 口径一致。
    """
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        m = _ab_metrics_of(row)
        if not m:
            continue
        if str(row.get("source") or "") != "llm":
            continue
        key = str(row.get("recipe_id") or "builtin")
        b = buckets.setdefault(
            key,
            {
                "key": key,
                "name": str(row.get("recipe_name") or key),
                "days": 0,
                "views": 0,
                "likes": 0,
                "shares": 0,
                "collects": 0,
                "comments": 0,
            },
        )
        b["days"] += 1
        for k in AB_NUM_KEYS:
            v = _ab_int(m.get(k))
            if v is not None:
                b[k] += v
    out: list[dict[str, Any]] = []
    for b in buckets.values():
        days = b["days"] or 1
        item = dict(b)
        # 逐天均值：浏览量取整看着舒服，其余保留一位小数（点赞几十的量级，小数有意义）
        for k in AB_NUM_KEYS:
            item["avg_" + k] = round(b[k] / days) if k == "views" else round(b[k] / days, 1)
        views = b["views"]
        engage = b["likes"] + b["collects"] + b["comments"]
        # 互动率 = 真金白银的反馈 / 曝光；收藏率 = 「有用，以后再看」，
        # 清单干货型文案的胜负主要看收藏率
        item["engage_rate"] = (100.0 * engage / views) if views else None
        item["collect_rate"] = (100.0 * b["collects"] / views) if views else None
        out.append(item)
    out.sort(key=lambda x: (x["engage_rate"] is None, -(x["engage_rate"] or 0)))
    return out


_AB_CSS = _REVIEWS_LIST_CSS + """
.wrap.ab { max-width:920px; }
.tips { background:#fff8ee; border:1px solid #f2d9b8; color:#8a5a1f; font-size:12px;
  line-height:1.7; padding:10px 14px; border-radius:12px; margin-bottom:14px; }
.tips b { color:#6d4310; }
.cardbox { background:var(--card); border:1px solid var(--line); border-radius:14px;
  padding:14px 16px; margin-bottom:14px; }
.cardbox h3 { font-size:14px; margin:0 0 10px; font-weight:700; }
table.cmp { width:100%; border-collapse:collapse; font-size:12px; }
table.cmp th { text-align:right; padding:6px 8px; border-bottom:1px solid var(--line);
  color:var(--muted); font-weight:600; white-space:nowrap; }
table.cmp th:first-child, table.cmp td:first-child { text-align:left; }
table.cmp td { text-align:right; padding:7px 8px; border-bottom:1px solid #f1f2f4;
  font-variant-numeric:tabular-nums; }
table.cmp tr:last-child td { border-bottom:none; }
table.cmp td.win { color:var(--brand); font-weight:700; }
.day { background:var(--card); border:1px solid var(--line); border-radius:14px;
  padding:12px 14px; margin-bottom:10px; }
.day.fallback { background:#fbf8f4; border-style:dashed; }
.day .head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.day .d { font-size:16px; font-weight:700; }
.pill { font-size:11px; padding:2px 8px; border-radius:20px; white-space:nowrap; }
.pill.rcp { background:#fff1e8; border:1px solid #f6cfb6; color:#a8451f; }
.pill.ok { background:#e8f6ee; border:1px solid #bfe4ce; color:#177245; }
.pill.todo { background:#f3f0e9; border:1px solid #e4e0d6; color:#8c8578; }
.pill.drop { background:#fdecec; border:1px solid #f6c4c4; color:#a12a2a; }
.grid { display:grid; grid-template-columns:repeat(5,1fr); gap:8px; margin-top:10px; }
.grid label { display:block; font-size:11px; color:var(--muted); margin-bottom:3px; }
.grid input { width:100%; padding:8px 8px; border:1px solid var(--line); border-radius:8px;
  font:inherit; font-size:14px; text-align:right; font-variant-numeric:tabular-nums; }
.grid input:focus { outline:none; border-color:var(--brand); }
.note-row { margin-top:8px; }
.note-row input { width:100%; padding:8px 10px; border:1px solid var(--line); border-radius:8px;
  font:inherit; font-size:13px; }
.day .cap { font-size:12px; color:#5b554a; margin-top:8px; line-height:1.55;
  background:#f7f5f0; border-radius:8px; padding:7px 10px; white-space:pre-wrap; }
.day .rate { font-size:11px; color:var(--muted); margin-top:6px; }
.day .rate b { color:var(--brand); }
.bar-wrap { position:sticky; bottom:0; padding:12px 0 16px; background:linear-gradient(
  to top, var(--bg) 62%, rgba(245,243,238,0)); }
.bar-wrap button { width:100%; padding:13px; border-radius:12px; border:none;
  background:var(--brand); color:#fff; font-size:15px; font-weight:700; cursor:pointer; }
.bar-wrap .hint { text-align:center; font-size:11px; color:var(--muted); margin-top:6px; }
.ok-banner { background:#e8f6ee; border:1px solid #bfe4ce; color:#177245; font-size:13px;
  padding:10px 14px; border-radius:12px; margin-bottom:14px; }
@media (max-width:560px){ .grid { grid-template-columns:repeat(3,1fr); } }
"""


def _render_ab_page(
    rows: list[dict[str, Any]],
    error: str,
    saved: int | None,
    failed: int | None = None,
) -> str:
    filled = [r for r in rows if _ab_metrics_of(r)]
    aggs = _agg_ab(rows)

    head_bits: list[str] = []
    if error:
        head_bits.append(f'<div class="tips" style="background:#fdecec;border-color:#f6c4c4;'
                         f'color:#a12a2a">{html.escape(error)}</div>')
    if saved is not None:
        word = "没有需要保存的改动" if saved == 0 else f"已保存 {saved} 天的数据"
        head_bits.append(f'<div class="ok-banner">✓ {word}</div>')
    if failed:
        head_bits.append(
            f'<div class="tips" style="background:#fdecec;border-color:#f6c4c4;color:#a12a2a">'
            f"有 {failed} 天写入失败（其余已保存），详见 <code>docker logs jbs-hotsearch-watch</code></div>"
        )

    # ---- 汇总对比表 ----
    if aggs:
        ths = "".join(f"<th>{lab}</th>" for _, lab in AB_FIELDS)
        # 每列的最优值加粗，方便一眼看出哪套赢
        best = {
            k: max((a["avg_" + k] for a in aggs), default=None)
            for k in ("views", "likes", "shares", "collects")
        }
        trs = []
        for a in aggs:
            cells = []
            for k, _lab in AB_FIELDS:
                v = a["avg_" + k]
                cls = " class='win'" if v and best.get(k) is not None and v == best[k] else ""
                shown_v = f"{v:,}" if isinstance(v, int) else str(v)
                cells.append(f"<td{cls}>{shown_v}</td>")
            rate = f'{a["engage_rate"]:.2f}%' if a["engage_rate"] is not None else "—"
            crate = f'{a["collect_rate"]:.2f}%' if a["collect_rate"] is not None else "—"
            trs.append(
                f"<tr><td><b>{html.escape(a['name'])}</b>"
                f'<div style="font-size:11px;color:var(--muted)">{a["days"]} 天样本</div></td>'
                + "".join(cells)
                + f"<td>{rate}</td><td>{crate}</td></tr>"
            )
        cmp_html = (
            '<div class="cardbox"><h3>配方效果对比（只统计已回填的天）</h3>'
            '<table class="cmp"><thead><tr><th>配方</th>' + ths
            + "<th>互动率</th><th>收藏率</th></tr></thead><tbody>"
            + "".join(trs)
            + "</tbody></table>"
            '<div style="font-size:11px;color:var(--muted);margin-top:10px;line-height:1.7">'
            "互动率 =（点赞+收藏+评论）/ 浏览量 · 收藏率 = 收藏 / 浏览量 · 表中数值为各天均值<br>"
            "口径：只统计已回填、且确实是 LLM 出文案的天（模板兜底那天不算）；<br>"
            "样本少于 10 天时差异多半是运气，别急着下结论。</div></div>"
        )
    else:
        cmp_html = (
            '<div class="cardbox"><h3>配方效果对比</h3>'
            '<div style="font-size:12px;color:var(--muted);line-height:1.7">'
            "还没有任何一天回填了互动数据。在下面填完并保存后，这里会按配方出均值对比。</div></div>"
        )

    if not rows:
        body = """
        <div class="empty">
          <b>暂无文案记录</b>
          日榜跑起来并且生成了小红书文案后，每天会写一条 caption_runs。<br>
          还没有的话先跑 <code>python -m jbs_hotsearch social</code>。
        </div>"""
    else:
        items = []
        for r in rows:
            m = _ab_metrics_of(r)
            date = str(r["board_date"])
            recipe_name = str(r.get("recipe_name") or r.get("recipe_id") or "内置")
            source = str(r.get("source") or "")
            fallback = source != "llm"
            pills = [f'<span class="pill rcp">{html.escape(recipe_name)}</span>']
            if fallback:
                pills.append('<span class="pill drop">模板兜底 · 建议不算</span>')
            pills.append(
                f'<span class="pill {"ok" if m else "todo"}">'
                f'{"已回填" if m else "待回填"}</span>'
            )
            inputs = []
            for k, lab in AB_FIELDS:
                val = m.get(k)
                n = _ab_int(val)
                shown = "" if n is None else str(n)
                inputs.append(
                    f'<div><label for="{k}-{date}">{lab}</label>'
                    f'<input id="{k}-{date}" name="{k}:{date}" type="number" min="0" '
                    f'step="1" inputmode="numeric" placeholder="0" value="{shown}"></div>'
                )
            note_val = html.escape(str(m.get(AB_NOTE_KEY) or ""))
            cap = str(r.get("caption") or "").strip()
            cap_html = (
                f'<div class="cap">{html.escape(cap[:160])}{"…" if len(cap) > 160 else ""}</div>'
                if cap
                else ""
            )
            views = _ab_int(m.get("views")) or 0
            if views:
                engage = (
                    (_ab_int(m.get("likes")) or 0)
                    + (_ab_int(m.get("collects")) or 0)
                    + (_ab_int(m.get("comments")) or 0)
                )
                rate_html = (
                    f'<div class="rate">互动率 <b>{100.0 * engage / views:.2f}%</b>'
                    f' · 收藏率 <b>{100.0 * (_ab_int(m.get("collects")) or 0) / views:.2f}%</b>'
                    f'（基于 {views:,} 浏览）</div>'
                )
            else:
                rate_html = ""
            items.append(
                f'<div class="day{" fallback" if fallback else ""}">'
                f'<div class="head"><span class="d">{date}</span>{"".join(pills)}</div>'
                f'<div class="grid">{"".join(inputs)}</div>'
                f'<div class="note-row"><label for="note-{date}" '
                f'style="font-size:11px;color:var(--muted)">备注（发布时点 / 标题改动等）</label>'
                f'<input id="note-{date}" name="note:{date}" type="text" '
                f'placeholder="选填，如：晚 8 点发" value="{note_val}"></div>'
                f"{rate_html}{cap_html}</div>"
            )
            # 相对路径故意写成相对：直接访问 8787 时解析为 /ab/save，
            # 走 nginx 的 /watch/ 前缀时解析为 /watch/ab/save（前缀被 nginx 剥掉后同样是 /ab/save）
            body = (
                '<form method="post" action="save">'
            + "".join(items)
            + '<div class="bar-wrap"><button type="submit">💾 保存全部改动</button>'
            '<div class="hint">留空 = 该项不填；全部留空 = 清空这一天，不进统计</div></div>'
            "</form>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>文案 A/B · 发布数据回填</title>
<style>{_AB_CSS}</style></head>
<body><div class="wrap ab">
  <header class="hero">
    <div class="brand">JBS · 文案实验</div>
    <h1>发布数据回填</h1>
    <div class="sub">每天日报用了哪套配方是自动记录的，这里补上小红书的实际数据</div>
    <div class="meta">共 {len(rows)} 天 · 已回填 {len(filled)} 天 · 数据源：Supabase caption_runs</div>
  </header>
  <nav class="top"><a href="../">← 回到大盘</a> · <a href="../reviews/">评论聚合</a>
    · <a href="../weekly/">热度周报</a></nav>
  {"".join(head_bits)}
  <div class="tips">
    <b>怎么判：</b>看完曝光再看<b>互动率</b>（赞+藏+评 ÷ 浏览）——曝光主要看发布时段和封面，
    互动率才反映文案本身。<br>
    <b>口径要一致：</b>每篇都等发满 <b>48 小时</b>再回填，别今天填昨天那篇、一个月后再填上周那篇，
    观察窗口不一样等于白比。日期偏好或特殊情况写进「备注」。<br>
    <b>别急着下结论：</b>单组样本 &lt; 10 天时差异多半是运气；跑满 3~4 周再看。
  </div>
  {cmp_html}
  {body}
  <div class="foot">gen by jbs-hotsearch · 文案 A/B</div>
</div></body></html>"""


def _notify(cfg: Config, state: _State, snap: Snapshot) -> None:
    """有必要告警时推一次 webhook（配置了 HS_WATCH_WEBHOOK_URL 才发）。"""
    if not cfg.watch_webhook_url:
        return
    signature = _alert_signature(snap)
    with state.lock:
        first = state.last_alert_sig is None
        changed = state.last_alert_sig != signature
        state.last_alert_sig = signature
    if not signature:
        return
    if not changed:
        return
    # 首轮静默：服务刚启动时把历史欠账一次性全推出去没有意义
    if first and not snap.today_status:
        return
    details: list[dict[str, Any]] = []
    for board in snap.boards:
        for item in board.items:
            if item.level in ALERT_LEVELS:
                details.append(
                    {
                        "task": board.key,
                        "task_name": board.name,
                        "date": item.date,
                        "level": item.level,
                        "message": item.error or item.note,
                    }
                )
        for asset in board.assets:
            if asset.level == LEVEL_BAD:
                details.append(
                    {
                        "task": board.key,
                        "task_name": board.name,
                        "date": snap.today,
                        "level": asset.level,
                        "message": f"{asset.path} 缺失：{asset.note}",
                    }
                )
    _post_webhook(
        cfg.watch_webhook_url,
        {"service": "jbs-watch", "today": snap.today, "alerts": details},
    )


def _get_snapshot(cfg: Config, state: _State) -> Snapshot:
    cached = state.fresh(cfg.watch_cache_seconds)
    if cached:
        return cached
    snap = build_snapshot(cfg, days=cfg.watch_days, grace_minutes=cfg.watch_grace_minutes)
    with state.lock:
        state.snapshot = snap
        state.fetched_at = time.monotonic()
    _notify(cfg, state, snap)
    return snap


def _make_handler(cfg: Config, state: _State, refresh: int, reviews_cache: _ReviewsCache):  # noqa: ANN202
    class Handler(BaseHTTPRequestHandler):
        server_version = "jbs-watch/1.0"

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload: dict[str, Any]) -> None:
            self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _redirect(self, location: str) -> None:
            self.send_response(301)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _saved_count(self, query: dict) -> int | None:
            """从 URL 的 ?saved=N 里取刚保存的天数；没这个参数返回 None（不显示提示条）。"""
            if "saved" not in query:
                return None
            return _ab_int((query.get("saved") or ["0"])[0]) or 0

        def do_GET(self) -> None:  # noqa: N802
            raw_path = self.path.split("?", 1)[0]
            path = raw_path.rstrip("/") or "/"
            query = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
            try:
                if path == "/healthz":
                    snap = _get_snapshot(cfg, state)
                    levels = {
                        b.key: (b.today_status.level if b.today_status else LEVEL_UNKNOWN)
                        for b in snap.boards
                    }
                    healthy = all(lv in HEALTHY_LEVELS for lv in levels.values())
                    self._json(
                        200 if healthy else 503,
                        {
                            "healthy": healthy,
                            "today": snap.today,
                            "tasks": levels,
                            "breaking": [k for k, v in levels.items() if v in BREAKING_LEVELS],
                            "generated_at": snap.generated_at,
                        },
                    )
                    return
                if path in ("/api/status.json", "/status.json"):
                    snap = _get_snapshot(cfg, state)
                    self._send(200, render_json(snap).encode("utf-8"),
                               "application/json; charset=utf-8")
                    return
                # /reviews 系列路由：
                #   /reviews      301 -> /reviews/
                #   /reviews/     列表页（动态扫 data/reviews/*.html）
                #   /reviews/<f>  文件下载（仅 .html / .txt，防 ../ 穿越）
                if raw_path == "/reviews":
                    self._redirect("/reviews/")
                    return
                if raw_path.startswith("/reviews/"):
                    tail = urllib.parse.unquote(raw_path[len("/reviews/"):], encoding="utf-8")
                    if not tail or tail.endswith("/"):
                        rows = reviews_cache.get(cfg)
                        self._send(
                            200,
                            _render_reviews_index(rows).encode("utf-8"),
                            "text/html; charset=utf-8",
                        )
                        return
                    # 海报页：/reviews/<name>.html/poster -> <name>.poster.html
                    if tail.endswith("/poster"):
                        base_name = tail[: -len("/poster")]
                        if base_name.endswith(".html"):
                            poster_name = f"{Path(base_name).stem}.poster.html"
                            found = _resolve_reviews_file(cfg, poster_name)
                            if found and found[0] is not None:
                                file_path, mime = found
                                try:
                                    body = file_path.read_bytes()
                                except OSError as exc:
                                    logger.warning("读 reviews 海报失败 %s：%s", file_path, exc)
                                    self._json(500, {"error": "read failed"})
                                    return
                                self._send(200, body, mime)
                                return
                    found = _resolve_reviews_file(cfg, tail)
                    if not found or found[0] is None:
                        self._json(404, {"error": "not found", "path": raw_path})
                        return
                    file_path, mime = found
                    try:
                        body = file_path.read_bytes()
                    except OSError as exc:
                        logger.warning("读 reviews 文件失败 %s：%s", file_path, exc)
                        self._json(500, {"error": "read failed"})
                        return
                    self._send(200, body, mime)
                    return
                # /weekly 系列路由（结构同 /reviews，数据来自 data/weekly/）
                if raw_path == "/weekly":
                    self._redirect("/weekly/")
                    return
                if raw_path.startswith("/weekly/"):
                    tail = urllib.parse.unquote(raw_path[len("/weekly/"):], encoding="utf-8")
                    if not tail or tail.endswith("/"):
                        rows = _list_weekly(_weekly_dir(cfg))
                        self._send(
                            200,
                            _render_weekly_index(rows).encode("utf-8"),
                            "text/html; charset=utf-8",
                        )
                        return
                    found = _resolve_weekly_file(cfg, tail)
                    if not found or found[0] is None:
                        self._json(404, {"error": "not found", "path": raw_path})
                        return
                    file_path, mime = found
                    try:
                        body = file_path.read_bytes()
                    except OSError as exc:
                        logger.warning("读 weekly 文件失败 %s：%s", file_path, exc)
                        self._json(500, {"error": "read failed"})
                        return
                    self._send(200, body, mime)
                    return
                # /ab 系列：文案 A/B 的数据回填页（写 Supabase，公网入口靠 nginx basic auth 挡）
                if raw_path == "/ab":
                    self._redirect("/ab/")
                    return
                if raw_path.startswith("/ab/"):
                    if raw_path[len("/ab/"):].strip("/"):
                        self._json(404, {"error": "not found", "path": raw_path})
                        return
                    rows, error = _fetch_ab_runs(cfg)
                    failed = None
                    if "fail" in query:
                        failed = _ab_int((query.get("fail") or ["0"])[0]) or 0
                    self._send(
                        200,
                        _render_ab_page(rows, error, self._saved_count(query), failed).encode(
                            "utf-8"
                        ),
                        "text/html; charset=utf-8",
                    )
                    return
                if path == "/":
                    snap = _get_snapshot(cfg, state)
                    self._send(200, render(snap, refresh).encode("utf-8"),
                               "text/html; charset=utf-8")
                    return
                self._json(404, {"error": "not found", "path": path})
            except Exception as exc:  # noqa: BLE001 - 页面宁可显示报错也别断连
                logger.exception("处理请求失败：%s", self.path)
                self._json(500, {"error": str(exc)})

        def do_POST(self) -> None:  # noqa: N802
            """只接 /ab/save：表单回填 → PATCH caption_runs.metrics。

            写完 303 跳回列表页，避免刷新重复提交；Location 用相对路径，
            这样不论直接访问 8787 还是走 nginx 的 /watch/ 前缀都能跳对。
            """
            raw_path = self.path.split("?", 1)[0]
            path = raw_path.rstrip("/") or "/"
            if path != "/ab/save":
                self._json(404, {"error": "not found", "path": path})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length).decode("utf-8", "ignore") if length else ""
                form = urllib.parse.parse_qs(body, keep_blank_values=True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("解析 /ab/save 请求体失败")
                self._json(400, {"error": f"请求体解析失败：{exc}"})
                return

            # 先把当前列表拉一遍：只接受页面上真实存在的日期，
            # 防止手搓请求往任意 board_date 写数据。
            try:
                rows, error = _fetch_ab_runs(cfg)
            except Exception as exc:  # noqa: BLE001
                rows, error = [], str(exc)
            if error:
                self._json(502, {"error": error})
                return
            allowed = {str(r["board_date"]) for r in rows}
            origin: dict[str, dict[str, Any]] = {
                str(r["board_date"]): _ab_metrics_of(r) for r in rows
            }

            # 表单字段名形如 "views:2026-09-16" / "note:2026-09-16"
            draft: dict[str, dict[str, Any]] = {d: {} for d in allowed}
            for field, values in form.items():
                if ":" not in field:
                    continue
                key, date = field.split(":", 1)
                if date not in allowed or not values:
                    continue
                raw = (values[0] or "").strip()
                if key != AB_NOTE_KEY and key not in AB_NUM_KEYS:
                    continue
                if key == AB_NOTE_KEY:
                    if raw:
                        draft[date][AB_NOTE_KEY] = raw[:300]
                    continue
                n = _ab_int(raw)
                if n is None:  # 空 / 非数字 = 这一项没填，不写进去
                    continue
                draft[date][key] = n

            saved = 0
            errors: list[str] = []
            for date in sorted(draft, reverse=True):
                new_metrics = draft[date] or None
                if new_metrics == (origin.get(date) or None):
                    continue  # 没改动就别打 Supabase
                ok, err = _save_ab_metrics(cfg, date, new_metrics)
                if ok:
                    saved += 1
                else:
                    errors.append(err)
            if errors:
                logger.warning("回填 caption_runs 部分失败：%s", errors)

            # "./" 让它解析成当前目录（/ab/ 或 /watch/ab/），不要拼绝对路径 ——
            # 大盘可能被挂在任意前缀下（本地 8787、线上 /watch/）。
            if errors:
                self.send_response(303)
                self.send_header("Location", f"./?saved={saved}&fail={len(errors)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(303)
            self.send_header("Location", f"./?saved={saved}")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:  # 静音默认访问日志
            logger.debug("%s - %s", self.address_string(), fmt % args)

    return Handler


def serve(
    cfg: Config,
    host: str | None = None,
    port: int | None = None,
    days: int | None = None,
    refresh: int | None = None,
    open_browser: bool = False,
) -> None:
    host = host or cfg.watch_host
    port = port or cfg.watch_port
    refresh = refresh or cfg.watch_refresh_seconds
    if days:
        cfg.watch_days = days

    state = _State()
    reviews_cache = _ReviewsCache()
    handler = _make_handler(cfg, state, refresh, reviews_cache)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"监听大盘已启动：{url}")
    print(f"  JSON    {url}api/status.json")
    print(f"  评论聚合 {url}reviews/")
    print(f"  热度周报 {url}weekly/")
    print(f"  文案 A/B {url}ab/   （回填发布数据 + 看配方效果）")
    print(f"  海报页   {url}reviews/鬼河怒放.html/poster")
    print(f"  健康位  {url}healthz   （今天出榜成功=200，否则 503）")
    print(f"  窗口 {cfg.watch_days} 天 · 每 {cfg.watch_cache_seconds}s 回源一次 · 页面 {refresh}s 自动刷新")
    print("Ctrl+C 退出")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
