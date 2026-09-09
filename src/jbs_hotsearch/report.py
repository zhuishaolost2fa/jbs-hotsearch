# -*- coding: utf-8 -*-
"""Markdown 报告：控制台和文件共用同一份渲染。"""
from __future__ import annotations

from datetime import datetime

from .config import Config
from .models import DailyBoard


def _badge(item) -> str:
    if item.prev_rank is None:
        return "🆕 新上榜"
    delta = item.prev_rank - item.rank
    if delta > 0:
        return f"↑{delta}"
    if delta < 0:
        return f"↓{abs(delta)}"
    return "— 持平"


def render_markdown(board: DailyBoard) -> str:
    lines: list[str] = []
    lines.append(f"# 剧本杀热门榜 Top {len(board.items)} · {board.board_date.isoformat()}")
    lines.append("")
    lines.append(f"> 生成耗时 {board.elapsed_ms} ms ｜ 数据源：" + "、".join(r.source for r in board.source_results))
    lines.append("")

    failed = [r for r in board.source_results if not r.ok]
    if failed:
        lines.append("**降级说明**：")
        for r in failed:
            lines.append(f"- `{r.source}` 未取到数据：{r.error}")
        lines.append("- 榜单基于剩余可用源生成，热度口径请以 Top 榜表格的「上榜理由」为准。")
        lines.append("")

    lines.append("| # | 剧本 | 热度 | 较昨日 | 评分 | 人数 | 时长 | 标签 |")
    lines.append("|---|------|------|--------|------|------|------|------|")
    for item in board.items:
        rating = f"{item.rating:g}" if item.rating else "—"
        lines.append(
            "| {rank} | **{title}** | {score:.1f} | {badge} | {rating} | {players} | {duration} | {tags} |".format(
                rank=item.rank,
                title=item.title,
                score=item.hot_score,
                badge=_badge(item),
                rating=rating,
                players=item.players or "—",
                duration=item.duration or "—",
                tags="、".join(item.tags) if item.tags else "—",
            )
        )
    lines.append("")

    lines.append("## 上榜理由")
    lines.append("")
    for item in board.items:
        lines.append(f"- **{item.title}**：{item.reason or '—'}（来源：{'、'.join(item.sources)}）")
    lines.append("")

    lines.append("## 数据源明细")
    lines.append("")
    lines.append("| 源 | 状态 | 条目 | 耗时 |")
    lines.append("|----|------|------|------|")
    for r in board.source_results:
        lines.append(
            f"| `{r.source}` | {'✅' if r.ok else '❌'} | {len(r.candidates)} | {r.elapsed_ms}ms |"
        )
    lines.append("")
    lines.append(f"_生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")
    return "\n".join(lines)


def write_report(cfg: Config, board: DailyBoard) -> str:
    report_dir = cfg.data_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{board.board_date.isoformat()}.md"
    markdown = render_markdown(board)
    path.write_text(markdown, encoding="utf-8")
    print(markdown)
    return str(path)
