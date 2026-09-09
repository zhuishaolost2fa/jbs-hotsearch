# -*- coding: utf-8 -*-
"""把 Charles 导出的 HAR 文件转成「一行一条」的拼场 curl。

为什么用 HAR：Charles 右键没有批量 Export cURL（Windows 5.2.1），
但 File -> Export Session 能导出 HAR。HAR 的 postData.text 保留**请求体原始字节**
（含 sign 的完整 JSON），正好满足「sign 绑定请求体、必须精确回放」的约束。

用法：
    python har_to_curls.py puzzle.har -o ../data/miquan_puzzle_curls.txt
    python har_to_curls.py puzzle.har            # 默认写 ../data/miquan_puzzle_curls.txt
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 只关心拼场接口（拼场文件只能放拼场 curl，否则回放结构会乱）
TARGET_URL_PATTERNS = ("/v10/group/homeScriptGroupList",)


def harvest(har_path: Path) -> list[str]:
    """返回逐条 curl 字符串（按 HAR 原始顺序）。"""
    payload = json.loads(har_path.read_text(encoding="utf-8"))
    entries = payload.get("log", {}).get("entries", [])
    curls: list[str] = []
    for e in entries:
        req = e.get("request", {})
        url = req.get("url", "")
        if not any(pat in url for pat in TARGET_URL_PATTERNS):
            continue
        # 跳过非 POST
        if (req.get("method") or "").upper() != "POST":
            continue

        # 组装 header 参数
        header_args: list[str] = []
        for h in req.get("headers", []):
            k = (h.get("name") or "").strip()
            v = h.get("value")
            if not k or v is None:
                continue
            low = k.lower()
            # HTTP/2 伪头（:method/:scheme/:path/:authority）不是真实 header，必须跳过；
            # host/content-length/accept-encoding 交给 httpx 自动处理。
            if low.startswith(":") or low in ("host", "content-length", "accept-encoding"):
                continue
            # 单引号包裹避免 shell 展开/空格问题
            header_args.append(f"-H '{k}: {v}'")

        # body 必须用原始 JSON 字符串（含 sign），不能用 key-value 重组
        body_text = (req.get("postData") or {}).get("text")
        if not body_text:
            continue
        # 单引号包裹 body；JSON 里理论无单引号，如有则用双引号形式兜底
        if "'" in body_text:
            body_arg = f'--data-binary "{body_text}"'
        else:
            body_arg = f"--data-binary '{body_text}'"

        partial = " ".join(header_args)
        curls.append(f"curl {partial} {body_arg} '{url}'".strip())
    return curls


def main() -> int:
    parser = argparse.ArgumentParser(description="Charles HAR -> 拼场 curl（一行一条）")
    parser.add_argument("har", type=Path, help="Charles 导出的 .har 文件")
    parser.add_argument("-o", "--output", type=Path, default=None,
                        help="输出路径（默认: ../data/miquan_puzzle_curls.txt）")
    args = parser.parse_args()

    if not args.har.is_file():
        print(f"找不到 HAR 文件: {args.har}", file=sys.stderr)
        return 1

    curls = harvest(args.har)
    if not curls:
        print("HAR 里没有匹配到 homeScriptGroupList 的 POST 请求", file=sys.stderr)
        return 1

    out = args.output or (Path(__file__).resolve().parent.parent.parent / "data" / "miquan_puzzle_curls.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(curls) + "\n", encoding="utf-8")

    print(f"从 {args.har.name} 提取 {len(curls)} 条拼场/剧本 curl")
    print(f"已写入: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
