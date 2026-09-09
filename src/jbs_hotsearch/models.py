# -*- coding: utf-8 -*-
"""数据模型：源产出 -> 融合排序 -> 榜单。

数据流向：
    Source.fetch() -> [ScriptCandidate]  （每个源只负责把自己平台的信号翻译成 0~1 的 value）
    rank_fuse()    -> [RankedScript]     （跨源融合、去重、打分）
    DailyBoard     -> Store.save()       （落库产物）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


def _num(value: Any) -> float | None:
    """把 5.9 / "5.9" / None 统一收敛成 float | None，脏数据不抛异常。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass
class ScriptCandidate:
    """一个数据源给出的单个剧本热度样本。"""

    title: str
    source: str
    # 源内相对热度 0~1（越大越热），由 source 自己按平台信号归一化
    value: float = 0.0
    # 源权重（config 里配，可在 source 构造时覆盖）
    weight: float = 1.0
    tags: list[str] = field(default_factory=list)
    players: str | None = None
    duration: str | None = None
    rating: float | None = None
    # 平台原生信号，保留原值用于解释「为什么上榜」
    signals: dict[str, Any] = field(default_factory=dict)
    url: str | None = None

    def __post_init__(self) -> None:
        self.title = (self.title or "").strip()
        self.rating = _num(self.rating)
        self.value = max(0.0, min(1.0, _num(self.value) or 0.0))


@dataclass
class RankedScript:
    """融合排序后的榜单条目。"""

    rank: int
    title: str
    title_key: str
    hot_score: float  # 0~100
    sources: list[str]
    source_detail: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    players: str | None = None
    duration: str | None = None
    rating: float | None = None
    url: str | None = None
    reason: str = ""
    # 与上一期对比
    prev_rank: int | None = None
    is_new: bool = False

    @property
    def rank_change(self) -> int | None:
        """正数=名次上升。"""
        if self.prev_rank is None:
            return None
        return self.prev_rank - self.rank

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "title": self.title,
            "title_key": self.title_key,
            "hot_score": round(self.hot_score, 2),
            "sources": self.sources,
            "source_detail": self.source_detail,
            "tags": self.tags,
            "players": self.players,
            "duration": self.duration,
            "rating": self.rating,
            "url": self.url,
            "reason": self.reason,
            "prev_rank": self.prev_rank,
            "rank_change": self.rank_change,
            "is_new": self.is_new,
        }


@dataclass
class SourceResult:
    """单次抓取结果（含失败原因，便于执行面明示降级而不静默造数）。"""

    source: str
    ok: bool
    candidates: list[ScriptCandidate] = field(default_factory=list)
    error: str | None = None
    note: str | None = None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "items": len(self.candidates),
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "note": self.note,
        }


@dataclass
class DailyBoard:
    board_date: date
    items: list[RankedScript]
    source_results: list[SourceResult] = field(default_factory=list)
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "board_date": self.board_date.isoformat(),
            "generated_at": None,  # 由 store 填充
            "elapsed_ms": self.elapsed_ms,
            "sources": [r.to_dict() for r in self.source_results],
            "items": [i.to_dict() for i in self.items],
        }
