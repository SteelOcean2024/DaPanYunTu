# -*- coding: utf-8 -*-
"""
导出前端静态数据
================
把「慢路径」的行业映射 + 全市场代码表导出成浏览器能直接用的 JSON。

为什么需要
----------
本地跑的时候，行业映射由 Python 从 Parquet 读出来；但像 GitHub Pages 这类
**没有 Python 后端**的静态部署，浏览器只能拿到静态文件。
所以把映射预先导出成 JSON 放进仓库，前端自己加载。

为什么必须带上「代码表」而不只是「行业映射」
----------------------------------------
行业映射只覆盖 5222 只（申万不收录北交所、部分次新），而全市场是 5580 只。
只导出映射的话，浏览器算出来的「全市场中位数」是 5222 只的中位数，
跟后端 5580 只的结果**对不上**——实测差 0.04 个百分点，RS 全体偏移。
所以这里导出的是 **代码表 ∪ 行业映射**，没归上行业的记为「未分类」，
和后端 attach_industry() 的口径完全一致。

导出格式刻意压缩过——重复的行业名只存一次，用下标引用：

    {
      "generatedAt": "...",
      "l1": ["银行", "电子", ...],           # 一级行业名，去重
      "l2": ["国有大型银行Ⅱ", ...],          # 二级行业名，去重
      "l2Parent": [0, 0, 3, ...],           # 二级行业下标 → 一级行业下标
      "stocks": [["600000", 0, 0], ...]     # [代码, 一级下标, 二级下标]
    }

用法
----
    python scripts/export_web_data.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logger import get_logger, setup_logging  # noqa: E402
from src.common.paths import WEB_DIR  # noqa: E402
from src.heatmap import industry as industry_mod  # noqa: E402
from src.heatmap import universe as universe_mod  # noqa: E402

log = get_logger(__name__)

#: 输出位置（前端以 ../data/industry-map.json 引用）
OUT_FILE: Path = WEB_DIR / "data" / "industry-map.json"

#: 没能归入任何申万行业的个股，与后端 :data:`src.analysis.sector.UNCLASSIFIED` 一致
UNCLASSIFIED = "未分类"


def build_payload(industry_df, codes) -> dict:
    """把「代码表 + 行业映射」压成「名字去重 + 下标引用」的紧凑结构。"""
    l1_names: list[str] = []
    l1_index: dict[str, int] = {}
    l2_names: list[str] = []
    l2_index: dict[str, int] = {}
    l2_parent: list[int] = []

    def intern(l1: str, l2: str) -> tuple[int, int]:
        if l1 not in l1_index:
            l1_index[l1] = len(l1_names)
            l1_names.append(l1)
        i1 = l1_index[l1]
        if l2 in l2_index:
            return i1, l2_index[l2]
        i2 = len(l2_names)
        l2_index[l2] = i2
        l2_names.append(l2)
        l2_parent.append(i1)
        return i1, i2

    mapped: dict[str, tuple[str, str]] = {}
    for row in industry_df.itertuples(index=False):
        mapped[str(row.code)] = (
            str(getattr(row, "l1_name", "") or UNCLASSIFIED),
            str(getattr(row, "l2_name", "") or UNCLASSIFIED),
        )

    stocks: list[list] = []
    for code in codes:
        l1, l2 = mapped.get(code, (UNCLASSIFIED, UNCLASSIFIED))
        i1, i2 = intern(l1, l2)
        stocks.append([code, i1, i2])

    return {
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "l1": l1_names,
        "l2": l2_names,
        "l2Parent": l2_parent,
        "stocks": stocks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="导出前端静态数据（行业映射 + 代码表）")
    parser.add_argument("--out", default=str(OUT_FILE), help="输出文件路径")
    args = parser.parse_args()

    setup_logging("INFO")
    # 不自动构建：这里只做导出，缺缓存应提示用户先跑 build_industry_map.py
    industry_df = industry_mod.load_map(auto_build=False)
    if industry_df is None or len(industry_df) == 0:
        log.error("行业映射为空，请先运行：python scripts/build_industry_map.py")
        return 1
    universe_df = universe_mod.load_universe(auto_build=False)

    # 代码表 ∪ 行业映射，与后端 HeatmapService.prepare() 的并集口径一致
    codes = sorted(set(universe_df["code"]) | set(industry_df["code"]))
    payload = build_payload(industry_df, codes)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    log.info(
        "已导出 %s：%d 只（代码表 %d ∪ 行业映射 %d）/ %d 个一级 / %d 个二级 / %.0f KB",
        out,
        len(payload["stocks"]),
        len(universe_df),
        len(industry_df),
        len(payload["l1"]),
        len(payload["l2"]),
        out.stat().st_size / 1024,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
