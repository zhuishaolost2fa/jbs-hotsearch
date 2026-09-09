# -*- coding: utf-8 -*-
"""内部调度器：每天 HS_RUN_AT 触发一次（纯标准库，不引 APScheduler）。

设计取舍：
  - 不依赖第三方调度库，减少镜像体积与依赖失效风险；
  - 单进程单线程顺序执行，天然互斥（不会出现上一次没跑完就来下一次）；
  - 跨时区用 zoneinfo，容器里也能跑（tzdata 由基础镜像提供）。
"""
from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta

from .config import Config
from .tz_util import get_tz

logger = logging.getLogger(__name__)

_RUNNING = True


def _install_stop_handlers() -> None:
    def handler(signum, frame):  # noqa: ARG001
        global _RUNNING
        _RUNNING = False
        logger.info("收到停止信号，本轮结束后退出")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):  # 非主线程 / Windows 限制
            pass


def parse_run_at(run_at: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = run_at.split(":")
        hour, minute = int(hour_text), int(minute_text)
    except ValueError as exc:
        raise ValueError(f"HS_RUN_AT 格式应为 HH:MM，当前是 {run_at!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"HS_RUN_AT 越界：{run_at}")
    return hour, minute


def next_run(cfg: Config, now: datetime | None = None) -> datetime:
    hour, minute = parse_run_at(cfg.run_at)
    tz = get_tz(cfg.timezone, cfg.tz_fallback_offset)
    now = now or datetime.now(tz)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


def serve(cfg: Config, task) -> None:
    """常驻循环：到点执行一次 task()，出错按天重试不影响后续。"""
    _install_stop_handlers()
    tz = get_tz(cfg.timezone, cfg.tz_fallback_offset)

    today = datetime.now(tz).date()
    if cfg.run_on_start:
        logger.info("启动时补跑一次当天榜单")
        try:
            task(cfg, today)
        except Exception:  # noqa: BLE001
            logger.exception("启动补跑失败")

    while _RUNNING:
        target = next_run(cfg)
        logger.info("下一次执行时间：%s", target.strftime("%Y-%m-%d %H:%M:%S %Z"))
        while _RUNNING and datetime.now(tz) < target:
            time.sleep(min(30, max(1, (target - datetime.now(tz)).total_seconds())))
        if not _RUNNING:
            break
        try:
            task(cfg, datetime.now(tz).date())
        except Exception:  # noqa: BLE001
            logger.exception("本轮执行失败")
