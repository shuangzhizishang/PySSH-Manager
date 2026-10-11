"""命令功能的真实链路验证：直接输入 / 粘贴、命令库、输出回拷、终端剪切。

不做命令输入栏——Windows→Linux 就是**直接在终端里打字或粘贴**。

覆盖：
1. 直接在终端里键入命令（Windows→Linux，键盘输入）
2. Ctrl+Shift+V 把 Windows 剪贴板粘贴到远端（多行一次贴）
3. 命令库发送命令并记入历史
4. 收藏终端选中内容为命令
5. 复制全部输出到 Windows 剪贴板（Linux→Windows）
6. 输出另存为文件
7. 终端选中后「剪切」：复制到剪贴板并清空选区
8. 选中即复制开关
9. 命令库条目：复制 / 剪切 / 粘贴 / 撤销

运行::

    .venv\\Scripts\\python.exe tests\\command_features_e2e.py --host <你的服务器> --password <密码>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-cmd-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from sshmanager.commands import MODE_COPY, MODE_CUT, get_store, reset_store
from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, MainWindow
from sshmanager.ui.command_library import CommandLibraryDialog

MARKER = "PSSH-DIRECT-OK"
PASTE_MARKER = "PSSH-PASTE-OK"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]

    reset_store()

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    host = Host(label=args.host, hostname=args.host, port=args.port, username=args.username)
    host.verify_host_key = False
    context.hosts.add(host)

    if args.password:
        context.credentials.set(host.id, args.password)

    window = MainWindow(context)
    window.resize(1280, 820)
    window.show()

    state = {
        "type_directly": False,
        "paste_from_windows": False,
        "library_send_and_history": False,
        "favorite_selection": False,
        "copy_output_to_windows": False,
        "save_output": False,
        "terminal_cut": False,
        "select_to_copy": False,
        "snippet_clipboard_undo": False,
    }

    deadline = time.time() + args.timeout
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    pane = None
    build_dir = Path(__file__).resolve().parent.parent / "build"
    output_file = Path(tempfile.mkdtemp(prefix="pyssh-cmd-out-")) / "output.txt"

    def wait_for(predicate, timeout: float = 25.0, interval: float = 200) -> bool:
        end = time.time() + timeout

        while time.time() < end:
            if predicate():
                return True

            app.processEvents()
            time.sleep(interval / 1000.0)

        return predicate()

    def terminal_text() -> str:
        return pane.view.output_text()

    # ------------------------------------------------------------------

    def step_connect() -> None:
        nonlocal pane

        window.connect_host(host)

        def poll() -> None:
            nonlocal pane

            session = window._session_for(host.id)

            if session is None or session.session is None:
                if time.time() > deadline:
                    print("会话未建立", flush=True)
                    app.quit()
                    return

                QTimer.singleShot(400, poll)
                return

            pane = session
            ready = wait_for(
                lambda: any(marker in terminal_text() for marker in ("#", "$", ">")) and len(terminal_text()) > 3,
                timeout=30,
            )

            if not ready:
                print("没有看到 shell 提示符", flush=True)
                app.quit()
                return

            print("SSH 会话已就绪（无命令输入栏，直接操作终端）", flush=True)
            QTimer.singleShot(300, step_type_directly)

        QTimer.singleShot(500, poll)

    # ------------------------------------------------------------------
    # 1) 直接键入
    # ------------------------------------------------------------------
    def step_type_directly() -> None:
        pane.view.setFocus()
        app.processEvents()

        QTest.keyClicks(pane.view, f"echo {MARKER}")
        QTest.keyClick(pane.view, Qt.Key.Key_Return)

        ok = wait_for(lambda: MARKER in terminal_text())
        state["type_directly"] = ok
        print(f"1. 直接在终端键入命令（Windows→Linux）：输出可见={ok}", flush=True)

        QTimer.singleShot(200, step_paste_from_windows)

    # ------------------------------------------------------------------
    # 2) 从 Windows 剪贴板粘贴到远端
    # ------------------------------------------------------------------
    def step_paste_from_windows() -> None:
        QGuiApplication.clipboard().setText(f"echo {PASTE_MARKER}\necho PSSH-PASTE-LINE-2")
        pane.view.setFocus()
        app.processEvents()

        # Ctrl+Shift+V：把 Windows 剪贴板内容发到远端
        QTest.keyClick(pane.view, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)

        ok = wait_for(lambda: PASTE_MARKER in terminal_text() and "PSSH-PASTE-LINE-2" in terminal_text())
        state["paste_from_windows"] = ok
        print(f"2. 粘贴 Windows 剪贴板到 Linux（多行）：两行都出现={ok}", flush=True)

        QTimer.singleShot(200, step_history_and_library)

    # ------------------------------------------------------------------
    # 3) 命令库发送 + 历史
    # ------------------------------------------------------------------
    def step_history_and_library() -> None:
        store = get_store()
        command = "echo PSSH-FROM-LIBRARY"

        # 直接调用与命令库相同的发送入口（记历史）
        pane.send_command(command)

        history_ok = wait_for(lambda: any(command in entry.text for entry in store.history_for(host.id))) and bool(
            store.history_for(host.id)
        )
        output_ok = wait_for(lambda: "PSSH-FROM-LIBRARY" in terminal_text())

        state["library_send_and_history"] = history_ok and output_ok
        print(
            f"3. 命令库发送 + 历史：历史 {len(store.history_for(host.id))} 条={history_ok}，输出={output_ok}",
            flush=True,
        )

        QTimer.singleShot(200, step_favorite_selection)

    # ------------------------------------------------------------------
    # 4) 收藏选中内容
    # ------------------------------------------------------------------
    def step_favorite_selection() -> None:
        # 选中包含刚才那条命令的若干行
        pane.view._anchor = (0, 0)
        pane.view._cursor_cell = (pane.view.emulator.lines - 1, 0)
        app.processEvents()

        selected = pane.view.selected_text()
        pane.view.select_to_copy = False  # 避免选中即复制干扰
        window.favorite_selection()

        store = get_store()
        stored = [item.text for item in store.snippets]
        ok = bool(selected.strip()) and any("PSSH" in text for text in stored)

        state["favorite_selection"] = ok
        print(f"4. 收藏选中内容：选中 {len(selected)} 字符，命令库现有 {len(stored)} 条={ok}", flush=True)

        pane.view.clear_selection()
        QTimer.singleShot(200, step_library_dialog)

    # ------------------------------------------------------------------
    # 5) 命令库对话框：发送 + 条目复制/剪切/粘贴/撤销
    # ------------------------------------------------------------------
    def step_library_dialog() -> None:
        store = get_store()
        store.add("echo PSSH-SNIPPET-A", group="e2e", host_id=host.id)
        snippet = store.add("echo PSSH-SNIPPET-B", group="e2e", host_id=host.id)

        dialog = CommandLibraryDialog(store, host_id=host.id, host_label=host.label, parent=window)
        dialog.send_requested.connect(pane.send_command)
        dialog.show()
        app.processEvents()
        dialog.grab().save(str(build_dir / "command-library.png"))

        dialog.select_ids([snippet.id])
        sent_ok = False

        if dialog.selected_snippet_ids():
            dialog.send_selected()
            app.processEvents()
            sent_ok = wait_for(lambda: "PSSH-SNIPPET-B" in terminal_text())
        else:
            print("命令库里没能选中条目", flush=True)

        # 条目复制 → 粘贴到新分组 → 撤销；再剪切 → 粘贴 → 撤销
        before = len(store.snippets)
        dialog.select_ids([snippet.id])
        dialog.copy_selection(MODE_COPY)
        dialog.paste_target.setCurrentText("e2e-复制组")
        dialog.paste_clipboard()
        after_paste = len(store.snippets)
        dialog.undo()
        after_undo = len(store.snippets)

        copy_ok = after_paste == before + 1 and after_undo == before

        dialog.select_ids([store.snippets[0].id])
        dialog.copy_selection(MODE_CUT)
        dialog.paste_target.setCurrentText("e2e-剪切组")
        dialog.paste_clipboard()
        moved = any(item.group == "e2e-剪切组" for item in store.snippets)
        dialog.undo()
        restored = not any(item.group == "e2e-剪切组" for item in store.snippets)

        state["library_send_and_history"] = state["library_send_and_history"] and sent_ok
        state["snippet_clipboard_undo"] = copy_ok and moved and restored
        print(
            f"5. 命令库：发送={sent_ok}；复制粘贴撤销 {before}→{after_paste}→{after_undo}；"
            f"剪切移动={moved}，撤销还原={restored}",
            flush=True,
        )

        dialog.close()
        QTimer.singleShot(200, step_copy_output)

    # ------------------------------------------------------------------
    # 6) 复制全部输出到 Windows
    # ------------------------------------------------------------------
    def step_copy_output() -> None:
        text = pane.copy_output()
        clipboard = QGuiApplication.clipboard().text()
        ok = MARKER in text and MARKER in clipboard and PASTE_MARKER in clipboard

        state["copy_output_to_windows"] = ok
        print(
            f"6. 复制全部输出（Linux→Windows）：{len(text.splitlines())} 行，剪贴板含标记={MARKER in clipboard}",
            flush=True,
        )

        QTimer.singleShot(200, step_save_output)

    # ------------------------------------------------------------------
    # 7) 输出另存
    # ------------------------------------------------------------------
    def step_save_output() -> None:
        size = pane.view.save_output(output_file)
        content = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
        ok = size > 0 and MARKER in content

        state["save_output"] = ok
        print(f"7. 输出另存：{output_file.name} {size} 字节，含标记={MARKER in content}", flush=True)

        QTimer.singleShot(200, step_terminal_cut)

    # ------------------------------------------------------------------
    # 8) 终端剪切 + 选中即复制
    # ------------------------------------------------------------------
    def step_terminal_cut() -> None:
        view = pane.view

        view._anchor = (0, 0)
        view._cursor_cell = (0, 60)
        app.processEvents()

        QGuiApplication.clipboard().clear()
        view.cut()
        cut_text = QGuiApplication.clipboard().text()
        cleared = view._selection_range() is None

        state["terminal_cut"] = bool(cut_text) and cleared
        print(f"8. 终端剪切：剪贴板 {len(cut_text)} 字符，选区已清空={cleared}", flush=True)

        window.set_select_to_copy(False)
        off_flag = view.select_to_copy is False
        window.set_select_to_copy(True)
        on_flag = view.select_to_copy is True

        QGuiApplication.clipboard().clear()
        view._anchor = (0, 0)
        view._cursor_cell = (3, 40)
        view.mouseReleaseEvent(None)  # type: ignore[arg-type]
        copied = QGuiApplication.clipboard().text()

        state["select_to_copy"] = off_flag and on_flag and bool(copied)
        print(
            f"9. 选中即复制：off={off_flag} on={on_flag}，松开鼠标后剪贴板 {len(copied)} 字符",
            flush=True,
        )

        finish()

    # ------------------------------------------------------------------

    def finish() -> None:
        watchdog.stop()

        try:
            output_file.unlink(missing_ok=True)
        except Exception:
            pass

        window.shutdown()

        from sshmanager.sshclient import close_all_clients
        from sshmanager.ui.session_tab import drain_sessions

        close_all_clients()
        drain_sessions(timeout=8)
        app.quit()

    QTimer.singleShot(300, step_connect)
    app.exec()

    print("--- 判定 ---", flush=True)

    for name, value in state.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    ok = all(state.values())
    print("COMMAND_FEATURES_E2E_OK" if ok else "COMMAND_FEATURES_E2E_FAILED", flush=True)

    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
