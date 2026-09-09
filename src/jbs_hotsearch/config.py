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

    # ---- 数据源：米圈拼场（组局频次 = 实时约本热度）----
    group_enabled: bool = False
    group_curls_file: str = "./data/miquan_puzzle_curls.txt"
    group_weight: float = 0.8
    group_threshold: float = 3.0

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
            group_enabled=_get_bool("HS_SOURCE_MIQUAN_GROUP_ENABLED", False),
            group_curls_file=_get("HS_SOURCE_MIQUAN_GROUP_CURLS_FILE", "./data/miquan_puzzle_curls.txt"),
            group_weight=_get_float("HS_SOURCE_MIQUAN_GROUP_WEIGHT", 1.2),
            group_threshold=_get_float("HS_SOURCE_MIQUAN_GROUP_THRESHOLD", 5.0),
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
        )
        # 相对路径按「运行时当前目录」解析，而不是包安装位置：
        # 非 editable 安装时包躺在 site-packages 里，那里既不该写数据也通常不可写。
        if cfg.data_dir and not cfg.data_dir.is_absolute():
            cfg.data_dir = Path.cwd() / cfg.data_dir
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg
