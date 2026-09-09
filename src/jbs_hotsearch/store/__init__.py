# -*- coding: utf-8 -*-
"""存储工厂：按 HS_STORE_BACKEND 选实现，auto = Supabase 优先、失败降级本地。"""
from __future__ import annotations

import logging

from ..config import Config
from ..models import DailyBoard
from .base import Store
from .local_store import LocalStore
from .supabase_store import SupabaseStore

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_TAG = "local"


def build_store(cfg: Config) -> Store:
    backend = (cfg.store_backend or "auto").lower()
    if backend == "local":
        return LocalStore(cfg)
    try:
        return SupabaseStore(cfg)
    except Exception as exc:  # noqa: BLE001
        if backend == "supabase":
            raise
        logger.warning("Supabase 初始化失败，降级本地存储：%s", exc)
        return LocalStore(cfg)


def resolve_store_pair(cfg: Config):
    """返回 (primary_store, fallback_store_or_None)。

    auto 模式下提供兜底：先算排名再写，写失败时用本地补一份，避免当天榜单丢失。
    """
    backend = (cfg.store_backend or "auto").lower()
    if backend == "supabase":
        return SupabaseStore(cfg), None
    if backend == "local":
        return LocalStore(cfg), None
    try:
        return SupabaseStore(cfg), LocalStore(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Supabase 不可用，仅写本地：%s", exc)
        return LocalStore(cfg), None


def board_status(board: DailyBoard, error: str | None = None, store_name: str = "") -> str:
    """出榜状态。区分「榜单没算出来」和「榜单算出来了但没写进 Supabase」。"""
    failed_sources = [r for r in board.source_results if not r.ok]
    if error:
        return "stored_locally" if store_name == "local" else "failed"
    if failed_sources:
        return "partial" if len(failed_sources) < len(board.source_results) else "failed"
    return "success"
