# -*- coding: utf-8 -*-
"""
申万行业分类映射
================
云图的分组骨架：把全市场个股挂到「一级行业 → 二级行业」下面。

为什么必须单独做这一层
----------------------
腾讯/新浪的行情接口只返回**代码、价格、涨跌幅、市值**，**不返回行业**。
而云图的核心恰恰是「按行业分块」。所以行业归属只能另外拉、另外缓存：

* 行业分类几个月才调整一次 → 没必要每次刷新都拉；
* 成分股 5000+ 条，拉一次约 60 秒 → 更不能每次刷新都拉。

于是拆成慢/快两条路径：

    慢路径（本模块）  拉行业树 + 每个二级行业的成分股 → 落 Parquet
    快路径（quotes）  腾讯批量行情 → 只补价格/涨跌幅/市值

数据源
------
akshare 封装的申万研究所公开接口：

=================================  ==================================
``sw_index_first_info()``          一级行业，31 个
``sw_index_second_info()``         二级行业，131 个（含「上级行业」）
``index_component_sw(symbol=...)`` 某个行业的成分股
=================================  ==================================

两个坑
------
1. ``index_component_sw`` 要传**纯数字**代码（``801016``），
   带 ``.SI`` 后缀会查不到——接口不报错，只返回空表。
2. 该接口走 HTTPS 且证书链不完整，akshare 内部用 requests 会刷
   ``InsecureRequestWarning``。不影响结果，这里显式屏蔽掉，免得日志被淹。
"""
from __future__ import annotations

import json
import time
import warnings
from pathlib import Path

import pandas as pd

from src.common.logger import get_logger
from src.common.paths import DATA_DIR

log = get_logger(__name__)

#: 行业映射缓存目录
INDUSTRY_DIR: Path = DATA_DIR / "industry"
#: 映射主体（Parquet）
MAP_FILE: Path = INDUSTRY_DIR / "sw_industry_map.parquet"
#: 元信息（记录构建时间、来源统计）
META_FILE: Path = INDUSTRY_DIR / "sw_industry_map.meta.json"

#: 默认缓存有效期：7 天。行业分类调整很慢，没必要更频繁。
DEFAULT_MAX_AGE_SEC: int = 7 * 24 * 3600

#: 映射表的列定义（顺序即落盘顺序）
MAP_COLUMNS: tuple[str, ...] = (
    "code",       # 纯 6 位代码，如 600000
    "name",       # 证券名称
    "l1_code",    # 一级行业代码，如 801080.SI
    "l1_name",    # 一级行业名称，如 电子
    "l2_code",    # 二级行业代码，如 801081.SI
    "l2_name",    # 二级行业名称，如 半导体
)

#: 没能归入任何申万行业的个股，统一挂到这个虚拟行业下
UNCLASSIFIED: tuple[str, str, str, str] = ("999999.SI", "未分类", "999998.SI", "未分类")


# ============================================================
# 缓存读写
# ============================================================


def map_path() -> Path:
    """行业映射 Parquet 的绝对路径。"""
    return MAP_FILE


def map_age_sec() -> float | None:
    """
    映射缓存已存在多久（秒）。文件不存在返回 ``None``。

    以文件修改时间为准，而不是读 meta——meta 可能被手删，
    但文件在就说明数据在。
    """
    if not MAP_FILE.exists():
        return None
    return max(0.0, time.time() - MAP_FILE.stat().st_mtime)


def is_fresh(max_age_sec: int = DEFAULT_MAX_AGE_SEC) -> bool:
    """缓存是否存在且未过期。"""
    age = map_age_sec()
    return age is not None and age < max_age_sec


def read_map() -> pd.DataFrame:
    """
    读取行业映射（不触发重建）。文件不存在时抛 ``FileNotFoundError``。

    返回的 DataFrame 一定包含 :data:`MAP_COLUMNS` 六列。
    """
    if not MAP_FILE.exists():
        raise FileNotFoundError(
            f"行业映射不存在：{MAP_FILE}\n"
            f"请先运行：python scripts/build_industry_map.py"
        )
    df = pd.read_parquet(MAP_FILE)
    missing = [c for c in MAP_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"行业映射缺少列 {missing}，文件可能已损坏：{MAP_FILE}")
    return df[list(MAP_COLUMNS)]


# ============================================================
# 构建
# ============================================================


def _strip_si(code: str) -> str:
    """``801016.SI`` → ``801016``。接口只认纯数字。"""
    return str(code).split(".")[0].strip()


def _fetch_industry_tree() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    拉一级、二级行业表。

    返回 ``(一级, 二级)`` 两个 DataFrame，均已规范化列名。
    """
    import akshare as ak

    # akshare 走的是证书链不完整的 HTTPS，屏蔽噪声警告
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        first = ak.sw_index_first_info()
        second = ak.sw_index_second_info()

    first = first.rename(columns={"行业代码": "l1_code", "行业名称": "l1_name"})
    second = second.rename(
        columns={"行业代码": "l2_code", "行业名称": "l2_name", "上级行业": "l1_name"}
    )
    return first[["l1_code", "l1_name"]], second[["l2_code", "l2_name", "l1_name"]]


def build_map(*, verbose: bool = True) -> pd.DataFrame:
    """
    全量重建行业映射并落盘。

    流程：拉行业树 → 逐个二级行业拉成分股 → 挂上一级行业 → 原子写 Parquet。

    131 个二级行业 × 约 0.5 秒 ≈ 60~90 秒，属于**慢操作**，
    只在缓存缺失/过期时由 :func:`load_map` 触发。

    :param verbose: 是否打印进度（CLI 用 True，后台用 False）
    :returns: 映射 DataFrame
    """
    import akshare as ak

    INDUSTRY_DIR.mkdir(parents=True, exist_ok=True)

    l1, l2 = _fetch_industry_tree()
    l1_name_to_code: dict[str, str] = dict(zip(l1["l1_name"], l1["l1_code"]))

    if verbose:
        log.info("行业树：一级 %d 个，二级 %d 个", len(l1), len(l2))

    rows: list[dict] = []
    failed: list[str] = []

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        for i, (l2_code, l2_name, l1_name) in enumerate(
            zip(l2["l2_code"], l2["l2_name"], l2["l1_name"]), start=1
        ):
            symbol = _strip_si(l2_code)
            try:
                cons = ak.index_component_sw(symbol=symbol)
            except Exception as exc:  # noqa: BLE001 - 单个行业失败不能中断整体
                failed.append(f"{l2_name}({symbol}): {type(exc).__name__}")
                if verbose:
                    log.warning("成分股拉取失败 %s(%s)：%s", l2_name, symbol, exc)
                continue

            if cons is None or cons.empty:
                failed.append(f"{l2_name}({symbol}): 空表")
                continue

            # 该接口列名是「证券代码 / 证券名称」
            code_col = "证券代码" if "证券代码" in cons.columns else cons.columns[1]
            name_col = "证券名称" if "证券名称" in cons.columns else cons.columns[2]

            for code, name in zip(cons[code_col], cons[name_col]):
                rows.append(
                    {
                        "code": str(code).zfill(6),
                        "name": str(name).strip(),
                        "l1_code": l1_name_to_code.get(l1_name, ""),
                        "l1_name": l1_name,
                        "l2_code": l2_code,
                        "l2_name": l2_name,
                    }
                )

            if verbose and (i % 20 == 0 or i == len(l2)):
                log.info("  成分股进度 %d/%d，累计 %d 条", i, len(l2), len(rows))

    if not rows:
        raise RuntimeError("行业映射构建失败：没有拉到任何成分股")

    df = pd.DataFrame(rows, columns=list(MAP_COLUMNS))
    # 同一只股票可能出现在多个行业（极少），保留第一条
    df = df.drop_duplicates(subset=["code"], keep="first").reset_index(drop=True)

    # --- 原子写：先写临时文件再改名，避免读到半截 Parquet ---
    tmp = MAP_FILE.with_suffix(".parquet.tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(MAP_FILE)

    META_FILE.write_text(
        json.dumps(
            {
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "built_at_ts": time.time(),
                "stock_count": int(len(df)),
                "l1_count": int(df["l1_name"].nunique()),
                "l2_count": int(df["l2_name"].nunique()),
                "failed_industries": failed,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if verbose:
        log.info(
            "行业映射已写入 %s（%d 只 / %d 个一级 / %d 个二级，失败 %d 个行业）",
            MAP_FILE.name,
            len(df),
            df["l1_name"].nunique(),
            df["l2_name"].nunique(),
            len(failed),
        )
    return df


def load_map(
    *,
    max_age_sec: int = DEFAULT_MAX_AGE_SEC,
    auto_build: bool = True,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    读取行业映射；缓存缺失或过期时自动重建。

    :param max_age_sec: 缓存有效期，超过则重建
    :param auto_build: 为 False 时只读缓存，不重建（适合离线环境）
    :raises FileNotFoundError: 缓存不存在且 ``auto_build=False``
    """
    if is_fresh(max_age_sec):
        return read_map()

    if not auto_build:
        return read_map()

    age = map_age_sec()
    if verbose:
        if age is None:
            log.info("行业映射缓存不存在，开始构建（约 1~2 分钟）…")
        else:
            log.info("行业映射已过期（%.1f 天），开始重建…", age / 86400)
    return build_map(verbose=verbose)


def attach_industry(quotes: pd.DataFrame, mapping: pd.DataFrame) -> pd.DataFrame:
    """
    给行情表挂上行业列。**未匹配到的个股归入「未分类」**，而不是丢弃。

    丢弃会让云图的个股总数对不上大盘，用户会以为数据缺了；
    归入「未分类」则一眼能看出是哪几只没归上。

    :param quotes: 至少含 ``code`` 列
    :param mapping: :func:`read_map` 的返回值
    """
    out = quotes.merge(mapping[["code", "l1_code", "l1_name", "l2_code", "l2_name"]],
                       on="code", how="left")

    for col, default in (
        ("l1_code", UNCLASSIFIED[0]),
        ("l1_name", UNCLASSIFIED[1]),
        ("l2_code", UNCLASSIFIED[2]),
        ("l2_name", UNCLASSIFIED[3]),
    ):
        out[col] = out[col].fillna(default)

    return out
