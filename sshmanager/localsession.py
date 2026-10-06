"""本地终端会话：用 ConPTY（pywinpty）在应用内嵌一个系统终端。

Windows 上 ``pty`` 模块不可用（Unix 专属），所以走 ConPTY：
``winpty.PtyProcess`` 就是基于 ConPTY 的伪控制台。
"""

from __future__ import annotations

import os
import sys
import threading

from PySide6.QtCore import QThread, Signal


def default_shell_command() -> str:
    """挑选一个合适的本地 shell 命令行。"""

    if os.name != "nt":
        return os.environ.get("SHELL") or "/bin/sh"

    # 优先 PowerShell 7（pwsh），其次 Windows PowerShell，最后 cmd
    for candidate in ("pwsh.exe", "powershell.exe"):
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if directory and os.path.exists(os.path.join(directory, candidate)):
                return f"{candidate} -NoLogo"

    return "cmd.exe /Q"


def conpty_available() -> bool:
    """ConPTY 是否可用（Windows 10 1809+ 才有）。"""

    if os.name != "nt":
        return True

    try:
        import winpty  # noqa: F401
    except ImportError:
        return False

    return True


class LocalShellSession(QThread):
    """把本地 shell 的读写放到线程里，接口与 SSHSession 保持一致。"""

    data_received = Signal(bytes)
    connected = Signal()
    disconnected = Signal()
    failed = Signal(str)
    title_changed = Signal(str)

    def __init__(self, command: str | None = None, columns: int = 120, rows: int = 32, parent=None) -> None:
        super().__init__(parent)

        self.command = command or default_shell_command()

        self._columns = max(20, columns)
        self._rows = max(5, rows)
        self._process = None
        self._stop_requested = False
        self._closing = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------

    @property
    def display_name(self) -> str:
        return "本地终端"

    def set_terminal_size(self, columns: int, rows: int) -> None:
        columns = max(20, int(columns))
        rows = max(5, int(rows))

        if (columns, rows) == (self._columns, self._rows):
            return

        self._columns, self._rows = columns, rows

        with self._lock:
            process = self._process

        if process is None:
            return

        try:
            process.setwinsize(rows, columns)
        except Exception:
            pass

    def send(self, data: str | bytes) -> None:
        payload = data.decode("utf-8", errors="ignore") if isinstance(data, bytes) else data

        with self._lock:
            process = self._process

        if process is None:
            return

        try:
            process.write(payload)
        except Exception:
            pass

    def request_stop(self) -> None:
        self._stop_requested = True

        with self._lock:
            process = self._process

        if process is not None:
            try:
                process.close()
            except Exception:
                pass

    # ------------------------------------------------------------------

    def run(self) -> None:  # noqa: C901 - 线程主循环
        try:
            from winpty import PtyProcess
        except ImportError:
            self.failed.emit("未安装 pywinpty，无法使用本地终端（pip install pywinpty）")
            return

        try:
            process = PtyProcess.spawn(self.command)
        except Exception as error:  # noqa: BLE001
            self.failed.emit(f"启动 {self.command} 失败：{error}")
            return

        with self._lock:
            self._process = process

        try:
            process.setwinsize(self._rows, self._columns)
        except Exception:
            pass

        self.connected.emit()

        try:
            while not self._stop_requested:
                try:
                    data = process.read(4096)
                except EOFError:
                    break
                except Exception:
                    break

                if data:
                    self.data_received.emit(data.encode("utf-8", errors="replace"))
        finally:
            try:
                process.close()
            except Exception:
                pass

            with self._lock:
                self._process = None

            if not self._closing:
                self.disconnected.emit()

    def mark_closing(self) -> None:
        self._closing = True
