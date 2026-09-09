# -*- coding: utf-8 -*-
"""标题归一化 —— 跨源去重的唯一依据。

跨源时同一剧本的写法五花八门：《猫岛谋杀循环》/ 猫岛谋杀循环 / 猫岛谋杀循环（剧本杀）
/ 猫岛谋杀循环 第一季 ……这套规则的取舍：
  1. 括号内容一律丢掉（副标题/版本说明不算身份）；
  2. 「：」后面的副标题丢掉（《xx》主标题才是身份），但如果去掉后不足 2 字就保留原串；
  3. 全角半角、空格、《》、英文大小写统一。
"""
from __future__ import annotations

import re
import unicodedata

# 日文假名「の/ノ」在中文剧本名里几乎都等于「的」，
# 例如米圈写《六角馆の谋杀鉴赏》、剧本库写《六角馆的谋杀鉴赏》，必须视为同一本。
_KANA_NO = str.maketrans("のノ", "的的")

# 括号：中文/英文
_BRACKET_RE = re.compile(r"[（(\[【][^）)\]】]*[）)\]】]")
# 书名号等装饰标点
_DECOR_RE = re.compile(r"[《》〈〉\"'“”‘’·・!！?？,，。.、:：;；\-—_~～\s]+")
# 干扰身份的常见词缀
_NOISE_RE = re.compile(r"(剧本杀|盒装本|城限本|独家本| نسخ|正版|完整版|桌游)$")
_COLON_RE = re.compile(r"[:：]")


def _strip_noise(text: str) -> str:
    prev = None
    while prev != text:  # 连续剥 grew noise，例如「XX剧本杀（盒装本）」
        prev = text
        text = _NOISE_RE.sub("", text).strip()
    return text


def normalize_title(title: str) -> str:
    """归一化后的展示标题。"""
    if not title:
        return ""
    text = unicodedata.normalize("NFKC", title).strip()
    text = text.translate(_KANA_NO)
    text = _BRACKET_RE.sub("", text)
    text = _DECOR_RE.sub("", text)
    text = _strip_noise(text)
    if _COLON_RE.search(text):
        head = _COLON_RE.split(text)[0].strip()
        if len(head) >= 2:
            text = head
    return text.strip()


def title_key(title: str) -> str:
    """去重键：归一化 + 转小写 + 去掉剩余非字母数字字符。"""
    text = normalize_title(title)
    if not text:
        return ""
    return re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", "", text).lower()
