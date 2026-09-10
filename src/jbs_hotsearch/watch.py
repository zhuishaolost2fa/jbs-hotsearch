# -*- coding: utf-8 -*-
"""监听大盘的 Web 服务：`python -m jbs_hotsearch watch`。

同样只用标准库 —— 监控自己不该有依赖风险。
路由：
    GET /               大盘 HTML（服务端渲染，禁 JS 也能看）
    GET /api/status.json 同一份数据的 JSON（给脚本 / 别的面板接）
    GET /healthz        今天出榜成功 → 200，否则 503（可直接拿去做容器健康检查）
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


def _make_handler(cfg: Config, state: _State, refresh: int):  # noqa: ANN202
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

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
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
    handler = _make_handler(cfg, state, refresh)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"监听大盘已启动：{url}")
    print(f"  JSON    {url}api/status.json")
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
