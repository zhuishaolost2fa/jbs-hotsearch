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


def next_weekly_run(cfg: Config, now: datetime | None = None) -> datetime:
    """下一次周报时间：周 cfg.weekly_day（1=周一 … 5=周五）的 cfg.weekly_at。

    过了本周这个点就顺延一周 —— 所以每次跑完自动跳到下周，调用方不用记账。
    """
    hour, minute = parse_run_at(cfg.weekly_at)
    tz = get_tz(cfg.timezone, cfg.tz_fallback_offset)
    now = now or datetime.now(tz)
    target_wd = max(0, min(6, cfg.weekly_day - 1))  # 1=周一 -> weekday()=0
    delta = (target_wd - now.weekday()) % 7
    day = now.date() + timedelta(days=delta)
    target = datetime.combine(day, datetime.min.time()).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=tz
    )
    if target <= now:
        target += timedelta(days=7)
    return target


def serve(cfg: Config, task, weekly_task=None, weekly_due=None) -> None:
    """常驻循环：到点执行一次 task()，出错按天重试不影响后续。

    weekly_task：周报任务（可选）。传了就一起排进时间表，取「更早到点的那个」执行
    —— 不另外起线程，保持单进程单线程天然互斥。
    weekly_due：callable(cfg, now) -> bool，启动时判断「本周期周报是不是该补跑」
    （比如容器重启错过了周五那次）。不传就不补跑，缺了会在监听大盘上显示成红色。
    """
    _install_stop_handlers()
    tz = get_tz(cfg.timezone, cfg.tz_fallback_offset)
    weekly_on = bool(cfg.weekly_enabled and weekly_task)

    today = datetime.now(tz).date()
    if cfg.run_on_start:
        logger.info("启动时补跑一次当天榜单")
        try:
            task(cfg, today)
        except Exception:  # noqa: BLE001
            logger.exception("启动补跑失败")

    if weekly_on and weekly_due:
        try:
            if weekly_due(cfg, datetime.now(tz)):
                logger.info("本周期周报缺失，启动补跑一次")
                weekly_task(cfg)
        except Exception:  # noqa: BLE001
            logger.exception("周报启动补跑失败")

    while _RUNNING:
        events: list[tuple[str, datetime]] = [("daily", next_run(cfg))]
        if weekly_on:
            events.append(("weekly", next_weekly_run(cfg)))
        kind, target = min(events, key=lambda e: e[1])
        logger.info(
            "下一次执行：%s %s", kind, target.strftime("%Y-%m-%d %H:%M:%S %Z")
        )
        while _RUNNING and datetime.now(tz) < target:
            time.sleep(min(30, max(1, (target - datetime.now(tz)).total_seconds())))
        if not _RUNNING:
            break
        try:
            if kind == "weekly":
                weekly_task(cfg)
            else:
                task(cfg, datetime.now(tz).date())
        except Exception:  # noqa: BLE001
            logger.exception("本轮执行失败（%s）", kind)
