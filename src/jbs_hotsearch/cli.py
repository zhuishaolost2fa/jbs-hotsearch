# -*- coding: utf-8 -*-
"""命令行入口。

    python -m jbs_hotsearch once      # 立刻生成当天榜单（写库 + 出报告）
    python -m jbs_hotsearch preview   # 只算不写，打印报告
    python -m jbs_hotsearch serve     # 常驻，每天 HS_RUN_AT 触发
    python -m jbs_hotsearch doctor    # 环境与权限自检
    python -m jbs_hotsearch board     # 看最近一期本地快照
    python -m jbs_hotsearch social    # 出榜并生成小红书素材（截图 + 文案）
    python -m jbs_hotsearch reviews   # 从 HAR 评测聚合出店家榜 + 小红书素材
    python -m jbs_hotsearch status    # 终端里看最近 N 天跑没跑成
    python -m jbs_hotsearch watch     # 起监听大盘（Web，含 /healthz 健康检查位）
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from .config import Config
from .scheduler import serve

# doctor / pipeline / store 会牵出数据源（httpx）。这里故意延迟导入：
# 「今天为什么没出榜」恰恰是最可能在依赖缺失 / 环境损坏时发生的场景，
# status / watch 必须能在装不上依赖的机器上直接跑起来。


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _task(cfg: Config, board_date: date) -> object:
    from .pipeline import run_once

    return run_once(cfg, board_date)


# ---------------------------------------------------------------- 监听大盘
def _print_status(cfg: Config, days: int, as_json: bool = False) -> int:
    """终端版大盘：不启服务，直接把结论打出来（适合 SSH / crontab 邮件）。"""
    from .dashboard import LEVEL_LABEL, render_json
    from .watchdog import build_snapshot

    snap = build_snapshot(cfg, days=days, grace_minutes=cfg.watch_grace_minutes)
    if as_json:
        print(render_json(snap))
        return 1 if snap.alerts or any(b.asset_alerts for b in snap.boards) else 0

    mark = {"ok": "✔", "warn": "!", "bad": "✖", "missing": "·", "pending": "…",
            "idle": "–", "running": "▶", "stuck": "⏱", "unknown": "?"}
    print(f"jbs 每日任务 · 近 {days} 天 · 更新于 {snap.generated_at}")
    if snap.error:
        print(f"  !! {snap.error}")

    alerted = False
    for board in snap.boards:
        print(f"\n── {board.name}（{'每天' if board.kind == 'daily' else ('按需' if board.kind == 'ondemand' else '每次部署')}）")
        if board.error:
            print(f"   !! {board.error}")

        if board.kind == "asset":
            for a in board.assets:
                print(
                    f"   {mark.get(a.level, '?')} {a.path:<16}"
                    f"{LEVEL_LABEL.get(a.level, a.level)}"
                    f"  http={a.http_status or '-':<4} {a.note}"
                )
            if board.asset_alerts:
                alerted = True
            continue

        print(f"{'日期':<12}{'结论':<9}{'任务':>4}{'产出':>6}{'耗时':>9}  说明")
        for item in reversed(board.items):
            label = f"{mark.get(item.level, '?')} {LEVEL_LABEL.get(item.level, item.level)}"
            srcs = " ".join(f"{s.get('source')}{'' if s.get('ok') else '✖'}" for s in item.sources)
            note = srcs or item.detail or (item.error or "")
            print(
                f"{item.date:<12}{label:<9}{item.runs or '-':>4}"
                f"{item.item_count if item.item_count is not None else '-':>6}"
                f"{(str(round(item.duration_ms / 1000, 1)) + 's') if item.duration_ms else '-':>9}  {note[:60]}"
            )
            if item.error:
                print(f"             └─ {item.error[:110]}")
        print(
            f"   成功率 {round(board.success_rate * 100)}% "
            f"({board.success_days}/{len(board.tracked)}) · 连续 {board.streak} 天"
        )
        if board.alerts:
            alerted = True

    if alerted:
        names = [b.name for b in snap.boards if b.alerts or b.asset_alerts]
        print(f"\n需要关注：{'、'.join(names)}")
        return 1
    return 0


def _run_watch(cfg: Config, args: argparse.Namespace) -> int:
    """起 Web 大盘；带 --emit 时只生成一份静态 HTML 就退出。"""
    from .dashboard import render, render_json
    from .watch import serve as serve_watch
    from .watchdog import build_snapshot

    if args.days:
        cfg.watch_days = args.days
    if args.refresh:
        cfg.watch_refresh_seconds = args.refresh

    if args.emit:
        snap = build_snapshot(cfg, days=cfg.watch_days, grace_minutes=cfg.watch_grace_minutes)
        target = Path(args.emit)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = render_json(snap) if args.json else render(snap, cfg.watch_refresh_seconds)
        target.write_text(payload, encoding="utf-8")
        print(f"已生成：{target}")
        if snap.alerts:
            print(f"注意：窗口内有 {len(snap.alerts)} 天需要关注")
        return 0

    serve_watch(
        cfg,
        host=args.host,
        port=args.port,
        days=args.days,
        refresh=args.refresh,
        open_browser=not args.no_open,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jbs-hotsearch", description="剧本杀每日热门榜 Top10")
    parser.add_argument(
        "command",
        choices=["once", "serve", "preview", "doctor", "board", "social", "reviews", "status", "watch"],
    )
    parser.add_argument("--date", help="指定榜单日期 YYYY-MM-DD（默认今天）")
    parser.add_argument("--days", type=int, help="status / watch 的回看天数")
    parser.add_argument("--host", help="watch 监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, help="watch 监听端口（默认 8787）")
    parser.add_argument("--refresh", type=int, help="大盘页面自动刷新秒数（0 = 不自动刷新）")
    parser.add_argument("--emit", help="只渲染一份静态文件到该路径，不启动服务")
    parser.add_argument("--json", action="store_true", help="配合 status / --emit 输出 JSON")
    parser.add_argument("--no-open", action="store_true", help="watch 启动时不自动打开浏览器")
    parser.add_argument("--har", help="reviews 子命令：从米圈导出的 HAR 文件路径")
    parser.add_argument("--script", help="reviews 子命令：剧本名（用作输出文件名与文案标题）")
    parser.add_argument("--min-reviews", type=int, default=2, help="reviews 子命令：上榜门槛，默认 2 条评论")
    args = parser.parse_args(argv)

    cfg = Config.load()
    setup_logging(cfg.log_level)

    board_date = None
    if args.date:
        board_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    if args.command == "doctor":
        from .doctor import run as run_doctor

        return run_doctor(cfg)

    if args.command == "status":
        return _print_status(cfg, days=args.days or cfg.watch_days, as_json=args.json)

    if args.command == "watch":
        return _run_watch(cfg, args)

    if args.command == "board":
        from .store.local_store import LocalStore

        snapshot = LocalStore(cfg).latest(board_date)
        if not snapshot:
            print(
                "本地还没有快照；Supabase 模式下榜单不在本地落乐观鸭，"
                "用 Supabase 后台或直接读 script_hot_latest 视图。"
            )
            return 1
        print(f"# {snapshot['board_date']} 的热门榜（本地快照）")
        for row in snapshot["items"]:
            badge = "🆕" if row.get("is_new") else (f"{row['rank_change']:+d}" if row.get("rank_change") else "—")
            print(
                f"{row['rank']:>2}. {row['title']}  热度 {row['hot_score']}  {badge}  "
                f"{'/'.join(row['sources'])}"
            )
        return 0

    if args.command in ("once", "social"):
        try:
            # 走 _task：内部延迟 import pipeline，避免装不上依赖时连 CLI 都起不来
            board = _task(cfg, board_date)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger("cli").error("出榜失败：%s", exc)
            return 1
        if args.command == "social":
            d = board.board_date.isoformat()
            social_dir = cfg.data_dir / "social"
            print(f"小红书素材已生成（{d}）：")
            for name in (f"{d}.png", f"{d}.txt", "index.html"):
                p = social_dir / name
                print(f"  {'OK' if p.exists() else '缺失'}  {p}")
        return 0 if board.items else 2

    if args.command == "preview":
        from .pipeline import preview

        print(preview(cfg, board_date))
        return 0

    if args.command == "reviews":
        if not args.har or not args.script:
            print("--har 与 --script 必填（reviews 子命令：HAR 路径 + 剧本名）", file=sys.stderr)
            return 2
        from .reviews import write_reviews

        har = Path(args.har)
        if not har.is_file():
            print(f"HAR 文件不存在：{har}", file=sys.stderr)
            return 1
        result = write_reviews(cfg, har, args.script, min_reviews=args.min_reviews)
        print("店家榜已生成：")
        for k in ("script", "reviews_total", "reviews_with_shop", "shops_ranked", "shops_skipped_low_review"):
            print(f"  {k:<25} {result[k]}")
        print(f"  {'page':<25} {result['page']}")
        print(f"  {'caption':<25} {result['caption']}（来源：{result['caption_source']}）")
        return 0 if result.get("ok") else 1

    if args.command == "serve":
        if board_date:
            print("--date 只适用于 once/preview/board", file=sys.stderr)
            return 2
        print(f"常驻模式启动：每天 {cfg.run_at} ({cfg.timezone})，Ctrl+C 退出")
        serve(cfg, _task)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
