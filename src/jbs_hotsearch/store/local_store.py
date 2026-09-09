# -*- coding: utf-8 -*-
"""本地降级存储：SQLite（历史 + 涨跌）+ JSON 快照（给人看 / 给别的程序读）。

Supabase 不可用（没配、表没建、网络断）时用它，保证「每天那份榜单」不会丢。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from ..models import DailyBoard
from .base import Store

SCHEMA = """
create table if not exists script_hot_daily (
    board_date  text not null,
    rank        int  not null,
    title       text not null,
    title_key   text not null,
    hot_score   real not null,
    prev_rank   int,
    is_new      int  not null default 0,
    payload     text not null,
    primary key (board_date, title_key)
);
create table if not exists script_hot_runs (
    id          integer primary key autoincrement,
    board_date  text not null,
    started_at  text not null default (datetime('now')),
    status      text not null,
    duration_ms int,
    item_count  int,
    error       text,
    payload     text
);
create index if not exists idx_hot_daily_date on script_hot_daily (board_date desc);
"""


class LocalStore(Store):
    name = "local"

    def __init__(self, cfg) -> None:
        self.dir = Path(cfg.data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.dir / "hotsearch.db"
        self.snapshot_dir = self.dir / "snapshots"
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA)

    # ---------------- Store 接口 ----------------
    def prev_ranks(self, board_date: date) -> dict[str, int]:
        day = board_date.isoformat()
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "select board_date from script_hot_daily where board_date < ? order by board_date desc limit 1",
                (day,),
            ).fetchone()
            if not row:
                return {}
            rows = conn.execute(
                "select title_key, rank from script_hot_daily where board_date = ?", (row[0],)
            ).fetchall()
        return {k: r for k, r in rows}

    def save_board(self, board: DailyBoard) -> None:
        day = board.board_date.isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("delete from script_hot_daily where board_date = ?", (day,))
            conn.executemany(
                "insert into script_hot_daily (board_date, rank, title, title_key, hot_score, prev_rank, is_new, payload)"
                " values (?,?,?,?,?,?,?,?)",
                [
                    (
                        day,
                        item.rank,
                        item.title,
                        item.title_key,
                        round(item.hot_score, 2),
                        item.prev_rank,
                        int(item.is_new),
                        json.dumps(item.to_dict(), ensure_ascii=False),
                    )
                    for item in board.items
                ],
            )
        snapshot = self.snapshot_dir / f"{day}.json"
        snapshot.write_text(json.dumps(board.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    def save_run(self, board: DailyBoard, status: str, error: str | None = None) -> None:
        day = board.board_date.isoformat()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "insert into script_hot_runs (board_date, status, duration_ms, item_count, error, payload)"
                " values (?,?,?,?,?,?)",
                (
                    day,
                    status,
                    board.elapsed_ms,
                    len(board.items),
                    error,
                    json.dumps([r.to_dict() for r in board.source_results], ensure_ascii=False),
                ),
            )

    def latest(self, board_date: date | None = None) -> dict | None:
        """读最近一期快照（给 inspect 命令用）。"""
        files = sorted(self.snapshot_dir.glob("*.json"), reverse=True)
        for path in files:
            if board_date and path.stem != board_date.isoformat():
                continue
            return json.loads(path.read_text(encoding="utf-8"))
        return None
