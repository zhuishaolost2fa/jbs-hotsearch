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
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from ..models import ScriptCandidate
from ..normalize import title_key
from ..tz_util import get_tz
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
        self.time_window_days = max(1, int(cfg.group_time_window_days))
        self.per_shop_cap = max(0, int(cfg.group_per_shop_cap))

    def _window_bounds(self):
        """返回 (今天, 截止日期)，只统计 [今天, 截止日期] 内开场的排期。

        截止 = 今天 + (time_window_days - 1) 天，即「含今天共 N 天」。
        用配置时区取「今天」，避免 UTC 偏移把当天场次划到昨天/明天。
        """
        tz = get_tz(self.cfg.timezone, self.cfg.tz_fallback_offset)
        today = datetime.now(tz).date()
        return today, today + timedelta(days=self.time_window_days - 1)

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

        # ---- 时间窗过滤 ----
        # 拼场接口返回的是未来约一个月的排期（groupOpenTime 今天 → +24 天）。
        # 全量累加会让「单店连排一个月」的刷量本虚高登顶，所以只统计「今天起 N 天内」
        # 开场的场次（含今天）。无开本时间的场次不纳入时间窗统计（数据异常，保守丢弃）。
        today, cutoff = self._window_bounds()
        tz = get_tz(self.cfg.timezone, self.cfg.tz_fallback_offset)
        _kept: list[dict] = []
        dropped_out = 0
        dropped_no_time = 0
        for g in groups:
            ms = g.get("groupOpenTime")
            if not ms:
                dropped_no_time += 1
                continue
            try:
                d = datetime.fromtimestamp(int(ms) / 1000, tz).date()
            except (TypeError, ValueError, OSError):
                dropped_no_time += 1
                continue
            if today <= d <= cutoff:
                _kept.append(g)
            else:
                dropped_out += 1
        groups = _kept
        if not groups:
            raise RuntimeError(f"时间窗 {today} ~ {cutoff} 内没有拼场排期（窗口外 {dropped_out} 场）")

        # ---- 去重口径 ----
        # 按「店 + 本 + 开场时间」去重：同一家店在同一时刻对同一剧本的重复发布
        #（如同时挂两个车位）合并成 1 个组局；不同时刻的开场各算一个组局。
        # 热度 = 真实组局数，而不是「店数」——店数会把「同一店连开多场」的真实
        # 热度抹平。跨页重复的同一场（同 groupId）也天然被这个键覆盖。
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
            ot = str(g.get("groupOpenTime") or "")
            if sid and pid and ot:
                dedup = ("shop_script_time", sid, pid, ot)
            elif sid and pid:
                dedup = ("shop_script", sid, pid)
            else:
                dedup = ("group", gid or key)
            if dedup in seen:
                continue
            seen.add(dedup)
            if not key:
                continue
            by_key.setdefault(key, []).append(g)

        total_groups = len(seen_group) or len(groups)  # 时间窗内总场次（唯一 groupId）
        total_instances = len(seen)  # 去重后的组局数（店 + 本 + 时刻）

        # ---- 单店组局封顶 ----
        # 同一家店对同一剧本在时间窗内连排多场（刷量手法），最多只计 cap 场组局，
        # 超过部分不再计入。时间窗已把「连排一个月」压到「近 N 天」，封顶再压掉
        # 「单店近 N 天连排 3+ 场」的残留虚高。按开场时间排序、保留最近 cap 场。
        if self.per_shop_cap > 0:
            for key in list(by_key):
                by_shop: dict[tuple, list[dict]] = {}
                for g in by_key[key]:
                    k = (str(g.get("shopId") or ""), str(g.get("scriptId") or ""))
                    by_shop.setdefault(k, []).append(g)
                capped: list[dict] = []
                for items in by_shop.values():
                    items.sort(key=lambda g: g.get("groupOpenTime") or 0)
                    capped.extend(items[: self.per_shop_cap])
                by_key[key] = capped

        # 组局数门槛：组局数低于 threshold 视为噪声不参与
        #（避免只有 1 场组局在排的冷门本干扰榜单）。
        eligible = {k: gs for k, gs in by_key.items() if len(gs) >= self.threshold}
        if not eligible:
            raise RuntimeError("拼场组局数全部低于门槛，无有效热度信号")

        # 相对归一化：时间窗内最热剧本（最多组局）的组局数 = 1.0，其余按占比线性拉开。
        # 拼场是「实时局部」信号，用「占时间窗内最大组局数的比例」比固定低阈值更有区分度——
        # 否则头部剧本 value 会全部封顶 1.0，丧失排序意义。
        max_count = max(len(gs) for gs in eligible.values())

        candidates: list[ScriptCandidate] = []
        for key, gs in eligible.items():
            count = len(gs)  # 组局数（店 + 本 + 时刻去重）
            unique_shops = len({(str(g.get("shopId") or ""), str(g.get("scriptId") or "")) for g in gs})
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
                        "shop_count": unique_shops,
                        "raw_group_count": raw_count.get(key, count),
                        "max_count": max_count,
                        "total_groups": total_groups,
                        "total_instances": total_instances,
                        "threshold": self.threshold,
                        "window_days": self.time_window_days,
                        "per_shop_cap": self.per_shop_cap,
                        "window_start": today.isoformat(),
                        "window_end": cutoff.isoformat(),
                        "window_dropped": dropped_out + dropped_no_time,
                        "script_id": str(best.get("scriptId") or ""),
                        "shop": best.get("shopName"),
                    },
                    url=best.get("scriptCoverUrl") or None,
                )
            )

        # 频次相同时按名字稳定排序，保证每天输出可复现
        candidates.sort(key=lambda c: (-(c.signals.get("group_count") or 0), c.title))
        return candidates
