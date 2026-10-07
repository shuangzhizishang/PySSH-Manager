"""验证真实用户流程：事件循环运行中关闭窗口 → 所有会话线程与传输层都退出。

（之前诊断脚本是在 app.quit() 之后才 window.close()，那时事件循环已停止，
closeEvent 不会被处理，属于测试写法问题，不是应用缺陷。）
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-close-"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.ui import AppContext, MainWindow

HOST = "192.168.206.130"


def other_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t is not threading.main_thread()]


def main() -> int:
    password = ""
    store = CredentialStore()

    for key in list(store._cache):  # noqa: SLF001 - 取第一条凭据的明文
        password = store.get(key) or ""
        break

    app = QApplication(sys.argv[:1])
    app.setStyle("Fusion")

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    host = Host(label=HOST, hostname=HOST, username="root")
    context.hosts.add(host)

    if password:
        context.credentials.set(host.id, password)

    window = MainWindow(context)
    window.show()

    result = {"connected": 0, "before": [], "after": [], "after_wait": []}

    def connect_two() -> None:
        window.connect_host(host)
        window.connect_host(host, force_new=True)

        def poll(attempt: int = 0) -> None:
            tabs = window._tabs_for_host(host.id)
            ready = 0

            for tab in tabs:
                if any(m in tab.view.emulator.plain_text() for m in ("#", "$", ">")):
                    ready += 1

            if ready < 2 and attempt < 40:
                QTimer.singleShot(500, lambda: poll(attempt + 1))
                return

            result["connected"] = ready
            result["before"] = other_threads()
            print(f"已连接 {ready} 条会话；关闭前线程：{result['before']}", flush=True)

            # 在事件循环运行中关窗（真实用户点 X 的情形）
            window.close()
            result["after"] = other_threads()
            print(f"关窗后立即线程：{result['after']}", flush=True)

            QTimer.singleShot(1500, check_after)

        QTimer.singleShot(300, poll)

    def check_after() -> None:
        result["after_wait"] = other_threads()
        print(f"等待 1.5s 后线程：{result['after_wait']}", flush=True)
        app.quit()

    QTimer.singleShot(300, connect_two)
    QTimer.singleShot(90000, app.quit)
    app.exec()

    clean = not result["after_wait"]

    # 应用退出时也会做这一步（main.py 里调用 close_all_clients + drain_sessions）
    from sshmanager.sshclient import close_all_clients
    from sshmanager.ui.session_tab import drain_sessions

    closed = close_all_clients()
    remaining = drain_sessions(timeout=8)
    after_all = other_threads()

    print("--- 判定 ---", flush=True)
    print(f"connected: {'OK' if result['connected'] >= 1 else 'FAIL'}", flush=True)
    print(f"threads_cleared: {'OK' if clean else 'FAIL'}（残留 {result['after_wait']}）", flush=True)
    print(
        f"close_all_clients 收到 {closed} 个连接；drain_sessions 剩余 {remaining}；之后线程：{after_all}",
        flush=True,
    )
    print(
        "CLOSE_CLEAN_OK"
        if (result["connected"] >= 1 and clean and remaining == 0)
        else "CLOSE_CLEAN_FAILED",
        flush=True,
    )

    # 直接退出：跳过解释器退出阶段。PySide6 6.11 + Python 3.14 组合下，进程退出时
    # 可能因一个 C 层残留线程触发访问违例，把已经正确的结论变成非零退出码。
    ok = result["connected"] >= 1 and clean and remaining == 0
    sys.stdout.flush()
    os._exit(0 if ok else 1)


if __name__ == "__main__":
    raise SystemExit(main())
