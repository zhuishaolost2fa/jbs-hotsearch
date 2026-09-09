# -*- coding: utf-8 -*-
"""命令行入口。

    python -m jbs_hotsearch once      # 立刻生成当天榜单（写库 + 出报告）
    python -m jbs_hotsearch preview   # 只算不写，打印报告
    python -m jbs_hotsearch serve     # 常驻，每天 HS_RUN_AT 触发
    python -m jbs_hotsearch doctor    # 环境与权限自检
    python -m jbs_hotsearch board     # 看最近一期本地快照
    python -m jbs_hotsearch social    # 出榜并生成小红书素材（截图 + 文案）
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime

from .config import Config
from .doctor import run as run_doctor
from .pipeline import preview, run_once
from .scheduler import serve
from .store.local_store import LocalStore


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _task(cfg: Config, board_date: date) -> object:
    return run_once(cfg, board_date)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jbs-hotsearch", description="剧本杀每日热门榜 Top10")
    parser.add_argument("command", choices=["once", "serve", "preview", "doctor", "board", "social"])
    parser.add_argument("--date", help="指定榜单日期 YYYY-MM-DD（默认今天）")
    args = parser.parse_args(argv)

    cfg = Config.load()
    setup_logging(cfg.log_level)

    board_date = None
    if args.date:
        board_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    if args.command == "doctor":
        return run_doctor(cfg)

    if args.command == "board":
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
            board = run_once(cfg, board_date)
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
        print(preview(cfg, board_date))
        return 0

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
