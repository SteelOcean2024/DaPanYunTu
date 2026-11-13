# -*- coding: utf-8 -*-
"""
全市场股票代码表
================
云图要覆盖「全部 A 股」，而申万行业成分股并不等于全部 A 股
（次新股、B 股、部分北交所票可能还没被纳入行业分类）。

所以除了行业映射，还需要一份**完整代码表**做并集，
否则云图个股数会明显少于大盘实际家数，用户会以为数据缺了。

代码表变化很慢（只有新股上市/退市才变），因此和行业映射一样走缓存：
akshare ``stock_info_a_code_name()`` 实测约 42 秒 / 5580 只，缓存 7 天。
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

from src.common.logger import get_logger
from src.common.paths import DATA_DIR

log = get_logger(__name__)

#: 缓存目录（与行业映射同级）
HEATMAP_DIR: Path = DATA_DIR / "heatmap"
#: 代码表文件
UNIVERSE_FILE: Path = HEATMAP_DIR / "universe.parquet"

#: 默认有效期 7 天
DEFAULT_MAX_AGE_SEC: int = 7 * 24 * 3600

COLUMNS: tuple[str, ...] = ("code", "name")


def build_universe(*, verbose: bool = True) -> pd.DataFrame:
    """
    拉取全市场代码表并落盘（约 42 秒）。

    :returns: 含 ``code`` / ``name`` 两列的 DataFrame
    """
    import akshare as ak

    HEATMAP_DIR.mkdir(parents=True, exist_ok=True)

    raw = ak.stock_info_a_code_name()
    df = pd.DataFrame(
        {
            "code": raw["code"].astype(str).str.zfill(6),
            "name": raw["name"].astype(str).str.strip(),
        }
    )
    df = df.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)

    tmp = UNIVERSE_FILE.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(UNIVERSE_FILE)

    if verbose:
        log.info("代码表已写入 %s（%d 只）", UNIVERSE_FILE.name, len(df))
    return df


def read_universe() -> pd.DataFrame:
    """读代码表（不重建）。文件不存在时抛 ``FileNotFoundError``。"""
    if not UNIVERSE_FILE.exists():
        raise FileNotFoundError(
            f"代码表不存在：{UNIVERSE_FILE}\n"
            f"请先运行：python scripts/build_industry_map.py"
        )
    return pd.read_parquet(UNIVERSE_FILE)[list(COLUMNS)]


def load_universe(
    *,
    max_age_sec: int = DEFAULT_MAX_AGE_SEC,
    auto_build: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    读代码表；缓存缺失或过期时自动重建。

    :param auto_build: 为 False 时只读缓存
    """
    if UNIVERSE_FILE.exists():
        age = max(0.0, time.time() - UNIVERSE_FILE.stat().st_mtime)
        if age < max_age_sec:
            return read_universe()
        if verbose:
            log.info("代码表已过期（%.1f 天），重建中…", age / 86400)
    elif verbose:
        log.info("代码表缓存不存在，开始拉取（约 40 秒）…")

    if not auto_build:
        return read_universe()
    return build_universe(verbose=verbose)
