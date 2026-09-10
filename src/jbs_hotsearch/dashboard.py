# -*- coding: utf-8 -*-
"""监听大盘的 HTML 渲染。

自包含：CSS / JS 全内联，不引 CDN —— 断网、内网、离线双击打开都要能看。
只吃 `Snapshot`，不碰任何网络与文件 IO，方便 `watch --emit` 生成静态快照。
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta

from .tz_util import get_tz
from .watchdog import (
    LEVEL_BAD,
    LEVEL_IDLE,
    LEVEL_MISSING,
    LEVEL_OK,
    LEVEL_PENDING,
    LEVEL_RUNNING,
    LEVEL_STUCK,
    LEVEL_UNKNOWN,
    LEVEL_WARN,
    Snapshot,
    TaskBoard,
)

# 空单元格占位。单独定义成常量而不是塞在 f-string 里，
# 是因为 f-string 内不能再出现反斜杠转义（Python 3.11 会直接 SyntaxError，
# PEP 701 放宽到 3.12 才行，而本项目声明支持 3.11）。
EMPTY = '<span class="note">—</span>'

LEVEL_LABEL = {
    LEVEL_OK: "成功",
    LEVEL_WARN: "可疑",
    LEVEL_BAD: "失败",
    LEVEL_MISSING: "缺跑",
    LEVEL_PENDING: "未到点",
    LEVEL_IDLE: "无任务",
    LEVEL_RUNNING: "进行中",
    LEVEL_STUCK: "卡住",
    LEVEL_UNKNOWN: "未知",
}

CSS = """
:root{
  --bg:#f5f6f8; --card:#ffffff; --line:#e5e7eb; --text:#111827; --muted:#6b7280;
  --ok:#16a34a; --warn:#d97706; --bad:#dc2626; --missing:#94a3b8;
  --idle:#e2e8f0; --running:#3b82f6; --stuck:#f97316; --unknown:#cbd5e1;
  --ok-bg:#dcfce7; --warn-bg:#fef3c7; --bad-bg:#fee2e2; --missing-bg:#eef2f7;
  --idle-bg:#f1f5f9; --running-bg:#dbeafe; --stuck-bg:#ffedd5; --unknown-bg:#f1f5f9;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:4px}
h1{font-size:20px;margin:0;font-weight:650}
.sub{color:var(--muted);font-size:13px}
.meta{margin-left:auto;display:flex;align-items:center;gap:10px;color:var(--muted);font-size:12px}
button{font:inherit;font-size:12px;padding:5px 12px;border:1px solid var(--line);background:#fff;
  color:var(--text);border-radius:6px;cursor:pointer}
button:hover{background:#f3f4f6}
.banner{padding:12px 14px;border-radius:10px;margin:14px 0;font-size:13px}
.banner.bad{background:var(--bad-bg);border:1px solid #fecaca;color:#991b1b}
.banner.warn{background:var(--warn-bg);border:1px solid #fde68a;color:#92400e}
.banner.err{background:#eef2ff;border:1px solid #c7d2fe;color:#3730a3}
.banner b{font-weight:650}
.overview{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.card .k{color:var(--muted);font-size:12px;margin-bottom:6px;display:flex;gap:6px;align-items:center}
.card .v{font-size:22px;font-weight:650;line-height:1.25}
.card .u{font-size:12px;color:var(--muted);font-weight:400;margin-left:4px}
.card .d{font-size:12px;color:var(--muted);margin-top:8px;line-height:1.5}
.card.ok .v{color:var(--ok)} .card.bad .v{color:var(--bad)}
.card.warn .v{color:var(--warn)} .card.stuck .v{color:var(--stuck)}
.card.missing .v{color:var(--missing)} .card.idle .v{color:#64748b}
.card.running .v{color:var(--running)} .card.unknown .v{color:#94a3b8}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;
  padding:16px 18px;margin:16px 0}
h2{font-size:14px;margin:0 0 4px;font-weight:650;display:flex;align-items:center;gap:8px}
h2 .tag{font-size:11px;font-weight:500;color:var(--muted);background:#f1f5f9;
  padding:1px 8px;border-radius:999px}
.h2sub{color:var(--muted);font-size:12px;margin:0 0 14px}
.cal{display:flex;gap:3px;overflow-x:auto;padding-bottom:4px}
.cal .col{display:flex;flex-direction:column;gap:3px}
.cell{width:15px;height:15px;border-radius:3px;background:var(--idle);position:relative}
.cell.ok{background:var(--ok)} .cell.warn{background:var(--warn)}
.cell.bad{background:var(--bad)} .cell.missing{background:var(--missing)}
.cell.idle{background:var(--idle)} .cell.running{background:var(--running)}
.cell.stuck{background:var(--stuck)} .cell.unknown{background:var(--unknown)}
.cell.pending{background:#fff;border:1px solid var(--unknown)}
.cell.future{background:transparent;border:1px dashed #e5e7eb}
.legend{display:flex;gap:12px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:12px}
.legend i{display:inline-block;width:11px;height:11px;border-radius:3px;margin-right:5px;
  vertical-align:-1px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:600;color:var(--muted);font-size:12px;
  padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid #f1f2f4;vertical-align:top}
tr:last-child td{border-bottom:none}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;white-space:nowrap}
.pill.ok{background:var(--ok-bg);color:#15803d}
.pill.warn{background:var(--warn-bg);color:#b45309}
.pill.bad{background:var(--bad-bg);color:#b91c1c}
.pill.missing{background:var(--missing-bg);color:#64748b}
.pill.pending{background:#f8fafc;color:#94a3b8;border:1px solid var(--line)}
.pill.idle{background:var(--idle-bg);color:#64748b}
.pill.running{background:var(--running-bg);color:#1d4ed8}
.pill.stuck{background:var(--stuck-bg);color:#c2410c}
.pill.unknown{background:var(--unknown-bg);color:#94a3b8}
.src{display:inline-block;margin:1px 4px 1px 0;padding:1px 7px;border-radius:5px;
  font-size:11px;background:#f1f5f9;color:#475569}
.src.no{background:var(--bad-bg);color:#b91c1c}
.note{color:var(--muted);font-size:12px}
.err-text{color:#b91c1c;font-size:12px;word-break:break-all}
.ok-text{color:#15803d;font-size:12px}
.bar{height:6px;border-radius:3px;background:#f1f5f9;overflow:hidden;margin-top:6px}
.bar i{display:block;height:100%;background:var(--ok)}
.bar i.low{background:var(--warn)} .bar i.bad{background:var(--bad)}
.srcrow{display:grid;grid-template-columns:150px 1fr 90px;gap:12px;align-items:center;
  padding:9px 0;border-bottom:1px solid #f1f2f4}
.srcrow:last-child{border-bottom:none}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
footer{color:var(--muted);font-size:12px;margin-top:22px;text-align:center}
@media(max-width:860px){.overview{grid-template-columns:1fr}
  .srcrow{grid-template-columns:110px 1fr 70px}}
"""

JS = """
(function(){
  var s = document.getElementById('cd');
  if(!s) return;
  var left = parseInt(s.dataset.sec||'0',10);
  setInterval(function(){
    left -= 1;
    if(left <= 0){ location.reload(); return; }
    s.textContent = left + 's';
  }, 1000);
})();
"""


def _esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""))


def _fmt_duration(ms: int | None) -> str:
    return f"{ms / 1000:.1f}s" if ms else "—"


def _fmt_time(iso: str | None, tz) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    return dt.astimezone(tz).strftime("%m-%d %H:%M")


def _fmt_clock(iso: str | None, tz) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "—"
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S")


def _pill(level: str) -> str:
    return f'<span class="pill {level}">{LEVEL_LABEL.get(level, level)}</span>'


def _calendar(board: TaskBoard, snap: Snapshot) -> str:
    if not board.items or board.kind == "asset":
        return ""
    by_date = {i.date: i for i in board.items}
    first = datetime.fromisoformat(board.items[0].date).date()
    last = datetime.fromisoformat(board.items[-1].date).date()
    start = first - timedelta(days=first.weekday())  # 对齐到周一

    cols: list[str] = []
    day = start
    while day <= last:
        cells: list[str] = []
        for _ in range(7):
            key = day.isoformat()
            item = by_date.get(key)
            if day > last or item is None:
                cells.append('<div class="cell future"></div>')
            else:
                tip = f"{key} · {LEVEL_LABEL.get(item.level, item.level)}"
                if item.detail:
                    tip += f" · {item.detail}"
                if item.error:
                    tip += f" · {item.error[:80]}"
                cells.append(f'<div class="cell {item.level}" title="{_esc(tip)}"></div>')
            day += timedelta(days=1)
        cols.append(f'<div class="col">{"".join(cells)}</div>')
    return f'<div class="cal">{"".join(cols)}</div>'


def _legend(board: TaskBoard) -> str:
    if board.kind == "daily":
        items = [("ok", "成功"), ("warn", "可疑"), ("bad", "失败"), ("missing", "没跑")]
    else:
        items = [("ok", "全部成功"), ("bad", "有失败"), ("running", "进行中"),
                 ("stuck", "卡住"), ("idle", "无任务")]
    return '<div class="legend">' + "".join(
        f'<span><i style="background:var(--{lvl})"></i>{label}</span>' for lvl, label in items
    ) + "</div>"


def _render_rows(board: TaskBoard, snap: Snapshot, tz) -> str:
    rows: list[str] = []
    for item in reversed(board.items):
        srcs = "".join(
            f'<span class="src{" no" if not s.get("ok") else ""}">'
            f'{_esc(s.get("source"))}{"" if s.get("ok") else " ✕"}</span>'
            for s in item.sources
        )
        extra = ""
        if srcs:
            extra = srcs
        elif item.detail:
            extra = f'<span class="note">{_esc(item.detail)}</span>'
        detail = (
            f'<div class="err-text">{_esc(item.error)}</div>'
            if item.error
            else (f'<div class="note">{_esc(item.note)}</div>' if item.note and not item.detail else "")
        )
        rows.append(
            "<tr>"
            f"<td>{item.date}"
            f'<div class="note">周{"一二三四五六日"[datetime.fromisoformat(item.date).weekday()]}</div></td>'
            f"<td>{_pill(item.level)}</td>"
            f'<td class="num">{item.runs or "—"}</td>'
            f'<td class="num">{item.item_count if item.item_count is not None else "—"}'
            + (f'<div class="note">库内 {item.daily_rows}</div>' if item.daily_rows else "")
            + "</td>"
            f'<td class="num">{_fmt_duration(item.duration_ms)}</td>'
            f"<td>{extra or EMPTY}</td>"
            f'<td class="num">{_fmt_time(item.finished_at, tz)}</td>'
            f"<td>{detail}</td>"
            "</tr>"
        )
    return "".join(rows)


def _render_assets(board: TaskBoard) -> str:
    if not board.assets:
        return '<div class="note">没有配置产物探测。</div>'
    rows: list[str] = []
    for a in board.assets:
        if a.level == LEVEL_OK:
            detail = f'<span class="ok-text">{_esc(a.note)}</span>'
        elif a.level == LEVEL_UNKNOWN:
            detail = f'<span class="note">{_esc(a.note)}</span>'
        else:
            detail = f'<span class="err-text">{_esc(a.note)}</span>'
        rows.append(
            "<tr>"
            f'<td class="mono"><a href="{_esc(a.url)}" target="_blank" '
            f'rel="noopener" style="color:#1d4ed8;text-decoration:none">{_esc(a.path)}</a></td>'
            f"<td>{_pill(a.level)}</td>"
            f'<td class="num">{a.http_status if a.http_status else "—"}</td>'
            f'<td class="num">{a.size if a.size is not None else "—"}</td>'
            f"<td>{detail}</td>"
            f'<td class="num note">{_esc(a.last_modified or "—")}</td>'
            "</tr>"
        )
    return (
        '<table><thead><tr><th>产物</th><th>结论</th><th class="num">HTTP</th>'
        '<th class="num">字节</th><th>说明</th><th class="num">Last-Modified</th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_sources(board: TaskBoard) -> str:
    if not board.sources:
        return ""
    out: list[str] = []
    for src in board.sources:
        pct = round(src.rate * 100)
        cls = "bad" if src.rate < 0.8 else ("low" if src.rate < 1 else "")
        err = f'<div class="err-text">{_esc(src.last_error)}</div>' if src.last_error else ""
        out.append(
            '<div class="srcrow">'
            f"<div><b>{_esc(src.name)}</b></div>"
            f'<div><div class="bar"><i class="{cls}" style="width:{max(pct, 2)}%"></i></div>{err}</div>'
            f'<div class="num">{pct}%<div class="note">{src.ok_days}/{src.total} 天</div></div>'
            "</div>"
        )
    return (
        '<section><h2>数据源健康 <span class="tag">hotsearch</span></h2>'
        '<p class="h2sub">窗口期内各数据源的成功占比</p>' + "".join(out) + "</section>"
    )


def _overview_card(board: TaskBoard) -> str:
    today = board.today_status
    level = today.level if today else LEVEL_UNKNOWN
    if board.kind == "asset":
        ok = board.metrics.get("ok", 0)
        total = board.metrics.get("total", 0)
        sub = f"{ok}/{total} 个产物正常"
        if board.metrics.get("unknown"):
            sub += f" · {board.metrics['unknown']} 个探测失败"
    elif board.kind == "ondemand":
        m = board.metrics
        sub = f"窗口内 {m.get('window_done', 0)} 完成 / {m.get('window_failed', 0)} 失败"
        if m.get("stuck"):
            sub += f" · <b style='color:#c2410c'>{m['stuck']} 个卡住</b>"
    else:
        sub = f"连续成功 {board.streak} 天 · 成功率 {round(board.success_rate * 100)}%"
    detail = (today.error or today.note) if today else ""
    desc = f'<div class="d">{_esc(board.desc)}<br>{sub}</div>'
    if detail and level in (LEVEL_BAD, LEVEL_WARN, LEVEL_STUCK, LEVEL_UNKNOWN):
        desc += f'<div class="d err-text">{_esc(detail[:120])}</div>'
    return (
        f'<div class="card {level}">'
        f'<div class="k"><b>{_esc(board.name)}</b> {_pill(level)}</div>'
        f'<div class="v">{LEVEL_LABEL.get(level, level)}</div>{desc}</div>'
    )


def _board_section(board: TaskBoard, snap: Snapshot, tz) -> str:
    parts: list[str] = []
    if board.kind == "asset":
        parts.append(
            f'<section><h2>{_esc(board.name)} <span class="tag">每次部署</span></h2>'
            f'<p class="h2sub">{_esc(board.desc)}</p>'
        )
        if board.error:
            parts.append(f'<div class="banner err">{_esc(board.error)}</div>')
        parts.append(_render_assets(board))
        parts.append(
            '<div class="legend"><span><i style="background:var(--ok)"></i>正常</span>'
            '<span><i style="background:var(--warn)"></i>可疑（空 / 内容不对）</span>'
            '<span><i style="background:var(--bad)"></i>缺失（404）</span>'
            '<span><i style="background:var(--unknown)"></i>探测失败 ≠ 生成失败</span></div>'
        )
        parts.append("</section>")
        return "".join(parts)

    kind_label = "每天" if board.kind == "daily" else "按需触发"
    parts.append(
        f'<section><h2>{_esc(board.name)} <span class="tag">{kind_label}</span></h2>'
        f'<p class="h2sub">{_esc(board.desc)}</p>'
    )
    if board.error:
        parts.append(f'<div class="banner err">{_esc(board.error)}</div>')
    parts.append(_calendar(board, snap))
    parts.append(_legend(board))
    parts.append(
        '<table style="margin-top:16px"><thead><tr>'
        "<th>日期</th><th>结论</th><th class=\"num\">任务数</th><th class=\"num\">产出</th>"
        "<th class=\"num\">耗时</th><th>数据源 / 明细</th><th class=\"num\">结束</th><th>说明</th>"
        "</tr></thead><tbody>" + _render_rows(board, snap, tz) + "</tbody></table>"
    )
    parts.append("</section>")
    return "".join(parts)


def render(snap: Snapshot, refresh_seconds: int = 60) -> str:
    tz = get_tz(snap.timezone)

    banners: list[str] = []
    if snap.error:
        banners.append(f'<div class="banner err"><b>取数异常：</b>{_esc(snap.error)}</div>')
    for board in snap.boards:
        bad = [i for i in board.items if i.level == LEVEL_BAD]
        missing = [i for i in board.items if i.level == LEVEL_MISSING]
        stuck = [i for i in board.items if i.level == LEVEL_STUCK]
        warn = [i for i in board.items if i.level == LEVEL_WARN]
        if bad:
            tail = bad[-1]
            banners.append(
                f'<div class="banner bad"><b>{_esc(board.name)}：{len(bad)} 天失败</b> —— '
                f'{_esc(", ".join(i.date for i in bad[-5:]))}'
                + (
                    f'<div style="margin-top:4px">最近一次：{_esc(tail.error or tail.note)}</div>'
                    if (tail.error or tail.note)
                    else ""
                )
                + "</div>"
            )
        if missing:
            banners.append(
                f'<div class="banner bad"><b>{_esc(board.name)}：{len(missing)} 天没跑</b> —— '
                f'{_esc(", ".join(i.date for i in missing[-5:]))}'
                f'{_esc(f" 等 {len(missing)} 天" if len(missing) > 5 else "")}'
                " —— 服务进程可能没起来。</div>"
            )
        if stuck:
            banners.append(
                f'<div class="banner warn"><b>{_esc(board.name)}：有任务卡住</b> —— '
                f'{_esc(stuck[-1].error or stuck[-1].note)}</div>'
            )
        if warn and not bad:
            banners.append(
                f'<div class="banner warn"><b>{_esc(board.name)}：{len(warn)} 天可疑</b> —— '
                f'{_esc(", ".join(i.date for i in warn[-5:]))}'
                + (f"：{_esc(warn[-1].error or warn[-1].note)}" if (warn[-1].error or warn[-1].note) else "")
                + "</div>"
            )

    overview = "".join(_overview_card(b) for b in snap.boards)
    sections = "".join(_board_section(b, snap, tz) for b in snap.boards)
    hot = snap.board("hotsearch")
    sources_html = _render_sources(hot) if hot else ""

    refresh_note = (
        f' · <span id="cd" data-sec="{refresh_seconds}">{refresh_seconds}s</span> 后自动刷新'
        if refresh_seconds
        else ""
    )
    plan = f'{_esc(hot.plan_at or "—")} 出榜' if hot else ""

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>jbs 每日任务监听大盘</title>
<style>{CSS}</style></head>
<body><div class="wrap">
<header>
  <h1>jbs 每日任务监听大盘</h1>
  <span class="sub">热门榜 {plan} · 回看 {snap.days} 天</span>
  <div class="meta">
    <span>更新于 {_esc(_fmt_clock(snap.generated_at, tz))}</span>
    <button onclick="location.reload()">刷新</button>
  </div>
</header>
{''.join(banners)}
<div class="overview">{overview}</div>
{sections}
{sources_html}
<footer>generated by <code>python -m jbs_hotsearch watch</code>{refresh_note}</footer>
</div><script>{JS}</script></body></html>"""


def render_json(snap: Snapshot) -> str:
    return json.dumps(snap.to_dict(), ensure_ascii=False, indent=2)
