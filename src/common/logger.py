"""
日志
====
统一的日志配置：同时输出到控制台和滚动文件。

设计说明
--------
交易系统的日志和普通程序不一样，要求：

1. **必须落盘**——事后复盘要能查到「几点几分因为什么下了什么单」；
2. **必须带毫秒时间戳**——同一秒内可能有多条决策；
3. **不能因为日志目录不存在就崩**——启动时自动创建。

因此这里用 RotatingFileHandler 而不是普通 FileHandler，
避免跑几个月后日志文件把磁盘写满。
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from src.common.paths import LOG_DIR, ensure_runtime_dirs

#: 日志格式：时间(含毫秒) 级别 模块名 消息
LOG_FORMAT = "%(asctime)s.%(msecs)03d [%(levelname)-7s] %(name)-28s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 单个日志文件最大 10MB，保留 5 个备份
MAX_BYTES = 10 * 1024 * 1024
BACKUP_COUNT = 5

_configured = False


def setup_logging(level: str = "INFO", *, force: bool = False) -> None:
    """
    配置根 logger。重复调用是安全的（只会生效一次，除非 force=True）。

    :param level: 日志级别字符串，如 "INFO" / "DEBUG"
    :param force: 为 True 时强制重新配置（测试里会用到）
    """
    global _configured
    if _configured and not force:
        return

    ensure_runtime_dirs()

    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(numeric_level)

    # 清掉已有 handler，避免重复输出（比如在 Jupyter 里反复 import）
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # --- 控制台 ---
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    # --- 滚动文件 ---
    file_handler = RotatingFileHandler(
        LOG_DIR / "heatmap.log",
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setLevel(numeric_level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """
    获取一个 logger。首次调用会自动完成全局配置。

    :param name: 通常传 ``__name__``
    """
    if not _configured:
        setup_logging()
    return logging.getLogger(name)
