# -*- coding: utf-8 -*-
"""已解析过滤：把「jbsttj-backend 已经解析入库」的剧本从热门榜里剔除。

业务动机（用户原话）：这个榜不是「全网最火」，而是「当下最火、且还没被解析过」——
用来给「下一步解析哪个剧本」做优先级排序，已经解析过的（DM 手册已在库）就不该再占坑。

「已解析」的判据沿用 jbsttj-backend 的官方口径（见其 sql/script_requests.sql 注释）：
    script_dm_documents 里 is_active=true 且 total_chunks>0。

匹配链路：米圈剧本中文标题 -> title_key 归一化 -> scripts.title / scripts.aliases。
匹配不到（剧本库里压根没有这本）一律视为「未解析」，保留在榜上 —— 宁可少过滤、不误删。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

import httpx

from .config import Config
from .models import RankedScript
from .normalize import title_key

logger = logging.getLogger(__name__)


@dataclass
class ParsedFilter:
    """加载一次、可复用的已解析剧本过滤集合。"""

    enabled: bool = False
    parsed_keys: frozenset[str] = field(default_factory=frozenset)
    # 加载/查询失败时的说明；非空意味着「过滤未生效，榜单含已解析剧本」。
    error: str | None = None

    @classmethod
    def load(cls, cfg: Config) -> "ParsedFilter":
        if not cfg.filter_parsed_enabled:
            return cls(enabled=False)
        if not (cfg.supabase_url and cfg.supabase_service_role_key):
            return cls(enabled=True, error="未配置 Supabase 凭据，跳过已解析过滤")

        headers = {
            "apikey": cfg.supabase_service_role_key,
            "Authorization": f"Bearer {cfg.supabase_service_role_key}",
        }
        base = cfg.supabase_url.rstrip("/")
        try:
            with httpx.Client(timeout=cfg.http_timeout) as client:
                # 1) 已解析的 script_code 集合（is_active 且 有 chunk）
                docs = client.get(
                    f"{base}/rest/v1/script_dm_documents",
                    headers=headers,
                    params={"select": "script_code", "is_active": "eq.true", "total_chunks": "gt.0"},
                )
                docs.raise_for_status()
                parsed_codes = {d["script_code"] for d in docs.json() if d.get("script_code")}

                if not parsed_codes:
                    return cls(enabled=True)

                # 2) code -> title/aliases 映射（含中文别名）
                scripts = client.get(
                    f"{base}/rest/v1/scripts",
                    headers=headers,
                    params={"select": "code,title,aliases"},
                )
                scripts.raise_for_status()
                by_code = {s["code"]: s for s in scripts.json()}

                keys: set[str] = set()
                for code in parsed_codes:
                    s = by_code.get(code)
                    if not s:
                        continue
                    for t in ([s.get("title")] + list(s.get("aliases") or [])):
                        k = title_key(t or "")
                        if k:
                            keys.add(k)
                logger.info("已解析过滤加载完成：%d 本已解析剧本（%d 个归一化标题键）", len(parsed_codes), len(keys))
                return cls(enabled=True, parsed_keys=frozenset(keys))
        except Exception as exc:  # noqa: BLE001
            logger.error("加载已解析剧本列表失败，本轮不启用过滤：%s", exc)
            return cls(enabled=True, error=str(exc))

    def apply(self, items: list[RankedScript]) -> tuple[list[RankedScript], list[RankedScript]]:
        """把 items 拆成 (保留, 已解析被过滤)。未启用/加载失败时原样返回。"""
        if not self.enabled or self.error:
            return items, []
        kept: list[RankedScript] = []
        filtered: list[RankedScript] = []
        for it in items:
            (filtered if it.title_key in self.parsed_keys else kept).append(it)
        return kept, filtered
