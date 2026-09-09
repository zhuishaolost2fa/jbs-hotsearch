# -*- coding: utf-8 -*-
"""数据源契约：新增一个源 = 实现 Source.fetch()，再在 registry 里注册。

约定：
  - fetch() **不允许抛异常出去**，失败一律用 SourceResult(ok=False, error=...) 表达；
  - 每个源自行把平台信号压成 0~1 的 value（rank.py 只做跨源加权，不理解业务字段）；
  - 拿不到数据就返回 ok=False，**绝不返回写死的榜单**。
"""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass

from ..config import Config
from ..models import ScriptCandidate, SourceResult


@dataclass
class Source(abc.ABC):
    cfg: Config
    name: str = "base"
    weight: float = 1.0

    @abc.abstractmethod
    def _collect(self) -> list[ScriptCandidate]:
        """真正取数；失败请抛异常，由 fetch() 统一收敛。"""
        raise NotImplementedError

    def fetch(self) -> SourceResult:
        started = time.perf_counter()
        try:
            candidates = self._collect()
        except Exception as exc:  # noqa: BLE001 - 单源失败不能拖垮整轮
            return SourceResult(
                source=self.name,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        return SourceResult(
            source=self.name,
            ok=True,
            candidates=candidates,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


def normalize_values(cands: list[ScriptCandidate], key=lambda c: c.signals["heat"]) -> None:
    """把 signals 里的原始热度线性压到 0~1（原地修改 value）。

    用 max 归一而非 rank，保留「断层级热度」的差异。
    """
    values = []
    for c in cands:
        try:
            values.append(float(key(c) or 0.0))
        except (KeyError, TypeError, ValueError):
            values.append(0.0)
    top = max(values) if values else 0.0
    if top <= 0:
        for c in cands:
            c.value = 0.0
        return
    for c, v in zip(cands, values):
        c.value = max(0.0, min(1.0, v / top))
