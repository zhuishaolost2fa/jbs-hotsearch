# -*- coding: utf-8 -*-
"""配置加载：读环境变量 + 可选的 .env 文件（不引入 python-dotenv）。

优先级：进程环境变量 > .env > 代码默认值。
所有 Supabase / SiliconFlow 变量可直接从 jbsttj-backend/.env 拷过来用。
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


def _load_dotenv(path: Path) -> None:
    """极简 .env 解析：KEY=VALUE，忽略 # 注释与空行，不覆盖已有环境变量。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _get(key: str, default: str = "") -> str:
    return (os.environ.get(key) or default).strip()


def _get_bool(key: str, default: bool = False) -> bool:
    raw = _get(key, "")
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "y", "on")


def _get_float(key: str, default: float) -> float:
    try:
        return float(_get(key, "") or default)
    except ValueError:
        return default


def _get_int(key: str, default: int) -> int:
    try:
        return int(float(_get(key, "") or default))
    except ValueError:
        return default


@dataclass
class Config:
    # ---- 调度 ----
    run_at: str = "09:00"
    timezone: str = "Asia/Shanghai"
    tz_fallback_offset: float = 8.0
    run_on_start: bool = True

    # ---- 榜单 ----
    top_n: int = 10
    log_level: str = "INFO"
    data_dir: Path = field(default_factory=lambda: Path("./data"))

    # ---- 存储 ----
    store_backend: str = "auto"
    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # ---- 数据源：米圈 ----
    miquan_enabled: bool = True
    miquan_curls_file: str = "./data/miquan_curls.txt"
    miquan_weight: float = 1.0
    # true = 剧本榜只作元数据（评分/标签/封面/人数），热度完全由拼场决定；
    # false = 剧本榜的「平台热度」也参与打分（历史累计人气计入榜单）。
    miquan_as_metadata: bool = True

    # ---- 数据源：米圈拼场（去重后 = 有多少家店在开这个本）----
    group_enabled: bool = False
    group_curls_file: str = "./data/miquan_puzzle_curls.txt"
    group_weight: float = 0.8
    # 去重后 count = 唯一店家数，此阈值 = 至少几家店在开才算有效信号
    group_threshold: float = 2.0
    # 拼场时间窗：只统计「今天起 N 天内」开场的排期（含今天），忽略更远的未来排期。
    # 拼场接口返回未来约一个月的排期（groupOpenTime 今天→+24 天），
    # 若全量累加，刷量店能靠「连排一个月」把热度虚高。N=3 = 今天+明+后天。
    group_time_window_days: int = 3
    # 单店组局封顶：同一家店对同一剧本在时间窗内最多计 N 场组局，超过部分视为刷量
    # 不再计入（防「单店连排多场」虚高，如刷量店对同一本连排十几场）。0 = 不封顶。
    group_per_shop_cap: int = 2

    # ---- 数据源：搜索 + LLM ----
    search_provider: str = "none"
    search_api_key: str = ""
    search_queries: list[str] = field(default_factory=list)
    llm_base_url: str = "https://api.siliconflow.cn/v1"
    llm_api_key: str = ""
    llm_model: str = "Qwen/Qwen2.5-72B-Instruct"
    search_weight: float = 0.8

    # ---- 融合打分权重 ----
    cross_source_boost: float = 0.15
    recency_boost: float = 0.10

    # ---- 已解析过滤 ----
    filter_parsed_enabled: bool = True
    filter_buffer_multiplier: int = 3

    # ---- 监听大盘（watch）----
    watch_host: str = "127.0.0.1"
    watch_port: int = 8787
    watch_days: int = 30               # 大盘回看窗口
    watch_grace_minutes: int = 60      # 过了计划时间 + 宽限还没有记录 = 判「今天没跑」
    watch_refresh_seconds: int = 60    # 页面自动刷新间隔
    watch_cache_seconds: int = 20      # 服务端回源 Supabase 的最小间隔
    watch_webhook_url: str = ""        # 出问题时的告警出口（POST JSON），留空则不告警

    # ---- 大盘上的其它任务（jbsttj-backend / 前端站点，共用同一个 Supabase）----
    watch_site_origin: str = "https://www.jbs-ttj.store"   # SEO / GEO 产物的线上域名
    watch_dm_enabled: bool = True      # DM 手册解析（script_dm_jobs）
    watch_seo_enabled: bool = True     # SEO / GEO 静态产物（sitemap / llms.txt / feed）
    # 中间态任务卡住多久算「僵尸」（Celery worker 挂了就会留下一堆卡住的任务）
    watch_stuck_hours: int = 2
    # 探测线上产物的单个请求超时（5 个产物并发探测，总耗时≈这一个值）
    watch_http_timeout: float = 6.0

    # ---- 小红书素材（海报截图 + LLM 文案）----
    social_enabled: bool = True
    social_poster_width: int = 1080
    social_poster_height: int = 1440
    social_poster_scale: int = 2
    # 海报底部是否追加「已解析·未入榜」区块（DM 手册已在库、被本轮剔除的剧本）
    social_show_parsed: bool = True
    # 该区块最多展示几本，超出折叠成「等 N 本」
    social_parsed_limit: int = 6

    @property
    def http_timeout(self) -> float:
        return 30.0

    @classmethod
    def load(cls, dotenv_path: Path | None = None) -> "Config":
        project_root = Path(__file__).resolve().parents[2]
        _load_dotenv(dotenv_path or project_root / ".env")

        cfg = cls(
            run_at=_get("HS_RUN_AT", "09:00"),
            timezone=_get("HS_TIMEZONE", "Asia/Shanghai"),
            run_on_start=_get_bool("HS_RUN_ON_START", True),
            top_n=_get_int("HS_TOP_N", 10),
            log_level=_get("HS_LOG_LEVEL", "INFO").upper(),
            data_dir=Path(_get("HS_DATA_DIR", "./data")),
            store_backend=_get("HS_STORE_BACKEND", "auto").lower(),
            # 兼容 jbsttj-backend 的变量名
            supabase_url=_get("SUPABASE_URL"),
            supabase_service_role_key=(
                _get("SUPABASE_SERVICE_ROLE_KEY") or _get("SUPABASE_SERVICE_KEY")
            ),
            miquan_enabled=_get_bool("HS_SOURCE_MIQUAN_ENABLED", True),
            miquan_curls_file=_get("HS_SOURCE_MIQUAN_CURLS_FILE", "./data/miquan_curls.txt"),
            miquan_weight=_get_float("HS_SOURCE_MIQUAN_WEIGHT", 1.0),
            miquan_as_metadata=_get_bool("HS_SOURCE_MIQUAN_AS_METADATA", True),
            group_enabled=_get_bool("HS_SOURCE_MIQUAN_GROUP_ENABLED", False),
            group_curls_file=_get("HS_SOURCE_MIQUAN_GROUP_CURLS_FILE", "./data/miquan_puzzle_curls.txt"),
            group_weight=_get_float("HS_SOURCE_MIQUAN_GROUP_WEIGHT", 0.8),
            group_threshold=_get_float("HS_SOURCE_MIQUAN_GROUP_THRESHOLD", 2.0),
            group_time_window_days=_get_int("HS_SOURCE_MIQUAN_GROUP_TIME_WINDOW_DAYS", 3),
            group_per_shop_cap=_get_int("HS_SOURCE_MIQUAN_GROUP_PER_SHOP_CAP", 2),
            search_provider=_get("HS_SEARCH_PROVIDER", "none").lower(),
            search_api_key=_get("HS_SEARCH_API_KEY"),
            search_queries=[
                q.strip()
                for q in re.split(r"[|\n]", _get("HS_SEARCH_QUERIES"))
                if q.strip()
            ],
            llm_base_url=_get("HS_LLM_BASE_URL", "https://api.siliconflow.cn/v1"),
            llm_api_key=_get("HS_LLM_API_KEY") or _get("SILICONFLOW_API_KEY"),
            llm_model=_get("HS_LLM_MODEL", "Qwen/Qwen2.5-72B-Instruct"),
            search_weight=_get_float("HS_SOURCE_SEARCH_WEIGHT", 0.8),
            cross_source_boost=_get_float("HS_CROSS_SOURCE_BOOST", 0.15),
            recency_boost=_get_float("HS_RECENCY_BOOST", 0.10),
            filter_parsed_enabled=_get_bool("HS_FILTER_PARSED_ENABLED", True),
            filter_buffer_multiplier=_get_int("HS_FILTER_BUFFER_MULTIPLIER", 3),
            social_enabled=_get_bool("HS_SOCIAL_ENABLED", True),
            social_poster_width=_get_int("HS_SOCIAL_POSTER_WIDTH", 1080),
            social_poster_height=_get_int("HS_SOCIAL_POSTER_HEIGHT", 1440),
            social_poster_scale=_get_int("HS_SOCIAL_POSTER_SCALE", 2),
            social_show_parsed=_get_bool("HS_SOCIAL_SHOW_PARSED", True),
            social_parsed_limit=_get_int("HS_SOCIAL_PARSED_LIMIT", 6),
            watch_host=_get("HS_WATCH_HOST", "127.0.0.1"),
            watch_port=_get_int("HS_WATCH_PORT", 8787),
            watch_days=_get_int("HS_WATCH_DAYS", 30),
            watch_grace_minutes=_get_int("HS_WATCH_GRACE_MINUTES", 60),
            watch_refresh_seconds=_get_int("HS_WATCH_REFRESH", 60),
            watch_cache_seconds=_get_int("HS_WATCH_CACHE_SECONDS", 20),
            watch_webhook_url=_get("HS_WATCH_WEBHOOK_URL"),
            watch_site_origin=_get("HS_WATCH_SITE_ORIGIN", "https://www.jbs-ttj.store").rstrip("/"),
            watch_dm_enabled=_get_bool("HS_WATCH_DM_ENABLED", True),
            watch_seo_enabled=_get_bool("HS_WATCH_SEO_ENABLED", True),
            watch_stuck_hours=_get_int("HS_WATCH_STUCK_HOURS", 2),
            watch_http_timeout=_get_float("HS_WATCH_HTTP_TIMEOUT", 10.0),
        )
        # 剧本榜降为元数据：热度完全由拼场决定，剧本榜只提供展示字段。
        # 通过把 miquan 源权重置 0 实现（rank.py 里 weight=0 的源不参与打分）。
        if cfg.miquan_as_metadata:
            cfg.miquan_weight = 0.0

        # 相对路径按「运行时当前目录」解析，而不是包安装位置：
        # 非 editable 安装时包躺在 site-packages 里，那里既不该写数据也通常不可写。
        if cfg.data_dir and not cfg.data_dir.is_absolute():
            cfg.data_dir = Path.cwd() / cfg.data_dir
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg
