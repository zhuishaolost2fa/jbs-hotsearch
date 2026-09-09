# -*- coding: utf-8 -*-
"""搜索 + LLM 聚合源 —— 用来交叉验证「网上现在在讨论哪些本」。

流程：多组查询 -> 搜索 API -> 结果标题/摘要喂给 LLM -> 输出结构化候选。
它只做一件事：把「被多个来源反复提到」变成可比较的热度值。

需要配置 HS_SEARCH_PROVIDER + HS_SEARCH_API_KEY（以及一个 OpenAI 兼容的 LLM）。
没配就返回 ok=False，**不会编数据**；pipeline 会记录降级并把米圈一条源的结果放大使用。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any

import httpx

from ..models import ScriptCandidate
from .base import Source

DEFAULT_QUERIES = [
    "{year} 剧本杀 热门排行榜 TOP10",
    "最近最火 剧本杀 新本 推荐 必玩",
    "剧本杀 高分好评榜 玩家口碑推荐",
    "剧本杀 门店 热门开本 排行",
]

EXTRACT_SYSTEM = (
    "你是一个严格的中文信息抽取器。只能提取检索结果中真实出现过的剧本杀名称，"
    "禁止编造、禁止用你自己的知识补充。输出必须是纯 JSON。"
)

EXTRACT_TEMPLATE = """以下是多个搜索结果片段，主题是「当前热门的剧本杀」。
请提取被提到的剧本杀，按「被多个来源提到的 > 只被一个来源提到的」判断热度。

搜索结果：
{chunks}

输出 JSON（不要代码块、不要解释）：
{{
  "scripts": [
    {{
      "title": "剧本名（不要带书名号，去掉副标题/版本说明）",
      "rating": 8.6,
      "tags": ["推理", "情感"],
      "players": "6人",
      "duration": "6小时",
      "published_at": "2026-03",
      "evidence_count": 3,
      "why_hot": "一句话说明它为什么上榜（≤30字）",
      "source_url": "https://..."
    }}
  ]
}}

要求：
1. title 为空/明显不是单本剧本（如「剧本杀行业报告」）则丢弃；
2. rating / published_at 无法确认为 null；
3. 最多 25 条。
"""


def _today_year() -> int:
    return date.today().year


class _SearchClient:
    """统一的搜索结果结构：[{title, url, snippet, published}]"""

    def __init__(self, provider: str, api_key: str, timeout: float) -> None:
        self.provider = provider
        self.api_key = api_key
        self.timeout = timeout

    def search(self, query: str, count: int = 8) -> list[dict[str, Any]]:
        if self.provider == "bocha":
            return self._bocha(query, count)
        if self.provider == "tavily":
            return self._tavily(query, count)
        if self.provider == "serpapi":
            return self._serpapi(query, count)
        raise ValueError(f"不支持的搜索 provider：{self.provider}")

    def _bocha(self, query: str, count: int) -> list[dict[str, Any]]:
        resp = httpx.post(
            "https://api.bochaai.com/v1/web-search",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"query": query, "count": count, "summary": True},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        pages = (payload.get("data") or {}).get("webPages") or {}
        out = []
        for item in pages.get("value") or []:
            out.append(
                {
                    "title": item.get("name") or "",
                    "url": item.get("url") or "",
                    "snippet": item.get("summary") or item.get("snippet") or "",
                    "published": item.get("publishedTime") or "",
                }
            )
        return out

    def _tavily(self, query: str, count: int) -> list[dict[str, Any]]:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": self.api_key,
                "query": query,
                "max_results": count,
                "search_depth": "basic",
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return [
            {
                "title": r.get("title") or "",
                "url": r.get("url") or "",
                "snippet": r.get("content") or "",
                "published": r.get("published_date") or "",
            }
            for r in resp.json().get("results", [])
        ]

    def _serpapi(self, query: str, count: int) -> list[dict[str, Any]]:
        resp = httpx.get(
            "https://serpapi.com/search.json",
            params={"q": query, "num": count, "engine": "google", "hl": "zh-cn", "api_key": self.api_key},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return [
            {
                "title": r.get("title") or "",
                "url": r.get("link") or "",
                "snippet": r.get("snippet") or "",
                "published": r.get("date") or "",
            }
            for r in resp.json().get("organic_results", [])
        ]


def _parse_month(value: Any) -> date | None:
    """'2026-03' / '2026-03-11' -> date；解析不了就 None（不猜）。"""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%Y/%m", "%Y"):
        try:
            return datetime.strptime(text[: len(fmt) + 2] if fmt == "%Y" else text, fmt).date()
        except ValueError:
            continue
    return None


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


class SearchLLMSource(Source):
    name = "search_llm"

    def __init__(self, cfg) -> None:
        super().__init__(cfg, name="search_llm", weight=cfg.search_weight)
        self.client = _SearchClient(cfg.search_provider, cfg.search_api_key, cfg.http_timeout)
        self.queries = [
            q.strip().replace("{year}", str(_today_year()))
            for q in (cfg.search_queries or DEFAULT_QUERIES)
        ]

    def _call_llm(self, chunks: str) -> dict:
        resp = httpx.post(
            f"{self.cfg.llm_base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.cfg.llm_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.cfg.llm_model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": EXTRACT_TEMPLATE.format(chunks=chunks)},
                ],
            },
            timeout=90.0,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(_strip_fences(content))

    def _collect(self) -> list[ScriptCandidate]:
        if self.cfg.search_provider in ("", "none"):
            raise RuntimeError("未配置 HS_SEARCH_PROVIDER（可选 bocha / tavily / serpapi）")
        if not self.cfg.search_api_key:
            raise RuntimeError("未配置 HS_SEARCH_API_KEY")
        if not self.cfg.llm_api_key:
            raise RuntimeError("未配置 HS_LLM_API_KEY（OpenAI 兼容地址 + Key）")

        evidence: dict[str, dict[str, Any]] = {}
        hits = 0
        for query in self.queries:
            results = self.client.search(query)
            if not results:
                continue
            hits += len(results)
            chunks = "\n".join(
                f"- [{i}] {r['title']} | {r['snippet'][:300]} | 发布于 {r['published'] or '未知'} | {r['url']}"
                for i, r in enumerate(results)
            )
            try:
                parsed = self._call_llm(chunks)
            except Exception as exc:  # noqa: BLE001 - 单个 query 抽失败不算整源失败
                continue
            for entry in parsed.get("scripts") or []:
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                info = evidence.setdefault(
                    title,
                    {"count": 0, "why": entry.get("why_hot") or "", "url": entry.get("source_url") or "", **entry},
                )
                info["count"] += int(entry.get("evidence_count") or 1)

        if not evidence:
            raise RuntimeError(f"{len(self.queries)} 组查询共 {hits} 条结果，LLM 未抽出任何剧本")

        top = max((v["count"] for v in evidence.values()), default=1) or 1
        candidates: list[ScriptCandidate] = []
        for title, info in evidence.items():
            candidates.append(
                ScriptCandidate(
                    title=title,
                    source=self.name,
                    weight=self.weight,
                    tags=[t for t in (info.get("tags") or []) if t][:8],
                    players=info.get("players"),
                    duration=info.get("duration"),
                    rating=info.get("rating"),
                    signals={
                        "raw_value": 0.75 * (info["count"] / top) + 0.15,
                        "evidence_count": info["count"],
                        "why_hot": info.get("why") or "",
                        "published_at": _parse_month(info.get("published_at")),
                    },
                    url=info.get("url") or None,
                )
            )
        candidates.sort(key=lambda c: -c.signals["raw_value"])
        for c in candidates:
            c.value = max(0.0, min(1.0, c.signals["raw_value"]))
        return candidates
