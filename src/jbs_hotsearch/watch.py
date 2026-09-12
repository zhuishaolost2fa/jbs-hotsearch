# -*- coding: utf-8 -*-
"""监听大盘的 Web 服务：`python -m jbs_hotsearch watch`。

同样只用标准库 —— 监控自己不该有依赖风险。
路由：
    GET /                       大盘 HTML（服务端渲染，禁 JS 也能看）
    GET /api/status.json        同一份数据的 JSON（给脚本 / 别的面板接）
    GET /healthz                今天出榜成功 → 200，否则 503（可直接拿去做容器健康检查）
    GET /reviews/               评论聚合列表页（from data/reviews/*.html）
    GET /reviews/<file>         评论聚合某本的页面或配套文案（鬼河怒放.html / .txt）
    GET /reviews/<file>/poster  某本的海报截图页（鬼河怒放.html/poster -> 鬼河怒放.poster.html）
"""
from __future__ import annotations

import html
import json
import logging
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
import re as _re

def _reviews_dir(cfg: Config) -> Path:
    return cfg.data_dir / "reviews"


def _list_reviews(reviews_dir: Path) -> list[dict[str, Any]]:
    """扫 reviews 目录：返回每本（*.html 一项，附同名 *.txt 文案内容）。"""
    out: list[dict[str, Any]] = []
    if not reviews_dir.is_dir():
        return out
    for html_path in sorted(reviews_dir.glob("*.html")):
        if html_path.name.endswith(".poster.html"):
            continue  # 海报页是详情页的附属，不在列表里单独展示
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

    只允许 .html / .txt；解析后的绝对路径必须仍在 reviews 目录下（防 ../ 越权）。
    """
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None, None
    if not (name.endswith(".html") or name.endswith(".txt")):
        return None, None
    reviews_dir = _reviews_dir(cfg).resolve()
    candidate = (reviews_dir / name).resolve()
    try:
        candidate.relative_to(reviews_dir)
    except ValueError:
        return None, None
    if not candidate.is_file():
        return None, None
    mime = "text/html; charset=utf-8" if name.endswith(".html") else "text/plain; charset=utf-8"
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

        def do_GET(self) -> None:  # noqa: N802
            raw_path = self.path.split("?", 1)[0]
            path = raw_path.rstrip("/") or "/"
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
                if path == "/":
                    snap = _get_snapshot(cfg, state)
                    self._send(200, render(snap, refresh).encode("utf-8"),
                               "text/html; charset=utf-8")
                    return
                self._json(404, {"error": "not found", "path": path})
            except Exception as exc:  # noqa: BLE001 - 页面宁可显示报错也别断连
                logger.exception("处理请求失败：%s", self.path)
                self._json(500, {"error": str(exc)})

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
