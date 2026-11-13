# -*- coding: utf-8 -*-
"""
全市场实时行情（腾讯批量接口）
==============================
一次请求可带数百只股票，5580 只全市场约 **2 秒**拉完——这是云图能"实时刷新"的关键。

为什么不用东财
--------------
东财 ``push2`` 接口一次就能返回全市场，但**按 IP 限流**：触发后 5~40 分钟不可用。
云图要求每几秒刷一次，用东财必然被限流。腾讯接口没有这个问题。

接口形态
--------
``http://qt.gtimg.cn/q=sh600000,sz000001,...``

返回 GBK 编码的文本，每只股票一段，``~`` 分隔的 88 个字段::

    v_sh600000="1~浦发银行~600000~10.21~10.17~10.14~500817~...";

字段位置（0 基，**已实测确认**）
--------------------------------
=====  ==================  ============================
下标    含义                示例
=====  ==================  ============================
1      名称                浦发银行
2      代码                600000
3      现价                10.21
4      昨收                10.17
5      今开                10.14
30     时间戳              20261112153031
31     涨跌额              0.04
32     涨跌幅(%)           0.39
33     最高                10.24
34     最低                10.03
36     成交量(手)          500817
37     成交额(万元)        50845
38     换手率(%)           0.15
44     流通市值(亿元)      3400.53
45     总市值(亿元)        3400.53
47     涨停价              11.19
48     跌停价              9.15
=====  ==================  ============================

**单位坑（已踩过）**：``[37]`` 是**万元**、``[44]/[45]`` 是**亿元**，
必须分别乘 ``1e4`` / ``1e8`` 换成元，否则云图面积会差好几个数量级。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests

from src.common.logger import get_logger
from src.data.codes import to_market_code

log = get_logger(__name__)

#: 批量行情接口
QUOTE_URL = "http://qt.gtimg.cn/q="

#: 单次请求携带的代码数。实测 600 稳定；900 反而更慢。
DEFAULT_CHUNK_SIZE = 600
#: 并发数。腾讯对批量查询比较宽松，8 并发实测无问题。
DEFAULT_WORKERS = 8
#: 单请求超时（秒）
DEFAULT_TIMEOUT = 20

#: 指数代码 → 展示名。腾讯用 sh000001 这类格式。
INDEX_CODES: dict[str, str] = {
    "sh000001": "上证指数",
    "sz399001": "深证成指",
    "sz399006": "创业板指",
    "sh000300": "沪深300",
    "sh000688": "科创50",
    "sh000905": "中证500",
    "sh000852": "中证1000",
    "bj899050": "北证50",
}

#: 输出列
QUOTE_COLUMNS: tuple[str, ...] = (
    "market_code", "code", "name", "exchange",
    "price", "prev_close", "open", "high", "low",
    "change", "change_pct",
    "volume", "amount", "turnover_rate",
    "float_mcap", "total_mcap",
    "limit_up", "limit_down",
    "quote_time",
)


def _to_float(value: str, default: float = 0.0) -> float:
    """把腾讯返回的字符串字段转 float，空串/异常一律回落到 ``default``。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_line(line: str) -> dict | None:
    """
    解析 ``v_sh600000="1~浦发银行~...";`` 一段。

    字段不足、停牌（现价为 0）都返回 ``None``，由调用方过滤——
    **不要在这里抛异常**，一只坏票不能毁掉整个快照。
    """
    if "=" not in line or "~" not in line:
        return None

    var, _, payload = line.partition("=")
    var = var.strip()
    if not var.startswith("v_") or len(var) < 4:
        return None

    market_code = var[2:]          # sh600000
    fields = payload.strip().strip('"').split("~")
    if len(fields) < 49:
        return None

    price = _to_float(fields[3])
    if price <= 0:
        # 停牌 / 退市 / 无行情
        return None

    # 交易所直接取变量名里的市场前缀（sh/sz/bj），**不要用代码反推**：
    # 指数代码规则和个股不同——深证成指 399001 反推不出来，
    # 上证指数 000001 还会和「平安银行」撞车。
    exchange = market_code[:2].upper()
    if exchange not in ("SH", "SZ", "BJ"):
        return None

    return {
        "market_code": market_code,
        "code": fields[2] or market_code[2:],
        "name": fields[1].strip(),
        "exchange": exchange,
        "price": price,
        "prev_close": _to_float(fields[4]),
        "open": _to_float(fields[5]),
        "high": _to_float(fields[33]),
        "low": _to_float(fields[34]),
        "change": _to_float(fields[31]),
        "change_pct": _to_float(fields[32]),
        # [36] 成交量(手) → 股
        "volume": _to_float(fields[36]) * 100,
        # [37] 成交额(万元) → 元
        "amount": _to_float(fields[37]) * 1e4,
        "turnover_rate": _to_float(fields[38]),
        # [44] 流通市值(亿元) → 元
        "float_mcap": _to_float(fields[44]) * 1e8,
        # [45] 总市值(亿元) → 元
        "total_mcap": _to_float(fields[45]) * 1e8,
        "limit_up": _to_float(fields[47]),
        "limit_down": _to_float(fields[48]),
        "quote_time": fields[30],
    }


def _make_session() -> requests.Session:
    """建一个带通用头的 session。腾讯要求带 Referer，否则部分节点返回空。"""
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
            ),
            "Referer": "http://finance.qq.com",
        }
    )
    return s


def _fetch_chunk(session: requests.Session, chunk: list[str], timeout: int) -> list[dict]:
    """拉一个批次并解析。网络异常返回空列表，不向上抛。"""
    try:
        resp = session.get(QUOTE_URL + ",".join(chunk), timeout=timeout)
        resp.encoding = "gbk"
    except requests.RequestException as exc:
        log.warning("行情批次失败（%d 只）：%s", len(chunk), exc)
        return []

    out: list[dict] = []
    for line in resp.text.split(";"):
        line = line.strip()
        if not line:
            continue
        row = _parse_line(line)
        if row is not None:
            out.append(row)
    return out


def fetch_quotes(
    codes: list[str],
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    workers: int = DEFAULT_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
) -> pd.DataFrame:
    """
    批量拉取行情。

    :param codes: 纯 6 位代码或带前缀代码，内部统一转换
    :param chunk_size: 每批数量
    :param workers: 并发线程数
    :param timeout: 单请求超时
    :returns: 含 :data:`QUOTE_COLUMNS` 列的 DataFrame（停牌股会被剔除）
    """
    if not codes:
        return pd.DataFrame(columns=list(QUOTE_COLUMNS))

    market_codes: list[str] = []
    for code in codes:
        try:
            market_codes.append(to_market_code(code))
        except ValueError:
            continue

    if not market_codes:
        return pd.DataFrame(columns=list(QUOTE_COLUMNS))

    chunks = [
        market_codes[i : i + chunk_size] for i in range(0, len(market_codes), chunk_size)
    ]

    session = _make_session()
    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for part in pool.map(lambda c: _fetch_chunk(session, c, timeout), chunks):
            rows.extend(part)

    df = pd.DataFrame(rows, columns=list(QUOTE_COLUMNS))
    if df.empty:
        return df

    df = df.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)
    # 强制数值列类型，避免上游空值导致 object dtype 传播
    for col in (
        "price", "prev_close", "open", "high", "low", "change", "change_pct",
        "volume", "amount", "turnover_rate", "float_mcap", "total_mcap",
        "limit_up", "limit_down",
    ):
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    return df


def fetch_index_quotes(timeout: int = DEFAULT_TIMEOUT) -> dict[str, dict]:
    """
    拉主要宽基指数。

    :returns: ``{指数名: {"price":…, "change_pct":…}}``；拉不到的指数不出现在结果里。
    """
    session = _make_session()
    rows = _fetch_chunk(session, list(INDEX_CODES), timeout)

    # 用完整市场代码做键，避免指数代码与个股代码撞车
    # （上证指数和「平安银行」的 code 都是 000001）
    by_code: dict[str, dict] = {row["market_code"]: row for row in rows}

    result: dict[str, dict] = {}
    for market_code, display in INDEX_CODES.items():
        row = by_code.get(market_code)
        if row:
            result[display] = {
                "price": row["price"],
                "change_pct": row["change_pct"],
                "amount": row["amount"],
                "quote_time": row["quote_time"],
            }
    return result


def now_stamp() -> str:
    """当前时间戳，格式与腾讯 ``[30]`` 字段一致：``20261112153031``。"""
    return time.strftime("%Y%m%d%H%M%S")
