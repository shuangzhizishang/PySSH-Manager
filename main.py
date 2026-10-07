"""PySSH Manager 启动入口。

用法::

    python main.py                      # 打开图形界面并按默认主机自动连接
    python main.py --host 10.0.0.5 --username root --password secret
    python main.py --no-autoconnect     # 只打开主机列表，不自动连接

主机列表保存在 %APPDATA%\\PySSH-Manager\\hosts.json，
密码经 Windows DPAPI 加密后保存在同目录的 credentials.json。
"""

from __future__ import annotations

import argparse
import os
import sys

# ==============================
# 默认连接目标（等价于旧版硬编码，只用于首次导入）
# ==============================

DEFAULT_HOST = "192.168.206.130"
DEFAULT_PORT = 22
DEFAULT_USERNAME = "root"

#: 仅当凭据库中还没有该主机密码时使用一次，随后即加密入库；
#: 想彻底移除可在 PyCharm 里删掉这一行并重新运行。
LEGACY_PASSWORD = "123456"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PySSH Manager - SSH 连接管理器")

    parser.add_argument("--host", default=DEFAULT_HOST, help="要连接的主机地址")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="SSH 端口，默认 22")
    parser.add_argument("--username", default=DEFAULT_USERNAME, help="登录用户名")
    parser.add_argument("--password", default="", help="登录密码（仅用于首次保存，会加密存储）")
    parser.add_argument(
        "--no-autoconnect",
        action="store_true",
        help="启动后不自动连接，只显示主机列表",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="启动时额外打开一个本地终端标签",
    )
    parser.add_argument(
        "--selftest",
        metavar="报告路径",
        default="",
        help="自检模式：检查依赖/界面/本地终端/SSH 后把结果写入 JSON 再退出（打包后验证用）",
    )

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 自检模式：不开主界面，检查完写报告就退出（打包后的 exe 没有控制台）
    if args.selftest:
        from sshmanager.selftest import run as run_selftest

        report = run_selftest(
            args.selftest,
            host="" if args.no_autoconnect else args.host,
            port=args.port,
            username=args.username,
            password=args.password,
        )
        return 0 if report.get("ok") else 1

    from PySide6.QtWidgets import QApplication

    from sshmanager import APP_NAME
    from sshmanager.credentials import CredentialStore
    from sshmanager.hosts import Host, HostStore
    from sshmanager.ui import AppContext, MainWindow

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName(APP_NAME)

    # 必须用 Fusion 样式：Windows 11 原生样式会忽略自定义 QPainter 的画笔颜色，
    # 导致终端文字被画成系统默认文字色（深色背景下几乎不可见）。
    app.setStyle("Fusion")

    if os.name == "nt":
        # 让任务栏正确显示应用图标分组
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PySSH.Manager")
        except Exception:
            pass

    context = AppContext(hosts=HostStore(), credentials=CredentialStore())
    window = MainWindow(context)

    target_id = ""

    if args.host and not args.no_autoconnect:
        host = next(
            (
                item
                for item in context.hosts.hosts
                if item.hostname == args.host and item.port == args.port
            ),
            None,
        )

        if host is None:
            host = Host(label=args.host, hostname=args.host, port=args.port, username=args.username)
            context.hosts.add(host)

        if args.password:
            context.credentials.set(host.id, args.password)
        elif not context.credentials.get(host.id) and LEGACY_PASSWORD:
            # 首次运行：把旧版写死在代码里的密码加密存起来
            context.credentials.set(host.id, LEGACY_PASSWORD)

        target_id = host.id
        window.refresh_hosts()
        window._select_host(host.id)

    window.show()

    if args.local:
        window.open_local_terminal()

    if target_id:
        host = context.hosts.get(target_id)

        if host is not None:
            window.connect_host(host)

    exit_code = app.exec()

    # 兜底：即使有会话正在连接/认证中途被关窗，也要把连接与传输层线程收干净，
    # 否则解释器退出阶段可能崩溃（access violation）。
    from sshmanager.sshclient import close_all_clients
    from sshmanager.ui.session_tab import drain_sessions

    close_all_clients()
    drain_sessions()

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
