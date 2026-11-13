# -*- coding: utf-8 -*-
"""
云图刷新服务
============
后台线程按固定间隔拉行情、重组云图，结果放进内存快照；
HTTP 层只读快照，**永远不阻塞在网络上**。

为什么用后台线程，而不是「请求来了再拉」
----------------------------------------
1. 一次全市场刷新约 2 秒。若每个 HTTP 请求都触发一次，
   三个浏览器标签页就会把腾讯接口打成 3 倍流量，还互相拖慢；
2. 前端是定时轮询模型，多个客户端应当看到**同一份**快照，
   否则同一时刻不同标签页显示的数字会不一样；
3. 网络抖动时后台线程可以保留上一份好数据，
   而不是把错误页甩给用户。

线程安全
--------
快照读写用同一把锁保护；组装好的 JSON **预先序列化**并缓存成 bytes，
这样每个 HTTP 请求只是把 bytes 写回 socket，不做任何计算。
"""
from __future__ import annotations

import json
import threading
import time

from src.analysis import sector as sector_mod
from src.common.logger import get_logger
from src.heatmap import industry as industry_mod
from src.heatmap import quotes as quotes_mod
from src.heatmap import treemap as treemap_mod
from src.heatmap import universe as universe_mod

log = get_logger(__name__)

#: 默认刷新间隔（秒）。实测全市场拉取约 2 秒，留出余量。
DEFAULT_INTERVAL = 5.0


class HeatmapService:
    """
    云图数据服务：持有最新快照，后台定时刷新。

    :param interval: 刷新间隔（秒），最小 1 秒
    :param metric: 面积口径，见 :data:`src.heatmap.treemap.METRICS`
    :param auto_build: 缓存缺失时是否自动构建行业映射/代码表
    """

    def __init__(
        self,
        *,
        interval: float = DEFAULT_INTERVAL,
        metric: str = treemap_mod.DEFAULT_METRIC,
        auto_build: bool = True,
    ) -> None:
        self.interval = max(1.0, float(interval))
        self.metric = metric
        self.auto_build = auto_build

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # --- 慢路径缓存（进程内）---
        self._universe = None
        self._industry = None
        self._codes: list[str] | None = None

        # --- 快照 ---
        self._treemap: dict | None = None
        self._treemap_json: bytes = b"{}"
        self._summary: dict | None = None
        self._summary_json: bytes = b"{}"
        # 最近一次的合并行情，用于「切换面积口径时不必重新拉行情」
        self._merged = None
        self._index_quotes: dict | None = None
        # 板块强度（由同一份 merged 行情聚合，不额外取数）
        self._sector: dict | None = None
        self._sector_json: bytes = b"{}"
        # 各面积口径的已序列化结果
        self._metric_cache: dict[str, bytes] = {}
        self._updated_at: str | None = None
        self._last_error: str | None = None
        self._last_duration_ms: float | None = None
        self._refresh_count = 0
        self._started_at = time.time()

    # ------------------------------------------------------------------
    # 准备（慢路径，只做一次）
    # ------------------------------------------------------------------

    def prepare(self) -> None:
        """
        加载行业映射与代码表。

        **这是慢操作**——缓存缺失时两者合计约 2 分钟，所以只在服务启动时做一次。
        """
        self._industry = industry_mod.load_map(auto_build=self.auto_build, verbose=True)
        self._universe = universe_mod.load_universe(auto_build=self.auto_build, verbose=True)

        # 代码表 ∪ 行业映射，保证覆盖面
        codes = set(self._universe["code"]) | set(self._industry["code"])
        self._codes = sorted(codes)
        log.info(
            "云图数据就绪：代码表 %d 只，行业映射 %d 只，并集 %d 只",
            len(self._universe),
            len(self._industry),
            len(self._codes),
        )

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------

    def refresh_once(self) -> dict:
        """
        拉一次行情并重建云图快照。

        失败时**保留上一份快照**并记录错误，然后向上抛——
        网络抖动不该让页面变成一片空白。

        :returns: 本次组装好的 treemap dict
        """
        if self._codes is None:
            raise RuntimeError("请先调用 prepare() 加载行业映射与代码表")

        t0 = time.time()
        try:
            df = quotes_mod.fetch_quotes(self._codes)
            if df.empty:
                raise RuntimeError("行情接口返回空数据")

            merged = industry_mod.attach_industry(df, self._industry)
            index_quotes = quotes_mod.fetch_index_quotes()
            tree = treemap_mod.build_treemap(
                merged, metric=self.metric, index_quotes=index_quotes
            )
            payload = json.dumps(tree, ensure_ascii=False, separators=(",", ":"))
            strength = sector_mod.compute_sector_strength(
                merged, updated_at=tree["updatedAt"]
            )
            sector_payload = json.dumps(
                strength, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            summary_payload = json.dumps(
                tree["summary"], ensure_ascii=False, separators=(",", ":")
            )
        except Exception as exc:  # noqa: BLE001 - 后台线程绝不能因异常退出
            with self._lock:
                self._last_error = f"{type(exc).__name__}: {exc}"
            log.warning("云图刷新失败（保留上一份快照）：%s", exc)
            raise

        duration_ms = (time.time() - t0) * 1000
        with self._lock:
            self._treemap = tree
            self._treemap_json = payload.encode("utf-8")
            self._summary = tree["summary"]
            self._summary_json = summary_payload.encode("utf-8")
            self._merged = merged
            self._index_quotes = index_quotes
            self._sector = strength
            self._sector_json = sector_payload
            # 行情变了，之前按别的口径算的缓存全部作废
            self._metric_cache = {self.metric: self._treemap_json}
            self._updated_at = tree["updatedAt"]
            self._last_error = None
            self._last_duration_ms = duration_ms
            self._refresh_count += 1

        # 落盘放在锁外：磁盘慢不该拖住读快照的请求
        sector_mod.append_snapshot(strength)

        log.info(
            "云图刷新 #%d：%d 只 / %d 板块 / %.0f ms",
            self._refresh_count,
            tree["stockCount"],
            tree["boardCount"],
            duration_ms,
        )
        return tree

    def _loop(self) -> None:
        """后台循环：刷新 → 睡眠 → 刷新。单次异常不会让线程退出。"""
        while not self._stop.is_set():
            t0 = time.time()
            try:
                self.refresh_once()
            except Exception:  # noqa: BLE001 - 已记录，继续下一轮
                pass
            # 扣除本轮耗时，让实际间隔尽量贴近 interval
            sleep_for = max(0.5, self.interval - (time.time() - t0))
            self._stop.wait(sleep_for)

    def start(self) -> None:
        """启动后台刷新线程（幂等）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="heatmap-refresh", daemon=True
        )
        self._thread.start()
        log.info("云图后台刷新已启动，间隔 %.1f 秒", self.interval)

    def stop(self, timeout: float = 5.0) -> None:
        """停止后台线程。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def treemap_json(self, metric: str | None = None) -> bytes:
        """
        返回云图 JSON（bytes），可指定面积口径。

        **切换口径不需要重新拉行情**——用最近一次的合并行情就地重算即可
        （5500 行约几十毫秒），所以前端的「总市值/流通市值/成交额」切换是秒回的。

        :param metric: 面积口径；``None`` 表示用服务默认口径
        """
        want = metric or self.metric
        with self._lock:
            if want == self.metric:
                return self._treemap_json
            cached = self._metric_cache.get(want)
            if cached is not None:
                return cached
            if self._merged is None:
                # 还没成功刷新过，先给一份空对象，前端会走 loading 分支
                return self._treemap_json
            tree = treemap_mod.build_treemap(
                self._merged, metric=want, index_quotes=self._index_quotes
            )
            payload = json.dumps(tree, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            self._metric_cache[want] = payload
            return payload

    def summary_json(self) -> bytes:
        """已序列化的汇总 JSON（bytes）。"""
        with self._lock:
            return self._summary_json

    def sector_json(self) -> bytes:
        """已序列化的板块强度 JSON（bytes），供 /api/sector/strength 使用。"""
        with self._lock:
            return self._sector_json

    def sector_strength(self) -> dict | None:
        """板块强度 dict（调试用；HTTP 层请用 sector_json）。"""
        with self._lock:
            return self._sector

    def treemap(self) -> dict | None:
        """云图 dict（调试用；HTTP 层请用 :meth:`treemap_json`）。"""
        with self._lock:
            return self._treemap

    def status(self) -> dict:
        """服务状态，供 ``/api/heatmap/status`` 使用。"""
        with self._lock:
            return {
                "ready": self._treemap is not None,
                "intervalSec": self.interval,
                "metric": self.metric,
                "updatedAt": self._updated_at,
                "refreshCount": self._refresh_count,
                "lastDurationMs": (
                    round(self._last_duration_ms, 1)
                    if self._last_duration_ms is not None
                    else None
                ),
                "lastError": self._last_error,
                "uptimeSec": round(time.time() - self._started_at, 1),
                "universeSize": len(self._codes) if self._codes else 0,
                "threadAlive": bool(self._thread and self._thread.is_alive()),
            }
