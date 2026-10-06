"""SSH 会话：把 :class:`~sshmanager.sshclient.SSHClient` 放到工作线程里。"""

from __future__ import annotations

import traceback

from PySide6.QtCore import QThread, Signal

from . import settings
from .hosts import Host
from .sshclient import SSHClient


class SSHSession(QThread):
    """一个会话对应一个线程：连接、读取、断开。"""

    #: 服务器输出（原始字节）
    data_received = Signal(bytes)

    connected = Signal()
    disconnected = Signal()
    failed = Signal(str)
    title_changed = Signal(str)

    def __init__(
        self,
        host: Host,
        credential: str | None = None,
        jump: SSHClient | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)

        self.host = host
        self.credential = credential
        self.jump = jump

        self.client = SSHClient(host)

        self._columns = settings.DEFAULT_COLUMNS
        self._rows = settings.DEFAULT_ROWS
        self._stop_requested = False
        self._close_reported = False

    # ------------------------------------------------------------------

    @property
    def display_name(self) -> str:
        return self.host.display_name

    def set_terminal_size(self, columns: int, rows: int) -> None:
        """由 GUI 线程调用；实际下发在读取循环中完成。"""

        columns = max(2, int(columns))
        rows = max(2, int(rows))

        if (columns, rows) == (self._columns, self._rows):
            return

        self._columns, self._rows = columns, rows
        self.client.resize(columns, rows)

    def send(self, data: str | bytes) -> None:
        self.client.send(data)

    def request_stop(self) -> None:
        self._stop_requested = True

    def cancel(self) -> None:
        """强制打断阻塞中的连接/读取（用于关闭窗口时避免线程悬挂）。"""

        self._stop_requested = True
        self.client.close()

    # ------------------------------------------------------------------

    def run(self) -> None:
        try:
            self.client.connect(
                hostname=self.host.hostname,
                port=self.host.port,
                username=self.host.username,
                password=self.credential if self.host.auth_type == "password" else None,
                key_path=self.host.key_path if self.host.auth_type == "key" else None,
                passphrase=self.credential if self.host.auth_type == "key" else None,
                verify_host_key=self.host.verify_host_key,
                term_type=self.host.term_type,
                width=self._columns,
                height=self._rows,
                jump=self.jump,
            )
        except Exception as error:  # noqa: BLE001 - 需要把任何失败反馈到界面
            if not self._stop_requested:
                self.failed.emit(f"{error}")

            return

        if self._stop_requested:
            self.client.close()
            self._report_closed()
            return

        self.connected.emit()

        try:
            while not self._stop_requested:
                data = self.client.recv()

                if not data:
                    # socket.timeout 也走这里：说明这一轮没数据，继续等
                    continue

                self.data_received.emit(data)
        except Exception as error:  # noqa: BLE001
            if not self._stop_requested:
                self.failed.emit(f"{error}\n{traceback.format_exc(limit=2)}")
        finally:
            self.client.close()
            self._report_closed()

    def _report_closed(self) -> None:
        if self._close_reported:
            return

        self._close_reported = True
        self.disconnected.emit()
