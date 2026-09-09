# -*- coding: utf-8 -*-
"""存储层接口。

Store 只做三件事：读上一期排名（用于算涨跌）、写本期榜单、写运行日志。
两种实现：Supabase PostgREST（主）与本地 SQLite+JSON（降级 / 离线）。
"""
from __future__ import annotations

import abc
from datetime import date

from ..models import DailyBoard


class StoreError(RuntimeError):
    """存储层不可恢复的错误（表不存在、凭证无效等）。"""


class SchemaMissing(StoreError):
    """表还没建 —— 需要人工去 Supabase SQL Editor 执行 sql/hot_scripts.sql。"""


class Store(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def prev_ranks(self, board_date: date) -> dict[str, int]:
        """返回 board_date 之前最近一期的 {title_key: rank}。"""
        raise NotImplementedError

    @abc.abstractmethod
    def save_board(self, board: DailyBoard) -> None:
        """写入本期榜单（同一天重跑需幂等覆盖）。"""
        raise NotImplementedError

    @abc.abstractmethod
    def save_run(self, board: DailyBoard, status: str, error: str | None = None) -> None:
        raise NotImplementedError

    def check(self) -> None:
        """doctor 自检：不可用时抛 StoreError。默认实现：查一次表。"""
        self.prev_ranks(date.today())
