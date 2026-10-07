"""自检：验证打包后的程序里各组件是否真的可用。

打包成 exe 后没有控制台，所以结果写到 JSON 文件里，便于 CI 或人工确认。

检查项：

1. **依赖导入**：PySide6 / paramiko / pyte / cryptography / argon2 / pywinpty
2. **随包资源**：Qt 平台插件、pywinpty 的 ConPTY 支持、应用图标
3. **界面构建**：用 Fusion 样式建出主窗口，菜单与工具栏动作齐全，然后干净退出
4. **本地终端**：ConPTY 起一个 shell，执行 ``echo`` 并读到回显
5. **SSH / SFTP**（给了主机才查）：连接、执行命令、列目录

用法::

    PySSH-Manager.exe --selftest %TEMP%\\pyssh-selftest.json
    PySSH-Manager.exe --selftest report.json --host 192.168.206.130 --username root --password 123456
"""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import Qt


def _check_imports() -> dict:
    result = {}

    for module in ("PySide6", "paramiko", "pyte", "cryptography", "argon2", "winpty"):
        entry = {"ok": False, "detail": ""}

        try:
            imported = __import__(module)
            entry["ok"] = True
            entry["detail"] = getattr(imported, "__version__", "已导入")
        except Exception as error:  # noqa: BLE001
            entry["detail"] = f"{type(error).__name__}: {error}"

        result[module] = entry

    return result


def _check_resources() -> dict:
    result = {}

    # Qt 平台插件（打包后必须能在 _internal 里找到）
    try:
        from PySide6.QtCore import QLibraryInfo

        plugins = Path(QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath))
        platform_dir = plugins / "platforms"
        result["qt_platform_plugin"] = {
            "ok": (platform_dir / "qwindows.dll").exists() or (platform_dir / "qoffscreen.dll").exists(),
            "detail": str(platform_dir),
        }
    except Exception as error:  # noqa: BLE001
        result["qt_platform_plugin"] = {"ok": False, "detail": str(error)}

    # ConPTY / winpty 支持（本地终端要用）
    try:
        import winpty

        module_dir = Path(winpty.__file__).parent
        candidates = list(module_dir.rglob("*.dll")) + list(module_dir.rglob("*.exe"))
        result["conpty"] = {
            "ok": bool(candidates),
            "detail": ", ".join(item.name for item in candidates[:4]) or str(module_dir),
        }
    except Exception as error:  # noqa: BLE001
        result["conpty"] = {"ok": False, "detail": str(error)}

    # cryptography 的 OpenSSL 后端
    try:
        from cryptography.hazmat.backends.openssl.backend import backend

        result["openssl"] = {"ok": backend is not None, "detail": "cryptography OpenSSL 后端可用"}
    except Exception as error:  # noqa: BLE001
        result["openssl"] = {"ok": False, "detail": str(error)}

    return result


def _check_gui(data_dir: Path) -> dict:
    """建出主窗口，确认界面与菜单完整，然后收尾退出。"""

    try:
        from PySide6.QtWidgets import QApplication
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "detail": f"无法导入 QtWidgets：{error}"}

    created_app = QApplication.instance() is None
    app = QApplication(sys.argv[:1]) if created_app else QApplication.instance()
    app.setStyle("Fusion")

    try:
        from .credentials import CredentialStore
        from .hosts import HostStore
        from .ui import AppContext, MainWindow

        context = AppContext(hosts=HostStore(), credentials=CredentialStore())
        window = MainWindow(context)

        actions = [
            "action_connect", "action_new_session", "action_sftp", "action_command_library",
            "action_copy_output", "action_save_output", "action_favorite_selection",
            "action_select_to_copy", "action_local", "action_split_h",
        ]
        missing = [name for name in actions if getattr(window, name, None) is None]

        menus = [action.text() for action in window.menuBar().actions()]
        detail = f"菜单 {menus}，缺失动作 {missing or '无'}"

        window.shutdown()
        ok = not missing and len(menus) >= 3

        return {"ok": ok, "detail": detail}
    except Exception as error:  # noqa: BLE001
        import traceback

        return {"ok": False, "detail": f"{type(error).__name__}: {error}\n{traceback.format_exc()}"}


def _check_local_terminal(timeout: float = 20.0) -> dict:
    """用 ConPTY 起本地 shell，跑一条 echo 看回显。"""

    try:
        from .localsession import LocalShellSession
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "detail": f"无法导入本地会话：{error}"}

    marker = "PYSSH-SELFTEST-OK"
    buffer: list[str] = []

    session = LocalShellSession()
    # 用直连：自检里没有跑事件循环，队列投递的信号不会送达
    session.data_received.connect(
        lambda data: buffer.append(data.decode("utf-8", "replace")),
        Qt.ConnectionType.DirectConnection,
    )

    try:
        session.start()
        time.sleep(1.5)  # 等 ConPTY 里的 shell 起来
        session.send(f"echo {marker}\r")

        deadline = time.time() + timeout

        while time.time() < deadline:
            if marker in "".join(buffer)[-4000:]:
                break

            time.sleep(0.2)

        output = "".join(buffer)
        return {"ok": marker in output, "detail": f"读到 {len(output)} 字符"}
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(error).__name__}: {error}"}
    finally:
        try:
            session.request_stop()

            if not session.wait(3000):
                session.terminate()
                session.wait(500)
        except Exception:
            pass


def _check_ssh(host: str, port: int, username: str, password: str, timeout: float = 25.0) -> dict:
    """真实连一次，跑命令 + 列 SFTP 目录。"""

    try:
        from .sftpcore import SftpClient
        from .sshclient import SSHClient
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "detail": f"无法导入模块：{error}"}

    client = SSHClient()

    try:
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            verify_host_key=False,
            timeout=timeout,
        )

        _, stdout, _ = client.exec_command("echo PYSSH-SELFTEST-SSH", timeout=timeout)
        output = stdout.read().decode("utf-8", "replace").strip()

        sftp = SftpClient(client)
        sftp.open()
        entries = sftp.listdir(sftp.home())
        sftp.close()

        return {
            "ok": "PYSSH-SELFTEST-SSH" in output and entries is not None,
            "detail": f"命令回显={output!r}，家目录 {len(entries)} 项",
        }
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(error).__name__}: {error}"}
    finally:
        try:
            client.close()
        except Exception:
            pass


def run(
    report_path: Path | str,
    host: str = "",
    port: int = 22,
    username: str = "",
    password: str = "",
) -> dict:
    """执行全部自检并写入报告，返回报告字典。"""

    report: dict = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "checks": {},
    }

    report["checks"]["imports"] = _check_imports()

    checks = report["checks"]

    imports_ok = all(item["ok"] for item in checks["imports"].values())
    report["checks"]["imports"]["ok"] = imports_ok

    checks["resources"] = _check_resources()
    checks["resources"]["ok"] = all(
        item["ok"] for key, item in checks["resources"].items() if isinstance(item, dict) and "ok" in item
    )

    data_dir = Path(os.environ.get("PYSSH_DATA_DIR") or tempfile.mkdtemp(prefix="pyssh-selftest-"))
    os.environ.setdefault("PYSSH_DATA_DIR", str(data_dir))

    checks["gui"] = _check_gui(data_dir)
    checks["local_terminal"] = _check_local_terminal()

    if host:
        checks["ssh"] = _check_ssh(host, port, username, password)

    report["ok"] = all(item.get("ok", False) for item in checks.values() if isinstance(item, dict))

    target = Path(report_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report
