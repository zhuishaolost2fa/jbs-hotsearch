# -*- coding: utf-8 -*-
"""每周热度榜周报：把一个统计周期的每日榜单聚合成一份「这一周谁最热」的总结。

和每日榜的区别（也是它的价值）：
    每日榜回答「今天谁最热」——受单日组局波动影响大；
    周报回答「这一周谁一直热」——用**上榜天数 × 热度**双维度过滤掉单日噪音。

时间范围口径（用户明确要求「明确的时间范围」）：
    **上周六 ~ 本周五，7 个自然日**，周五 10:00 出报告（日榜 09:00 跑完）。
    为什么不是自然周：周报是给「周末去玩」的人看的，周六早上看时周一~周日的口径
    已经隔了 5 天；截到本周五，读者看到的就是最新一期组局热度。
    页面上会把起止日期、星期、实际有数据的天数全部写出来 ——
    少了任何一项，读者都无法判断这份总结到底覆盖了哪几天。

产物（都在 data/weekly/，以周期起始日（周六）命名，如 2026-09-12）：
    {start}.html        周报页：时间范围 + 概览 + Top 榜 + 图片 + 复制文案按钮
    {start}.png         周报海报（长按保存 / 发小红书）
    {start}.txt         小红书文案

监控：本模块只管生成；watchdog 通过扫 data/weekly/ 的产物判断「这周跑了没」。
"""
from __future__ import annotations

import html
import json
import logging
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from .config import Config
from .shot import screenshot_slices
from .tz_util import get_tz

logger = logging.getLogger(__name__)

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


# ─────────── 时间范围 ──────────────────────────────────────────────
def period_end(anchor: date, weeks_ago: int = 1) -> date:
    """周期结束日 = anchor 当天或之前最近的那个**周五**，再往前推 weeks_ago-1 周。

    周五是周期的「截止日」：本周五出报告，覆盖到本周五当天那一期日榜。
    今天周四 → 上一个周五；今天周五 → 今天；今天周六 → 昨天。
    """
    end = anchor - timedelta(days=(anchor.weekday() - 4) % 7)
    return end - timedelta(weeks=max(0, weeks_ago - 1))


def week_range(anchor: date, weeks_ago: int = 1) -> tuple[date, date]:
    """返回以 anchor 为基准、往前推 weeks_ago 个周期的 (周六, 周五)，共 7 天。

    weeks_ago=1（默认）= 最近一个**完整**周期：周五当天就是「上周六~本周五」，
    其余日子跑则退回到上一个已结束的周期，不会拿到半周期数据。
    """
    end = period_end(anchor, weeks_ago)
    return end - timedelta(days=6), end


def _iso_week_label(day: date) -> str:
    """周期标签：以**截止日**所在的 ISO 周为准。

    周期是周六~周五，跨两个 ISO 周（起始的周六属于上一周），
    所以只能挑一天当代表 —— 用截止日，和「本周」的直觉一致。
    """
    iso = day.isocalendar()
    return f"{iso[0]} 年第 {iso[1]} 周"


# ─────────── 数据结构 ──────────────────────────────────────────────
@dataclass
class WeekRow:
    """一天里某个剧本的一条榜单记录。"""

    board_date: str
    rank: int
    title: str
    title_key: str
    hot_score: float
    is_new: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class WeekItem:
    """一个剧本在一周内的聚合结论。"""

    rank: int = 0                 # 周榜名次（按周热度降序）
    title: str = ""
    title_key: str = ""
    days: int = 0                 # 上榜天数
    avg_hot: float = 0.0          # 周均热度
    peak_hot: float = 0.0         # 单日最高热度
    best_rank: int = 0            # 最好名次（数字最小）
    worst_rank: int = 0
    first_rank: int = 0           # 本周首次上榜那天的名次
    last_rank: int = 0            # 本周最后一次的名次
    trend: float = 0.0            # 后半周均热 - 前半周均热（正 = 在升温）
    is_new: bool = False          # 本期新晋（本周内首次出现）
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def heat_index(self) -> float:
        """排序用的综合分：周均热度 × 上榜天数占比。

        为什么不是纯 avg_hot：只上 1 天但那天爆热的本，不该压过 7 天稳居前列的本。
        为什么不是纯 sum：sum 会天然偏向满勤本，把「只上 2 天但极热」的黑马埋掉。
        乘法是这两者的折中，天数占比是连续值（days/7），不会像布尔那样断层。
        """
        return self.avg_hot * (self.days / 7.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "title": self.title,
            "title_key": self.title_key,
            "days": self.days,
            "avg_hot": round(self.avg_hot, 2),
            "peak_hot": round(self.peak_hot, 2),
            "best_rank": self.best_rank,
            "worst_rank": self.worst_rank,
            "last_rank": self.last_rank,
            "trend": round(self.trend, 2),
            "is_new": self.is_new,
        }


@dataclass
class WeeklyReport:
    start: date
    end: date
    items: list[WeekItem] = field(default_factory=list)
    covered_days: int = 0         # 实际有榜单数据的天数
    total_rows: int = 0           # 窗口内总条目数
    unique_scripts: int = 0
    backend: str = "supabase"
    error: str | None = None

    @property
    def key(self) -> str:
        return self.start.isoformat()

    @property
    def range_text(self) -> str:
        return f"{self.start.isoformat()} ~ {self.end.isoformat()}"

    @property
    def range_cn(self) -> str:
        return (
            f"{self.start.month}月{self.start.day}日（{WEEKDAY_CN[self.start.weekday()]}）"
            f"— {self.end.month}月{self.end.day}日（{WEEKDAY_CN[self.end.weekday()]}）"
        )

    @property
    def risers(self) -> list[WeekItem]:
        """升温榜：至少上 2 天（1 天的样本算不出趋势），按 trend 降序。"""
        return sorted(
            [i for i in self.items if i.days >= 2 and i.trend > 0],
            key=lambda i: -i.trend,
        )[:3]

    @property
    def newcomers(self) -> list[WeekItem]:
        return [i for i in self.items if i.is_new]

    @property
    def regulars(self) -> list[WeekItem]:
        """常青：满勤（覆盖天数 = 本周有效天数）。"""
        return [i for i in self.items if i.days >= self.covered_days and self.covered_days > 0]


# ─────────── 取数 ──────────────────────────────────────────────────
def _fetch_supabase(cfg: Config, start: date, end: date) -> list[WeekRow]:
    if not (cfg.supabase_url and cfg.supabase_service_role_key):
        raise RuntimeError("缺 Supabase 配置")
    base = f"{cfg.supabase_url.rstrip('/')}/rest/v1"
    headers = {
        "apikey": cfg.supabase_service_role_key,
        "Authorization": f"Bearer {cfg.supabase_service_role_key}",
        "Accept": "application/json",
    }
    params = [
        ("select", "board_date,rank,title,title_key,hot_score,is_new,tags,players,duration,rating,reason"),
        ("board_date", f"gte.{start.isoformat()}"),
        ("board_date", f"lte.{end.isoformat()}"),
        ("order", "board_date.asc,rank.asc"),
        ("limit", "5000"),
    ]
    with httpx.Client(timeout=cfg.http_timeout, headers=headers) as client:
        resp = client.get(f"{base}/script_hot_daily", params=params)
    if resp.status_code >= 400:
        raise RuntimeError(f"Supabase 读取失败 {resp.status_code}: {resp.text[:200]}")
    out: list[WeekRow] = []
    for row in resp.json():
        out.append(
            WeekRow(
                board_date=str(row.get("board_date") or ""),
                rank=int(row.get("rank") or 0),
                title=str(row.get("title") or ""),
                title_key=str(row.get("title_key") or ""),
                hot_score=float(row.get("hot_score") or 0),
                is_new=bool(row.get("is_new")),
                meta={
                    "tags": row.get("tags") or [],
                    "players": row.get("players"),
                    "duration": row.get("duration"),
                    "rating": row.get("rating"),
                    "reason": row.get("reason") or "",
                },
            )
        )
    return out


def _fetch_local(cfg: Config, start: date, end: date) -> list[WeekRow]:
    db = Path(cfg.data_dir) / "hotsearch.db"
    if not db.is_file():
        return []
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select board_date, rank, title, title_key, hot_score, is_new, payload"
            " from script_hot_daily where board_date between ? and ?"
            " order by board_date asc, rank asc",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    out: list[WeekRow] = []
    for r in rows:
        try:
            payload = json.loads(r["payload"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        out.append(
            WeekRow(
                board_date=str(r["board_date"]),
                rank=int(r["rank"] or 0),
                title=str(r["title"] or ""),
                title_key=str(r["title_key"] or ""),
                hot_score=float(r["hot_score"] or 0),
                is_new=bool(r["is_new"]),
                meta=payload if isinstance(payload, dict) else {},
            )
        )
    return out


# ─────────── 聚合 ──────────────────────────────────────────────────
def aggregate(rows: list[WeekRow], top_n: int = 10) -> tuple[list[WeekItem], int, int]:
    """返回 (Top N 周榜, 覆盖天数, 去重后剧本数)。"""
    by_key: dict[str, list[WeekRow]] = {}
    for r in rows:
        if not r.title_key:
            continue
        by_key.setdefault(r.title_key, []).append(r)

    items: list[WeekItem] = []
    for key, group in by_key.items():
        group.sort(key=lambda r: r.board_date)
        scores = [g.hot_score for g in group]
        ranks = [g.rank for g in group]
        days = len(group)
        # 趋势：前半 vs 后半（只有 1 天时无从比较，记 0）
        half = days // 2
        trend = 0.0
        if days >= 2:
            first_half = scores[:half] if half else scores[:1]
            second_half = scores[half:] if half else scores[1:]
            trend = (sum(second_half) / len(second_half)) - (sum(first_half) / len(first_half))
        items.append(
            WeekItem(
                title=group[-1].title or group[0].title,
                title_key=key,
                days=days,
                avg_hot=sum(scores) / days,
                peak_hot=max(scores),
                best_rank=min(ranks),
                worst_rank=max(ranks),
                first_rank=group[0].rank,
                last_rank=group[-1].rank,
                trend=trend,
                is_new=any(g.is_new for g in group),
                meta=group[-1].meta,
            )
        )

    items.sort(key=lambda i: (-i.heat_index, -i.avg_hot, i.best_rank))
    for idx, item in enumerate(items, 1):
        item.rank = idx

    covered = len({r.board_date for r in rows})
    return items[:top_n], covered, len(by_key)


def build_report(cfg: Config, start: date, end: date, top_n: int | None = None) -> WeeklyReport:
    """拉数 + 聚合，拿到一份可直接渲染的周报。"""
    top_n = top_n or cfg.top_n
    report = WeeklyReport(start=start, end=end)
    try:
        rows = _fetch_supabase(cfg, start, end)
        report.backend = "supabase"
    except Exception as exc:  # noqa: BLE001 - 降级本地，保证周报不会整个拿不到
        logger.warning("Supabase 取周数据失败，降级本地 SQLite：%s", exc)
        rows = _fetch_local(cfg, start, end)
        report.backend = "local"
        report.error = f"{exc}；已降级读本地 SQLite"

    items, covered, unique = aggregate(rows, top_n=top_n)
    report.items = items
    report.covered_days = covered
    report.unique_scripts = unique
    report.total_rows = len(rows)
    return report


# ─────────── 渲染 ──────────────────────────────────────────────────
_CSS = """
:root {
  --bg:#f5f3ee; --card:#fff; --ink:#211d18; --muted:#8c8578;
  --brand:#e5532b; --line:#ece7dd; --chip:#fff5ee; --chip-border:#f9d9c5;
  --up:#d93b2b; --down:#2b8a5b;
}
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--ink); line-height:1.6; padding:20px 14px 60px; }
.wrap { max-width:720px; margin:0 auto; }
.hero { background:linear-gradient(135deg,#3a2015 0%,#6b2c1c 45%,#c8502a 100%);
  color:#fff; border-radius:0 0 22px 22px; padding:26px 22px 22px; margin:0 -14px 18px; }
.hero .brand { font-size:13px; letter-spacing:3px; opacity:.82; }
.hero h1 { font-size:26px; font-weight:800; margin:6px 0 4px; }
.hero .sub { font-size:14px; opacity:.9; }
.range { display:inline-block; margin-top:10px; padding:7px 14px; border-radius:10px;
  background:rgba(255,255,255,.14); border:1px solid rgba(255,255,255,.3); font-size:13px; }
.range b { font-size:15px; letter-spacing:.5px; }
.range .wk { opacity:.8; font-size:12px; display:block; margin-top:2px; }
.copy-btn { margin-top:12px; font-size:13px; padding:7px 16px; border-radius:20px;
  border:1px solid rgba(255,255,255,.35); background:rgba(255,255,255,.12); color:#fff; cursor:pointer; }
.copy-btn:active { background:rgba(255,255,255,.22); }
.summary { display:flex; gap:12px; margin:0 0 14px; padding:14px 16px; background:var(--card);
  border:1px solid var(--line); border-radius:14px; }
.summary > div { flex:1; text-align:center; }
.summary b { display:block; font-size:20px; color:var(--brand); }
.summary span { font-size:12px; color:var(--muted); }
.section-title { font-size:14px; font-weight:700; color:var(--muted); margin:22px 4px 10px; letter-spacing:1px; }
.item { background:var(--card); border:1px solid var(--line); border-radius:14px;
  padding:12px 14px; margin-bottom:10px; }
.item-head { display:flex; align-items:center; gap:8px; }
.rank { flex:0 0 26px; height:26px; border-radius:8px; background:#cfc8bb; color:#fff;
  display:inline-flex; align-items:center; justify-content:center; font-weight:800; font-size:14px; }
.rank-1 { background:linear-gradient(135deg,#f2c14e,#c8961e); }
.rank-2 { background:linear-gradient(135deg,#c6ccd2,#8d939b); }
.rank-3 { background:linear-gradient(135deg,#d9a06b,#b0723a); }
.title { font-size:17px; font-weight:700; }
.hot { margin-left:auto; font-size:13px; color:var(--muted); text-align:right; }
.hot b { color:var(--ink); font-size:15px; }
.item-meta { font-size:12px; color:var(--muted); margin-top:6px; display:flex; flex-wrap:wrap; gap:8px; }
.pill { padding:1px 8px; border:1px solid var(--line); border-radius:20px; }
.pill.new { background:var(--chip); border-color:var(--chip-border); color:var(--brand); font-weight:700; }
.trend-up { color:var(--up); font-weight:700; }
.trend-down { color:var(--down); font-weight:700; }
.bar { height:6px; border-radius:4px; background:var(--line); margin-top:8px; overflow:hidden; }
.bar > i { display:block; height:100%; background:linear-gradient(90deg,#f2c14e,#e5532b); }
.chips { display:flex; flex-wrap:wrap; gap:6px; margin-top:10px; }
.chip { font-size:12px; padding:3px 9px; background:var(--chip); border:1px solid var(--chip-border);
  border-radius:20px; }
.shot { background:var(--card); border:1px solid var(--line); border-radius:16px; padding:12px; margin-top:16px; }
/* 切片：两列网格，每张独立卡片 + 序号，边界一眼看得出（原来是一列长图往下堆） */
.slices { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.slices.single { grid-template-columns:1fr; }
.slice { position:relative; background:#fff; border:1px solid var(--line); border-radius:12px;
  padding:6px; cursor:zoom-in; }
.slice img { display:block; width:100%; height:auto; border-radius:8px; border:1px solid var(--line); }
.slice .no { position:absolute; left:12px; top:12px; background:rgba(33,29,24,.74); color:#fff;
  font-size:11px; font-weight:700; padding:2px 8px; border-radius:20px; letter-spacing:.5px; }
.slice-acts { display:flex; gap:8px; margin-top:10px; }
.slice-btn { flex:1; padding:10px 6px; border-radius:10px; font-size:13px; font-weight:600;
  border:1px solid var(--chip-border); background:var(--chip); color:var(--brand); cursor:pointer; }
.slice-btn:active { opacity:.8; }
.slice-btn.primary { background:var(--brand); border-color:var(--brand); color:#fff; }
.tip { text-align:center; color:var(--muted); font-size:12px; margin-top:8px; }
/* 大图预览：手机上长按这张大图即可存入相册 */
.lightbox { position:fixed; inset:0; z-index:99; display:none; flex-direction:column;
  align-items:center; justify-content:center; padding:18px; background:rgba(18,12,8,.94); }
.lightbox.on { display:flex; }
.lightbox img { max-width:100%; max-height:68vh; border-radius:12px; background:#fff; }
.lightbox .lb-no { color:#fff; font-size:13px; margin-top:14px; }
.lightbox .lb-save { margin-top:14px; padding:10px 22px; border-radius:24px; background:var(--brand);
  color:#fff; font-size:14px; font-weight:600; }
.lightbox .lb-nav { display:flex; gap:26px; align-items:center; margin-top:12px; color:#fff; font-size:14px; }
.lightbox .lb-nav span { padding:6px 16px; border:1px solid rgba(255,255,255,.35); border-radius:20px; cursor:pointer; }
.lightbox .lb-close { position:absolute; right:16px; top:14px; color:#fff; font-size:28px;
  line-height:1; padding:6px 10px; cursor:pointer; }
.warn { background:#fff8f1; border:1px dashed #f9d9c5; border-radius:12px; padding:12px 14px;
  font-size:12px; color:#6b6255; margin-bottom:14px; }
.foot { text-align:center; color:var(--muted); font-size:11px; margin-top:20px; }
"""

_SHEET_CSS = """
body { background:#fff; padding:0; }
.wrap { max-width:375px; margin:0 auto; padding:18px 16px 26px; background:#fff; }
.hero { background:#fff; color:var(--ink); border-radius:0; padding:0 0 14px; margin:0 0 14px;
  border-bottom:2px solid var(--line); }
.hero .brand { color:var(--brand); opacity:1; }
.hero h1 { font-size:22px; color:var(--ink); }
.hero .sub { color:var(--muted); opacity:1; }
.range { background:#fff5ee; border-color:var(--chip-border); color:var(--ink); }
.range .wk { color:var(--muted); }
.copy-btn { display:none; }
.foot { display:block; margin-top:14px; }
"""


def _meta_pills(item: WeekItem) -> str:
    """一行元信息：上榜天数 / 最好名次 / 趋势 / 新晋标。"""
    parts = [f'<span class="pill">上榜 {item.days} 天</span>',
             f'<span class="pill">最好第 {item.best_rank}</span>']
    if item.days >= 2:
        if item.trend > 0.5:
            parts.append(f'<span class="pill trend-up">升温 +{item.trend:.1f}</span>')
        elif item.trend < -0.5:
            parts.append(f'<span class="pill trend-down">降温 {item.trend:.1f}</span>')
        else:
            parts.append('<span class="pill">热度平稳</span>')
    if item.is_new:
        parts.append('<span class="pill new">本期新晋</span>')
    meta = item.meta or {}
    if meta.get("rating"):
        parts.append(f'<span class="pill">评分 {meta["rating"]}</span>')
    if meta.get("players"):
        # players 原文通常已带「人」（如 "6人（3男3女）"），再拼一个「人」会变成「6人（3男3女）人」
        text = str(meta["players"])
        parts.append(f'<span class="pill">{html.escape(text if "人" in text else text + "人")}</span>')
    return "".join(parts)


def _render_items(items: list[WeekItem]) -> str:
    if not items:
        return '<div class="warn">本期没有任何榜单数据 —— 可能整个周期都没跑成，去监听大盘确认。</div>'
    # 进度条按 **排序口径**（heat_index = 周均 × 天数占比）画，不是按周均热度：
    # 否则会出现「周均 68 的本排第 3，进度条却比第 1 名还满」的视觉矛盾。
    top_heat = max((i.heat_index for i in items), default=1.0) or 1.0
    out = []
    for i in items:
        rank_cls = f"rank rank-{i.rank}" if i.rank <= 3 else "rank"
        pct = max(6, int(i.heat_index / top_heat * 100))
        out.append(f"""
        <article class="item">
          <div class="item-head">
            <span class="{rank_cls}">{i.rank}</span>
            <span class="title">{html.escape(i.title)}</span>
            <span class="hot"><b>{i.avg_hot:.1f}</b> 周均</span>
          </div>
          <div class="bar"><i style="width:{pct}%"></i></div>
          <div class="item-meta">{_meta_pills(i)}</div>
        </article>""")
    return "".join(out)


def _render_extra(report: WeeklyReport) -> str:
    """升温榜 / 新晋 / 满勤 三个补充区块（有才显示）。"""
    parts = []
    risers = report.risers
    if risers:
        chips = "".join(
            f'<span class="chip">{html.escape(i.title)} '
            f'<b class="trend-up">+{i.trend:.1f}</b></span>'
            for i in risers
        )
        parts.append(f'<div class="section-title">🔥 升温最快</div><div class="chips">{chips}</div>')
    newcomers = report.newcomers
    if newcomers:
        chips = "".join(
            f'<span class="chip">{html.escape(i.title)}</span>' for i in newcomers[:6]
        )
        parts.append(f'<div class="section-title">🆕 本期新晋</div><div class="chips">{chips}</div>')
    regulars = report.regulars
    if regulars and len(regulars) < len(report.items):
        chips = "".join(
            f'<span class="chip">{html.escape(i.title)}</span>' for i in regulars[:6]
        )
        parts.append(
            f'<div class="section-title">🌲 全程在榜（{report.covered_days} 天满勤）</div>'
            f'<div class="chips">{chips}</div>'
        )
    return "".join(parts)


def render_week_html(
    report: WeeklyReport,
    caption: str,
    png_name: str | None = None,
    extra_pngs: list[str] | None = None,
) -> str:
    """周报页：时间范围 + 概览 + 榜单 + 海报切片 + 复制文案按钮。"""
    caption_json = json.dumps(caption, ensure_ascii=False)
    warn = f'<div class="warn">{html.escape(report.error)}</div>' if report.error else ""
    shot = ""
    if png_name:
        pngs = [png_name, *(extra_pngs or [])]
        single = " single" if len(pngs) == 1 else ""
        imgs = "".join(
            f'<figure class="slice" onclick="openLb({i - 1})">'
            f'<img src="{urllib.parse.quote(p)}" data-file="{html.escape(p)}" '
            f'alt="{html.escape(report.range_text)} 周报 {i}/{len(pngs)}">'
            f'<figcaption class="no">第 {i} 张</figcaption>'
            f"</figure>"
            for i, p in enumerate(pngs, 1)
        )
        shot = (
            f'<div class="section-title">📌 海报切片（共 {len(pngs)} 张 · 每张 3:4）</div>'
            f'<div class="shot">'
            f'<div class="slices{single}">{imgs}</div>'
            f'<div class="slice-acts">'
            f'<button class="slice-btn primary" type="button" id="saveAllBtn" '
            f'onclick="saveAllSlices(this)">⬇️ 一键保存全部（{len(pngs)} 张）</button>'
            f'<button class="slice-btn" type="button" onclick="openLb(0)">🔍 逐张预览保存</button>'
            f"</div>"
            f'<div class="tip">点任意一张可放大，长按存进相册；安卓「一键保存」会依次下载，'
            f'iPhone 请用「逐张预览」长按保存</div>'
            f"</div>"
        )
    else:
        shot = '<div class="warn">海报没生成（容器缺 Playwright 浏览器），先复制文案用。</div>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(report.range_text)} · 剧本杀热度周报</title>
<style>{_CSS}</style></head>
<body>
<script>window.__CAPTION__ = {caption_json};</script>
<div class="wrap">
  <header class="hero">
    <div class="brand">JBS · 热度周报</div>
    <h1>剧本杀热度周报</h1>
    <div class="sub">按「周均热度 × 上榜天数」排序，过滤单日波动</div>
    <div class="range">
      <b>{html.escape(report.range_text)}</b>
      <span class="wk">{html.escape(report.range_cn)} · {html.escape(_iso_week_label(report.end))}</span>
    </div>
    <div><button class="copy-btn" type="button" onclick="copyCaption(this)">📋 复制小红书文案</button></div>
  </header>
  <div class="lightbox" id="lb" onclick="closeLb()">
    <div class="lb-close">×</div>
    <img id="lbImg" src="" alt="周报海报大图" onclick="event.stopPropagation()">
    <div class="lb-no" id="lbNo"></div>
    <div class="lb-save" onclick="event.stopPropagation(); saveOne(lbIdx)">⬇️ 保存本张</div>
    <div class="lb-nav">
      <span onclick="event.stopPropagation(); lbStep(-1)">← 上一张</span>
      <span onclick="event.stopPropagation(); lbStep(1)">下一张 →</span>
    </div>
  </div>
  {warn}
  <section class="summary">
    <div><b>{len(report.items)}</b><span>本期 Top</span></div>
    <div><b>{report.unique_scripts}</b><span>上榜剧本</span></div>
    <div><b>{report.covered_days}/7</b><span>有效天数</span></div>
  </section>
  <div class="section-title">🏆 本期热度榜</div>
  {_render_items(report.items)}
  {_render_extra(report)}
  {shot}
  <div class="foot">统计口径：{html.escape(report.range_text)}（周六至周五，7 天）· 数据源 {html.escape(report.backend)}</div>
</div>
<script>
function copyCaption(btn) {{
  const text = (typeof window !== 'undefined' && window.__CAPTION__) || '';
  if (!text) {{ btn.textContent = '暂无文案'; return; }}
  const ok = function() {{ btn.textContent = '✓ 已复制'; setTimeout(function(){{ btn.textContent = '📋 复制小红书文案'; }}, 1500); }};
  const fail = function() {{ btn.textContent = '复制失败'; setTimeout(function(){{ btn.textContent = '📋 复制小红书文案'; }}, 1500); }};
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(ok).catch(fail);
  }} else {{
    const ta = document.createElement('textarea');
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try {{ document.execCommand('copy'); ok(); }} catch (err) {{ fail(); }}
    document.body.removeChild(ta);
  }}
}}

/* ---- 海报切片：大图预览 + 一键保存 ---- */
var lbIdx = 0;
function lbImages() {{ return [].slice.call(document.querySelectorAll('.slice img')); }}
function openLb(i) {{
  var imgs = lbImages();
  if (!imgs.length) return;
  lbIdx = i;
  document.getElementById('lbImg').src = imgs[i].src;
  document.getElementById('lbNo').textContent = '第 ' + (i + 1) + ' / ' + imgs.length + ' 张 · 长按图片保存到相册';
  document.getElementById('lb').classList.add('on');
  document.body.style.overflow = 'hidden';
}}
function lbStep(d) {{
  var imgs = lbImages();
  if (!imgs.length) return;
  openLb((lbIdx + d + imgs.length) % imgs.length);
}}
function closeLb() {{
  document.getElementById('lb').classList.remove('on');
  document.body.style.overflow = '';
}}
function saveOne(i) {{
  var imgs = lbImages();
  if (!imgs[i]) return;
  var a = document.createElement('a');
  a.href = imgs[i].src;
  a.download = imgs[i].getAttribute('data-file') || ('slice-' + (i + 1) + '.png');
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
}}
/* 一键保存：逐个触发 <a download>。安卓会依次下载到「下载」目录，
   iOS Safari 不支持连续下载（只会打开一张），所以页面提示 iPhone 用预览长按保存。 */
async function saveAllSlices(btn) {{
  var imgs = lbImages();
  if (!imgs.length) return;
  var label = btn.textContent;
  btn.disabled = true;
  for (var i = 0; i < imgs.length; i++) {{
    btn.textContent = '正在保存 ' + (i + 1) + '/' + imgs.length + ' …';
    saveOne(i);
    await new Promise(function(r) {{ setTimeout(r, 600); }});
  }}
  btn.textContent = '✓ 已触发 ' + imgs.length + ' 张，去相册/下载看看';
  setTimeout(function() {{ btn.textContent = label; btn.disabled = false; }}, 2500);
}}
document.addEventListener('keydown', function(e) {{
  var lb = document.getElementById('lb');
  if (!lb || !lb.classList.contains('on')) return;
  if (e.key === 'Escape') closeLb();
  if (e.key === 'ArrowLeft') lbStep(-1);
  if (e.key === 'ArrowRight') lbStep(1);
}});
/* 大图里左右滑动切换 */
(function(){{
  var x0 = null, lb = document.getElementById('lb');
  if (!lb) return;
  lb.addEventListener('touchstart', function(e) {{ x0 = e.touches[0].clientX; }}, {{passive:true}});
  lb.addEventListener('touchend', function(e) {{
    if (x0 === null) return;
    var dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 45) lbStep(dx < 0 ? 1 : -1);
    x0 = null;
  }});
}})();
</script>
</body></html>"""


def render_week_sheet(report: WeeklyReport) -> str:
    """截图底稿：白底 375px，去掉复制按钮，只留榜单本体。"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(report.range_text)} · 周报海报</title>
<style>{_CSS}{_SHEET_CSS}</style></head>
<body>
<div class="wrap">
  <header class="hero">
    <div class="brand">JBS · 热度周报</div>
    <h1>剧本杀热度周报</h1>
    <div class="sub">按「周均热度 × 上榜天数」排序</div>
    <div class="range">
      <b>{html.escape(report.range_text)}</b>
      <span class="wk">{html.escape(report.range_cn)} · {html.escape(_iso_week_label(report.end))}</span>
    </div>
  </header>
  <section class="summary">
    <div><b>{len(report.items)}</b><span>本期 Top</span></div>
    <div><b>{report.unique_scripts}</b><span>上榜剧本</span></div>
    <div><b>{report.covered_days}/7</b><span>有效天数</span></div>
  </section>
  <div class="section-title">🏆 本期热度榜</div>
  {_render_items(report.items)}
  {_render_extra(report)}
  <div class="foot">统计口径：{html.escape(report.range_text)}（周六至周五，7 天）</div>
</div>
</body></html>"""


# ─────────── 文案 ──────────────────────────────────────────────────
_TEMPLATE_CAPTION = """📅 杭州剧本杀周报 · {range_text}（{range_cn}）

这一周（有效 {covered} 天）最热的几本，周末想打本可以直接抄作业：
{lines}

挑本小 tips：
- 上榜天数比单日排名更能说明问题，满勤的基本不会踩雷；
- 标了「升温」的是后半周才开始起势的本，现在去打正好；
- 新晋本信息少，建议先看剧评再决定。

#剧本杀 #杭州剧本杀 #周报 #剧本杀推荐"""


def _template_caption(report: WeeklyReport) -> str:
    lines = []
    for i in report.items[:5]:
        flag = "🆕" if i.is_new else ""
        extra = f" · 上榜 {i.days} 天"
        if i.days >= 2 and i.trend > 0.5:
            extra += " · 升温中"
        lines.append(f"{i.rank}. {i.title}（周均 {i.avg_hot:.1f}{extra}）{flag}")
    return _TEMPLATE_CAPTION.format(
        range_text=report.range_text,
        range_cn=report.range_cn,
        covered=report.covered_days,
        lines="\n".join(lines),
    )


def gen_caption(cfg: Config, report: WeeklyReport) -> tuple[str, str]:
    """LLM 优先，失败回退模板。返回 (文案, 来源)。"""
    if not report.items:
        return f"{report.range_text} 这一周没有榜单数据。", "template"

    if cfg.llm_api_key:
        try:
            lines = []
            for i in report.items[:8]:
                flag = "新晋" if i.is_new else ""
                trend = ""
                if i.days >= 2:
                    if i.trend > 0.5:
                        trend = "升温"
                    elif i.trend < -0.5:
                        trend = "降温"
                lines.append(
                    f"{i.rank}. {i.title}｜周均热度 {i.avg_hot:.1f}｜"
                    f"上榜 {i.days} 天｜最好第 {i.best_rank} 名"
                    + (f"｜{trend}" if trend else "")
                    + (f"｜{flag}" if flag else "")
                )
            top1 = report.items[0]
            prompt = (
                f"你是小红书剧本杀垂类博主。下面是杭州剧本杀热度榜的**一周总结**：\n"
                f"统计区间：{report.range_text}（周六至周五，7 天），其中 {report.covered_days} 天有数据。\n\n"
                f"这份周报是周五发的，读者要**趁周末去玩**，所以推荐时要有「这周末就能约」的语气。\n\n"
                f"下面是本周 Top{len(lines)}，**已经按最终排名排好序，第 1 名就是本周第一**：\n"
                + "\n".join(lines)
                + "\n\n⚠️ 排序口径是「周均热度 × 上榜天数」，所以**周均热度最高的那个不一定是第一名**"
                "（它可能只上榜一两天）。写文案时必须以我上面给的序号为准，"
                f"**本周第一名是「{top1.title}」**，不要按热度自己重新排序，也不要把别的本说成榜首。\n\n"
                "写一段 250 字内的小红书文案，要求：\n"
                f"1. 第一句就点明这是哪一周的周报（{report.range_text}），别让读者猜；\n"
                "2. 点名 Top3，写清楚为什么是它们（上榜天数 / 升温 / 新晋），不要只报排名；\n"
                "3. 数据必须来自上面提供的真实统计，禁止编造热度值、天数或名次；\n"
                "4. 文末 1~2 条挑本 tips；\n"
                "5. 末尾 4~5 个 # 话题标签；\n"
                "6. 口语化、有情绪，可以直接发。\n"
                "直接输出文案，不要解释。"
            )
            resp = httpx.post(
                f"{cfg.llm_base_url}/chat/completions",
                headers={"Authorization": f"Bearer {cfg.llm_api_key}"},
                json={
                    "model": cfg.llm_model,
                    "messages": [
                        {"role": "system", "content": "你是小红书剧本杀垂类博主，文案口语化、有情绪。"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.8, "max_tokens": 600, "repetition_penalty": 1.15,
                },
                timeout=cfg.http_timeout + 30.0,
            )
            if resp.status_code == 200:
                content = (
                    (resp.json().get("choices") or [{}])[0]
                    .get("message", {}).get("content", "").strip().strip('"')
                )
                if content:
                    return content, "llm"
        except Exception as exc:  # noqa: BLE001
            logger.warning("周报文案 LLM 失败，回退模板：%s", exc)

    return _template_caption(report), "template"


# ─────────── 主流程 ────────────────────────────────────────────────
def write_weekly(
    cfg: Config,
    weeks_ago: int = 1,
    anchor: date | None = None,
    skip_png: bool = False,
) -> dict[str, Any]:
    """生成一份周报；返回产物路径与摘要。"""
    anchor = anchor or _today(cfg)
    start, end = week_range(anchor, weeks_ago=weeks_ago)
    report = build_report(cfg, start, end, top_n=cfg.top_n)

    out_dir = Path(cfg.data_dir) / "weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    key = report.key

    caption, caption_source = gen_caption(cfg, report)

    png_name = None
    png_names: list[str] = []
    if not skip_png:
        sheet = out_dir / f"{key}.sheet.html"
        sheet.write_text(render_week_sheet(report), encoding="utf-8")
        # 按小红书 3:4 切片：第一张沿用 {key}.png，后续 {key}-2.png、{key}-3.png…
        slices = screenshot_slices(
            sheet,
            out_dir / f"{key}.png",
            width=375,
            scale=2,
            anchors=".hero,.summary,.item,.section-title,.chips,.foot",
        )
        png_names = [s.name for s in slices]
        png_name = png_names[0] if png_names else None

    page = out_dir / f"{key}.html"
    page.write_text(
        render_week_html(report, caption, png_name, extra_pngs=png_names[1:]),
        encoding="utf-8",
    )

    txt = out_dir / f"{key}.txt"
    txt.write_text(caption, encoding="utf-8")

    # 元数据：给 watchdog / 列表页读，省得再去解析 HTML
    meta_path = out_dir / f"{key}.json"
    meta_path.write_text(
        json.dumps(
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "range_text": report.range_text,
                "range_cn": report.range_cn,
                "iso_week": _iso_week_label(end),
                "covered_days": report.covered_days,
                "unique_scripts": report.unique_scripts,
                "total_rows": report.total_rows,
                "backend": report.backend,
                "caption_source": caption_source,
                "pngs": png_names,
                "items": [i.to_dict() for i in report.items],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "range_text": report.range_text,
        "covered_days": report.covered_days,
        "unique_scripts": report.unique_scripts,
        "total_rows": report.total_rows,
        "ranked": len(report.items),
        "backend": report.backend,
        "page": str(page),
        "png": str(out_dir / f"{key}.png") if png_name else None,
        "caption": str(txt),
        "caption_source": caption_source,
        "ok": bool(report.items),
    }


def _today(cfg: Config) -> date:
    tz = get_tz(cfg.timezone, cfg.tz_fallback_offset)
    return datetime.now(tz).date()


def _weekly_slot(cfg: Config, now: datetime) -> datetime:
    """最近一个（含今天）周报槽位：周 weekly_day 的 weekly_at，不因过期而顺延。"""
    hour_text, minute_text = (str(cfg.weekly_at).split(":") + ["0"])[:2]
    hour, minute = int(hour_text), int(minute_text)
    target_wd = max(0, min(6, cfg.weekly_day - 1))
    delta = (now.weekday() - target_wd) % 7  # 距离上一个槽位日过去了几天
    slot_day = now.date() - timedelta(days=delta)
    return datetime.combine(slot_day, datetime.min.time()).replace(
        hour=hour, minute=minute, second=0, microsecond=0, tzinfo=now.tzinfo
    )


def weekly_due(cfg: Config, now: datetime | None = None) -> bool:
    """这一周的周报是不是「该跑却没跑」？用于容器重启后的启动补跑。

    判据：最近的槽位时间已过 **且** 那个槽位该产出的周报文件不存在。
    """
    tz = get_tz(cfg.timezone, cfg.tz_fallback_offset)
    now = now or datetime.now(tz)
    slot = _weekly_slot(cfg, now)
    if now < slot:
        return False
    expected_start = slot.date() - timedelta(days=6)  # 周期 = 截止周五往前 6 天（周六）
    # 启用日之前的周期不补跑：那时还没有「周六~周五」口径的周报，产物天然不存在
    since = cfg.weekly_since or _fallback_since(now.date())
    if expected_start.isoformat() < since:
        return False
    meta = Path(cfg.data_dir) / "weekly" / f"{expected_start.isoformat()}.json"
    return not meta.is_file()


def _fallback_since(today: date) -> str:
    """未配置 weekly_since 时的兜底：只认「本周五截止」这个周期，更早的一概不补跑。"""
    return (period_end(today, weeks_ago=1) - timedelta(days=6)).isoformat()
