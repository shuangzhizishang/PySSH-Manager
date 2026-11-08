"""AI 助手界面端到端验证（本地 mock 服务器，不需要真实 API Key）。

验证链路：
1. 主窗口带 AI 停靠面板，输入问题 → 流式回复渲染
2. 回复里的命令渲染成卡片；点「执行」命令真的发到当前会话
3. ```pyssh-action 卡片点「执行操作」→ 主窗口收到操作名
4. 设置对话框：填 Key → 测试连接（走 mock）→ 保存进加密凭据库
5. 截图 build/ai-panel.png
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-ai-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 控制台/重定向可能是 GBK；状态文字里有 ✔ 等符号，统一按 UTF-8 输出
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import QApplication, QMessageBox

from sshmanager.ai.config import AiConfig, CREDENTIAL_KEY
from sshmanager.ai.prompts import build_system_prompt
from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, MainWindow
from sshmanager.ui.ai_panel import ActionCard, CommandCard
from sshmanager.ui.ai_settings import AiSettingsDialog

REPLY = (
    "CentOS 7 上这样装 MySQL：\n\n"
    "```bash\n# 装社区版\nrpm -ivh mysql80.rpm\n```\n\n"
    "然后启动服务：\n\n"
    "```bash\nsystemctl start mysqld\n```\n\n"
    "顺手帮你打开文件面板：\n\n"
    '```pyssh-action\n{"action": "open_sftp", "reason": "需要传安装包"}\n```\n'
)

STATE = {"request": None}

# 官方 /models 的返回结构（只保留自检需要的字段）
MODELS = [
    {
        "id": "deepseek-flash",
        "name": "DeepSeek-V4.1-Flash-1017",
        "context_window": 1048576,
        "max_output_tokens": 393216,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
    },
    {
        "id": "deepseek-v4-pro",
        "name": "DeepSeek-V4-Pro-0813",
        "context_window": 1048576,
        "max_output_tokens": 393216,
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "effort": {"supported_levels": ["low", "high", "max"], "default_level": "high"},
    },
]

BALANCE = {
    "is_available": True,
    "balance_infos": [
        {
            "currency": "CNY",
            "total_balance": "42.50",
            "granted_balance": "2.50",
            "topped_up_balance": "40.00",
        }
    ],
}

# 带缓存命中的 usage：命中 900 / 未命中 300
USAGE = {
    "prompt_tokens": 1200,
    "completion_tokens": 340,
    "total_tokens": 1540,
    "prompt_cache_hit_tokens": 900,
    "prompt_cache_miss_tokens": 300,
    "completion_tokens_details": {"reasoning_tokens": 120},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D102
        pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")

        if path.endswith("/models"):
            body = {"object": "list", "data": MODELS}
        elif path.endswith("/user/balance"):
            body = BALANCE
        else:
            self.send_response(404)
            self.end_headers()
            return

        data = json.dumps(body, ensure_ascii=False).encode()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8")) if body else {}

        STATE["request"] = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": payload,
        }

        # 和真实 API 一样：stream=False 返回一个 JSON，stream=True 才走 SSE
        if not payload.get("stream", False):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "id": "x",
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok，连接正常"}}],
                        "usage": USAGE,
                    },
                    ensure_ascii=False,
                ).encode()
            )
            self.wfile.flush()
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # 先来一小段思维链，再打字
        thinking = {"choices": [{"index": 0, "delta": {"reasoning_content": "先确认是哪套发行版…"}}]}
        self.wfile.write(f"data: {json.dumps(thinking, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

        # 按 12 个字符切块流式下发，模拟真实打字效果
        for index in range(0, len(REPLY), 12):
            chunk = {"choices": [{"index": 0, "delta": {"content": REPLY[index:index + 12]}}]}
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.01)

        # 真实 API 的最后一个数据块带 usage（choices 为空）
        final = {"choices": [], "usage": USAGE}
        self.wfile.write(f"data: {json.dumps(final, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class FakePanelSession(QObject):
    """假会话：只提供面板需要的信号/属性。"""

    data_received = Signal(bytes)

    def __init__(self) -> None:
        super().__init__()

        self.client = None


class FakeView:
    def __init__(self) -> None:
        self.text = "[root@shuangxiudao ~]# "
        self.inserted: list[str] = []

    def output_text(self) -> str:
        return self.text

    def insert_text(self, text: str) -> None:
        self.inserted.append(text)
        self.text += text


class FakePane:
    """假终端面板：记录 AI 发下去的命令。"""

    def __init__(self, host: Host) -> None:
        self.host = host
        self.view = FakeView()
        self.session = FakePanelSession()
        self.is_local = False
        self.commands: list[str] = []
        self.remote_title = ""

    def send_command(self, text: str) -> None:
        self.commands.append(text)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"

    AiConfig(base_url=base_url, model="deepseek-flash", timeout=20, auto_analyze_result=False).save()

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    context.credentials.set(CREDENTIAL_KEY, "sk-test-key")

    host = Host(label="demo-host", hostname="127.0.0.1", username="root")
    context.hosts.add(host)

    window = MainWindow(context)
    window.resize(1500, 900)
    window.show()

    build_dir = Path(__file__).resolve().parent.parent / "build"
    results = {
        "dock_visible": False,
        "model_default": False,
        "streamed_reply": False,
        "payload_thinking": False,
        "command_cards": False,
        "executed_command": False,
        "action_card": False,
        "usage_recorded": False,
        "usage_label": False,
        "settings_key_saved": False,
        "settings_test_ok": False,
        "models_refreshed": False,
        "usage_dialog": False,
        "balance_ok": False,
    }

    pane = FakePane(host)
    panel = window.ai_panel
    panel.attach_pane(pane)
    panel._environment = {
        "distro_id": "centos",
        "pretty_name": "CentOS Linux 7 (Core)",
        "package_managers": "yum",
        "host": "shuangxiudao",
        "uname": "Linux 3.10 x86_64",
    }

    action_names: list[str] = []
    panel.set_action_handler(lambda name: (action_names.append(name), True)[1])

    def wait_for(predicate, timeout: float = 20.0) -> bool:
        end = time.time() + timeout

        while time.time() < end:
            if predicate():
                return True

            app.processEvents()
            time.sleep(0.05)

        return predicate()

    def cards() -> list:
        found = []

        for index in range(panel.suggestions_layout.count()):
            widget = panel.suggestions_layout.itemAt(index).widget()

            if isinstance(widget, (CommandCard, ActionCard)):
                found.append(widget)

        return found

    def step1() -> None:
        if not window.ai_dock.isVisible():
            window.action_ai.setChecked(True)

        app.processEvents()
        results["dock_visible"] = window.ai_dock.isVisible()

        combo_data = panel.model_combo.currentData()
        results["model_default"] = (
            combo_data == "deepseek-flash" and panel.config.model == "deepseek-flash"
        )
        print(
            f"0. 默认模型：{combo_data}（下拉 {panel.model_combo.count()} 项，配置 {panel.config.model}，"
            f"思考强度 {panel.config.effort}）",
            flush=True,
        )

        panel.input_edit.setPlainText("帮我在这台机器上装 MySQL 8")
        panel.send()

        ok = wait_for(lambda: "MySQL" in panel.chat_view.toPlainText() and "rpm -ivh" in panel.chat_view.toPlainText())
        results["streamed_reply"] = ok
        print(f"1. 流式回复：{ok}", flush=True)

        wait_for(lambda: panel.ledger.total.requests >= 1, timeout=10)
        total = panel.ledger.total
        results["usage_recorded"] = bool(
            total.requests == 1
            and total.prompt_tokens == 1200
            and total.cached_tokens == 900
            and total.completion_tokens == 340
            and total.cost_cny > 0
        )
        label = panel.usage_label.text()
        results["usage_label"] = bool("tok" in label and "¥" in label and "命中" in label)
        print(
            f"   用量标签：{label}",
            flush=True,
        )
        print(
            f"   记账：请求 {total.requests} 次 · 输入 {total.prompt_tokens}（命中 {total.cached_tokens}）"
            f" · 输出 {total.completion_tokens} · 估算 ¥{total.cost_cny:.6f}",
            flush=True,
        )

        QTimer.singleShot(200, step2)

    def step2() -> None:
        found = wait_for(lambda: len(cards()) >= 3, timeout=10)
        results["command_cards"] = found
        print(f"2. 建议卡片：{found}（{len(cards())} 张）", flush=True)

        request = STATE["request"] or {}
        body = request.get("body") or {}
        system_prompt = ((body.get("messages") or [{}])[0] or {}).get("content", "")
        print(f"   请求路径：{request.get('path')}，Authorization={request.get('auth')}", flush=True)
        print(f"   系统提示词含 yum：{'yum' in system_prompt}；模型：{body.get('model')}", flush=True)

        results["payload_thinking"] = bool(
            body.get("reasoning_effort") == "high"
            and (body.get("thinking") or {}).get("type") == "enabled"
            and (body.get("stream_options") or {}).get("include_usage") is True
            and body.get("temperature") is None
        )
        print(
            f"   思考参数：reasoning_effort={body.get('reasoning_effort')} · "
            f"thinking={body.get('thinking')} · stream_options={body.get('stream_options')}",
            flush=True,
        )

        command_cards = [widget for widget in cards() if isinstance(widget, CommandCard)]
        print(f"   命令卡片：{[card.command.code.splitlines()[0] for card in command_cards]}", flush=True)

        command_cards[0].run_button.click()
        app.processEvents()

        results["executed_command"] = pane.commands == ["# 装社区版\nrpm -ivh mysql80.rpm"]
        print(f"3. 点执行 → 会话收到命令：{pane.commands}", flush=True)

        QTimer.singleShot(200, step3)

    def step3() -> None:
        action_cards = [widget for widget in cards() if isinstance(widget, ActionCard)]

        if action_cards:
            from PySide6.QtWidgets import QPushButton

            buttons = [child for child in action_cards[0].children() if isinstance(child, QPushButton)]

            if buttons:
                buttons[0].click()
                app.processEvents()

        results["action_card"] = action_names == ["open_sftp"]
        print(f"4. 软件操作卡片 → 主窗口收到：{action_names}", flush=True)

        QTimer.singleShot(200, step4)

    def step4() -> None:
        dialog = AiSettingsDialog(context.credentials, window)
        dialog.key_edit.setText("sk-test-key")
        dialog.base_edit.setText(base_url)
        dialog.test_connection()

        ok = wait_for(lambda: "连接成功" in dialog.status.text(), timeout=15)
        results["settings_test_ok"] = ok
        print(f"5. 设置对话框测试连接：{ok}（{dialog.status.text()[:60]}）", flush=True)

        dialog.refresh_models()
        models_ok = wait_for(
            lambda: dialog.model_combo.findData("deepseek-v4-pro") >= 0 and dialog.model_combo.count() >= 2,
            timeout=15,
        )
        results["models_refreshed"] = models_ok
        print(f"   官方模型列表：{models_ok}（{dialog.model_combo.count()} 项）", flush=True)

        dialog.resize(640, 700)
        app.processEvents()
        dialog.grab().save(str(build_dir / "ai-settings.png"))
        print(f"   设置对话框截图：{build_dir / 'ai-settings.png'}", flush=True)

        dialog.save()
        stored = context.credentials.get(CREDENTIAL_KEY)
        results["settings_key_saved"] = stored == "sk-test-key"
        print(f"   保存 Key 到凭据库：{results['settings_key_saved']}", flush=True)

        QTimer.singleShot(200, step5)

    def step5() -> None:
        from sshmanager.ui.ai_usage import AiUsageDialog

        usage_dialog = AiUsageDialog(panel.ledger, panel._make_client, window)
        usage_dialog.refresh()
        rows = usage_dialog.table.rowCount()
        results["usage_dialog"] = rows >= 1
        print(f"7. 用量明细对话框：{rows} 行", flush=True)

        usage_dialog.refresh_balance()
        balance_ok = wait_for(lambda: "42.50" in usage_dialog.balance_label.text(), timeout=15)
        results["balance_ok"] = balance_ok
        print(f"   余额：{balance_ok}（{usage_dialog.balance_label.text()[:80]}）", flush=True)

        usage_dialog.resize(760, 420)
        app.processEvents()
        usage_dialog.grab().save(str(build_dir / "ai-usage.png"))
        print(f"   用量明细截图：{build_dir / 'ai-usage.png'}", flush=True)
        usage_dialog.close()

        panel.input_edit.setPlainText("接下来还要做什么？")
        app.processEvents()
        window.grab().save(str(build_dir / "ai-panel.png"))
        print(f"8. 截图：{build_dir / 'ai-panel.png'}", flush=True)

        finish()

    def finish() -> None:
        window.shutdown()
        server.shutdown()
        app.quit()

    QTimer.singleShot(400, step1)
    app.exec()

    print("--- 判定 ---", flush=True)

    for name, value in results.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    ok = all(results.values())
    print("AI_E2E_OK" if ok else "AI_E2E_FAILED", flush=True)

    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
