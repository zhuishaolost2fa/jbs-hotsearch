# -*- coding: utf-8 -*-
"""米圈（joylovemeet）剧本搜索接口 —— 抓包签名回放源。

**为什么是「回放」而不是「构造」**：
该接口每个请求体都带 sign，且 sign 覆盖请求体本身 —— 改一个字段（哪怕 pageNum）
就 400003。所以只能原样重放曾经抓到的 curl，一行一个 pageNum。
好在 sign 不与时间绑定（2026-08 抓的包 09 月仍有效），一次抓包可以长期复用。

**怎么扩容**：在米圈 App 里翻到想抓的页，Charles 导出 N 条 curl，
一行一条追加到 MIQUAN_CURLS_FILE 即可，代码不用改。

热度口径：recommendNum（0~100 的平台热度，随请求实时刷新）+ scriptScore（评分，0~10）。
"""
from __future__ import annotations

import base64
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from ..models import ScriptCandidate
from .base import Source

API_URL_DEFAULT = "https://juzujujk.joylovemeet.cn/v9/script/scriptSearchPage"

# 同时支持单引号 / 双引号包裹的 curl（Charles 导出两版都有）
_URL_RE = re.compile(r"(?:--location\s+|['\"])(https?://[^\s'\"]+)", re.I)
_HEADER_SQ_RE = re.compile(r"(?:--header|-H)\s+'([^']*)'", re.I)
_HEADER_DQ_RE = re.compile(r'(?:--header|-H)\s+"((?:[^"\\]|\\.)*)"', re.I)
_DATA_SQ_RE = re.compile(r"--data(?:-binary|-raw)?\s+'([^']*)'", re.I)
_DATA_DQ_RE = re.compile(r'--data(?:-binary|-raw)?\s+"((?:[^"\\]|\\.)*)"', re.I)


def _unescape_dq(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def parse_curl(line: str, default_url: str = API_URL_DEFAULT):
    """一行 curl -> (url, headers, body_dict)。缺 data/sign 返回 None。"""
    line = line.strip()
    if not line:
        return None
    url_match = _URL_RE.search(line)
    url = url_match.group(1) if url_match else default_url

    headers: dict[str, str] = {}
    for pattern in (_HEADER_SQ_RE, _HEADER_DQ_RE):
        for m in pattern.finditer(line):
            raw = m.group(1)
            if pattern is _HEADER_DQ_RE:
                raw = _unescape_dq(raw)
            if ":" in raw:
                k, v = raw.split(":", 1)
                headers[k.strip()] = v.strip()

    dm = _DATA_SQ_RE.search(line) or _DATA_DQ_RE.search(line)
    if not dm:
        return None
    body_raw = _unescape_dq(dm.group(1)) if (_DATA_DQ_RE.search(line) and not _DATA_SQ_RE.search(line)) else dm.group(1)
    try:
        body = json.loads(body_raw)
    except json.JSONDecodeError:
        return None
    if "data" not in body or "sign" not in body:
        return None

    # Host 头交给 httpx 自己处理，避免抓包时的 IP 直连残留
    headers.pop("Host", None)
    headers.setdefault("Content-Type", "application/json")
    return url, headers, body


def _page_num(body: dict) -> str:
    try:
        return str(json.loads(base64.b64decode(body.get("data", "")).decode()).get("pageNum", "?"))
    except Exception:  # noqa: BLE001
        return "?"


def _players(item: dict) -> str | None:
    """玩家数 -> '6人 (3男3女)'，缺性别配置时退化成 '6人'。"""
    total = item.get("scriptPlayerLimit")
    if not total:
        return None
    male, female = item.get("scriptMalePlayerLimit"), item.get("scriptFemalePlayerLimit")
    if male and female:
        return f"{total}人（{male}男{female}女）"
    return f"{total}人"


def _duration(item: dict) -> str | None:
    minutes = item.get("groupDuration")
    if not minutes:
        return None
    hours = minutes / 60
    return f"{hours:.1f}小时".replace(".0", "")


def item_to_candidate(item: dict, weight: float) -> ScriptCandidate | None:
    title = (item.get("scriptName") or "").strip()
    if not title:
        return None
    tags = [t.strip() for t in (item.get("scriptTag") or "").split("@") if t.strip()]
    heat = float(item.get("recommendNum") or 0)
    score = float(item.get("scriptScore") or 0)
    return ScriptCandidate(
        title=title,
        source="miquan",
        weight=weight,
        tags=tags,
        players=_players(item),
        duration=_duration(item),
        rating=score if score else None,
        # heat 用于主归一化，score 作为质量修正
        signals={
            "heat": heat,
            "score": score,
            "difficulty": item.get("scriptDifficultyDegreeName"),
            "script_id": str(item.get("scriptId") or ""),
        },
        url=item.get("scriptCoverUrl") or None,
    )


class MiquanSource(Source):
    name = "miquan"

    def __init__(self, cfg) -> None:
        super().__init__(cfg, name="miquan", weight=cfg.miquan_weight)
        self.curls_file = Path(cfg.miquan_curls_file)
        if not self.curls_file.is_absolute():
            self.curls_file = Path.cwd() / self.curls_file

    def _requests(self):
        if not self.curls_file.is_file():
            raise FileNotFoundError(
                f"抓包文件不存在：{self.curls_file}（在米圈 App 里翻页抓 curl，一行一条存到这里）"
            )
        reqs = []
        for line in self.curls_file.read_text(encoding="utf-8").splitlines():
            parsed = parse_curl(line)
            if parsed:
                reqs.append(parsed)
        if not reqs:
            raise ValueError(f"{self.curls_file} 里没有可用的 curl（需要带 --data 且含 data+sign）")
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
                # 400003 基本等于 sign 对不上 -> 需要重新抓包
                raise RuntimeError(f"page {_page_num(body)} head={head}")
            return payload.get("data", {}).get("items", [])

        items: list[dict] = []
        errors: list[str] = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(one, r) for r in reqs]
            for future in futures:
                try:
                    items.extend(future.result())
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

        if not items:
            raise RuntimeError(f"全部 {len(reqs)} 页请求失败，前 3 条原因：{errors[:3]}")

        seen: set[str] = set()
        candidates: list[ScriptCandidate] = []
        for raw in items:
            sid = str(raw.get("scriptId") or "")
            key = sid or raw.get("scriptName") or ""
            if key in seen:
                continue
            seen.add(key)
            cand = item_to_candidate(raw, self.weight)
            if cand:
                candidates.append(cand)

        # 热度用**绝对值**而不是「除以当天最大值」：
        #   - 跨天可比 —— 今天的 91 和昨天的 91 是同一个意思（昨天的高分不会因为今天全员虚高而掉下去）；
        #   - recommendNum 本身就是 0~100 的平台热度值，天然可直接用。
        # 构成：0.75 × 平台热度/100 + 0.25 × 评分/10，避免「高分冷门本」霸榜。
        for c in candidates:
            heat = max(0.0, min(100.0, float(c.signals.get("heat") or 0))) / 100.0
            score = max(0.0, min(10.0, float(c.signals.get("score") or 0))) / 10.0
            c.signals["raw_value"] = 0.75 * heat + 0.25 * score
            c.value = max(0.0, min(1.0, c.signals["raw_value"]))
        return candidates
