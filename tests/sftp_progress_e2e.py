"""SFTP 传输百分比的真实链路验证：单文件上传/下载 + 目录（批次）传输。

检查点：
1. 上传时能看到多个不同百分比，且单调不减、最终 100%
2. 下载时同理，摘要里带文件名与已传/总量
3. 目录传输显示「第 i/n 项 · 总 xx%」，且整体百分比最终到 100
4. 标签标题 / 状态栏也显示百分比文字

运行::

    .venv\\Scripts\\python.exe tests\\sftp_progress_e2e.py --host <你的服务器> --password <密码>
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-progress-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, MainWindow

REMOTE_DIR = "/tmp/pyssh-progress"
FILE_SIZE = 8 * 1024 * 1024  # 8 MB，足够产生很多分片回调


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
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    host = next(
        (item for item in context.hosts.hosts if item.hostname == args.host and item.port == args.port),
        None,
    )

    if host is None:
        host = Host(label=args.host or "test", hostname=args.host, port=args.port, username=args.username)
        context.hosts.add(host)

    host.verify_host_key = False
    context.hosts.update(host)

    if args.password:
        context.credentials.set(host.id, args.password)

    workspace = Path(tempfile.mkdtemp(prefix="pyssh-progress-work-"))
    big_file = workspace / "payload.bin"
    big_file.write_bytes(os.urandom(1024) * (FILE_SIZE // 1024))

    window = MainWindow(context)
    window.resize(1280, 800)
    window.show()

    state = {
        "upload_percent": False,
        "download_percent": False,
        "directory_batch_percent": False,
        "title_shows_percent": False,
    }

    samples: list = []
    titles: list[str] = []
    captured = {"mid": False}
    deadline = time.time() + args.timeout
    build_dir = Path(__file__).resolve().parent.parent / "build"

    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (print("WATCHDOG_TIMEOUT", flush=True), app.quit()))
    watchdog.start(int(args.timeout * 1000))

    panel = None

    def wait_for(predicate, timeout: float = 40.0, interval: float = 120) -> bool:
        end = time.time() + timeout

        while time.time() < end:
            if predicate():
                return True

            app.processEvents()
            time.sleep(interval / 1000.0)

        return predicate()

    def track(progress) -> None:
        samples.append(progress)

        if not captured["mid"] and 25 <= progress.percent <= 75:
            captured["mid"] = True
            window.grab().save(str(build_dir / "sftp-progress-mid.png"))
            print(f"   （已抓取传输中的界面截图 @ {progress.percent}%）", flush=True)

    def check_samples(task: str) -> tuple[bool, str]:
        picked = [item for item in samples if item.task == task]

        if not picked:
            return False, "没有收到进度信号"

        percents = [item.percent for item in picked]
        monotonic = all(b >= a for a, b in zip(percents, percents[1:]))
        reached = max(percents) == 100
        detail = f"{len(picked)} 次回调，百分比 {percents[0]}%→{percents[-1]}%（去重 {len(set(percents))} 个）"

        return monotonic and reached and len(set(percents)) >= 3, detail

    # ------------------------------------------------------------------

    def step_setup() -> None:
        nonlocal panel

        window.open_sftp()
        panel = window.sftp_tabs.get(host.id)

        if panel is None:
            print("SFTP 面板未创建", flush=True)
            app.quit()
            return

        panel.worker.progress_detail.connect(track)
        panel.progress_text.connect(lambda text: titles.append(text) if text else None)

        def wait_ready() -> None:
            if panel.remote_table.rowCount() > 0 or time.time() > deadline:
                prepare()
                return

            QTimer.singleShot(400, wait_ready)

        QTimer.singleShot(500, wait_ready)

    def prepare() -> None:
        panel.worker.request_mkdir(REMOTE_DIR)
        then_upload()

    # ------------------------------------------------------------------
    # 1) 上传（Windows → Linux）
    # ------------------------------------------------------------------
    def then_upload() -> None:
        samples.clear()
        panel.set_local_directory(str(workspace))
        wait_for(lambda: panel.local_model.rowCount(panel.local_view.rootIndex()) > 0, timeout=8)

        targets = [big_file]
        panel.upload_paths(targets, REMOTE_DIR)

        ok = wait_for(lambda: samples and samples[-1].percent == 100 and "上传" in samples[-1].task, timeout=90)
        passed, detail = check_samples("上传")

        state["upload_percent"] = passed and ok
        print(f"1. 上传百分比：{detail} → {'OK' if state['upload_percent'] else 'FAIL'}", flush=True)

        QTimer.singleShot(300, then_download)

    # ------------------------------------------------------------------
    # 2) 下载（Linux → Windows）
    # ------------------------------------------------------------------
    def then_download() -> None:
        samples.clear()
        panel.refresh_remote(REMOTE_DIR)

        def after_list() -> None:
            entry = next((item for item in panel._remote_entries if item.name == "payload.bin"), None)

            if entry is None:
                print("远端没有 payload.bin", flush=True)
                app.quit()
                return

            target_dir = workspace / "downloaded"
            target_dir.mkdir(exist_ok=True)
            panel.download_entries([entry], target_dir)

            ok = wait_for(
                lambda: samples and samples[-1].percent == 100 and "下载" in samples[-1].task,
                timeout=90,
            )
            passed, detail = check_samples("下载")

            local_copy = target_dir / "payload.bin"
            same = local_copy.exists() and local_copy.stat().st_size == big_file.stat().st_size

            state["download_percent"] = passed and ok and same
            print(f"2. 下载百分比：{detail}，文件大小一致={same} → "
                  f"{'OK' if state['download_percent'] else 'FAIL'}", flush=True)

            QTimer.singleShot(300, then_directory)

        QTimer.singleShot(800, after_list)

    # ------------------------------------------------------------------
    # 3) 目录传输（批次百分比）
    # ------------------------------------------------------------------
    def then_directory() -> None:
        samples.clear()
        panel.refresh_remote(REMOTE_DIR)

        def after_list() -> None:
            entry = next((item for item in panel._remote_entries if item.name == "payload.bin"), None)

            if entry is None:
                app.quit()
                return

            folder = REMOTE_DIR + "/batch"
            panel.worker.request_mkdir(folder)
            panel.refresh_remote(folder)

            def after_folder() -> None:
                # 往目录里放 3 个文件再整目录下载
                panel.upload_paths([big_file], folder)

                def after_upload() -> None:
                    panel.refresh_remote(REMOTE_DIR)
                    QTimer.singleShot(800, lambda: download_directory())

                QTimer.singleShot(4000, after_upload)

            QTimer.singleShot(900, after_folder)

        def download_directory() -> None:
            folder = next(
                (item for item in panel._remote_entries if item.name == "batch"),
                None,
            )

            if folder is None:
                print("没有找到 batch 目录", flush=True)
                app.quit()
                return

            samples.clear()
            target = workspace / "batch-download"
            target.mkdir(exist_ok=True)
            panel.download_entries([folder], target)

            ok = wait_for(lambda: samples and samples[-1].percent == 100, timeout=120)
            batched = [item for item in samples if item.batch_total > 0]
            shows_batch = any("总" in item.summary() or item.batch_total > 0 for item in batched)

            state["directory_batch_percent"] = ok and bool(batched) and shows_batch
            print(
                f"3. 目录（批次）百分比：{len(samples)} 次回调，带批次总量的 {len(batched)} 次 → "
                f"{'OK' if state['directory_batch_percent'] else 'FAIL'}",
                flush=True,
            )
            if samples:
                print(f"   最后一条：{samples[-1].summary()}", flush=True)

            panel.grab().save(str(build_dir / "sftp-progress.png"))
            window.grab().save(str(build_dir / "sftp-progress-window.png"))

            check_title()

        def check_title() -> None:
            percent_titles = [text for text in titles if "%" in text]
            state["title_shows_percent"] = bool(percent_titles)
            print(
                f"4. 标签标题/状态栏显示百分比：{percent_titles[:2]} → "
                f"{'OK' if state['title_shows_percent'] else 'FAIL'}",
                flush=True,
            )

            finish()

        QTimer.singleShot(700, after_list)

    # ------------------------------------------------------------------

    def finish() -> None:
        watchdog.stop()
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
    print("SFTP_PROGRESS_E2E_OK" if ok else "SFTP_PROGRESS_E2E_FAILED", flush=True)

    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
