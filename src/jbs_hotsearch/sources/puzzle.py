# -*- coding: utf-8 -*-
"""米圈「拼场」（组局）源 —— 用真实约本频次衡量剧本当下热度。

和 scriptSearchPage 不同，`/v10/group/homeScriptGroupList` 返回的是**逐条拼场**：
每一条 = 某店某时开某本、还差几人。一个剧本当天出现多少条拼场，
直接反映「现在有多少人正想玩它」——这是比平台推荐指数更硬的实时热度信号。

**sign 规则（与 scriptSearchPage 一致）**：sign 覆盖请求体全部字段，改 pageNum 就
400003；但不绑时间（一次抓包长期复用）。所以每页都要单独抓，一行一条 curl。

**热度口径**：按 title_key 聚合拼场频次 group_count，value = count / max_count。
这里 count 是**去重后的频次 = 有多少家不同的店在开这个本**（见 `_collect` 去重说明），
而不是原始场次——否则同一店家对同一剧本连开十几场会把热度刷爆。
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from ..models import ScriptCandidate
from ..normalize import title_key
from .base import Source
from .miquan import parse_curl


def _players(item: dict) -> str | None:
    """拼场里已加入的男女玩家 + 剧本总人数。"""
    total = item.get("scriptPlayerLimit")
    if not total:
        return None
    male = item.get("scriptMalePlayerLimit")
    female = item.get("scriptFemalePlayerLimit")
    if male is not None and female is not None:
        return f"{total}人（{male}男{female}女）"
    return f"{total}人"


class MiquanGroupSource(Source):
    """拼场源：聚合约本频次 -> 每个剧本一个热度样本。"""

    name = "miquan_group"

    def __init__(self, cfg) -> None:
        super().__init__(cfg, name="miquan_group", weight=cfg.group_weight)
        self.curls_file = Path(cfg.group_curls_file)
        if not self.curls_file.is_absolute():
            self.curls_file = Path.cwd() / self.curls_file
        self.threshold = max(1.0, cfg.group_threshold)

    def _requests(self):
        if not self.curls_file.is_file():
            raise FileNotFoundError(
                f"拼场抓包文件不存在：{self.curls_file}（在米圈 App 拼场页滚动加载，导出 curl 一行一条）"
            )
        reqs = []
        for line in self.curls_file.read_text(encoding="utf-8").splitlines():
            parsed = parse_curl(line)
            if parsed:
                reqs.append(parsed)
        if not reqs:
            raise ValueError(f"{self.curls_file} 里没有可用的拼场 curl（需要带 --data 且含 data+sign）")
        return reqs

    def _collect(self) -> list[ScriptCandidate]:
        reqs = self._requests()

        def one(parsed):
            url, headers, body = parsed
            with httpx.Client(timeout=self.cfg.http_timeout, follow_redirects=True) as client:
                resp = client.post(url, headers=headers, json=body)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
            payload = resp.json()
            head = payload.get("head", {})
            if head.get("code") != 200:
                raise RuntimeError(f"head={head}")
            return payload.get("data", {}).get("items", [])

        groups: list[dict] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(one, r) for r in reqs]
            for future in futures:
                try:
                    groups.extend(future.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

        if not groups:
            raise RuntimeError(f"全部 {len(reqs)} 页拼场请求失败，前 3 条原因：{errors[:3]}")

        # ---- 去重口径 ----
        # 同店同本去重：同一店家对同一剧本开多场（常见刷量手法），只算 1 家店。
        # 去重键 = (shopId, scriptId)；字段缺失时退化为 groupId（保守，不去重）。
        # 这同时覆盖了翻页去重——跨页重复的同一条拼场，其 shopId/scriptId 必然相同。
        seen: set[tuple] = set()
        by_key: dict[str, list[dict]] = {}
        seen_group: set[str] = set()
        raw_count: Counter = Counter()  # 原始场次（去 groupId 重复、含同店同本重复），仅供报告透明展示

        for g in groups:
            name = (g.get("scriptName") or "").strip()
            key = title_key(name)
            gid = str(g.get("groupId") or "")

            # 原始场次：跨页重复的同一场（同 groupId）只算一次
            if gid:
                if gid not in seen_group:
                    seen_group.add(gid)
                    if key:
                        raw_count[key] += 1
            elif key:
                raw_count[key] += 1

            sid = str(g.get("shopId") or "")
            pid = str(g.get("scriptId") or "")
            dedup = ("shop_script", sid, pid) if (sid and pid) else ("group", gid or key)
            if dedup in seen:
                continue
            seen.add(dedup)
            if not key:
                continue
            by_key.setdefault(key, []).append(g)

        total_groups = len(seen_group) or len(groups)  # 当天总场次（唯一 groupId）
        total_shops = len(seen)  # 唯一 (店,本) 组合数 = 去重后有效样本

        # 频次门槛：去重后 count = 唯一店家数。低于 threshold 家店视为噪声不参与
        #（避免只有 1 家店在排的冷门本干扰榜单）。
        eligible = {k: gs for k, gs in by_key.items() if len(gs) >= self.threshold}
        if not eligible:
            raise RuntimeError("拼场频次全部低于门槛，无有效热度信号")

        # 相对归一化：当天最热剧本（最多店家开）的频次 = 1.0，其余按占比线性拉开。
        # 拼场是「实时局部」信号，用「占当天最大店数的比例」比固定低阈值更有区分度——
        # 否则头部剧本 value 会全部封顶 1.0，丧失排序意义。
        max_count = max(len(gs) for gs in eligible.values())

        candidates: list[ScriptCandidate] = []
        for key, gs in eligible.items():
            count = len(gs)
            # 展示字段取信息最全的那条（有标签优先）
            best = max(gs, key=lambda g: (bool(g.get("scriptTag")), len(g.get("joinUserList") or [])))
            tags = [t.strip() for t in (best.get("scriptTag") or "").split("@") if t.strip()]
            # 拼场里剧本名常带书名号（《红豆》），strip 掉外层装饰，保留冒号等（1:100…）
            title = (best.get("scriptName") or "").strip().strip("《》〈〉")
            value = count / max_count
            candidates.append(
                ScriptCandidate(
                    title=title,
                    source=self.name,
                    weight=self.weight,
                    tags=tags,
                    players=_players(best),
                    duration=None,
                    rating=None,
                    value=value,
                    signals={
                        "group_count": count,
                        "raw_group_count": raw_count.get(key, count),
                        "max_count": max_count,
                        "total_groups": total_groups,
                        "total_shops": total_shops,
                        "threshold": self.threshold,
                        "script_id": str(best.get("scriptId") or ""),
                        "shop": best.get("shopName"),
                    },
                    url=best.get("scriptCoverUrl") or None,
                )
            )

        # 频次相同时按名字稳定排序，保证每天输出可复现
        candidates.sort(key=lambda c: (-(c.signals.get("group_count") or 0), c.title))
        return candidates
