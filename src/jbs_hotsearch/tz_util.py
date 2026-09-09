# -*- coding: utf-8 -*-
"""时区获取：Windows 上没有系统 tz database，zoneinfo 会直接抛 ZoneInfoNotFoundError。

策略：优先 ZoneInfo（装了 tzdata 就能用）→ 兜底固定偏移（默认 UTC+8）。
避免「明明只差一个时区，服务却在启动时崩掉」。
"""
from __future__ import annotations

import logging
from datetime import timedelta, timezone, tzinfo

logger = logging.getLogger(__name__)

DEFAULT_FALLBACK_OFFSET_HOURS = 8


def get_tz(name: str, fallback_offset_hours: float = DEFAULT_FALLBACK_OFFSET_HOURS) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception as exc:  # noqa: BLE001 - 缺 tzdata / 名字非法都归到这里
        offset = timezone(timedelta(hours=fallback_offset_hours))
        logger.warning(
            "时区 %s 不可用（%s: %s），回退到固定偏移 UTC%+g",
            name,
            type(exc).__name__,
            exc,
            fallback_offset_hours,
        )
        return offset
