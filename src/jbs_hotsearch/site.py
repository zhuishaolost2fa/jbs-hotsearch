# -*- coding: utf-8 -*-
"""榜单展示页：把 DailyBoard 渲染成单文件静态 HTML（内嵌数据，无外部依赖）。

设计目标：
  - 移动端优先、桌面居中；零 JS 依赖（服务端直接渲染 DOM，禁 JS 也能看）；
  - 只依赖 board.to_dict() 的字段，不引入任何运行时网络请求；
  - 每天由 pipeline 跑完自动覆写 data/site/index.html，nginx 静态托管即可。
"""
from __future__ import annotations

import html
from datetime import datetime

from .config import Config
from .models import DailyBoard

_BOARD_TITLE = "杭州剧本杀热度榜"
_BOARD_SUBTITLE = "米圈杭州拼场 · 近 N 天真实组局"


def _escape(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _badge(item) -> str:
    """涨跌徽标：新上榜 / ↑N / ↓N / 持平。"""
    if item.get("is_new"):
        return '<span class="badge badge-new">新</span>'
    change = item.get("rank_change")
    if change is None:
        return ""
    if change > 0:
        return f'<span class="badge badge-up">↑{change}</span>'
    if change < 0:
        return f'<span class="badge badge-down">↓{abs(change)}</span>'
    return '<span class="badge badge-flat">—</span>'


def _rank_class(rank: int) -> str:
    if rank == 1:
        return "rank-gold"
    if rank == 2:
        return "rank-silver"
    if rank == 3:
        return "rank-bronze"
    return "rank-plain"


def _meta_bits(item) -> list[str]:
    """人数 / 时长等元信息行（评分已在 reason 里体现，不重复展示）。"""
    bits: list[str] = []
    players = item.get("players")
    if players:
        bits.append(f'<span class="meta"><i class="dot"></i>{_escape(players)}</span>')
    duration = item.get("duration")
    if duration:
        bits.append(f'<span class="meta"><i class="dot"></i>{_escape(duration)}</span>')
    return bits


def _tags(item) -> str:
    tags = item.get("tags") or []
    if not tags:
        return ""
    chips = "".join(f'<span class="chip">{_escape(t)}</span>' for t in tags[:5])
    return f'<div class="tags">{chips}</div>'


def _item_card(item) -> str:
    rank = int(item.get("rank", 0))
    title = _escape(item.get("title"))
    score = item.get("hot_score")
    score_s = f"{score:.0f}" if score is not None else "—"
    reason = _escape(item.get("reason") or "")
    meta = "".join(_meta_bits(item))
    return f"""
    <li class="item">
      <div class="rank {_rank_class(rank)}">{rank}</div>
      <div class="body">
        <div class="row">
          <h2 class="title">{title}</h2>
          {_badge(item)}
        </div>
        <div class="score">{score_s}<span class="unit">热度</span></div>
        {('<div class="reason">' + reason + '</div>') if reason else ""}
        {('<div class="metas">' + meta + '</div>') if meta else ""}
        {_tags(item)}
      </div>
    </li>"""


def _filtered_section(board: dict) -> str:
    filtered = board.get("filtered_items") or []
    if not filtered:
        return ""
    rows = "".join(
        f'<li><span class="strike">{_escape(it.get("title"))}</span>'
        f'<span class="fscore">原热度 {it.get("hot_score", 0):.0f}</span></li>'
        for it in filtered
    )
    return (
        '<section class="filtered">'
        '<h3>已解析 · 未入榜</h3>'
        '<p class="hint">以下剧本热度足够进榜，但 DM 手册已在库（已解析），本轮剔除：</p>'
        f'<ul class="flist">{rows}</ul>'
        "</section>"
    )


def _sources_section(board: dict) -> str:
    sources = board.get("sources") or []
    if not sources:
        return ""
    rows = "".join(
        f"<tr><td>{_escape(s.get('source'))}</td>"
        f"<td>{'正常' if s.get('ok') else '异常'}</td>"
        f"<td>{s.get('items', 0)}</td>"
        f"<td>{s.get('elapsed_ms', 0)}ms</td></tr>"
        for s in sources
    )
    return (
        '<section class="sources"><h3>数据源</h3>'
        '<table><thead><tr><th>源</th><th>状态</th><th>条目</th><th>耗时</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></section>"
    )


def _html_shell(board_date: str, generated_at: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{_BOARD_TITLE} · {_escape(board_date)}</title>
<meta name="description" content="{_BOARD_TITLE}，基于米圈杭州拼场近 3 天真实组局，每日更新。">
<style>
:root {{
  --bg: #f5f3ee;
  --card: #ffffff;
  --ink: #211d18;
  --muted: #8c8578;
  --line: #ece7dd;
  --accent: #e5532b;
  --accent-deep: #b83d1a;
  --gold: #c8961e;
  --silver: #8d939b;
  --bronze: #b0723a;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB",
    "Microsoft YaHei", "Segoe UI", Roboto, sans-serif;
  background: var(--bg);
  color: var(--ink);
  line-height: 1.5;
}}
.wrap {{ max-width: 640px; margin: 0 auto; padding: 0 16px 40px; }}

.hero {{
  background: linear-gradient(135deg, #3a2015 0%, #6b2c1c 45%, #c8502a 100%);
  color: #fff;
  padding: 34px 20px 30px;
  border-radius: 0 0 22px 22px;
  margin: 0 -16px 20px;
  box-shadow: 0 6px 20px rgba(60, 30, 15, .18);
}}
.hero .brand {{ font-size: 13px; letter-spacing: 2px; opacity: .82; }}
.hero h1 {{ font-size: 30px; font-weight: 800; margin: 8px 0 6px; letter-spacing: 1px; }}
.hero .sub {{ font-size: 14px; opacity: .9; }}
.hero .date {{ font-size: 13px; opacity: .75; margin-top: 4px; }}

.lead {{ text-align: center; color: var(--muted); font-size: 13px; margin: 0 4px 16px; }}

.list {{ list-style: none; }}
.item {{
  display: flex; gap: 12px; align-items: flex-start;
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 16px 14px;
  margin-bottom: 12px;
  box-shadow: 0 1px 3px rgba(0,0,0,.04);
}}
.rank {{
  flex: 0 0 40px; height: 40px; border-radius: 12px;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; font-weight: 800; color: #fff;
  background: #cfc8bb;
}}
.rank-gold {{ background: linear-gradient(135deg, #f2c14e, #c8961e); }}
.rank-silver {{ background: linear-gradient(135deg, #c6ccd2, #8d939b); }}
.rank-bronze {{ background: linear-gradient(135deg, #d9a06b, #b0723a); }}
.body {{ flex: 1; min-width: 0; }}
.row {{ display: flex; align-items: center; gap: 8px; }}
.title {{ font-size: 18px; font-weight: 700; line-height: 1.3; }}
.badge {{ font-size: 11px; padding: 2px 7px; border-radius: 20px; font-weight: 600; white-space: nowrap; }}
.badge-new {{ background: #ffe9dd; color: var(--accent-deep); }}
.badge-up {{ background: #ffe1d8; color: var(--accent-deep); }}
.badge-down {{ background: #e3f0e4; color: #2e7d32; }}
.badge-flat {{ background: #efede7; color: var(--muted); }}
.score {{
  display: flex; align-items: baseline; gap: 4px;
  color: var(--accent); font-size: 26px; font-weight: 800; line-height: 1.1;
  margin: 6px 0 4px;
}}
.score .unit {{ font-size: 12px; font-weight: 500; color: var(--muted); }}
.reason {{ font-size: 13px; color: #5b554a; margin-top: 2px; }}
.metas {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 6px; font-size: 13px; color: var(--muted); }}
.meta {{ display: inline-flex; align-items: center; gap: 6px; }}
.meta .dot {{ width: 4px; height: 4px; border-radius: 50%; background: var(--accent); opacity: .6; }}
.tags {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }}
.chip {{
  font-size: 11px; color: #6b6255; background: #f3f0e9;
  padding: 2px 9px; border-radius: 20px;
}}

.filtered, .sources {{
  background: var(--card); border: 1px solid var(--line);
  border-radius: 16px; padding: 16px; margin-top: 20px;
}}
.filtered h3, .sources h3 {{ font-size: 15px; margin-bottom: 8px; }}
.filtered .hint {{ font-size: 12px; color: var(--muted); margin-bottom: 8px; }}
.flist {{ list-style: none; }}
.flist li {{ display: flex; justify-content: space-between; padding: 6px 0; font-size: 14px; border-bottom: 1px dashed var(--line); }}
.flist li:last-child {{ border-bottom: none; }}
.strike {{ color: var(--muted); text-decoration: line-through; }}
.fscore {{ color: var(--muted); font-size: 12px; }}
.sources table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
.sources th, .sources td {{ text-align: left; padding: 6px 4px; border-bottom: 1px solid var(--line); }}
.sources th {{ color: var(--muted); font-weight: 500; }}

.footer {{ text-align: center; color: var(--muted); font-size: 12px; margin-top: 24px; line-height: 1.8; }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="brand">JBS · 每日更新</div>
    <h1>{_BOARD_TITLE}</h1>
    <div class="sub">{_BOARD_SUBTITLE}</div>
    <div class="date">{_escape(board_date)} · 生成于 {_escape(generated_at)}</div>
  </header>
  {body}
  <div class="footer">
    热度由米圈杭州拼场「近 3 天真实组局」计算，剧本榜仅作评分/标签参考。<br>
    数据仅供娱乐参考，不代表任何平台官方排名。
  </div>
</div>
</body>
</html>"""


def render_site(board: DailyBoard) -> str:
    data = board.to_dict()
    data["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    items = data.get("items") or []
    cards = "".join(_item_card(it) for it in items)
    body = (
        f'<div class="lead">共 {len(items)} 部 · 已过滤已解析剧本</div>'
        f'<ol class="list">{cards}</ol>'
        + _filtered_section(data)
        + _sources_section(data)
    )
    return _html_shell(data["board_date"], data["generated_at"], body)


def write_site(cfg: Config, board: DailyBoard) -> str:
    site_dir = cfg.data_dir / "site"
    site_dir.mkdir(parents=True, exist_ok=True)
    path = site_dir / "index.html"
    path.write_text(render_site(board), encoding="utf-8")
    return str(path)
