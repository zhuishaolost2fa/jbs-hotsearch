# -*- coding: utf-8 -*-
"""把 HTML 截成 PNG（Playwright 无头浏览器）。

单独成模块的原因：reviews 的店家榜海报、weekly 的周报海报都要用同一套逻辑，
复制两份必然会在某一处悄悄漂移。

设计取舍：
  - 高度自适应（只放大不收缩，单调收敛）：内容多时海报自动变长，内容少时保持基准高度。
  - **失败返回 False 而不是抛异常**：截图是锦上添花，不能因为它失败就让出榜/出周报整个挂掉。
    调用方据此降级成「只有文案」的页面。
  - playwright 是可选依赖：没装就降级，不 import 失败。
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


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
                    real = int(
                        page.evaluate(
                            "Math.max(document.body.scrollHeight,"
                            " document.documentElement.scrollHeight)"
                        )
                        or 0
                    )
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
