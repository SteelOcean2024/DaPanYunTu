# -*- coding: utf-8 -*-
"""
构建云图的「慢路径」缓存
========================
跑一次，把下面两样东西拉到本地：

1. **申万行业映射**——每只股票属于哪个一级/二级行业（约 60~90 秒）
2. **全市场代码表**——全部 A 股代码与名称（约 40 秒）

之后云图服务每次刷新只需要拉行情（约 2 秒），不用再碰这两样。

用法
----
::

    python scripts/build_industry_map.py            # 缓存缺失/过期才重建
    python scripts/build_industry_map.py --force    # 强制重建
    python scripts/build_industry_map.py --status   # 只看缓存状态

缓存位置
--------
``runtime/data/industry/`` 与 ``runtime/data/heatmap/``
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许直接以 `python scripts/xxx.py` 运行（把项目根加入 import 路径）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logger import get_logger, setup_logging  # noqa: E402
from src.heatmap import industry as industry_mod  # noqa: E402
from src.heatmap import universe as universe_mod  # noqa: E402

log = get_logger(__name__)


def _file_age(path: Path) -> float | None:
    """文件存在多久（秒）；不存在返回 None。"""
    if not path.exists():
        return None
    return max(0.0, time.time() - path.stat().st_mtime)


def show_status() -> None:
    """打印两个缓存的现状。"""
    print("缓存状态")
    print("-" * 56)

    rows = (
        ("申万行业映射", industry_mod.map_path(), industry_mod.map_age_sec()),
        ("全市场代码表", universe_mod.UNIVERSE_FILE, _file_age(universe_mod.UNIVERSE_FILE)),
    )
    for label, path, age in rows:
        if age is None:
            print(f"  {label:<12} 不存在            {path}")
        else:
            fresh = "有效" if age < industry_mod.DEFAULT_MAX_AGE_SEC else "已过期"
            print(f"  {label:<12} {age / 86400:>6.2f} 天  {fresh}  {path}")

    meta = industry_mod.META_FILE
    if meta.exists():
        print("\n行业映射元信息：")
        print(meta.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="构建大盘云图所需的行业映射与代码表缓存"
    )
    parser.add_argument("--force", action="store_true", help="强制重建，忽略有效期")
    parser.add_argument("--status", action="store_true", help="只显示缓存状态，不构建")
    parser.add_argument("--industry-only", action="store_true", help="只重建行业映射")
    parser.add_argument("--universe-only", action="store_true", help="只重建代码表")
    args = parser.parse_args()

    setup_logging()

    if args.status:
        show_status()
        return 0

    t0 = time.time()
    do_industry = not args.universe_only
    do_universe = not args.industry_only

    if do_industry:
        if args.force or not industry_mod.is_fresh():
            industry_mod.build_map(verbose=True)
        else:
            log.info("行业映射缓存仍有效，跳过（--force 可强制重建）")

    if do_universe:
        age = _file_age(universe_mod.UNIVERSE_FILE)
        if args.force or age is None or age >= universe_mod.DEFAULT_MAX_AGE_SEC:
            universe_mod.build_universe(verbose=True)
        else:
            log.info("代码表缓存仍有效，跳过（--force 可强制重建）")

    print()
    show_status()
    log.info("完成，用时 %.1f 秒", time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
