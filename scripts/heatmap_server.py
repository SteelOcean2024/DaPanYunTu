# -*- coding: utf-8 -*-
"""
大盘云图 · HTTP 服务
====================
把云图页面和数据接口挂到本地端口上。

启动
----
::

    python scripts/heatmap_server.py
    python scripts/heatmap_server.py --port 8770 --interval 3

然后浏览器打开 http://127.0.0.1:8770/

接口
----
=========================================  ====================================
路径                                        说明
=========================================  ====================================
``GET /``                                  云图页面
``GET /style.css`` ``/app.js``             前端静态资源
``GET /api/heatmap/treemap?metric=…``      云图数据（嵌套树 + 汇总）
``GET /api/heatmap/summary``               只要汇总
``GET /api/heatmap/status``                服务状态（刷新次数、耗时、错误）
=========================================  ====================================

为什么用标准库的 http.server
-----------------------------
这个服务只做两件事：把几个静态文件发出去、把内存里已经序列化好的 bytes 写回 socket。
没有模板渲染、没有鉴权、没有并发压力（就自己看）。
引入 Flask/FastAPI 只会多一堆依赖，而标准库的 ``ThreadingHTTPServer`` 已经够用，
且**零额外依赖**——这点对量化工程很重要，少一个依赖就少一个版本冲突。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# 允许直接以 `python scripts/xxx.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.common.logger import get_logger, setup_logging  # noqa: E402
from src.common.paths import WEB_DIR  # noqa: E402
from src.heatmap import treemap as treemap_mod  # noqa: E402
from src.heatmap.service import HeatmapService  # noqa: E402

log = get_logger(__name__)

#: 云图页面（结构视图）
STATIC_DIR: Path = WEB_DIR / "heatmap"
#: 行业情况页面（强弱视图）
SECTOR_STATIC_DIR: Path = WEB_DIR / "sector"
#: 前端共享资源（两个页面共用；静态部署时浏览器直连取数走这里）
SHARED_STATIC_DIR: Path = WEB_DIR / "shared"
#: 前端静态数据（行业映射导出，供无后端部署使用）
DATA_STATIC_DIR: Path = WEB_DIR / "data"

#: 允许的静态文件后缀（白名单，避免任意文件读取）
ALLOWED_SUFFIX = {".html", ".css", ".js", ".json", ".ico", ".png", ".svg", ".map"}

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")

#: 全局服务实例（在 main 里赋值，handler 通过它读快照）
SERVICE: HeatmapService | None = None


class HeatmapHandler(BaseHTTPRequestHandler):
    """请求处理器。只读内存快照，不做任何网络请求。"""

    server_version = "HeatmapServer/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 数据必须实时，禁止任何中间缓存
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器切标签/刷新时会直接断开，属正常现象
            pass

    def _send_json(self, body: bytes, status: int = 200) -> None:
        self._send_bytes(body, "application/json; charset=utf-8", status)

    def _send_json_obj(self, obj, status: int = 200) -> None:
        self._send_json(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json_obj({"error": message}, status)

    def log_message(self, fmt: str, *args) -> None:
        """压掉 http.server 默认的每请求一行日志（刷屏且没用）。"""
        return

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path.startswith("/api/heatmap/") or path.startswith("/api/sector/"):
            self._handle_api(path, query)
            return

        self._handle_static(path)

    def _handle_api(self, path: str, query: dict) -> None:
        if SERVICE is None:
            self._send_error_json("服务未初始化", 503)
            return

        try:
            if path == "/api/heatmap/treemap":
                metric = (query.get("metric") or [None])[0]
                if metric and metric not in treemap_mod.METRICS:
                    self._send_error_json(
                        f"未知面积口径 {metric!r}，可选：{list(treemap_mod.METRICS)}"
                    )
                    return
                self._send_json(SERVICE.treemap_json(metric))

            elif path == "/api/heatmap/summary":
                self._send_json(SERVICE.summary_json())

            elif path == "/api/heatmap/status":
                self._send_json_obj(SERVICE.status())

            elif path == "/api/sector/strength":
                self._send_json(SERVICE.sector_json())

            else:
                self._send_error_json(f"未知接口：{path}", 404)

        except Exception as exc:  # noqa: BLE001 - 单个请求出错不能拖垮服务
            log.exception("接口 %s 处理失败", path)
            self._send_error_json(f"{type(exc).__name__}: {exc}", 500)

    def _handle_static(self, path: str) -> None:
        # /sector 是第二套页面（行业情况），其余路径仍归云图
        # /shared 与 /data 是前端共享资源：本地也提供，
        # 这样「浏览器直连取数」这条静态部署链路能在本地被验证。
        if path.startswith("/shared/"):
            self._serve_file(SHARED_STATIC_DIR, path[len("/shared/"):], path)
            return
        if path.startswith("/data/"):
            self._serve_file(DATA_STATIC_DIR, path[len("/data/"):], path)
            return

        if path == "/sector" or path.startswith("/sector/"):
            rel = path[len("/sector"):].strip("/") or "index.html"
            self._serve_file(SECTOR_STATIC_DIR, rel, path)
            return

        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        self._serve_file(STATIC_DIR, rel, path)

    def _serve_file(self, base: Path, rel: str, path: str) -> None:
        # 防目录穿越：解析后必须仍在 base 内
        target = (base / rel).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            self._send_error_json("非法路径", 403)
            return

        if not target.is_file() or target.suffix.lower() not in ALLOWED_SUFFIX:
            self._send_error_json(f"未找到：{path}", 404)
            return

        ctype, _ = mimetypes.guess_type(str(target))
        if target.suffix.lower() == ".html":
            ctype = "text/html; charset=utf-8"
        elif target.suffix.lower() == ".js":
            ctype = "application/javascript; charset=utf-8"
        elif target.suffix.lower() == ".css":
            ctype = "text/css; charset=utf-8"

        self._send_bytes(target.read_bytes(), ctype or "application/octet-stream")


def _port_in_use(host: str, port: int) -> bool:
    """探测端口上是否已有服务在监听（只做一次短超时连接尝试）。"""
    target = "127.0.0.1" if host in ("", "0.0.0.0") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((target, port)) == 0


def main() -> int:
    parser = argparse.ArgumentParser(description="大盘云图本地服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认只监听本机）")
    parser.add_argument("--port", type=int, default=8770, help="监听端口（默认 8770）")
    parser.add_argument(
        "--interval", type=float, default=5.0, help="后台刷新间隔秒数（默认 5）"
    )
    parser.add_argument(
        "--metric",
        default=treemap_mod.DEFAULT_METRIC,
        choices=list(treemap_mod.METRICS),
        help="默认面积口径",
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="缓存缺失时不自动构建（离线启动，缺缓存会直接报错）",
    )
    args = parser.parse_args()

    setup_logging()

    # Windows 默认开启 SO_REUSEADDR，允许两个进程同时 bind 同一端口，
    # 不拦截就会出现多个实例同时刷行情、交错写同一份日志（本机已发生过）。
    if _port_in_use(args.host, args.port):
        log.error(
            "端口 %s:%d 已有服务在运行，本次启动已取消。若要重启，请先停掉旧进程。",
            args.host,
            args.port,
        )
        print(f"  端口 {args.port} 已被占用，未重复启动。", file=sys.stderr)
        return 1

    if not STATIC_DIR.is_dir():
        log.error("前端目录不存在：%s", STATIC_DIR)
        return 1

    global SERVICE
    SERVICE = HeatmapService(
        interval=args.interval, metric=args.metric, auto_build=not args.no_build
    )

    log.info("正在加载行业映射与代码表…")
    try:
        SERVICE.prepare()
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 1

    # 先同步刷一次，保证页面一打开就有数据（否则要等第一个间隔）
    log.info("首次拉取行情…")
    try:
        SERVICE.refresh_once()
    except Exception as exc:  # noqa: BLE001
        log.warning("首次拉取失败，将由后台线程重试：%s", exc)

    SERVICE.start()

    httpd = ThreadingHTTPServer((args.host, args.port), HeatmapHandler)
    httpd.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    log.info("大盘云图已启动：%s", url)
    log.info("刷新间隔 %.1f 秒 · 面积口径 %s", args.interval, args.metric)
    print(f"\n  大盘云图 → {url}\n  按 Ctrl+C 停止\n")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
        log.info("收到 Ctrl+C，正在停止…")
    finally:
        httpd.shutdown()
        SERVICE.stop()
        log.info("已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
