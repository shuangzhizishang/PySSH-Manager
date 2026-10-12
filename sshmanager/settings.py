"""应用路径与全局常量。"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "PySSH Manager"
APP_SLUG = "PySSH-Manager"

#: 允许通过环境变量覆盖数据目录，方便测试
ENV_DATA_DIR = "PYSSH_DATA_DIR"


def data_dir() -> Path:
    """返回存放主机列表与凭据的目录，不存在时自动创建。"""

    override = os.environ.get(ENV_DATA_DIR)

    if override:
        path = Path(override).expanduser()
    elif os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        path = Path(base) / APP_SLUG
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        path = Path(base) / APP_SLUG

    path.mkdir(parents=True, exist_ok=True)

    return path


def hosts_file() -> Path:
    return data_dir() / "hosts.json"


def credentials_file() -> Path:
    return data_dir() / "credentials.json"


def legacy_known_hosts_file() -> Path:
    """应用自带的 known_hosts，避免污染用户全局 ~/.ssh。"""

    return data_dir() / "known_hosts"


def keys_dir() -> Path:
    """受管理的 SSH 密钥目录。"""

    path = data_dir() / "keys"
    path.mkdir(parents=True, exist_ok=True)

    return path


DEFAULT_COLUMNS = 120
DEFAULT_ROWS = 32

DEFAULT_FONT_FAMILY = "Consolas"
DEFAULT_FONT_SIZE = 12


def trace_ui(message: str) -> None:
    """界面耗时追踪：设置 ``PYSSH_TRACE_UI=1`` 时把耗时打到 stderr。

    用来定位"某个操作卡住"这类问题（例如列目录、填表各花了多久）。
    """

    if os.environ.get("PYSSH_TRACE_UI", "").strip() not in ("1", "true", "True", "yes"):
        return

    import sys
    import time

    print(f"[ui {time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)
