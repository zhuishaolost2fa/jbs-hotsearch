# -*- coding: utf-8 -*-
"""监听大盘的数据层：把各处的运行留痕拉回来，按天回答「今天到底跑没跑成」。

盯的三类任务，性质完全不同，所以判定规则也不一样：

| 任务 | 留痕在哪 | 性质 |
|---|---|---|
| `hotsearch` 每日热门榜 | Supabase `script_hot_runs` | **每天必须跑**，没跑 = 缺跑 |
| `dm_ingest` DM 手册解析 | Supabase `script_dm_jobs` | **按需触发**，没任务很正常；要抓的是失败与卡住 |
| `seo_geo` SEO / GEO 产物 | 线上静态文件（sitemap / llms.txt …） | **每次部署生成**，看产物在不在、新不新 |

设计取舍：
  - **只用标准库 urllib**。监控必须能在「依赖还没装 / 环境刚崩过」的机器上跑起来 —— 那正是最需要看它的时候。
  - Supabase 拿不到就降级读本地 SQLite，保证大盘自己不会白屏。
  - **「查不到」不等于「挂了」**：网络探测失败记为 `unknown`（灰），绝不误报成红色失败。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Config
from .tz_util import get_tz

logger = logging.getLogger(__name__)

RUN_TABLE = "script_hot_runs"
DAILY_TABLE = "script_hot_daily"
DM_JOB_TABLE = "script_dm_jobs"

# ----------------------------------------------------------------------------
# 状态等级
# ----------------------------------------------------------------------------
LEVEL_OK = "ok"            # 成功
LEVEL_WARN = "warn"        # 跑完了但可疑（0 条 / 部分源失败）
LEVEL_BAD = "bad"          # 明确失败
LEVEL_MISSING = "missing"  # 该跑没跑
LEVEL_PENDING = "pending"  # 还没到计划时间
LEVEL_IDLE = "idle"        # 这天本来就没任务（按需型任务的常态，不是异常）
LEVEL_RUNNING = "running"  # 有任务在跑
LEVEL_STUCK = "stuck"      # 卡在中间态太久（worker 八成挂了）
LEVEL_UNKNOWN = "unknown"  # 探测失败，拿不到结论

ALERT_LEVELS = (LEVEL_BAD, LEVEL_MISSING, LEVEL_WARN, LEVEL_STUCK)

# DM 任务的中间态：停在任何一个超过阈值 = 卡住
DM_ACTIVE_STATUSES = {
    "pending", "downloading", "extracting", "chunking",
    "generating_qa", "embedding",
}
DM_FAILED = {"failed"}
DM_DONE = {"completed"}
DM_IGNORED = {"cancelled", "skipped"}

# SEO / GEO 线上产物：(路径, 应包含的关键字, 说明)
SEO_ASSETS: list[tuple[str, str, str]] = [
    ("/robots.txt", "Sitemap:", "爬虫入口，指向 sitemap"),
    ("/sitemap.xml", "<urlset", "可索引 URL 清单"),
    ("/llms.txt", "# ", "GEO：给大模型的站点索引"),
    ("/llms-full.txt", "# ", "GEO：全文版，AI 最易消化"),
    ("/feed.xml", "<rss", "RSS 推送流"),
]


class WatchError(RuntimeError):
    """拉不到任何运行日志（Supabase 不通 + 本地也没数据）。"""


# ----------------------------------------------------------------------------
# 数据结构
# ----------------------------------------------------------------------------
@dataclass
class DayStatus:
    """某一天（或某一次）的运行结论。"""

    date: str
    level: str
    status: str
    runs: int = 0
    ok: int = 0                        # 当天成功次数
    failed: int = 0                    # 当天失败次数
    item_count: int | None = None
    duration_ms: int | None = None
    finished_at: str | None = None
    sources: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    daily_rows: int | None = None
    detail: str | None = None          # 额外说明（如 "3 完成 / 1 失败"）

    @property
    def note(self) -> str:
        if self.error:
            return self.error
        if self.level == LEVEL_MISSING:
            return "这天没有运行记录"
        if self.level == LEVEL_PENDING:
            return "还没到今天的计划时间"
        if self.level == LEVEL_IDLE:
            return "这天没有解析任务（正常）"
        if self.level == LEVEL_RUNNING:
            return "有任务正在跑"
        if self.level == LEVEL_STUCK:
            return "有任务卡在中间态太久"
        if self.level == LEVEL_UNKNOWN:
            return "探测失败，拿不到结论"
        if self.detail:
            return self.detail
        if self.level == LEVEL_WARN:
            if not self.item_count:
                return "跑完了但一条都没写出来"
            return "部分数据源失败"
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "level": self.level,
            "status": self.status,
            "runs": self.runs,
            "ok": self.ok,
            "failed": self.failed,
            "item_count": self.item_count,
            "duration_ms": self.duration_ms,
            "finished_at": self.finished_at,
            "sources": self.sources,
            "error": self.error,
            "daily_rows": self.daily_rows,
            "detail": self.detail,
            "note": self.note,
        }


@dataclass
class AssetCheck:
    """一个线上静态产物的体检结果。"""

    path: str                      # 相对路径，如 /llms.txt
    desc: str = ""
    level: str = LEVEL_UNKNOWN
    http_status: int | None = None
    size: int | None = None
    last_modified: str | None = None
    note: str = ""
    url: str = ""                  # 完整地址，点开就能看

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "url": self.url,
            "desc": self.desc,
            "level": self.level,
            "http_status": self.http_status,
            "size": self.size,
            "last_modified": self.last_modified,
            "note": self.note,
        }


@dataclass
class SourceHealth:
    name: str
    ok_days: int = 0
    fail_days: int = 0
    last_error: str | None = None
    last_items: int | None = None

    @property
    def total(self) -> int:
        return self.ok_days + self.fail_days

    @property
    def rate(self) -> float:
        return (self.ok_days / self.total) if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok_days": self.ok_days,
            "fail_days": self.fail_days,
            "rate": round(self.rate, 4),
            "last_error": self.last_error,
            "last_items": self.last_items,
        }


@dataclass
class TaskBoard:
    """一块任务的看板。

    kind:
      daily    每天必须跑（缺跑算异常）        → hotsearch
      ondemand 按需触发，没任务是常态          → dm_ingest
      asset    不按天，看产物在不在            → seo_geo
    """

    key: str
    name: str
    kind: str = "daily"
    desc: str = ""
    items: list[DayStatus] = field(default_factory=list)
    assets: list[AssetCheck] = field(default_factory=list)
    sources: list[SourceHealth] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    plan_at: str | None = None
    grace_minutes: int | None = None
    next_run_at: str | None = None

    @property
    def expect_daily(self) -> bool:
        return self.kind == "daily"

    @property
    def today_status(self) -> DayStatus | None:
        if not self.items:
            return None
        return self.items[-1]

    @property
    def tracked(self) -> list[DayStatus]:
        """参与统计的日子。

        排除 pending（还没到点）、idle（本来就没任务）与 unknown（探测失败）——
        尤其 unknown：拉不到数据不等于任务失败，把它算进分母会把成功率凭空拉低。
        """
        skip = (LEVEL_PENDING, LEVEL_IDLE, LEVEL_UNKNOWN)
        return [i for i in self.items if i.level not in skip]

    @property
    def success_days(self) -> int:
        return sum(1 for i in self.tracked if i.level == LEVEL_OK)

    @property
    def success_rate(self) -> float:
        tracked = self.tracked
        return (self.success_days / len(tracked)) if tracked else 0.0

    @property
    def streak(self) -> int:
        count = 0
        for item in reversed(self.items):
            if item.level == LEVEL_OK:
                count += 1
            elif item.level in (LEVEL_PENDING, LEVEL_IDLE):
                continue  # 未到点 / 无任务不算断
            else:
                break
        return count

    @property
    def avg_duration_ms(self) -> int | None:
        values = [i.duration_ms for i in self.tracked if i.duration_ms]
        return int(sum(values) / len(values)) if values else None

    @property
    def alerts(self) -> list[DayStatus]:
        return [i for i in self.items if i.level in ALERT_LEVELS]

    @property
    def asset_alerts(self) -> list[AssetCheck]:
        return [a for a in self.assets if a.level in (LEVEL_BAD, LEVEL_WARN)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "desc": self.desc,
            "error": self.error,
            "plan_at": self.plan_at,
            "grace_minutes": self.grace_minutes,
            "next_run_at": self.next_run_at,
            "metrics": self.metrics,
            "summary": {
                "success_rate": round(self.success_rate, 4),
                "success_days": self.success_days,
                "tracked_days": len(self.tracked),
                "streak": self.streak,
                "avg_duration_ms": self.avg_duration_ms,
                "alert_days": len(self.alerts),
                "asset_alerts": len(self.asset_alerts),
            },
            "items": [i.to_dict() for i in self.items],
            "assets": [a.to_dict() for a in self.assets],
            "sources": [s.to_dict() for s in self.sources],
        }


@dataclass
class Snapshot:
    """一次大盘快照：所有渲染需要的字段都在这里，HTML 层不碰 IO。"""

    generated_at: str
    timezone: str
    today: str
    days: int
    backend: str
    boards: list[TaskBoard] = field(default_factory=list)
    error: str | None = None

    def board(self, key: str) -> TaskBoard | None:
        for board in self.boards:
            if board.key == key:
                return board
        return None

    @property
    def alerts(self) -> list[tuple[TaskBoard, DayStatus]]:
        out: list[tuple[TaskBoard, DayStatus]] = []
        for board in self.boards:
            out.extend((board, item) for item in board.alerts)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "timezone": self.timezone,
            "today": self.today,
            "days": self.days,
            "backend": self.backend,
            "error": self.error,
            "boards": [b.to_dict() for b in self.boards],
        }


# ----------------------------------------------------------------------------
# 取数：Supabase
# ----------------------------------------------------------------------------
def _supabase_get(cfg: Config, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
    if not (cfg.supabase_url and cfg.supabase_service_role_key):
        raise WatchError("缺 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY")
    url = f"{cfg.supabase_url.rstrip('/')}/rest/v1/{table}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "apikey": cfg.supabase_service_role_key,
            "Authorization": f"Bearer {cfg.supabase_service_role_key}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg.http_timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")[:300]
        if "PGRST205" in body or "does not exist" in body:
            raise WatchError(
                f"{table} 不存在：去 Supabase Dashboard -> SQL Editor 执行对应的建表 SQL"
            ) from exc
        raise WatchError(f"Supabase 读取失败 {exc.code}：{body}") from exc
    except urllib.error.URLError as exc:
        raise WatchError(f"连不上 Supabase：{exc.reason}") from exc
    except OSError as exc:
        # 关键：读响应体阶段的超时抛 socket.timeout / TimeoutError，**不是** URLError。
        # 少了这一层，网络一抖大盘就直接 500，连「降级本地 SQLite」的兜底都跑不到。
        raise WatchError(f"读 Supabase 超时或连接中断：{type(exc).__name__}: {exc}") from exc


# ----------------------------------------------------------------------------
# 取数：本地 SQLite 兜底（只有 hotsearch 有本地库）
# ----------------------------------------------------------------------------
def _local_db_path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "hotsearch.db"


def _local_runs(cfg: Config, since: str) -> list[dict[str, Any]]:
    path = _local_db_path(cfg)
    if not path.is_file():
        return []
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "select board_date, started_at, status, duration_ms, item_count, error, payload"
            " from script_hot_runs where board_date >= ? order by board_date desc, id desc",
            (since,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            sources = json.loads(row["payload"] or "[]")
        except json.JSONDecodeError:
            sources = []
        out.append(
            {
                "board_date": row["board_date"],
                "started_at": row["started_at"],
                "finished_at": row["started_at"],
                "status": row["status"],
                "duration_ms": row["duration_ms"],
                "item_count": row["item_count"],
                "source_status": sources,
                "store": "local",
                "error": row["error"],
            }
        )
    return out


def _local_daily_counts(cfg: Config, since: str) -> dict[str, int]:
    path = _local_db_path(cfg)
    if not path.is_file():
        return {}
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "select board_date, count(*) c from script_hot_daily where board_date >= ? group by board_date",
            (since,),
        ).fetchall()
    return {row[0]: row[1] for row in rows}


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def _parse_dt(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def _ts_key(row: dict[str, Any]) -> tuple[str, str]:
    return (str(row.get("finished_at") or ""), str(row.get("started_at") or ""))


def _group_by_day(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """同一天可能跑多次：取**最后一次**作为当天结论，并记下次数。"""
    grouped: dict[str, dict[str, Any]] = {}
    for row in runs:
        day = str(row.get("board_date") or "")
        if not day:
            continue
        bucket = grouped.setdefault(day, {"last": row, "runs": 0})
        bucket["runs"] += 1
        if _ts_key(row) > _ts_key(bucket["last"]):
            bucket["last"] = row
    return grouped


def _source_health(items: list[DayStatus]) -> list[SourceHealth]:
    stats: dict[str, SourceHealth] = {}
    for item in reversed(items):
        if item.level == LEVEL_MISSING or not item.sources:
            continue
        for src in item.sources:
            name = str(src.get("source") or "unknown")
            health = stats.setdefault(name, SourceHealth(name=name))
            if src.get("ok"):
                health.ok_days += 1
            else:
                health.fail_days += 1
                if src.get("error"):
                    health.last_error = str(src["error"])
            if src.get("items") is not None:
                health.last_items = int(src["items"])
    return sorted(stats.values(), key=lambda s: (s.rate, s.name))


def _next_run_label(cfg: Config, now: datetime) -> str | None:
    try:
        from .scheduler import next_run

        return next_run(cfg, now).strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception as exc:  # noqa: BLE001
        logger.debug("计算下次运行时间失败：%s", exc)
        return None


# ----------------------------------------------------------------------------
# 任务一：每日热门榜
# ----------------------------------------------------------------------------
def classify_hotsearch(row: dict[str, Any] | None) -> tuple[str, str]:
    if row is None:
        return LEVEL_MISSING, "missing"
    status = (row.get("status") or "").lower()
    item_count = row.get("item_count")
    if status == "failed":
        return LEVEL_BAD, "failed"
    if status == "partial":
        return LEVEL_WARN, "partial"
    # 标了 success 但一条都没写出来 —— 比报错更危险，因为没人会发现
    if status == "success" and not item_count:
        return LEVEL_WARN, "success"
    if status in ("success", "ok"):
        return LEVEL_OK, "success"
    return LEVEL_WARN, status or "unknown"


def build_hotsearch_board(cfg: Config, days: int, since: str, today: str,
                          now: datetime, tz) -> TaskBoard:
    board = TaskBoard(
        key="hotsearch",
        name="每日热门榜",
        kind="daily",
        desc="每天出一次 Top10 榜单，写 Supabase",
        plan_at=cfg.run_at,
        grace_minutes=cfg.watch_grace_minutes,
        next_run_at=_next_run_label(cfg, now),
    )

    runs: list[dict[str, Any]] = []
    daily_counts: dict[str, int] = {}
    try:
        runs = _supabase_get(
            cfg, RUN_TABLE,
            {
                "select": "board_date,started_at,finished_at,status,duration_ms,item_count,source_status,store,error",
                "board_date": f"gte.{since}",
                "order": "board_date.desc,started_at.desc",
                "limit": "1000",
            },
        )
        daily_counts = {}
        rows = _supabase_get(
            cfg, DAILY_TABLE,
            {"select": "board_date", "board_date": f"gte.{since}", "limit": "5000"},
        )
        for row in rows:
            day = row.get("board_date")
            if day:
                daily_counts[day] = daily_counts.get(day, 0) + 1
    except WatchError as exc:
        logger.warning("Supabase 读取失败，降级本地 SQLite：%s", exc)
        runs = _local_runs(cfg, since)
        daily_counts = _local_daily_counts(cfg, since)
        board.error = f"{exc}；已降级读本地 SQLite"

    grouped = _group_by_day(runs)

    # 今天是否「该跑没跑」：过了计划时间 + 宽限期还没记录
    try:
        plan_hour, plan_minute = (str(cfg.run_at).split(":") + ["0"])[:2]
        plan_dt = now.replace(hour=int(plan_hour), minute=int(plan_minute), second=0, microsecond=0)
    except ValueError:
        plan_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
    today_pending = today not in grouped and now < plan_dt + timedelta(minutes=cfg.watch_grace_minutes)

    today_date = datetime.fromisoformat(today).date()
    for offset in range(days - 1, -1, -1):
        day = (today_date - timedelta(days=offset)).isoformat()
        bucket = grouped.get(day)
        if bucket is None:
            level = LEVEL_PENDING if (offset == 0 and today_pending) else LEVEL_MISSING
            board.items.append(DayStatus(date=day, level=level, status=level))
            continue
        row = bucket["last"]
        level, status = classify_hotsearch(row)
        sources = row.get("source_status") or []
        if not isinstance(sources, list):
            sources = []
        board.items.append(
            DayStatus(
                date=day, level=level, status=status, runs=bucket["runs"],
                item_count=row.get("item_count"), duration_ms=row.get("duration_ms"),
                finished_at=row.get("finished_at") or row.get("started_at"),
                sources=sources, error=row.get("error"),
                daily_rows=daily_counts.get(day),
            )
        )

    board.sources = _source_health(board.items)
    return board


# ----------------------------------------------------------------------------
# 任务二：DM 手册解析（按需触发，没任务是常态）
# ----------------------------------------------------------------------------
def build_dm_board(cfg: Config, days: int, today: str, now: datetime, tz) -> TaskBoard:
    board = TaskBoard(
        key="dm_ingest",
        name="DM 手册解析",
        kind="ondemand",
        desc="上传手册才触发：PDF 提取 → 分块去重 → 生成问答 → 向量化入库",
    )

    today_date = datetime.fromisoformat(today).date()

    try:
        # 窗口内的任务：按 created_at 过滤（UTC 比较，多取一天避免时区边界漏掉）
        jobs = _supabase_get(
            cfg, DM_JOB_TABLE,
            {
                "select": "id,script_code,status,created_at,finished_at,error_message,stage_detail,"
                          "total_pages,total_chunks,total_qa",
                "created_at": f"gte.{(today_date - timedelta(days=days)).isoformat()}",
                "order": "created_at.desc",
                "limit": "1000",
            },
        )
    except WatchError as exc:
        board.error = str(exc)
        board.items = [
            DayStatus(date=(today_date - timedelta(days=o)).isoformat(),
                      level=LEVEL_UNKNOWN, status="unknown")
            for o in range(days - 1, -1, -1)
        ]
        return board

    # 中间态任务才是重点：窗口外的老僵尸也要抓出来（worker 挂了会留下跨天的卡死任务）
    stuck_rows: list[dict[str, Any]] = []
    try:
        active = ",".join(sorted(DM_ACTIVE_STATUSES))
        stuck_rows = _supabase_get(
            cfg, DM_JOB_TABLE,
            {
                "select": "id,script_code,status,created_at,stage_detail,total_pages",
                "status": f"in.({active})",
                "order": "created_at.asc",
                "limit": "200",
            },
        )
    except WatchError as exc:
        logger.debug("查询卡住的任务失败：%s", exc)

    # 按**本地时区**的日期分组（created_at 是 UTC，直接截字符串会错 8 小时）
    by_day: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        dt = _parse_dt(job.get("created_at"))
        if not dt:
            continue
        day = dt.astimezone(tz).date().isoformat()
        by_day.setdefault(day, []).append(job)

    for offset in range(days - 1, -1, -1):
        day = (today_date - timedelta(days=offset)).isoformat()
        rows = by_day.get(day) or []
        if not rows:
            board.items.append(DayStatus(date=day, level=LEVEL_IDLE, status="idle"))
            continue

        done = sum(1 for j in rows if j.get("status") in DM_DONE)
        failed = [j for j in rows if j.get("status") in DM_FAILED]
        active = [j for j in rows if j.get("status") in DM_ACTIVE_STATUSES]
        chunks = sum(int(j.get("total_chunks") or 0) for j in rows if j.get("status") in DM_DONE)
        qa = sum(int(j.get("total_qa") or 0) for j in rows if j.get("status") in DM_DONE)
        last = rows[0]
        detail = f"{done} 完成 / {len(failed)} 失败"
        if active:
            detail += f" / {len(active)} 进行中"
        if chunks or qa:
            detail += f" · {chunks} 块 / {qa} 问答"

        if failed:
            first = failed[0]
            msg = (first.get("error_message") or "解析失败").strip()
            code = first.get("script_code") or "?"
            if len(failed) > 1:
                msg = f"{len(failed)} 个失败，首个：{code} {msg}"
            else:
                msg = f"{code}：{msg}"
            board.items.append(
                DayStatus(date=day, level=LEVEL_BAD, status="failed", runs=len(rows),
                          ok=done, failed=len(failed), item_count=done,
                          finished_at=last.get("finished_at") or last.get("created_at"),
                          error=msg[:300], detail=detail)
            )
            continue
        if active:
            board.items.append(
                DayStatus(date=day, level=LEVEL_RUNNING, status="running", runs=len(rows),
                          ok=done, failed=0, item_count=done,
                          finished_at=last.get("created_at"), detail=detail)
            )
            continue
        board.items.append(
            DayStatus(date=day, level=LEVEL_OK, status="completed", runs=len(rows),
                      ok=done, failed=0, item_count=done,
                      finished_at=last.get("finished_at") or last.get("created_at"),
                      detail=detail)
        )

    # 僵尸任务：中间态停太久 —— 单独标记到「今天」，因为它是「现在」的问题
    cutoff = now - timedelta(hours=cfg.watch_stuck_hours)
    zombies = []
    for row in stuck_rows:
        dt = _parse_dt(row.get("created_at"))
        if dt and dt.astimezone(tz) < cutoff:
            zombies.append(row)
    if zombies:
        oldest = min(zombies, key=lambda r: str(r.get("created_at")))
        age = now - (_parse_dt(oldest.get("created_at")) or now).astimezone(tz)
        today_item = board.items[-1] if board.items else None
        if today_item and today_item.level == LEVEL_IDLE:
            today_item.level = LEVEL_STUCK
            today_item.status = "stuck"
            today_item.error = (
                f"{len(zombies)} 个任务卡在中间态，最老的已 {age.total_seconds() / 3600:.1f} 小时"
                f"（{oldest.get('script_code') or '?'} · {oldest.get('status')}）"
                " —— Celery worker 可能没在消费"
            )
    board.metrics = {
        "stuck": len(zombies),
        "window_jobs": len(jobs),
        "window_done": sum(1 for j in jobs if j.get("status") in DM_DONE),
        "window_failed": sum(1 for j in jobs if j.get("status") in DM_FAILED),
    }
    return board


# ----------------------------------------------------------------------------
# 任务三：SEO / GEO 静态产物（每次部署生成，看产物在不在）
# ----------------------------------------------------------------------------
def _probe_asset(cfg: Config, origin: str, path: str, marker: str, desc: str) -> AssetCheck:
    check = AssetCheck(path=path, desc=desc, url=f"{origin}{path}")
    url = f"{origin}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "jbs-watch/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=cfg.watch_http_timeout) as resp:
            # 只读前 64KB：够判断内容对不对，又不会把 llms-full.txt 整个拉下来
            body = resp.read(65536).decode("utf-8", "ignore")
            check.http_status = resp.status
            check.size = len(body)
            check.last_modified = resp.headers.get("Last-Modified")
            if not body.strip():
                check.level, check.note = LEVEL_WARN, "返回了 200 但内容是空的"
            elif marker and marker not in body:
                check.level = LEVEL_WARN
                check.note = f"内容里没找到预期标记 {marker!r}，可能生成错了"
            else:
                check.level, check.note = LEVEL_OK, f"{len(body)} 字节"
    except urllib.error.HTTPError as exc:
        check.http_status = exc.code
        check.level = LEVEL_BAD if exc.code == 404 else LEVEL_WARN
        check.note = "产物不存在（404）—— 这次构建可能没生成它" if exc.code == 404 else f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        # 网络不通 ≠ 生成失败。记 unknown，避免把「我查不到」误报成「它挂了」
        check.level = LEVEL_UNKNOWN
        check.note = f"探测失败：{type(exc).__name__}: {exc}"
    return check


def build_seo_board(cfg: Config, days: int, today: str) -> TaskBoard:
    board = TaskBoard(
        key="seo_geo",
        name="SEO / GEO 产物",
        kind="asset",
        desc=f"前端构建期生成，部署在 {cfg.watch_site_origin}",
    )
    origin = cfg.watch_site_origin
    # 并发探测：串行的话 5 个产物 × 超时 能把页面拖到几十秒才出来
    with ThreadPoolExecutor(max_workers=len(SEO_ASSETS)) as pool:
        futures = [
            pool.submit(_probe_asset, cfg, origin, path, marker, desc)
            for path, marker, desc in SEO_ASSETS
        ]
        board.assets = [f.result() for f in futures]

    bad = [a for a in board.assets if a.level == LEVEL_BAD]
    warn = [a for a in board.assets if a.level == LEVEL_WARN]
    unknown = [a for a in board.assets if a.level == LEVEL_UNKNOWN]

    if unknown and not (bad or warn):
        level = LEVEL_UNKNOWN
    elif bad:
        level = LEVEL_BAD
    elif warn:
        level = LEVEL_WARN
    else:
        level = LEVEL_OK

    note = ""
    if bad:
        note = f"{len(bad)} 个产物缺失：" + "、".join(a.path for a in bad)
    elif warn:
        note = f"{len(warn)} 个产物可疑：" + "、".join(a.path for a in warn)
    elif unknown:
        note = f"{len(unknown)} 个产物探测失败（网络不通，不代表生成失败）"
    board.items = [DayStatus(date=today, level=level, status=level, error=note or None)]
    board.metrics = {
        "origin": origin,
        "total": len(board.assets),
        "ok": sum(1 for a in board.assets if a.level == LEVEL_OK),
        "unknown": len(unknown),
    }
    return board


# ----------------------------------------------------------------------------
# 组装
# ----------------------------------------------------------------------------
def build_snapshot(cfg: Config, days: int = 30, grace_minutes: int = 60) -> Snapshot:
    tz = get_tz(cfg.timezone, cfg.tz_fallback_offset)
    now = datetime.now(tz)
    today = now.date().isoformat()
    since = (now.date() - timedelta(days=days - 1)).isoformat()

    # 三块任务互不依赖，并发拉：总耗时 = 最慢的那一个，而不是三者之和
    jobs: list[tuple[str, Any, tuple]] = [
        ("hotsearch", build_hotsearch_board, (cfg, days, since, today, now, tz)),
    ]
    if cfg.watch_dm_enabled:
        jobs.append(("dm_ingest", build_dm_board, (cfg, days, today, now, tz)))
    if cfg.watch_seo_enabled:
        jobs.append(("seo_geo", build_seo_board, (cfg, days, today)))

    if len(jobs) == 1:
        boards = [jobs[0][1](*jobs[0][2])]
    else:
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = [(key, pool.submit(fn, *args)) for key, fn, args in jobs]
            boards = []
            for key, future in futures:
                try:
                    boards.append(future.result())
                except Exception as exc:  # noqa: BLE001 - 一块挂了不能拖垮整个大盘
                    logger.warning("采集 %s 失败：%s", key, exc)
                    boards.append(
                        TaskBoard(key=key, name=key, error=f"采集失败：{exc}")
                    )

    hot = boards[0]
    snap = Snapshot(
        generated_at=now.isoformat(timespec="seconds"),
        timezone=cfg.timezone,
        today=today,
        days=days,
        backend="local" if (hot.error and "降级" in hot.error) else "supabase",
        boards=boards,
        error=hot.error,
    )
    return snap
