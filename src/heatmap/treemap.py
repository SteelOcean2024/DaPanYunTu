# -*- coding: utf-8 -*-
"""
云图组装
========
把「行业映射 + 全市场行情」拼成前端直接可渲染的嵌套树，并算出顶部汇总。

输出结构（三级）
----------------
::

    {
      "market": "all",
      "metric": "total_mcap",
      "updatedAt": "2026-11-12T15:30:31+08:00",
      "stockCount": 5480,
      "boardCount": 31,
      "summary": { ... },
      "nodes": [                      # 一级行业
        {
          "code": "801080.SI", "name": "电子",
          "value": 15921856668306,    # 面积 = 该行业市值合计
          "changePct": 0.81,          # 市值加权涨跌幅
          "stockCount": 530,
          "children": [               # 二级行业
            {
              "code": "801081.SI", "name": "半导体",
              "value": ..., "changePct": ..., "stockCount": 150,
              "children": [           # 个股（叶子）
                {
                  "code": "601138.SH", "name": "工业富联",
                  "value": 1271410992636,
                  "price": 43.95, "changePct": -3.53,
                  "turnoverAmount": 5177950000,
                  "totalMarketCap": ..., "floatMarketCap": ...,
                  "boardName": "电子", "subBoardName": "消费电子"
                }
              ]
            }
          ]
        }
      ]
    }

关于「面积口径」
----------------
``metric`` 决定 ``value`` 用哪个字段：

===============  ==================
metric           面积代表
===============  ==================
``total_mcap``   总市值（默认）
``float_mcap``   流通市值
``amount``       当日成交额
===============  ==================

**涨跌幅一律按 ``value`` 加权**，不是简单平均——
否则一只 20 亿的小票能和 2 万亿的工商银行等价影响板块颜色，
板块颜色就完全失真了。
"""
from __future__ import annotations

import time

import pandas as pd

#: 支持的面积口径
METRICS: dict[str, str] = {
    "total_mcap": "总市值",
    "float_mcap": "流通市值",
    "amount": "成交额",
}

#: 默认面积口径
DEFAULT_METRIC = "total_mcap"


def _weighted_change(group: pd.DataFrame, value_col: str) -> float:
    """
    按 ``value_col`` 加权计算涨跌幅（%）。

    权重全为 0 时回落到算术平均，避免除零。
    """
    weights = group[value_col].to_numpy(dtype="float64")
    pct = group["change_pct"].to_numpy(dtype="float64")
    total = weights.sum()
    if total <= 0:
        return float(pct.mean()) if len(pct) else 0.0
    return float((weights * pct).sum() / total)


def _stock_node(row) -> dict:
    """把一行行情转成云图叶子节点。"""
    return {
        "code": f"{row.code}.{row.exchange}",
        "name": row.name,
        "value": float(row.value),
        "price": round(float(row.price), 3),
        "changePct": round(float(row.change_pct), 2),
        "turnoverAmount": float(row.amount),
        "totalMarketCap": float(row.total_mcap),
        "floatMarketCap": float(row.float_mcap),
        "boardName": row.l1_name,
        "subBoardName": row.l2_name,
    }


def build_summary(df: pd.DataFrame, index_quotes: dict | None = None) -> dict:
    """
    顶部汇总：涨跌家数、成交额、加权涨跌幅、涨跌停家数。

    :param df: 已挂行业且含 ``value`` 列的行情表
    :param index_quotes: :func:`src.heatmap.quotes.fetch_index_quotes` 的结果
    """
    pct = df["change_pct"]
    advance = int((pct > 0).sum())
    decline = int((pct < 0).sum())
    flat = int((pct == 0).sum())

    # 涨停/跌停：现价触及涨跌停价（容差 1 分钱）
    limit_up = int(((df["price"] >= df["limit_up"] - 0.005) & (df["limit_up"] > 0)).sum())
    limit_down = int(((df["price"] <= df["limit_down"] + 0.005) & (df["limit_down"] > 0)).sum())

    summary = {
        "advanceCount": advance,
        "flatCount": flat,
        "declineCount": decline,
        "stockCount": int(len(df)),
        "limitUpCount": limit_up,
        "limitDownCount": limit_down,
        "turnoverAmount": float(df["amount"].sum()),
        "indexChangePct": round(_weighted_change(df, "total_mcap"), 3),
        "countsSource": "tencent",
        "turnoverSource": "tencent",
    }

    if index_quotes:
        summary["indexes"] = {
            name: {"price": round(q["price"], 2), "changePct": round(q["change_pct"], 2)}
            for name, q in index_quotes.items()
        }
    return summary


def build_treemap(
    quotes: pd.DataFrame,
    *,
    metric: str = DEFAULT_METRIC,
    market: str = "all",
    index_quotes: dict | None = None,
    min_stock_value: float = 0.0,
) -> dict:
    """
    组装云图 JSON。

    :param quotes: 已挂行业的行情表（含 ``l1_name`` / ``l2_name`` 列）
    :param metric: 面积口径，见 :data:`METRICS`
    :param market: 市场标识，原样回传（``all`` / ``etf`` / ``cb`` / ``hk``）
    :param index_quotes: 指数行情，放进 summary
    :param min_stock_value: 面积小于该值的个股直接丢弃（默认不过滤）
    :raises ValueError: ``metric`` 不认识，或过滤后没有数据
    """
    if metric not in METRICS:
        raise ValueError(f"未知的面积口径 {metric!r}，可选：{list(METRICS)}")
    if quotes is None or quotes.empty:
        raise ValueError("行情表为空，无法组装云图")

    df = quotes.copy()
    df["value"] = pd.to_numeric(df[metric], errors="coerce").fillna(0.0).astype("float64")

    if min_stock_value > 0:
        df = df[df["value"] >= min_stock_value]

    # 面积为 0 的个股（例如成交额为 0 的停牌股）会让方块退化成一条线，
    # 直接剔除，否则前端要处理零宽矩形。
    df = df[df["value"] > 0]
    if df.empty:
        raise ValueError(f"按 {metric} 过滤后没有任何个股，无法组装云图")

    nodes: list[dict] = []
    for l1_name, g1 in df.groupby("l1_name", sort=False):
        subs: list[dict] = []
        for l2_name, g2 in g1.groupby("l2_name", sort=False):
            stocks = [
                _stock_node(row)
                for row in g2.sort_values("value", ascending=False).itertuples(index=False)
            ]
            subs.append(
                {
                    "code": str(g2["l2_code"].iloc[0]),
                    "name": l2_name,
                    "value": float(g2["value"].sum()),
                    "changePct": round(_weighted_change(g2, "value"), 2),
                    "stockCount": int(len(g2)),
                    "children": stocks,
                }
            )

        subs.sort(key=lambda n: n["value"], reverse=True)
        nodes.append(
            {
                "code": str(g1["l1_code"].iloc[0]),
                "name": l1_name,
                "value": float(g1["value"].sum()),
                "changePct": round(_weighted_change(g1, "value"), 2),
                "stockCount": int(len(g1)),
                "children": subs,
            }
        )

    # 大板块排前面：前端按顺序切分，顺序稳定则每次刷新方块位置不会乱跳
    nodes.sort(key=lambda n: n["value"], reverse=True)

    return {
        "market": market,
        "period": "day",
        "metric": metric,
        "metricLabel": METRICS[metric],
        "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
        "stockCount": int(len(df)),
        "boardCount": len(nodes),
        "summary": build_summary(df, index_quotes),
        "nodes": nodes,
    }
