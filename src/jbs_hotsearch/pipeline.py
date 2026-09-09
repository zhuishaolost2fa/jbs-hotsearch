# -*- coding: utf-8 -*-
"""每日任务编排：抓源 -> 融合打分 -> 落库 -> 出报告 -> 记日志。

口径约定（重要）：
  - **至少有一个源成功才产出榜单**；全源失败 -> 抛错，不写老数据充数；
  - 单源失败不影响本轮，会在 board.source_results 与运行日志里显式记录；
  - 落库失败会降级本地，保证当天的榜不丢。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from .config import Config
from .filter_parsed import ParsedFilter
from .models import DailyBoard, RankedScript, SourceResult
from .rank import rank_fuse
from .report import render_markdown, write_report
from .site import write_site
from .sources import enabled_sources
from .store import board_status, resolve_store_pair

logger = logging.getLogger(__name__)


def _reason(item: RankedScript) -> str:
    """把来源信号翻译成一句话上榜理由（尽量用真实字段，不发挥）。"""
    parts: list[str] = []
    grp = item.source_detail.get("miquan_group")
    if grp:
        shops = grp.get("shop_count")  # 唯一店数
        groups = grp.get("group_count")  # 组局数（店+本+时刻去重）
        days = grp.get("window_days")
        span = f"近 {int(days)} 天" if days else "近期"
        if shops:
            if groups and groups > shops:
                parts.append(f"{span} {shops} 家店开 {groups} 场组局")
            else:
                parts.append(f"{span} {shops} 家店开组局")
    mq = item.source_detail.get("miquan")
    if mq:
        # 剧本榜降为元数据后，评分仅作质量参考，不再把「平台热度」当作上榜理由
        score = mq.get("score")
        if score:
            parts.append(f"评分 {score}")
    web = item.source_detail.get("search_llm")
    if web:
        count = web.get("evidence_count")
        why = (web.get("why_hot") or "").strip()
        if count:
            parts.append(f"检索 {count} 处提及" + (f"（{why}）" if why else ""))
        elif why:
            parts.append(why)
    # 交叉验证只对「多个热度源」有意义；剧本榜降元数据后 weight=0，不算热度源
    heat_sources = [s for s, d in item.source_detail.items() if d.get("weight", 0) > 0]
    if len(heat_sources) > 1:
        parts.append(f"{len(heat_sources)} 个热度源交叉验证")
    return "；".join(parts) if parts else "综合热度领先"


def collect(cfg: Config) -> list[SourceResult]:
    sources = enabled_sources(cfg)
    if not sources:
        raise RuntimeError(
            "没有任何可用数据源：把 HS_SOURCE_MIQUAN_ENABLED 打开或配置 HS_SEARCH_PROVIDER"
        )
    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        return list(pool.map(lambda s: s.fetch(), sources))


def build_board(cfg: Config, results: list[SourceResult], board_date: date, elapsed_ms: int) -> DailyBoard:
    ok_results = [r for r in results if r.ok]
    if not ok_results:
        detail = "；".join(f"{r.source}: {r.error}" for r in results)
        raise RuntimeError(f"全部数据源失败 -> {detail}")

    candidates = [c for r in ok_results for c in r.candidates]
    primary, _fallback = resolve_store_pair(cfg)
    try:
        prev_ranks = primary.prev_ranks(board_date)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取上一期排名失败，本期全部按新上榜处理：%s", exc)
        prev_ranks = {}

    # 先按 top_n × buffer 抓更多候选，过滤「已解析」后再截到 top_n，保证有足够补位。
    buffer = max(1, cfg.filter_buffer_multiplier) if cfg.filter_parsed_enabled else 1
    ranked = rank_fuse(
        candidates, cfg, today=board_date, prev_ranks=prev_ranks, limit=cfg.top_n * buffer
    )

    parsed_filter = ParsedFilter.load(cfg)
    kept, filtered = parsed_filter.apply(ranked)
    items = kept[: cfg.top_n]

    # 过滤后重排 rank（prev_rank / is_new 已在 rank_fuse 里算好，保持不动）
    for idx, item in enumerate(items, start=1):
        item.rank = idx

    for item in items:
        item.reason = _reason(item)
    return DailyBoard(
        board_date=board_date,
        items=items,
        source_results=results,
        elapsed_ms=elapsed_ms,
        filtered_items=filtered,
        filter_note=parsed_filter.error or "",
    )


def run_once(cfg: Config, board_date: date | None = None) -> DailyBoard:
    board_date = board_date or date.today()
    started = time.perf_counter()
    logger.info("开始生成 %s 的剧本杀热门榜", board_date.isoformat())

    results = collect(cfg)
    board = build_board(cfg, results, board_date, int((time.perf_counter() - started) * 1000))

    primary, fallback = resolve_store_pair(cfg)
    error: str | None = None
    active = primary
    try:
        primary.save_board(board)
        logger.info("榜单已写入 %s（%d 条）", primary.name, len(board.items))
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
        if fallback is None or type(fallback) is type(primary):
            raise
        logger.error("主存储 %s 写入失败，降级本地：%s", primary.name, exc)
        fallback.save_board(board)
        active = fallback

    write_report(cfg, board)
    # 榜单展示页（nginx 静态托管 data/site/index.html）
    write_site(cfg, board)
    # 运行日志必须写进真正落地的那个存储，否则「今天到底有没有成功」查不到
    active.save_run(board, board_status(board, error, active.name), error)
    logger.info("完成，用时 %dms", board.elapsed_ms)
    return board


def preview(cfg: Config, board_date: date | None = None) -> str:
    """只算不写的预览（docker / 调试用）。"""
    board_date = board_date or date.today()
    started = time.perf_counter()
    results = collect(cfg)
    board = build_board(cfg, results, board_date, int((time.perf_counter() - started) * 1000))
    return render_markdown(board)
