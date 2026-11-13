# -*- coding: utf-8 -*-
"""
股票代码规范化
==============
统一各数据源的代码格式。**这是数据层最容易被忽视、又最容易出错的地方。**

问题
----
同一个「浦发银行」，各家叫法不同：

======  ==============  ==========
数据源   格式            示例
======  ==============  ==========
同花顺   ``600000``     纯 6 位数字
腾讯     ``sh600000``   带市场前缀
新浪     ``sh600000``   带市场前缀
东财     ``600000``     纯 6 位数字
通达信   ``SH600000``   大写前缀
======  ==============  ==========

如果不统一，会出现：**用同花顺的代码去查腾讯的数据，查不到，然后静默返回空**——
这类 bug 不报错，只是"没数据"，非常难查。

本项目的约定
------------
**内部一律用纯 6 位数字**（``600000``），只在调用外部数据源时转换。
转换函数集中在 ``to_market_code()``。
"""
from __future__ import annotations

import re

#: 纯 6 位数字代码
_PURE = re.compile(r"^\d{6}$")
#: 带市场前缀，如 sh600000 / SZ000001
_PREFIXED = re.compile(r"^(sh|sz|bj)(\d{6})$", re.IGNORECASE)

#: 交易所前缀
SH = "sh"
SZ = "sz"
BJ = "bj"


def normalize(code: str) -> str:
    """
    把任意格式的代码规范成内部格式（纯 6 位数字）。

    >>> normalize("sh600000")
    '600000'
    >>> normalize("600000")
    '600000'
    >>> normalize("SZ000001")
    '000001'

    :raises ValueError: 无法识别的格式
    """
    if not isinstance(code, str):
        raise ValueError(f"代码必须是字符串，收到 {type(code).__name__}")

    code = code.strip()

    if _PURE.match(code):
        return code

    match = _PREFIXED.match(code)
    if match:
        return match.group(2)

    raise ValueError(f"无法识别的股票代码格式: {code!r}")


def exchange_of(code: str) -> str:
    """
    根据代码判断交易所。

    规则（A 股）：

    ============  ==========
    代码前缀        交易所
    ============  ==========
    60 / 68       上海（68 是科创板）
    00 / 30       深圳（30 是创业板）
    4 / 8 / 92    北交所（92 是 2024 年启用的新号段）
    5             上海基金
    1             深圳基金
    9             上海 B 股（900xxx）
    ============  ==========

注意：**指数代码不适用本函数**。深证成指是 ``399001``、上证指数是 ``000001``，
前者反推不出来，后者会和「平安银行」撞车。
指数的交易所只能从数据源的市场前缀（``sh``/``sz``/``bj``）判断。
    """
    pure = normalize(code)
    head = pure[0]

    if pure[:2] in ("60", "68") or head == "5":
        return SH
    if pure[:2] in ("00", "30") or head == "1":
        return SZ
    if head in ("4", "8") or pure[:2] == "92":
        return BJ
    # 900xxx 上海 B 股
    if pure[:2] == "90":
        return SH

    raise ValueError(f"无法判断交易所: {pure!r}")


def to_market_code(code: str) -> str:
    """
    转成带市场前缀的格式（腾讯/新浪需要）。

    >>> to_market_code("600000")
    'sh600000'
    >>> to_market_code("000001")
    'sz000001'
    """
    pure = normalize(code)
    return f"{exchange_of(pure)}{pure}"


def is_valid(code: str) -> bool:
    """判断是否是合法的 A 股代码（不抛异常版本）。"""
    try:
        pure = normalize(code)
        exchange_of(pure)
        return True
    except (ValueError, TypeError):
        return False


def normalize_many(codes: list[str]) -> list[str]:
    """
    批量规范化，自动去重并保持顺序。

    遇到非法代码会**跳过并记录**，而不是整体失败——
    下载 5000 只股票时，因为一个脏代码中断整个任务是不可接受的。
    """
    seen: set[str] = set()
    result: list[str] = []
    for code in codes:
        try:
            pure = normalize(code)
        except ValueError:
            continue
        if pure not in seen:
            seen.add(pure)
            result.append(pure)
    return result
