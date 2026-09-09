# -*- coding: utf-8 -*-
"""跨源融合打分。

设计原则：**每个源只负责把自己平台的信号压成 0~1 的 value**，
跨源可比性在这一层用「加权和 + 多源加成 + 新鲜度加成」完成，公式透明可解释，
最终 hot_score 归一化到 0~100。

    base   = Σ(weight_i × value_i) / Σ(weight_i)              # 0~1
    boost  = base × (1 + cross_boost × (source_count - 1))     # 多源交叉验证
    score  = min(1, boost + recency) × 100
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from .config import Config
from .models import RankedScript, ScriptCandidate
from .normalize import title_key

RECENCY_WINDOW_DAYS = 120


def _merge_group(key: str, cands: list[ScriptCandidate], cfg: Config, today: date) -> RankedScript:
    """同一剧本（同一 title_key）的多个源样本融合成一条榜单项。

    打分只用「热度源」（weight > 0）：weight=0 的源（如剧本榜降元数据时）
    只贡献展示字段（评分/标签/封面/人数），不参与 base 和交叉验证计数。
    """
    heat_cands = [c for c in cands if c.weight > 0]
    weight_sum = sum(c.weight for c in heat_cands) or 1.0
    base = sum(c.weight * c.value for c in heat_cands) / weight_sum

    source_count = len({c.source for c in heat_cands})
    boosted = base * (1.0 + cfg.cross_source_boost * (source_count - 1))

    # 新鲜度：源给出 published_at 且在窗口内才加成，避免长期霸榜
    recency = 0.0
    for c in cands:
        pub = c.signals.get("published_at")
        if isinstance(pub, date) and 0 <= (today - pub).days <= RECENCY_WINDOW_DAYS:
            recency = max(recency, cfg.recency_boost * (1 - (today - pub).days / RECENCY_WINDOW_DAYS))

    score = min(1.0, boosted + recency) * 100.0

    # 展示字段挑最强的那个源（“信息最全”优先：有 tags > 无 tags）
    best = max(cands, key=lambda c: (bool(c.tags), bool(c.rating), c.value))
    titles = sorted({c.title for c in cands}, key=lambda t: (-len(t), t))
    title = best.title if key == title_key(best.title) else titles[0]

    tags: list[str] = []
    for c in cands:
        for t in c.tags:
            if t and t not in tags:
                tags.append(t)

    return RankedScript(
        rank=0,
        title=title,
        title_key=key,
        hot_score=score,
        sources=sorted({c.source for c in cands}),
        source_detail={
            c.source: {
                "value": round(c.value, 4),
                "weight": c.weight,
                **{k: v for k, v in c.signals.items() if not isinstance(v, date)},
            }
            for c in cands
        },
        tags=tags[:8],
        players=best.players,
        duration=best.duration,
        rating=best.rating,
        url=best.url,
    )


def rank_fuse(
    candidates: list[ScriptCandidate],
    cfg: Config,
    today: date | None = None,
    prev_ranks: dict[str, int] | None = None,
    limit: int | None = None,
) -> list[RankedScript]:
    """按 title_key 聚合 -> 打分 -> 排序 -> 取 Top N -> 补(prev_rank / is_new)。

    limit 默认取 cfg.top_n；过滤已解析时传更大的值（top_n × buffer），
    保证剔除几本之后仍有足够候选补位。
    """
    today = today or date.today()
    prev_ranks = prev_ranks or {}
    limit = limit or cfg.top_n

    groups: dict[str, list[ScriptCandidate]] = defaultdict(list)
    for c in candidates:
        key = title_key(c.title)
        if not key:
            continue
        groups[key].append(c)

    ranked = [_merge_group(k, v, cfg, today) for k, v in groups.items()]
    ranked.sort(key=lambda r: (-r.hot_score, -(r.rating or 0.0), r.title))

    for idx, item in enumerate(ranked[:limit], start=1):
        item.rank = idx
        prev = prev_ranks.get(item.title_key)
        item.prev_rank = prev
        item.is_new = prev is None

    return ranked[:limit]
