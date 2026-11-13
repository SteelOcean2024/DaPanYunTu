# -*- coding: utf-8 -*-
"""
板块强度指标测试
================
全部是针对 :func:`src.analysis.sector.compute_sector_strength` 的**纯函数**测试，
不联网、不落盘（落盘那两条用 tmp_path 隔离）。

口径备忘：
- ``change_pct`` 是百分数（``2.0`` 表示 +2%）；
- ``amount`` / ``total_mcap`` 是元；
- ``rsMedian`` = 板块中位数 − 全市场中位数。
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from src.analysis import sector as sector_mod


def make_df() -> pd.DataFrame:
    """两只票一个板块，两个板块：甲(2.0, 1.0) / 乙(-1.0, -2.0)。"""
    return pd.DataFrame(
        {
            "code": ["600000", "600001", "600002", "600003"],
            "name": ["A", "B", "C", "D"],
            "change_pct": [2.0, 1.0, -1.0, -2.0],
            "amount": [100.0, 50.0, 30.0, 20.0],
            "total_mcap": [1e10, 2e10, 3e10, 4e10],
            "price": [10.0, 20.0, 30.0, 40.0],
            "limit_up": [0.0, 0.0, 0.0, 0.0],
            "l1_name": ["甲", "甲", "乙", "乙"],
            "l2_name": ["甲1", "甲1", "乙1", "乙1"],
        }
    )


def by_name(result: dict, level: str, name: str) -> dict:
    for row in result[level]:
        if row["name"] == name:
            return row
    raise AssertionError(f"{level} 里没有 {name}")


def test_empty_input_returns_empty_shape() -> None:
    out = sector_mod.compute_sector_strength(pd.DataFrame())
    assert out["l1"] == [] and out["l2"] == []
    assert out["market"] == {}


def test_market_median_and_capw() -> None:
    out = sector_mod.compute_sector_strength(make_df())
    m = out["market"]
    assert m["stockCount"] == 4
    # 中位数 = 中间两个的平均 = (1 + -1) / 2
    assert m["medianPct"] == pytest.approx(0.0)
    # 市值加权 = (1e10*2 + 2e10*1 + 3e10*-1 + 4e10*-2) / 1e11 = -0.7
    assert m["capwPct"] == pytest.approx(-0.7, abs=1e-6)
    assert m["amountShare"] == pytest.approx(1.0)
    assert m["rsMedian"] == pytest.approx(0.0)


def test_sector_median_is_typical_stock_not_weighted() -> None:
    """甲板块：市值小的票涨得多，市值大的涨得少 —— 中位数 1.5、加权 1.333。"""
    out = sector_mod.compute_sector_strength(make_df())
    jia = by_name(out, "l1", "甲")
    assert jia["medianPct"] == pytest.approx(1.5)
    # 接口里 round 到 3 位小数，所以容差按 1e-3 给
    assert jia["capwPct"] == pytest.approx(4e10 / 3e10, abs=1e-3)
    # spread = 加权 − 中位数 < 0：小票在拉
    assert jia["spread"] == pytest.approx(4 / 3 - 1.5, abs=1e-3)
    assert jia["spread"] < 0


def test_rs_is_sector_minus_market() -> None:
    out = sector_mod.compute_sector_strength(make_df())
    m = out["market"]["medianPct"]
    for name in ("甲", "乙"):
        row = by_name(out, "l1", name)
        assert row["rsMedian"] == pytest.approx(row["medianPct"] - m, abs=1e-6)
    assert by_name(out, "l1", "甲")["rsMedian"] > 0
    assert by_name(out, "l1", "乙")["rsMedian"] < 0


def test_breadth_and_counts() -> None:
    out = sector_mod.compute_sector_strength(make_df())
    jia = by_name(out, "l1", "甲")
    assert (jia["upCount"], jia["downCount"], jia["flatCount"]) == (2, 0, 0)
    assert jia["breadth"] == pytest.approx(1.0)
    yi = by_name(out, "l1", "乙")
    assert (yi["upCount"], yi["downCount"], yi["flatCount"]) == (0, 2, 0)
    assert yi["breadth"] == pytest.approx(0.0)


def test_amount_share_sums_to_one() -> None:
    out = sector_mod.compute_sector_strength(make_df())
    # 「未分类」也在列表里，所以直接对全部求和
    assert sum(r["amountShare"] for r in out["l1"]) == pytest.approx(1.0)
    assert by_name(out, "l1", "甲")["amountShare"] == pytest.approx(0.75)


def test_sorted_by_rs_desc() -> None:
    out = sector_mod.compute_sector_strength(make_df())
    rs = [r["rsMedian"] for r in out["l1"]]
    assert rs == sorted(rs, reverse=True)
    assert out["l1"][0]["name"] == "甲"


def test_both_levels_present() -> None:
    out = sector_mod.compute_sector_strength(make_df())
    assert {r["name"] for r in out["l1"]} == {"甲", "乙"}
    assert {r["name"] for r in out["l2"]} == {"甲1", "乙1"}
    assert all(r["level"] == "l2" for r in out["l2"])


def test_leader_is_top_amount() -> None:
    out = sector_mod.compute_sector_strength(make_df())
    assert by_name(out, "l1", "甲")["leaderName"] == "A"
    assert by_name(out, "l1", "甲")["leaderPct"] == pytest.approx(2.0)
    assert by_name(out, "l1", "乙")["leaderName"] == "C"


def test_limit_up_detection() -> None:
    df = make_df()
    df.loc[0, "limit_up"] = df.loc[0, "price"]      # 正好涨停
    df.loc[1, "limit_up"] = df.loc[1, "price"] + 1  # 涨停价还在上方，没到
    df.loc[2, "limit_up"] = 0.0                     # 无涨停价（停牌等），不算
    out = sector_mod.compute_sector_strength(df)
    assert by_name(out, "l1", "甲")["limitUpCount"] == 1
    assert by_name(out, "l1", "乙")["limitUpCount"] == 0


def test_unclassified_bucket_is_kept() -> None:
    """未分类不是板块，但不能丢——丢了板块家数之和对不上大盘。"""
    df = make_df()
    df.loc[0, "l1_name"] = sector_mod.UNCLASSIFIED
    out = sector_mod.compute_sector_strength(df)
    assert sector_mod.UNCLASSIFIED in {r["name"] for r in out["l1"]}
    assert sum(r["stockCount"] for r in out["l1"]) == len(df)


def test_missing_industry_column_is_skipped() -> None:
    df = make_df().drop(columns=["l2_name"])
    out = sector_mod.compute_sector_strength(df)
    assert out["l2"] == []
    assert len(out["l1"]) == 2


def test_updated_at_passthrough() -> None:
    out = sector_mod.compute_sector_strength(make_df(), updated_at="2026-01-01T10:00:00")
    assert out["updatedAt"] == "2026-01-01T10:00:00"


# ----------------------------------------------------------------------
# 落盘限流
# ----------------------------------------------------------------------


@pytest.fixture
def snap(tmp_path, monkeypatch):
    """把快照目录指到 tmp_path，并复位限流状态。"""
    monkeypatch.setattr(sector_mod, "SECTOR_DIR", tmp_path)
    monkeypatch.setattr(sector_mod, "SNAPSHOT_FILE", tmp_path / "snapshots.jsonl")
    monkeypatch.setattr(sector_mod, "_last_snapshot_bucket", None)
    return tmp_path / "snapshots.jsonl"


def test_append_snapshot_writes_once_per_bucket(snap) -> None:
    strength = sector_mod.compute_sector_strength(make_df(), updated_at="T")
    assert sector_mod.append_snapshot(strength, now=1000.0) is True
    # 同一个 5 分钟桶内不再写
    assert sector_mod.append_snapshot(strength, now=1001.0) is False
    # 跨桶后恢复
    assert sector_mod.append_snapshot(
        strength, now=1000.0 + sector_mod.SNAPSHOT_INTERVAL_SEC
    ) is True
    lines = snap.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["updatedAt"] == "T"
    assert len(rec["l1"]) == 2
    assert "market" in rec


def test_append_snapshot_skips_when_no_rows(snap) -> None:
    assert sector_mod.append_snapshot({"l1": []}, now=1000.0) is False
    assert not snap.exists()
