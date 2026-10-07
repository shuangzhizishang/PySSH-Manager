"""命令功能的真实链路验证：输入栏发送、输出回拷、另存、剪切、命令库。

覆盖：
1. 命令输入栏发送（Windows→Linux）并在终端看到输出
2. 多行命令逐行执行
3. 发送过的命令进入历史
4. 收藏命令，并从命令库再发送
5. 复制全部输出到 Windows 剪贴板（Linux→Windows）
6. 输出另存为文件
7. 终端选中后「剪切」：复制到剪贴板并清空选区
8. 选中即复制开关
9. 命令库条目：复制 / 剪切 / 粘贴 / 撤销

运行::

    .venv\\Scripts\\python.exe tests\\command_features_e2e.py --host 192.168.206.130 --password 123456
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

from PySide6.QtCore import QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox

from sshmanager.commands import MODE_COPY, MODE_CUT, get_store, reset_store
from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, MainWindow
from sshmanager.ui.command_library import CommandLibraryDialog

MARKER = "PSSH-COMMAND-BAR-OK"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.206.130")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
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
        "send_from_bar": False,
        "multiline": False,
        "history_recorded": False,
        "favorite_and_send": False,
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
            prompt_seen = wait_for(
                lambda: any(marker in terminal_text() for marker in ("#", "$", ">")) and len(terminal_text()) > 3,
                timeout=30,
            )

            if not prompt_seen:
                print("没有看到 shell 提示符", flush=True)
                app.quit()
                return

            print("SSH 会话已就绪，命令输入栏可见 =", pane.command_bar_visible, flush=True)
            QTimer.singleShot(300, step_send_from_bar)

        QTimer.singleShot(500, poll)

    # ------------------------------------------------------------------
    # 1) 输入栏发送
    # ------------------------------------------------------------------
    def step_send_from_bar() -> None:
        pane.command_bar.set_text(f"echo {MARKER}")
        app.processEvents()
        pane.command_bar.send_button.click()

        ok = wait_for(lambda: MARKER in terminal_text())
        state["send_from_bar"] = ok
        print(f"1. 命令输入栏发送（Windows→Linux）：输出可见={ok}", flush=True)

        QTimer.singleShot(200, step_multiline)

    # ------------------------------------------------------------------
    # 2) 多行命令
    # ------------------------------------------------------------------
    def step_multiline() -> None:
        pane.command_bar.set_text("echo PSSH-LINE-1\necho PSSH-LINE-2")
        app.processEvents()
        pane.command_bar.send_button.click()

        ok = wait_for(
            lambda: "PSSH-LINE-1" in terminal_text() and "PSSH-LINE-2" in terminal_text()
        )
        state["multiline"] = ok
        print(f"2. 多行命令逐行执行：两行都出现={ok}", flush=True)

        QTimer.singleShot(200, step_history)

    # ------------------------------------------------------------------
    # 3) 历史
    # ------------------------------------------------------------------
    def step_history() -> None:
        store = get_store()
        entries = store.history_for(host.id)
        recorded = any(MARKER in entry.text for entry in entries) and len(entries) >= 2

        state["history_recorded"] = recorded
        print(f"3. 命令历史：共 {len(entries)} 条，包含刚才的命令={recorded}", flush=True)

        QTimer.singleShot(200, step_favorite)

    # ------------------------------------------------------------------
    # 4) 收藏 + 从命令库发送
    # ------------------------------------------------------------------
    def step_favorite() -> None:
        store = get_store()
        command = "echo PSSH-FROM-LIBRARY"
        pane.command_bar.set_text(command)
        app.processEvents()
        pane.favorite_command(command)

        snippet = next((item for item in store.snippets if item.text == command), None)

        if snippet is None:
            print("收藏失败", flush=True)
            app.quit()
            return

        dialog = CommandLibraryDialog(store, host_id=host.id, host_label=host.label, parent=window)
        dialog.send_requested.connect(pane.send_command)
        dialog.show()
        app.processEvents()

        # 选中刚收藏的那条并发送
        for row in range(dialog.table.rowCount()):
            if dialog.table.item(row, 0).data(0x0100) == snippet.id:  # UserRole
                dialog.table.selectRow(row)
                break

        app.processEvents()
        dialog.send_selected()
        app.processEvents()

        ok = wait_for(lambda: "PSSH-FROM-LIBRARY" in terminal_text())

        # 命令库条目操作：复制 → 粘贴到新分组 → 撤销
        before = len(store.snippets)
        dialog.table.selectRow(
            next(
                (
                    row
                    for row in range(dialog.table.rowCount())
                    if dialog.table.item(row, 0).data(0x0100) == snippet.id
                ),
                0,
            )
        )
        app.processEvents()
        dialog.copy_selection(MODE_COPY)
        dialog.paste_target.setCurrentText("e2e-分组")
        dialog.paste_clipboard()
        after_paste = len(store.snippets)
        dialog.undo()
        after_undo = len(store.snippets)

        clipboard_ok = after_paste == before + 1 and after_undo == before

        # 剪切：移动分组后再撤销
        dialog.table.selectRow(0)
        app.processEvents()
        dialog.copy_selection(MODE_CUT)
        dialog.paste_target.setCurrentText("e2e-剪切组")
        dialog.paste_clipboard()
        moved = any(item.group == "e2e-剪切组" for item in store.snippets)
        dialog.undo()
        restored = not any(item.group == "e2e-剪切组" for item in store.snippets)

        state["favorite_and_send"] = ok
        state["snippet_clipboard_undo"] = clipboard_ok and moved and restored
        print(f"4. 收藏并从命令库发送：输出可见={ok}", flush=True)
        print(
            f"   条目复制/粘贴/撤销：粘贴后 {after_paste} → 撤销后 {after_undo}（原 {before}）；"
            f"剪切移动={moved}，撤销还原={restored}",
            flush=True,
        )

        dialog.close()
        QTimer.singleShot(200, step_copy_output)

    # ------------------------------------------------------------------
    # 5) 复制全部输出到 Windows
    # ------------------------------------------------------------------
    def step_copy_output() -> None:
        text = pane.view.copy_all_output()
        clipboard = QGuiApplication.clipboard().text()
        ok = MARKER in text and MARKER in clipboard and "PSSH-LINE-2" in clipboard

        state["copy_output_to_windows"] = ok
        print(
            f"5. 复制全部输出（Linux→Windows）：{len(text.splitlines())} 行，"
            f"剪贴板含标记={MARKER in clipboard}",
            flush=True,
        )

        QTimer.singleShot(200, step_save_output)

    # ------------------------------------------------------------------
    # 6) 输出另存
    # ------------------------------------------------------------------
    def step_save_output() -> None:
        size = pane.view.save_output(output_file)
        content = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
        ok = size > 0 and MARKER in content

        state["save_output"] = ok
        print(f"6. 输出另存：{output_file.name} {size} 字节，含标记={MARKER in content}", flush=True)

        QTimer.singleShot(200, step_terminal_cut)

    # ------------------------------------------------------------------
    # 7) 终端剪切 + 选中即复制
    # ------------------------------------------------------------------
    def step_terminal_cut() -> None:
        view = pane.view

        # 造一个覆盖首行的选区
        view._anchor = (0, 0)
        view._cursor_cell = (0, 60)
        app.processEvents()

        QGuiApplication.clipboard().clear()
        view.cut()
        cut_text = QGuiApplication.clipboard().text()
        cleared = view._selection_range() is None

        state["terminal_cut"] = bool(cut_text) and cleared
        print(f"7. 终端剪切：剪贴板 {len(cut_text)} 字符，选区已清空={cleared}", flush=True)

        # 选中即复制开关
        view.select_to_copy = False
        window.set_select_to_copy(False)
        off_flag = view.select_to_copy is False
        window.set_select_to_copy(True)
        on_flag = view.select_to_copy is True

        panes_seen = [id(pane_item) for pane_item in window._all_terminal_panes_all()]
        print(f"   [diag] 主窗口收集到的终端面板={panes_seen}，当前 pane={id(pane)}，flag={view.select_to_copy}", flush=True)

        QGuiApplication.clipboard().clear()
        view._anchor = (0, 0)
        view._cursor_cell = (3, 40)
        selected = view.selected_text()
        view.mouseReleaseEvent(None)  # type: ignore[arg-type]
        copied = QGuiApplication.clipboard().text()

        state["select_to_copy"] = off_flag and on_flag and bool(copied)
        print(
            f"8. 选中即复制：off={off_flag} on={on_flag} flag={view.select_to_copy} "
            f"选中文本={len(selected)} 字符，剪贴板 {len(copied)} 字符",
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
