# -*- coding: utf-8 -*-
"""汉字 → 拼音首字母。

不引入 pypinyin：服务器上多一个依赖就多一份 pip 装不上的风险。
这里用 GB2312 一级字库（3755 个常用字）的区位码分段表，覆盖股票名称足够。
股票名里的字母（ST、TCL、万科A）原样保留并转小写，数字与符号跳过。
"""
from functools import lru_cache

# (起始字节, 结束字节, 首字母)，按 GB2312 一级汉字区位顺序排列
_RANGES = [
    (0xB0A1, 0xB0C4, "a"), (0xB0C5, 0xB2C0, "b"), (0xB2C1, 0xB4ED, "c"),
    (0xB4EE, 0xB6E9, "d"), (0xB6EA, 0xB7A1, "e"), (0xB7A2, 0xB8C0, "f"),
    (0xB8C1, 0xB9FD, "g"), (0xB9FE, 0xBBF6, "h"), (0xBBF7, 0xBFA5, "j"),
    (0xBFA6, 0xC0AB, "k"), (0xC0AC, 0xC2E7, "l"), (0xC2E8, 0xC4C2, "m"),
    (0xC4C3, 0xC5B5, "n"), (0xC5B6, 0xC5BD, "o"), (0xC5BE, 0xC6D9, "p"),
    (0xC6DA, 0xC8BA, "q"), (0xC8BB, 0xC8F5, "r"), (0xC8F6, 0xCBF9, "s"),
    (0xCBFA, 0xCDD9, "t"), (0xCDDA, 0xCEF3, "w"), (0xCEF4, 0xD1B8, "x"),
    (0xD1B9, 0xD4D0, "y"), (0xD4D1, 0xD7F9, "z"),
]

# 多音字覆盖：区位码表按「主要读音」归类，股票名称里的特定读法会取错。
# 行 —— 银行(háng)，表里落在 x，而 A 股叫「XX银行」的有一大批，
#       用户按 zsyh 搜招商银行必须能命中，所以强制 h。
# 其余多音字（长/重/厦/藏）在各自主流名称里读音一致，不动，避免改出新错。
_OVERRIDE = {
    "行": "h",
}


@lru_cache(maxsize=4096)
def _char_initial(ch):
    """单个汉字的拼音首字母；非汉字或超出字库返回空串。"""
    if "a" <= ch.lower() <= "z":
        return ch.lower()
    if not ("\u4e00" <= ch <= "\u9fff"):
        return ""
    if ch in _OVERRIDE:
        return _OVERRIDE[ch]
    try:
        b = ch.encode("gbk")
    except UnicodeEncodeError:
        return ""
    if len(b) != 2:
        return ""
    code = (b[0] << 8) | b[1]
    for lo, hi, letter in _RANGES:
        if lo <= code <= hi:
            return letter
    return ""


def initials(text):
    """整串取首字母，如 平安银行 → payh。"""
    if not text:
        return ""
    return "".join(_char_initial(c) for c in str(text))
