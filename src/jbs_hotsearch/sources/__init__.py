# -*- coding: utf-8 -*-
"""数据源注册表：pipeline 只依赖 enabled_sources()，新增源改这里一处。"""
from __future__ import annotations

from ..config import Config
from .base import Source
from .miquan import MiquanSource
from .search_llm import SearchLLMSource

__all__ = ["Source", "MiquanSource", "SearchLLMSource", "enabled_sources"]


def enabled_sources(cfg: Config) -> list[Source]:
    sources: list[Source] = []
    if cfg.miquan_enabled:
        sources.append(MiquanSource(cfg))
    if cfg.search_provider not in ("", "none"):
        sources.append(SearchLLMSource(cfg))
    return sources
