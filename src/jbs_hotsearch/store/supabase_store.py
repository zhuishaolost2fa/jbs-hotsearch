# -*- coding: utf-8 -*-
"""Supabase PostgREST 写入版存储。

幂等策略：**先删当天旧行再批量插入**（而不是 upsert）。
理由：删除当日不同 title_key 的旧数据、避免 rank 唯一键冲突，同时让重跑完全可预期；
写之前会先从已有表读上一期（board_date 严格小于今天）算涨跌。
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any

import httpx

from ..models import DailyBoard
from .base import SchemaMissing, Store, StoreError

logger = logging.getLogger(__name__)

TABLE = "script_hot_daily"
RUN_TABLE = "script_hot_runs"


class SupabaseStore(Store):
    name = "supabase"

    def __init__(self, cfg) -> None:
        if not (cfg.supabase_url and cfg.supabase_service_role_key):
            raise StoreError("缺 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
        self.cfg = cfg
        self.base = f"{cfg.supabase_url.rstrip('/')}/rest/v1"
        self.headers = {
            "apikey": cfg.supabase_service_role_key,
            "Authorization": f"Bearer {cfg.supabase_service_role_key}",
            "Content-Type": "application/json",
        }

    # ---------------- 内部工具 ----------------
    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.cfg.http_timeout, headers=self.headers)

    def _raise_for_postgrest(self, resp: httpx.Response) -> None:
        """把 PostgREST 的报错翻译成人话，尤其是「表不存在」。"""
        if resp.status_code < 400:
            return
        body = resp.text[:300]
        if "PGRST205" in body or "does not exist" in body:
            raise SchemaMissing(
                "表不存在：Supabase 里还没建 public.script_hot_daily。"
                "去 Dashboard -> SQL Editor 执行一次 sql/hot_scripts.sql"
            )
        if resp.status_code in (401, 403):
            raise StoreError(f"Supabase 凭证无权访问（{resp.status_code}）：检查 SERVICE_ROLE_KEY")
        raise StoreError(f"Supabase 请求失败 {resp.status_code}: {body}")

    # ---------------- Store 接口 ----------------
    def prev_ranks(self, board_date: date) -> dict[str, int]:
        params = {
            "select": "board_date,title_key,rank",
            "board_date": f"lt.{board_date.isoformat()}",
            "order": "board_date.desc",
            "limit": "200",
        }
        with self._client() as client:
            resp = client.get(f"{self.base}/{TABLE}", params=params)
        self._raise_for_postgrest(resp)
        rows = resp.json()
        if not rows:
            return {}
        latest = max(r["board_date"] for r in rows)
        return {r["title_key"]: int(r["rank"]) for r in rows if r["board_date"] == latest}

    def save_board(self, board: DailyBoard) -> None:
        day = board.board_date.isoformat()
        with self._client() as client:
            # 1) 清掉当天旧数据，保证重跑幂等
            resp = client.delete(f"{self.base}/{TABLE}", params={"board_date": f"eq.{day}"})
            self._raise_for_postgrest(resp)

            # 2) 批量插入
            rows = [self._row(day, item) for item in board.items]
            if rows:
                resp = client.post(
                    f"{self.base}/{TABLE}",
                    headers={"Prefer": "return=minimal"},
                    json=rows,
                )
                self._raise_for_postgrest(resp)

    def save_run(self, board: DailyBoard, status: str, error: str | None = None) -> None:
        payload: dict[str, Any] = {
            "board_date": board.board_date.isoformat(),
            "finished_at": "now()",
            "status": status,
            "duration_ms": board.elapsed_ms,
            "item_count": len(board.items),
            "source_status": [r.to_dict() for r in board.source_results],
            "store": self.name,
            "error": error,
        }
        try:
            with self._client() as client:
                resp = client.post(
                    f"{self.base}/{RUN_TABLE}",
                    headers={"Prefer": "return=minimal"},
                    json=payload,
                )
                self._raise_for_postgrest(resp)
        except StoreError as exc:
            # 运行日志写不进去不能让整轮失败
            logger.warning("运行日志写入 Supabase 失败：%s", exc)

    @staticmethod
    def _row(day: str, item) -> dict[str, Any]:
        return {
            "board_date": day,
            "rank": item.rank,
            "title": item.title,
            "title_key": item.title_key,
            "hot_score": round(item.hot_score, 2),
            "prev_rank": item.prev_rank,
            "is_new": item.is_new,
            "tags": item.tags or None,
            "players": item.players,
            "duration": item.duration,
            "rating": item.rating,
            "cover_url": item.url,
            "reason": item.reason,
            "sources": item.sources,
            "source_detail": item.source_detail,
        }
