# -*- coding: utf-8 -*-
"""把 HTML 截成 PNG（Playwright 无头浏览器）。

单独成模块的原因：reviews 的店家榜海报、weekly 的周报海报都要用同一套逻辑，
复制两份必然会在某一处悄悄漂移。

设计取舍：
  - 高度自适应（只放大不收缩，单调收敛）：内容多时海报自动变长，内容少时保持基准高度。
  - **失败返回 False 而不是抛异常**：截图是锦上添花，不能因为它失败就让出榜/出周报整个挂掉。
    调用方据此降级成「只有文案」的页面。
  - playwright 是可选依赖：没装就降级，不 import 失败。
  - screenshot_slices：按小红书 3:4 把长海报切成多张，切点对齐内容块边界，
    不会把一条榜单从中间切断。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 量内容真实高度的 JS（body 与 documentElement 取大者）
_HEIGHT_JS = (
    "Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"
)


def screenshot_html(
    html_path: Path,
    png_path: Path,
    width: int = 375,
    height: int = 900,
    scale: int = 2,
    rounds: int = 4,
) -> bool:
    """把 html_path 渲染截图到 png_path，成功返回 True。

    rounds：高度收敛的最大轮数。内容恒定，通常一两轮就收敛。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("未安装 playwright，跳过截图")
        return False

    png_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"]
            )
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": height}, device_scale_factor=scale
                )
                page.goto(html_path.as_uri())
                page.wait_for_load_state("networkidle")
                for _ in range(rounds):
                    real = int(page.evaluate(_HEIGHT_JS) or 0)
                    real = max(real, height)
                    if abs(real - height) <= 2:
                        break
                    height = real
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_load_state("networkidle")
                page.screenshot(path=str(png_path), full_page=False)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - 截图失败不能拖垮主流程
        logger.warning("截图失败 %s -> %s：%s", html_path.name, png_path.name, exc)
        return False
    return png_path.is_file()


# 切片切点要避开的内容块（列表项 / 区块标题 / 页头页脚等），切在这些元素的边界上
_DEFAULT_ANCHORS = ".hero,.item,.section-title,.summary,.chips,.parsed,.foot,.footer,.warn"

_ANCHOR_TOPS_JS = """
(sel) => {
  const tops = [];
  document.querySelectorAll(sel).forEach((el) => {
    const r = el.getBoundingClientRect();
    const t = r.top + window.scrollY;
    if (t > 1 && r.height > 8) tops.push(Math.round(t));
  });
  return [...new Set(tops)].sort((a, b) => a - b);
}
"""


def screenshot_slices(
    html_path: Path,
    first_png: Path,
    width: int = 375,
    scale: int = 2,
    page_height: int | None = None,
    anchors: str = _DEFAULT_ANCHORS,
    max_overshoot: float = 1.22,
    min_tail: float = 0.3,
    max_slices: int = 12,
) -> list[Path]:
    """把海报按小红书 3:4 切成多张 PNG；返回切片路径（空列表 = 失败）。

    命名：第一张就是 first_png（沿用原文件名，页面/列表引用不用改），
    后续是 {stem}-2.png、{stem}-3.png …

    为什么切点要挑内容块边界：按固定高度硬切，一半概率把某条榜单从中间切成
    两张图的上半截和下半截，发出来就是废片。锚点元素（榜单条目、区块标题等）
    的顶边都是安全的下刀位置，在目标高度附近选最近的锚点即可。

    单页策略（内容 ≈ 一屏时）：
      - 内容不足一页：按整页高截，底部露背景色，保证输出恒为 3:4；
      - 内容超出但 < max_overshoot 倍：CSS zoom 轻微压缩进一页（日报常差 2~3%，
        缩了完全看不出来），避免为 36px 的页脚多切一张图。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("未安装 playwright，跳过截图")
        return []

    ph = page_height or round(width * 4 / 3)  # 3:4 基准页高
    first_png.parent.mkdir(parents=True, exist_ok=True)

    def _out_path(idx: int) -> Path:
        return first_png if idx == 1 else first_png.with_name(f"{first_png.stem}-{idx}.png")

    # 先清掉旧切片（内容变短时旧的第 4、5 张会残留在磁盘上）
    for old in first_png.parent.glob(f"{first_png.stem}-*.png"):
        try:
            old.unlink()
        except OSError:
            pass

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                args=["--no-sandbox", "--disable-dev-shm-usage", "--font-render-hinting=none"]
            )
            try:
                page = browser.new_page(
                    viewport={"width": width, "height": ph}, device_scale_factor=scale
                )
                page.goto(html_path.as_uri())
                page.wait_for_load_state("networkidle")
                total = 0
                for _ in range(4):
                    total = int(page.evaluate(_HEIGHT_JS) or 0)
                    if abs(total - ph) <= 2 or total < ph:
                        break
                    page.set_viewport_size({"width": width, "height": total})
                    page.wait_for_load_state("networkidle")
                total = max(total, 1)

                def _shot(idx: int, top: int, bottom: int) -> Path:
                    path = _out_path(idx)
                    page.screenshot(
                        path=str(path),
                        clip={"x": 0, "y": top, "width": width, "height": bottom - top},
                    )
                    return path

                # ---- 单页：内容装得进一页（允许轻微压缩）----
                if total <= ph * max_overshoot:
                    if total > ph:
                        z = ph / total
                        page.evaluate(
                            """([z, w]) => {
                              document.documentElement.style.width = (w / z) + 'px';
                              document.body.style.zoom = z;
                              document.body.style.width = (w / z) + 'px';
                            }""",
                            [z, width],
                        )
                        page.wait_for_load_state("networkidle")
                    return [_shot(1, 0, ph)]

                # ---- 多页：按锚点边界贪心切 ----
                tops = [t for t in (page.evaluate(_ANCHOR_TOPS_JS, anchors) or []) if t > 8]
                cuts = [0]
                while cuts[-1] + ph < total and len(cuts) < max_slices:
                    target = cuts[-1] + ph
                    lo = cuts[-1] + int(ph * 0.45)
                    hi = target + int(ph * 0.15)
                    cand = [t for t in tops if lo < t <= hi]
                    cuts.append(max(cand) if cand else target)
                # 尾片太短（只剩一行页脚那种）→ 把最后一切点往前挪，凑到最小高度
                if len(cuts) >= 2 and total - cuts[-1] < ph * min_tail:
                    need = int(ph * min_tail) - (total - cuts[-1])
                    prev = cuts[-2]
                    cand = [t for t in tops if prev - need <= t < prev - int(ph * 0.25)]
                    if cand:
                        cuts[-1] = max(cand)
                cuts.append(total)

                outs = []
                for i in range(len(cuts) - 1):
                    outs.append(_shot(i + 1, cuts[i], min(cuts[i + 1], cuts[i] + ph)))
                return outs
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - 截图失败不能拖垮主流程
        logger.warning("切片截图失败 %s -> %s：%s", html_path.name, first_png.name, exc)
        # 失败时清掉可能的半成品，避免留下残缺的图
        for leftover in first_png.parent.glob(f"{first_png.stem}*.png"):
            try:
                leftover.unlink()
            except OSError:
                pass
        return []

