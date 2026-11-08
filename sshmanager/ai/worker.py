"""把 AI 请求放到后台线程，避免流式等待时界面卡住。"""

from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from .client import AiError, DeepSeekClient


class AiChatWorker(QThread):
    """跑一轮对话；把增量通过信号送回界面。"""

    delta = Signal(str)
    reasoning = Signal(str)
    usage = Signal(object)
    failed = Signal(str)
    finished_ok = Signal(str)

    def __init__(self, client: DeepSeekClient, messages: list[dict], parent=None) -> None:
        super().__init__(parent)

        self._client = client
        self._messages = messages
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:  # noqa: D102 - QThread 入口
        parts: list[str] = []

        try:
            for event in self._client.chat(self._messages, stream=True):
                if self._cancelled:
                    break

                if event.kind == "text":
                    parts.append(event.text)
                    self.delta.emit(event.text)
                elif event.kind == "reasoning":
                    self.reasoning.emit(event.text)
                elif event.kind == "usage" and event.usage:
                    self.usage.emit(event.usage)
        except AiError as error:
            if not self._cancelled:
                self.failed.emit(str(error))
            return
        except Exception as error:  # noqa: BLE001
            if not self._cancelled:
                self.failed.emit(f"出错了：{error}")
            return

        text = "".join(parts)

        if self._cancelled:
            return

        self.finished_ok.emit(text)


class AiTestWorker(QThread):
    """设置对话框里的"测试连接"。"""

    ok = Signal(str)
    failed = Signal(str)

    def __init__(self, client: DeepSeekClient, parent=None) -> None:
        super().__init__(parent)

        self._client = client

    def run(self) -> None:  # noqa: D102
        try:
            reply = self._client.test_connection()
        except AiError as error:
            self.failed.emit(str(error))
            return
        except Exception as error:  # noqa: BLE001
            self.failed.emit(f"出错了：{error}")
            return

        self.ok.emit(reply or "连接成功")


class AiEnvWorker(QThread):
    """采集当前主机环境（阻塞式 SSH 调用放后台）。"""

    done = Signal(dict)

    def __init__(self, client, parent=None) -> None:
        super().__init__(parent)

        self._client = client

    def run(self) -> None:  # noqa: D102
        from .context import capture_environment

        try:
            self.done.emit(capture_environment(self._client))
        except Exception:  # noqa: BLE001
            self.done.emit({})


class AiCaptureWorker(QThread):
    """在独立通道上跑一条命令并取回输出（用于一键诊断等）。"""

    done = Signal(str)

    def __init__(self, client, command: str, parent=None) -> None:
        super().__init__(parent)

        self._client = client
        self._command = command

    def run(self) -> None:  # noqa: D102
        from .context import capture_command_output

        try:
            self.done.emit(capture_command_output(self._client, self._command))
        except Exception as error:  # noqa: BLE001
            self.done.emit(f"（采集失败：{error}）")


class AiModelsWorker(QThread):
    """拉取官方模型列表（``GET /models``）。"""

    ok = Signal(list)
    failed = Signal(str)

    def __init__(self, client: DeepSeekClient, parent=None) -> None:
        super().__init__(parent)

        self._client = client

    def run(self) -> None:  # noqa: D102
        try:
            models = self._client.list_models()
        except AiError as error:
            self.failed.emit(str(error))
            return
        except Exception as error:  # noqa: BLE001
            self.failed.emit(f"出错了：{error}")
            return

        self.ok.emit(models)


class AiBalanceWorker(QThread):
    """查询账户余额（``GET /user/balance``）。"""

    ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, client: DeepSeekClient, parent=None) -> None:
        super().__init__(parent)

        self._client = client

    def run(self) -> None:  # noqa: D102
        try:
            info = self._client.balance()
        except AiError as error:
            self.failed.emit(str(error))
            return
        except Exception as error:  # noqa: BLE001
            self.failed.emit(f"出错了：{error}")
            return

        self.ok.emit(info)

