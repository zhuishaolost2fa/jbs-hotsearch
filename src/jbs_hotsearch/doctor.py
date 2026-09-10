# -*- coding: utf-8 -*-
"""启动自检：把「为什么跑不出榜单」提前暴露，而不是等第二天任务悄悄失败。"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config
from .scheduler import parse_run_at
from .sources.miquan import MiquanSource
from .store.supabase_store import SupabaseStore

logger = logging.getLogger(__name__)


def run(cfg: Config) -> int:
    """返回 0 = 全绿；1 = 有 blocking 问题。"""
    ok = True
    print("== 配置 ==")
    print(f"  每天 {cfg.run_at} ({cfg.timezone}) ｜ Top {cfg.top_n} ｜ 存储 {cfg.store_backend}")
    try:
        parse_run_at(cfg.run_at)
    except ValueError as exc:
        print(f"  ❌ {exc}")
        ok = False

    print("== 数据源 ==")
    if cfg.miquan_enabled:
        path = Path(cfg.miquan_curls_file)
        if not path.is_file():
            print(f"  ❌ 米圈抓包文件不存在：{path}")
            ok = False
        else:
            from .sources.miquan import parse_curl

            lines = [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if parse_curl(line)
            ]
            print(f"  ✅ 米圈：{path.name} 共 {len(lines)} 页可用")
            if lines:
                src = MiquanSource(cfg)
                result = src.fetch()
                print(f"     {'✅' if result.ok else '❌'} 实拉验证：{len(result.candidates)} 本 {result.error or ''}")
                ok = ok and result.ok
    else:
        print("  ⚠️  米圈源已关闭")

    if cfg.search_provider in ("", "none"):
        print("  ⚠️  搜索源未启用（HS_SEARCH_PROVIDER=none），榜单只依赖米圈")
    elif not cfg.search_api_key or not cfg.llm_api_key:
        print(f"  ❌ 搜索源 {cfg.search_provider} 缺 HS_SEARCH_API_KEY / HS_LLM_API_KEY")
        ok = False
    else:
        print(f"  ✅ 搜索源 {cfg.search_provider} + LLM {cfg.llm_model}")

    print("== 存储 ==")
    if cfg.store_backend == "local":
        print(f"  本地模式：{cfg.data_dir}")
    else:
        try:
            store = SupabaseStore(cfg)
            store.check()
            prev = store.prev_ranks(__import__("datetime").date.today())
            print(f"  ✅ Supabase 可访问，上一期榜单 {len(prev)} 条")
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ Supabase 不可用：{exc}")
            if cfg.store_backend == "supabase":
                ok = False
            else:
                print("     auto 模式会降级到本地 SQLite，不阻塞日常出榜")
    return 0 if ok else 1
