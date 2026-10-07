"""新增功能的 GUI 与链路验证：密钥管理、标签分组、拖拽递归传输、多会话、本地盘符。

会短暂弹窗并截图到 build/。

运行::

    .venv\\Scripts\\python.exe tests\\gui_extras_e2e.py --host <你的服务器> --password <密码>
"""

from __future__ import annotations

import argparse
import faulthandler
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

faulthandler.enable()

# 测试必须与真实数据目录隔离，避免污染主机列表与凭据
os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-extras-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.keygen import KeyStore
from sshmanager.ui import AppContext, KeyManagerDialog, MainWindow

build_dir = Path(__file__).resolve().parent.parent / "build"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.206.130")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    host = next(
        (item for item in context.hosts.hosts if item.hostname == args.host and item.port == args.port),
        None,
    )

    if host is None:
        host = Host(label=args.host, hostname=args.host, port=args.port, username=args.username)
        context.hosts.add(host)

    if args.password:
        context.credentials.set(host.id, args.password)

    window = MainWindow(context)
    window.resize(1280, 800)
    window.show()
    build_dir.mkdir(parents=True, exist_ok=True)

    state = {
        "key_manager": False,
        "key_generated": False,
        "host_groups": False,
        "recursive_transfer": False,
        "multi_session": False,
        "local_drives": False,
    }

    deadline = time.time() + args.timeout
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    # ------------------------------------------------------------------
    # 1) 密钥管理对话框：生成 Ed25519 + RSA 并展示
    # ------------------------------------------------------------------
    def step_keys() -> None:
        store = KeyStore()
        dialog = KeyManagerDialog(store, context.hosts, context.credentials, parent=window)
        dialog.resize(900, 560)
        dialog.show()
        app.processEvents()

        before = dialog.table.rowCount()

        # 通过对话框的生成逻辑（跳过模态子窗口，直接调用 store）
        created = []

        for name, key_type in (("e2e-ed25519", "ed25519"), ("e2e-rsa", "rsa")):
            try:
                created.append(store.generate(name=name, key_type=key_type, bits=2048))
            except Exception as error:  # noqa: BLE001
                print(f"生成 {name} 失败：{error}", flush=True)

        dialog.refresh()
        app.processEvents()
        dialog.table.selectRow(dialog.table.rowCount() - 1)
        app.processEvents()

        after = dialog.table.rowCount()
        preview = dialog.preview.toPlainText()
        paths_ok = all(key.path.exists() and key.public_path.exists() for key in created)

        state["key_manager"] = after >= before + 2
        state["key_generated"] = bool(created) and paths_ok and preview.startswith(("ssh-ed25519", "ssh-rsa"))
        print(f"1. 密钥管理：表内 {after} 条，公钥预览 {preview[:40]!r}", flush=True)

        dialog.grab().save(str(build_dir / "key-manager.png"))
        dialog.close()

        # 清理本次生成的密钥
        for key in created:
            store.remove(key.id)

        QTimer.singleShot(200, step_groups)

    # ------------------------------------------------------------------
    # 2) 标签分组：新增带标签的主机后，侧边栏应出现分组标题
    # ------------------------------------------------------------------
    def step_groups() -> None:
        for label, tag in (("prod-a", "prod"), ("prod-b", "prod"), ("stage-a", "stage")):
            context.hosts.add(Host(label=label, hostname=f"10.9.9.{len(context.hosts.hosts)}", tags=[tag]))

        window.refresh_hosts()
        app.processEvents()

        texts = [window.host_list.item(row).text() for row in range(window.host_list.count())]
        headers = [text for text in texts if text.startswith("— ")]
        state["host_groups"] = len(headers) >= 3 and any("prod" in text for text in headers)
        print(f"2. 侧边栏分组标题：{headers}", flush=True)

        window.grab().save(str(build_dir / "host-groups.png"))
        QTimer.singleShot(200, step_connect)

    # ------------------------------------------------------------------
    # 3) 先建立会话（拖拽下载要复用它），再打开 SFTP
    # ------------------------------------------------------------------
    def step_connect() -> None:
        window.connect_host(host)

        def poll() -> None:
            pane = window._session_for(host.id)

            if pane is not None and any(marker in pane.view.emulator.plain_text() for marker in ("#", "$", ">")):
                print("3. SSH 会话已就绪", flush=True)
                QTimer.singleShot(200, step_transfer)
                return

            if time.time() > deadline:
                print("SSH 会话未就绪", flush=True)
                app.quit()
                return

            QTimer.singleShot(400, poll)

        QTimer.singleShot(300, poll)

    # ------------------------------------------------------------------
    # 4) 递归传输：远端建目录树 → 通过 handle_drop 下载到临时目录
    # ------------------------------------------------------------------
    def step_transfer() -> None:
        window.open_sftp()
        panel = window.sftp_tabs.get(host.id)

        if panel is None:
            print("SFTP 面板未创建", flush=True)
            app.quit()
            return

        def wait_listing() -> None:
            if panel.remote_table.rowCount() > 0 or time.time() > deadline:
                prepare_remote(panel)
                return

            QTimer.singleShot(400, wait_listing)

        QTimer.singleShot(600, wait_listing)

    def prepare_remote(panel) -> None:
        """在远端准备一棵目录树，然后走拖拽入口下载回来。"""

        session = window._session_for(host.id)
        client = getattr(getattr(session, "session", None), "client", None)

        if client is None:
            print("没有可复用的 SSH 连接", flush=True)
            app.quit()
            return

        remote_root = "/tmp/pyssh-tree-test"

        try:
            client.exec_command(f"rm -rf {remote_root} && mkdir -p {remote_root}/sub", timeout=20)[1].read()
            setup = client.exec_command(
                f"printf 'alpha' > {remote_root}/a.txt && printf 'beta' > {remote_root}/sub/b.txt",
                timeout=20,
            )[1].read()
        except Exception as error:  # noqa: BLE001
            print(f"准备远端目录失败：{error}", flush=True)
            app.quit()
            return

        panel.refresh_remote("/tmp")

        def after_list() -> None:
            from sshmanager.sftpcore import RemoteEntry

            entry = RemoteEntry(name="pyssh-tree-test", path=remote_root, is_dir=True)
            workspace = Path(tempfile.mkdtemp(prefix="pyssh-droptest-"))

            panel.worker.task_finished.connect(lambda task, ok, message: on_task(task, ok, message, workspace, client))
            panel.handle_drop([entry], [], workspace)

        QTimer.singleShot(600, after_list)

    def on_task(task: str, ok: bool, message: str, workspace: Path, client) -> None:
        if "下载目录" not in task:
            return

        downloaded = workspace / "pyssh-tree-test"
        a_ok = (downloaded / "a.txt").exists() and (downloaded / "a.txt").read_text() == "alpha"
        b_ok = (downloaded / "sub" / "b.txt").exists() and (downloaded / "sub" / "b.txt").read_text() == "beta"

        state["recursive_transfer"] = bool(ok) and a_ok and b_ok
        print(f"3. 递归下载：ok={ok} a.txt={a_ok} sub/b.txt={b_ok} ({message})", flush=True)

        try:
            client.exec_command("rm -rf /tmp/pyssh-tree-test", timeout=15)[1].read()
        except Exception:
            pass

        QTimer.singleShot(200, step_multi_session)

    # ------------------------------------------------------------------
    # 4) 同一主机多会话
    # ------------------------------------------------------------------
    def step_multi_session() -> None:
        # 当前已有 1 条会话，再强制开两条
        window.connect_host(host, force_new=True)
        window.connect_host(host, force_new=True)

        def check() -> None:
            tabs = window._tabs_for_host(host.id)
            ready = 0

            for tab in tabs:
                pane = tab.active_pane() if hasattr(tab, "active_pane") else tab

                if pane is not None and any(m in pane.view.emulator.plain_text() for m in ("#", "$", ">")):
                    ready += 1

            # SSH 认证需要几秒，给足时间等三条都连上
            if (len(tabs) < 3 or ready < 3) and time.time() < deadline:
                QTimer.singleShot(600, check)
                return

            titles = [window.tabs_widget.tabText(window.tabs_widget.indexOf(tab)) for tab in tabs]

            state["multi_session"] = len(tabs) >= 3 and ready >= 3
            print(f"4. 多会话：标签 {len(tabs)} 个 → {titles}，已连接 {ready} 条", flush=True)

            window.grab().save(str(build_dir / "multi-session.png"))
            QTimer.singleShot(200, step_local_drives)

        QTimer.singleShot(400, check)

    # ------------------------------------------------------------------
    # 5) 本地盘符：C: / D: 全部可见且能切换
    # ------------------------------------------------------------------
    def step_local_drives() -> None:
        panel = window.sftp_tabs.get(host.id)

        if panel is None:
            print("SFTP 面板不存在，跳过盘符检查", flush=True)
            finish()
            return

        panel._reload_drives()
        drives = [str(panel.drive_combo.itemData(index)) for index in range(panel.drive_combo.count())]
        print(f"5. 盘符列表：{drives}", flush=True)

        c_drive = next((path for path in drives if path.upper().startswith("C:")), None)
        d_drive = next((path for path in drives if path.upper().startswith("D:")), None)

        checks = {"C盘可见": c_drive is not None, "D盘可见": d_drive is not None}

        if c_drive:
            checks["切到C盘根"] = panel.set_local_directory(c_drive)

        if d_drive:
            checks["切到D盘根"] = panel.set_local_directory(d_drive)

        project_root = Path(__file__).resolve().parent.parent

        if project_root.exists():
            checks["切到项目目录"] = panel.set_local_directory(str(project_root))

        # 面板当前应该停在项目目录
        checks["当前路径正确"] = Path(panel._local_root_path()) == project_root

        state["local_drives"] = all(checks.values())
        print("   " + " ".join(f"{name}={value}" for name, value in checks.items()), flush=True)

        # 文件列表是异步加载的，等它填充完再截图
        def wait_rows(attempt: int = 0) -> None:
            rows = panel.local_model.rowCount(panel.local_view.rootIndex())

            if rows == 0 and attempt < 10:
                QTimer.singleShot(300, lambda: wait_rows(attempt + 1))
                return

            print(f"   本地视图当前 {rows} 项", flush=True)
            panel.grab().save(str(build_dir / "sftp-local-drives.png"))
            finish()

        QTimer.singleShot(400, wait_rows)

    def finish() -> None:
        watchdog.stop()

        # 退出前确定性收尾（与 main.py 的退出路径一致）
        window.shutdown()

        from sshmanager.sshclient import close_all_clients
        from sshmanager.ui.session_tab import drain_sessions

        close_all_clients()
        drain_sessions(timeout=8)

        alive = [thread.name for thread in threading.enumerate() if thread is not threading.main_thread()]
        print(f"存活线程：{alive}", flush=True)

        app.quit()

    QTimer.singleShot(300, step_keys)
    app.exec()

    print("--- 判定 ---", flush=True)

    for name, value in state.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    ok = all(state.values())
    print("GUI_EXTRAS_E2E_OK" if ok else "GUI_EXTRAS_E2E_FAILED", flush=True)

    # 跳过解释器退出阶段（PySide6 6.11 + Python 3.14 退出期可能因 C 层残留线程崩溃）
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
