"""
路径解析
========
统一管理项目内的所有路径，避免各模块到处写相对路径。

设计说明
--------
所有路径都从 ``PROJECT_ROOT`` 派生，而 ``PROJECT_ROOT`` 由本文件的位置反推：

    src/common/paths.py  →  parents[0]=common, [1]=src, [2]=项目根

这样无论从哪个目录启动脚本，路径都是对的——不依赖当前工作目录（cwd）。
用 cwd 推路径是个常见坑：IDE 里跑和命令行里跑结果不一样。
"""
from __future__ import annotations

from pathlib import Path

# 项目根目录（src/common/paths.py 往上三层）
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

# ---- 静态目录 ----
DOCS_DIR: Path = PROJECT_ROOT / "docs"
SRC_DIR: Path = PROJECT_ROOT / "src"
TESTS_DIR: Path = PROJECT_ROOT / "tests"
#: 前端静态资源（云图页面的 HTML/CSS/JS）
WEB_DIR: Path = PROJECT_ROOT / "web"

# ---- 运行时目录（不进版本库）----
RUNTIME_DIR: Path = PROJECT_ROOT / "runtime"
LOG_DIR: Path = RUNTIME_DIR / "logs"
DATA_DIR: Path = RUNTIME_DIR / "data"

# 所有需要自动创建的运行时目录
_RUNTIME_DIRS = (RUNTIME_DIR, LOG_DIR, DATA_DIR)


def ensure_runtime_dirs() -> None:
    """创建所有运行时目录（已存在则跳过）。程序启动时调用一次即可。"""
    for d in _RUNTIME_DIRS:
        d.mkdir(parents=True, exist_ok=True)
