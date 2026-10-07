"""SFTP 复制/剪切/粘贴/撤销的真实链路验证（Windows ↔ Linux）。

覆盖：
1. Linux→Windows：远端复制 → 本地粘贴（下载）
2. 撤销：本地新建的文件被移除
3. Windows→Linux：本地复制 → 远端粘贴（上传）
4. 剪切 + 粘贴（Windows→Linux）：源移入回收站、目标出现；撤销后源恢复、目标消失
5. 远端内部复制（Linux→Linux，冲突自动改名）
6. 删除到回收站 + 撤销恢复

运行::

    .venv\\Scripts\\python.exe tests\\copy_paste_e2e.py --host 192.168.206.130 --password 123456
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-copypaste-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.sftpcore import SftpClient
from sshmanager.ui import AppContext, MainWindow

REMOTE_DIR = "/tmp/pyssh-copypaste"


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

    # 删除确认框在自动化里直接当"是"
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)  # type: ignore[assignment]

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    host = next(
        (item for item in context.hosts.hosts if item.hostname == args.host and item.port == args.port),
        None,
    )

    if host is None:
        host = Host(label=args.host, hostname=args.host, port=args.port, username=args.username)
        context.hosts.add(host)

    # 测试用隔离数据目录，known_hosts 是空的；这里直接跳过指纹校验，
    # 免得 SFTP 因"未知主机"拒绝连接（真实使用中先在 SSH 会话里信任一次即可）
    host.verify_host_key = False
    context.hosts.update(host)

    if args.password:
        context.credentials.set(host.id, args.password)

    workspace = Path(tempfile.mkdtemp(prefix="pyssh-cp-work-"))
    window = MainWindow(context)
    window.resize(1280, 800)
    window.show()

    state = {
        "linux_to_windows": False,
        "undo_download": False,
        "windows_to_linux": False,
        "cut_and_undo": False,
        "remote_to_remote": False,
        "trash_and_undo": False,
    }

    deadline = time.time() + args.timeout
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    panel = None
    client = None  # 独立连接，用于校验远端状态

    # ------------------------------------------------------------------

    def wait_for(predicate, timeout: float = 20.0, interval: float = 250) -> bool:
        """轮询等待条件成立（期间继续跑事件循环）。"""

        end = time.time() + timeout

        while time.time() < end:
            if predicate():
                return True

            app.processEvents()
            time.sleep(interval / 1000.0)

        return predicate()

    def use_side(side: str) -> None:
        """把某一栏设为活动栏（自动化里窗口不一定真正激活，焦点不可靠）。"""

        panel.set_active_side(side)
        (panel.remote_table if side == "remote" else panel.local_view).setFocus()
        app.processEvents()

    def goto_remote(path: str) -> bool:
        """切换远端目录并**等列表真正到达**（否则 _remote_path 还是旧值）。"""

        panel.refresh_remote(path)
        app.processEvents()

        return wait_for(lambda: panel._remote_path == path and panel.remote_table.rowCount() >= 0, timeout=15)

    def wait_prompt_ready() -> bool:
        return wait_for(lambda: panel.remote_table.rowCount() > 0, timeout=15)

    def select_local(path: Path) -> bool:
        index = panel.local_model.index(str(path))

        if not index.isValid():
            return False

        panel.local_view.setCurrentIndex(index)
        panel.local_view.selectionModel().select(
            index,
            panel.local_view.selectionModel().SelectionFlag.ClearAndSelect
            | panel.local_view.selectionModel().SelectionFlag.Rows,
        )
        app.processEvents()

        return True

    def select_remote(name: str) -> bool:
        for row, entry in enumerate(panel._remote_entries):
            if entry.name == name:
                panel.remote_table.selectRow(row)
                app.processEvents()
                return True

        return False

    def remote_exists(path: str) -> bool:
        try:
            return bool(client.exists(path))
        except Exception:
            return False

    def wait_undo_ready(before: int | None = None, timeout: float = 25.0) -> bool:
        """等界面把这次操作结算进撤销栈。

        注意不能只判断"栈非空"——上一步的记录还在栈里，必须等深度增长。
        """

        if before is None:
            before = 0

        return wait_for(lambda: panel.undo_stack.depth > before, timeout=timeout)

    # ------------------------------------------------------------------

    def step_setup() -> None:
        nonlocal panel

        window.open_sftp()
        panel = window.sftp_tabs.get(host.id)
        if panel is None:
            print("SFTP 面板未创建", flush=True)
            app.quit()
            return

        def wait_ready() -> None:
            if panel.remote_table.rowCount() > 0 or time.time() > deadline:
                prepare()
                return

            QTimer.singleShot(400, wait_ready)

        QTimer.singleShot(500, wait_ready)

    def prepare() -> None:
        nonlocal client

        # 这个测试只开 SFTP 面板，没有可复用的终端会话；自建一条独立连接做校验
        from sshmanager.sshclient import SSHClient

        window.connect_host(host)

        def use_session() -> None:
            nonlocal client

            session = window._session_for(host.id)
            ssh = getattr(getattr(session, "session", None), "client", None)
            transport = getattr(ssh, "transport", None)
            ready = (
                ssh is not None
                and transport is not None
                and getattr(transport, "is_authenticated", lambda: False)()
            )

            if not ready:
                if time.time() > deadline:
                    print("SSH 会话未就绪", flush=True)
                    app.quit()
                    return

                QTimer.singleShot(400, use_session)
                return

            client = SftpClient(ssh)
            client.open()
            start_checks()

        use_session()

    def start_checks() -> None:
        # 诊断：确认批次结果信号真的到达界面线程
        panel.worker.batch_result.connect(
            lambda batch, created, moved: print(
                f"[diag] sftp batch={batch} created={created} moved={moved}", flush=True
            )
        )
        panel.local_worker.batch_result.connect(
            lambda batch, created, moved: print(
                f"[diag] local batch={batch} created={created} moved={moved}", flush=True
            )
        )

        try:
            client.remove_tree(REMOTE_DIR)
        except Exception:
            pass

        client.mkdir_p(REMOTE_DIR)
        client.write_bytes(f"{REMOTE_DIR}/from-linux.txt", b"linux-content")

        # 本地工作目录里放一个文件供上传
        (workspace / "from-windows.txt").write_text("windows-content", encoding="utf-8")

        goto_remote(REMOTE_DIR)

        def after_list() -> None:
            if not select_remote("from-linux.txt"):
                print("远端列表里找不到测试文件", flush=True)
                app.quit()
                return

            test_linux_to_windows()

        QTimer.singleShot(700, after_list)

    # ------------------------------------------------------------------
    # 1) Linux → Windows（下载）
    # ------------------------------------------------------------------
    def test_linux_to_windows() -> None:
        depth_before = panel.undo_stack.depth
        use_side("remote")
        panel.copy_selection()

        # 本地栏定位到工作目录并粘贴
        panel.set_local_directory(str(workspace))
        app.processEvents()

        wait_for(lambda: panel.local_model.rowCount(panel.local_view.rootIndex()) > 0, timeout=5)
        use_side("local")
        panel.paste_clipboard()

        target = workspace / "from-linux.txt"
        ok = wait_for(lambda: target.exists() and target.read_text(encoding="utf-8") == "linux-content")

        state["linux_to_windows"] = ok
        print(f"1. Linux→Windows（下载）：目标存在且内容一致={ok}", flush=True)

        wait_undo_ready(depth_before)
        print(
            f"   撤销栈深度={panel.undo_stack.depth}，最近一条={panel.undo_stack.last_label!r}",
            flush=True,
        )

        # ------------------------------------------------------------------
        # 2) 撤销下载
        # ------------------------------------------------------------------
        entry = panel.undo_stack.peek()

        if entry is not None:
            print(f"   待撤销记录：created={entry.created} moved={entry.moved}", flush=True)

        panel.undo_last()

        undone = wait_for(lambda: not target.exists())
        state["undo_download"] = undone
        print(f"2. 撤销下载：本地文件已移除={undone}", flush=True)

        QTimer.singleShot(300, test_windows_to_linux)

    # ------------------------------------------------------------------
    # 3) Windows → Linux（上传）
    # ------------------------------------------------------------------
    def test_windows_to_linux() -> None:
        depth_before = panel.undo_stack.depth
        source = workspace / "from-windows.txt"
        panel.set_local_directory(str(workspace))
        wait_for(lambda: panel.local_model.rowCount(panel.local_view.rootIndex()) > 0, timeout=5)

        if not select_local(source):
            print("本地列表里找不到待上传文件", flush=True)
            app.quit()
            return

        use_side("local")
        panel.copy_selection()

        goto_remote(REMOTE_DIR)
        app.processEvents()
        use_side("remote")
        panel.paste_clipboard()

        remote_target = f"{REMOTE_DIR}/from-windows.txt"
        ok = wait_for(lambda: remote_exists(remote_target))
        wait_undo_ready(depth_before)

        state["windows_to_linux"] = ok
        print(f"3. Windows→Linux（上传）：远端已出现文件={ok}", flush=True)

        QTimer.singleShot(300, test_cut_and_undo)

    # ------------------------------------------------------------------
    # 4) 剪切 + 粘贴 + 撤销
    # ------------------------------------------------------------------
    def test_cut_and_undo() -> None:
        depth_before = panel.undo_stack.depth
        cut_source = workspace / "to-cut.txt"
        cut_source.write_text("cut-me", encoding="utf-8")

        panel.set_local_directory(str(workspace))
        wait_for(lambda: panel.local_model.rowCount(panel.local_view.rootIndex()) > 0, timeout=5)

        if not select_local(cut_source):
            print("本地列表里找不到待剪切文件", flush=True)
            app.quit()
            return

        use_side("local")
        panel.cut_selection()

        goto_remote(REMOTE_DIR)
        app.processEvents()
        use_side("remote")
        panel.paste_clipboard()

        remote_cut = f"{REMOTE_DIR}/to-cut.txt"
        moved = wait_for(lambda: remote_exists(remote_cut) and not cut_source.exists())
        wait_undo_ready(depth_before)

        print(f"4a. 剪切粘贴：远端出现={remote_exists(remote_cut)}，本地源已移走={not cut_source.exists()}", flush=True)

        # 撤销：远端副本应消失，本地源应恢复
        panel.undo_last()

        restored = wait_for(lambda: cut_source.exists() and not remote_exists(remote_cut), timeout=25)
        state["cut_and_undo"] = moved and restored
        print(f"4. 剪切+撤销：源恢复={cut_source.exists()}，远端副本移除={not remote_exists(remote_cut)}", flush=True)

        QTimer.singleShot(300, test_remote_to_remote)

    # ------------------------------------------------------------------
    # 5) 远端内部复制
    # ------------------------------------------------------------------
    def test_remote_to_remote() -> None:
        depth_before = panel.undo_stack.depth
        goto_remote(REMOTE_DIR)

        def after() -> None:
            if not select_remote("from-linux.txt"):
                print("远端列表里找不到文件", flush=True)
                app.quit()
                return

            use_side("remote")
            panel.copy_selection()
            panel.paste_clipboard()

            copied = f"{REMOTE_DIR}/from-linux (2).txt"
            ok = wait_for(lambda: remote_exists(copied))
            wait_undo_ready(depth_before)

            state["remote_to_remote"] = ok
            print(f"5. 远端内部复制（冲突改名）：生成 {Path(copied).name}={ok}", flush=True)

            panel.undo_last()
            removed = wait_for(lambda: not remote_exists(copied))
            print(f"   撤销后已移除={removed}", flush=True)
            state["remote_to_remote"] = ok and removed

            QTimer.singleShot(300, test_trash_and_undo)

        QTimer.singleShot(700, after)

    # ------------------------------------------------------------------
    # 6) 删除到回收站 + 撤销
    # ------------------------------------------------------------------
    def test_trash_and_undo() -> None:
        depth_before = panel.undo_stack.depth
        victim = f"{REMOTE_DIR}/to-delete.txt"
        client.write_bytes(victim, b"delete-me")
        goto_remote(REMOTE_DIR)

        def after() -> None:
            if not select_remote("to-delete.txt"):
                print("远端列表里找不到待删除文件", flush=True)
                app.quit()
                return

            use_side("remote")
            panel.delete_selected()

            gone = wait_for(lambda: not remote_exists(victim))
            wait_undo_ready(depth_before)
            panel.undo_last()
            back = wait_for(lambda: remote_exists(victim), timeout=25)

            state["trash_and_undo"] = gone and back
            print(f"6. 删除到回收站+撤销：已删除={gone}，撤销恢复={back}", flush=True)

            finish()

        QTimer.singleShot(700, after)

    # ------------------------------------------------------------------

    def finish() -> None:
        watchdog.stop()

        try:
            client.remove_tree(REMOTE_DIR)
            client.close()
        except Exception:
            pass

        window.shutdown()

        from sshmanager.sshclient import close_all_clients
        from sshmanager.ui.session_tab import drain_sessions

        close_all_clients()
        drain_sessions(timeout=8)
        app.quit()

    QTimer.singleShot(300, step_setup)
    app.exec()

    print("--- 判定 ---", flush=True)

    for name, value in state.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    ok = all(state.values())
    print("COPY_PASTE_E2E_OK" if ok else "COPY_PASTE_E2E_FAILED", flush=True)

    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
