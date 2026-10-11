"""会话录制/回放 与 SFTP 断点续传的真实链路验证。

运行::

    .venv\\Scripts\\python.exe tests\\recording_resume_e2e.py --host <你的服务器> --password <密码>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

# 与真实数据目录隔离
os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-rec-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.recording import load_recording
from sshmanager.sftpcore import SftpClient
from sshmanager.ui import AppContext, MainWindow
from sshmanager.ui.replay_dialog import ReplayDialog

MARKER = "PYSSH_RECORD_OK"
REMOTE_FILE = "/tmp/pyssh-resume-test.bin"
FILE_SIZE = 512 * 1024


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="")
    parser.add_argument("--port", type=int, default=22)
    parser.add_argument("--username", default="root")
    parser.add_argument("--password", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
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

    workspace = Path(tempfile.mkdtemp(prefix="pyssh-rec-work-"))
    window = MainWindow(context)
    window.resize(1200, 760)
    window.show()

    state = {"recording": False, "replay": False, "resume": False}
    deadline = time.time() + args.timeout

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    # ------------------------------------------------------------------
    # 1) 连接 → 录制 → 执行命令 → 停止录制 → 校验 cast 文件
    # ------------------------------------------------------------------
    def step_record() -> None:
        window.connect_host(host)

        def wait_prompt() -> None:
            pane = window._session_for(host.id)

            if pane is None or not any(m in pane.view.emulator.plain_text() for m in ("#", "$", ">")):
                if time.time() > deadline:
                    print("会话未就绪", flush=True)
                    app.quit()
                    return

                QTimer.singleShot(400, wait_prompt)
                return

            cast_path = workspace / "session.cast"
            pane.start_recording(cast_path)

            # 走真实按键路径（这样输入事件也会被记录），而不是直接调 session.send
            from PySide6.QtTest import QTest

            pane.view.setFocus()
            QTest.keyClicks(pane.view, f"echo {MARKER}")
            QTest.keyClick(pane.view, __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Key.Key_Return)

            QTimer.singleShot(1800, lambda: finish_record(pane, cast_path))

        QTimer.singleShot(300, wait_prompt)

    def finish_record(pane, cast_path: Path) -> None:
        pane.stop_recording()

        recording = load_recording(cast_path)
        text = "".join(event.data for event in recording.events if event.kind == "o")
        inputs = [event for event in recording.events if event.kind == "i"]

        header_ok = recording.width > 0 and recording.height > 0
        state["recording"] = (
            cast_path.exists()
            and header_ok
            and MARKER in text
            and len(inputs) >= 1
        )

        print(
            f"1. 录制：文件 {cast_path.stat().st_size} 字节，事件 {len(recording.events)} 条"
            f"（输出 {len(recording.events) - len(inputs)} / 输入 {len(inputs)}），"
            f"尺寸 {recording.width}x{recording.height}，时长 {recording.duration:.2f}s，"
            f"含标记={MARKER in text}",
            flush=True,
        )

        QTimer.singleShot(200, lambda: step_replay(cast_path))

    # ------------------------------------------------------------------
    # 2) 回放：加载录制并快进到结尾
    # ------------------------------------------------------------------
    def step_replay(cast_path: Path) -> None:
        dialog = ReplayDialog(cast_path, parent=window)
        dialog.resize(900, 560)
        dialog.show()
        app.processEvents()

        total = dialog.recording.duration if dialog.recording else 0.0
        dialog._on_seek(1000)  # 拉到结尾
        app.processEvents()
        replayed = dialog.view.emulator.plain_text()
        state["replay"] = dialog.recording is not None and MARKER in replayed and total > 0

        print(f"2. 回放：时长 {total:.1f}s，拉到结尾后含标记={MARKER in replayed}", flush=True)

        dialog._timer.stop()
        dialog.close()
        QTimer.singleShot(200, step_resume)

    # ------------------------------------------------------------------
    # 3) 断点续传
    # ------------------------------------------------------------------
    def step_resume() -> None:
        pane = window._session_for(host.id)
        client = getattr(getattr(pane, "session", None), "client", None)

        if client is None:
            print("没有可复用的连接", flush=True)
            app.quit()
            return

        # 远端造一个 512KB 文件
        client.exec_command(
            f"head -c {FILE_SIZE} /dev/urandom > {REMOTE_FILE} && stat -c %s {REMOTE_FILE}",
            timeout=30,
        )[1].read()

        sftp = SftpClient(client)
        sftp.open()

        target = workspace / "resume.bin"
        partial = target.with_name(target.name + ".part")

        # 第一次完整下载
        written_first, resumed_first = sftp.download(REMOTE_FILE, target, resume=True)

        # 模拟"上次只传了一部分"：删掉完整文件，只留前 200KB 的 .part
        chunk_size = 200 * 1024
        data = target.read_bytes()
        target.unlink()
        partial.write_bytes(data[:chunk_size])

        # 再传一次，应只补缺失部分
        written_second, resumed_second = sftp.download(REMOTE_FILE, target, resume=True)
        size_local = target.stat().st_size

        # 与远端内容逐字节比对
        _, stdout, _ = client.exec_command(f"md5sum {REMOTE_FILE} | cut -d' ' -f1", timeout=20)
        remote_md5 = stdout.read().decode().strip()

        import hashlib

        local_md5 = hashlib.md5(target.read_bytes()).hexdigest()
        state["resume"] = (
            resumed_second
            and written_second == FILE_SIZE - chunk_size
            and size_local == FILE_SIZE
            and remote_md5 == local_md5
        )

        print(
            f"3. 续传：首次 {written_first} 字节（续传={resumed_first}），"
            f"中断后补传 {written_second} 字节（续传={resumed_second}），"
            f"本地 {size_local} 字节，md5 一致={remote_md5 == local_md5}",
            flush=True,
        )

        sftp.remove(REMOTE_FILE)
        sftp.close()
        finish()

    def finish() -> None:
        watchdog.stop()
        window.shutdown()

        from sshmanager.sshclient import close_all_clients
        from sshmanager.ui.session_tab import drain_sessions

        close_all_clients()
        drain_sessions(timeout=8)
        app.quit()

    QTimer.singleShot(300, step_record)
    app.exec()

    print("--- 判定 ---", flush=True)

    for name, value in state.items():
        print(f"{name}: {'OK' if value else 'FAIL'}", flush=True)

    ok = all(state.values())
    print("RECORDING_RESUME_E2E_OK" if ok else "RECORDING_RESUME_E2E_FAILED", flush=True)

    # 跳过解释器退出阶段（PySide6 6.11 + Python 3.14 退出期可能因 C 层残留线程崩溃）
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
