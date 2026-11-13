# -*- coding: utf-8 -*-
"""
板块强度指标
============
把「全市场个股行情」聚合成「板块级强度」。

它回答的不是「今天谁涨了」，而是「哪个板块**真的**在动」。

为什么不能只看板块涨跌幅
------------------------
板块涨跌幅是**结果**，它丢了三件事：

1. **跑赢没跑赢**——普涨日所有板块都红。要看 ``rsMedian``（相对强度 =
   板块中位数涨幅 − 全市场中位数涨幅）。
2. **真动还是假动**——板块 +2% 可能是 80% 的票在涨，也可能只是 1 只权重股拉的。
   要看 ``breadth``（上涨家数占比）。
3. **大票还是小票在动**——``spread`` = 市值加权 − 中位数。
   大于 0 说明是权重行情，贴近 0 才是普涨。

再加上 ``amountShare``（该板块成交额 / 全市场成交额）。
**资金占比是比涨跌幅更早的信号**：涨跌幅是结果，资金占比是原因。
产业级事件当天，相关板块的成交额占比会先跳，价格随后跟上。

口径约定
--------
- ``change_pct`` 是**百分数**（``-2.61`` 表示 -2.61%），与腾讯报文、云图接口一致；
- ``amount`` / ``*Mcap`` 是**元**（上游 :mod:`src.heatmap.quotes` 已换算好）。

基线类指标（5 日 / 20 日 RS、放量倍数）需要历史，本模块只算**截面**指标；
历史由 :func:`append_snapshot` 逐日累积。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from src.common.logger import get_logger
from src.common.paths import DATA_DIR

log = get_logger(__name__)

#: 快照存放目录
SECTOR_DIR: Path = DATA_DIR / "sector"
#: 快照文件（JSONL，追加写；每 5 分钟最多一条）
SNAPSHOT_FILE: Path = SECTOR_DIR / "snapshots.jsonl"
#: 快照最小间隔（秒）。后台每 5 秒刷一轮，不加这个限制一天能写几万条。
SNAPSHOT_INTERVAL_SEC = 300.0

#: 没能归入任何申万行业的个股统一挂在这个名字下。
#: 它**不是板块**，但保留在列表里——丢掉会让板块家数之和对不上大盘。
UNCLASSIFIED = "未分类"

_last_snapshot_bucket: int | None = None


def _aggregate(name: str, level: str, group, market: dict | None) -> dict:
    """把一个板块的个股聚合成一条强度记录。"""
    n = int(len(group))
    pct = group["change_pct"]
    mcap = group["total_mcap"]
    amount = group["amount"]

    mcap_sum = float(mcap.sum())
    # 市值加权：指数口径。权重全为 0（数据异常）时退回等权，避免除零
    capw = float((mcap * pct).sum() / mcap_sum) if mcap_sum > 0 else float(pct.mean())
    median = float(pct.median())

    up = int((pct > 0).sum())
    down = int((pct < 0).sum())
    # 涨停判定：容差 0.005 元，避免浮点比较漏掉正好等于涨停价的票
    limit_up = int(
        ((group["price"] >= group["limit_up"] - 0.005) & (group["limit_up"] > 0)).sum()
    )
    amt = float(amount.sum())

    # 板块内成交额最大的那只票——「谁在动」的直接答案
    leader_code = leader_name = ""
    leader_pct = 0.0
    if n:
        top = group.loc[amount.idxmax()] if amount.notna().any() else None
        if top is not None:
            leader_code = str(top.get("code", ""))
            leader_name = str(top.get("name", ""))
            leader_pct = round(float(top.get("change_pct", 0.0)), 2)

    rec = {
        "name": name,
        "level": level,
        "stockCount": n,
        "upCount": up,
        "downCount": down,
        "flatCount": n - up - down,
        "limitUpCount": limit_up,
        "capwPct": round(capw, 3),
        "medianPct": round(median, 3),
        "spread": round(capw - median, 3),
        "breadth": round(up / n, 4) if n else 0.0,
        "amount": amt,
        "totalMcap": mcap_sum,
        "leaderCode": leader_code,
        "leaderName": leader_name,
        "leaderPct": leader_pct,
    }

    if market is None:
        rec["amountShare"] = 1.0
        rec["rsCapw"] = 0.0
        rec["rsMedian"] = 0.0
    else:
        m_amt = float(market.get("amount", 0.0))
        rec["amountShare"] = round(amt / m_amt, 6) if m_amt > 0 else 0.0
        rec["rsCapw"] = round(capw - float(market.get("capwPct", 0.0)), 3)
        rec["rsMedian"] = round(median - float(market.get("medianPct", 0.0)), 3)
    return rec


def compute_sector_strength(df, *, updated_at: str | None = None) -> dict:
    """
    把个股行情聚合成板块强度。**纯函数，不取数、不落盘。**

    :param df: 已挂好行业列的行情表，需含 ``change_pct`` / ``amount`` /
        ``total_mcap`` / ``price`` / ``limit_up`` / ``l1_name`` / ``l2_name``
    :param updated_at: 数据时间戳（原样回传，供前端显示）
    :returns: ``{"updatedAt":…, "market":{…}, "l1":[…], "l2":[…]}``；
        ``l1`` / ``l2`` 按 ``rsMedian`` 从强到弱排序
    """
    empty = {"updatedAt": updated_at, "market": {}, "l1": [], "l2": []}
    if df is None or len(df) == 0:
        return empty

    market = _aggregate("全市场", "market", df, None)
    out: dict = {"updatedAt": updated_at, "market": market, "l1": [], "l2": []}

    for key, col in (("l1", "l1_name"), ("l2", "l2_name")):
        if col not in df.columns:
            continue
        rows = [
            _aggregate(str(name), key, group, market)
            for name, group in df.groupby(col, sort=False)
            if str(name).strip()
        ]
        rows.sort(key=lambda r: r["rsMedian"], reverse=True)
        out[key] = rows
    return out


def append_snapshot(strength: dict, *, now: float | None = None) -> bool:
    """
    把**一级行业**快照追加到 JSONL，用于以后算 5 日 / 20 日基线与放量倍数。

    按 ``SNAPSHOT_INTERVAL_SEC`` 分桶限流：后台每 5 秒刷一轮，
    不加限流一天会写几万条，而且盘中没有额外信息量。

    :returns: 真正写入返回 ``True``，被限流跳过返回 ``False``
    """
    global _last_snapshot_bucket

    ts = time.time() if now is None else now
    bucket = int(ts // SNAPSHOT_INTERVAL_SEC)
    if bucket == _last_snapshot_bucket:
        return False

    rows = strength.get("l1") or []
    if not rows:
        return False

    record = {
        "ts": round(ts, 3),
        "updatedAt": strength.get("updatedAt"),
        "market": strength.get("market", {}),
        "l1": rows,
    }

    try:
        SECTOR_DIR.mkdir(parents=True, exist_ok=True)
        with SNAPSHOT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
    except OSError as exc:
        # 落盘失败不该影响实时刷新，记一条警告继续
        log.warning("板块快照写入失败：%s", exc)
        return False

    _last_snapshot_bucket = bucket
    return True
