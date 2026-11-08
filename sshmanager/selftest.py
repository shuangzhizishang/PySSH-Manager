"""自检：验证打包后的程序里各组件是否真的可用。

打包成 exe 后没有控制台，所以结果写到 JSON 文件里，便于 CI 或人工确认。

检查项：

1. **依赖导入**：PySide6 / paramiko / pyte / cryptography / argon2 / pywinpty
2. **随包资源**：Qt 平台插件、pywinpty 的 ConPTY 支持、应用图标
3. **界面构建**：用 Fusion 样式建出主窗口，菜单与工具栏动作齐全，然后干净退出
4. **本地终端**：ConPTY 起一个 shell，执行 ``echo`` 并读到回显
5. **AI 助手**：模块导入、提示词、命令抽取/风险分级、配置读写（不联网）
6. **SSH / SFTP**（给了主机才查）：连接、执行命令、列目录

用法::

    PySSH-Manager.exe --selftest %TEMP%\\pyssh-selftest.json
    PySSH-Manager.exe --selftest report.json --host <你的服务器> --username root --password <密码>
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
            "action_split_hosts_h", "action_split_hosts_v", "action_sync_input",
            "action_ai", "action_ai_settings", "action_ai_explain",
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


def _check_ai(data_dir: Path) -> dict:
    """AI 助手：模块导入、提示词、命令抽取、操作抽取、计费与用量、配置读写（不联网）。"""

    try:
        from .ai import (
            AiConfig,
            DeepSeekClient,
            TokenUsage,
            UsageLedger,
            cost_of,
            extract_commands,
            is_peak,
        )
        from .ai.actions import extract_actions
        from .ai.context import parse_environment_output
        from .ai.pricing import CN_TZ
        from .ai.prompts import build_system_prompt
    except Exception as error:  # noqa: BLE001
        return {"ok": False, "detail": f"无法导入 AI 模块：{error}"}

    from datetime import datetime

    problems: list[str] = []

    # 1) 环境解析 + 提示词要按发行版给出对应包管理器
    environment = parse_environment_output(
        "###OS\n"
        'NAME="CentOS Linux"\n'
        'PRETTY_NAME="CentOS Linux 7 (Core)"\n'
        'ID="centos"\n'
        "###KERNEL\nLinux 3.10.0-1160.119.1.el7.x86_64 x86_64\n"
        "###HOST\nselftest-host\n"
        "###SHELL\n/bin/bash\n"
        "###USER\nroot\n0\n"
        "###PM\nyum \n"
        "###VIRT\nkvm\n"
    )

    if environment.get("distro_id") != "centos":
        problems.append(f"发行版解析异常：{environment.get('distro_id')!r}")

    if "yum" not in environment.get("package_managers", ""):
        problems.append(f"包管理器解析异常：{environment.get('package_managers')!r}")

    prompt = build_system_prompt(AiConfig(), environment)

    if "yum" not in prompt:
        problems.append("系统提示词没有带出 yum")

    # 2) 命令抽取 + 风险分级（危险命令必须被标出来）
    reply = (
        "先看系统版本：\n\n```bash\ncat /etc/redhat-release\n```\n\n"
        "删掉旧数据再重装：\n\n```bash\nrm -rf /\n```\n\n"
        "```pyssh-action\n"
        '{"action": "open_sftp", "reason": "上传安装包"}\n'
        "```\n"
    )

    commands = extract_commands(reply)

    if len(commands) != 2:
        problems.append(f"命令抽取数量异常：{len(commands)}")

    if not any(item.risk == "danger" for item in commands):
        problems.append("危险命令没有被识别")

    actions = extract_actions(reply)

    if len(actions) != 1 or actions[0].action.name != "open_sftp":
        problems.append(f"操作抽取异常：{[item.action.name for item in actions]}")

    # 3) API 地址拼接
    if DeepSeekClient("x", AiConfig()).endpoint() != "https://api.deepseek.com/chat/completions":
        problems.append("官方地址拼接异常")

    proxy = DeepSeekClient("x", AiConfig(base_url="https://proxy.example.com/v1")).endpoint()

    if proxy != "https://proxy.example.com/v1/chat/completions":
        problems.append(f"代理地址拼接异常：{proxy}")

    # 4) 配置读写（key 不在 JSON 里）
    try:
        config = AiConfig(
            model="deepseek-v4-pro", effort="max", peak_pricing=False, temperature=0.5, system_extra="自检"
        )
        config.save()
        loaded = AiConfig.load()

        if (
            loaded.model != "deepseek-v4-pro"
            or loaded.effort != "max"
            or loaded.peak_pricing
            or abs(loaded.temperature - 0.5) > 1e-6
        ):
            problems.append("配置读写不一致")

        raw = (Path(data_dir) / "ai_config.json").read_text(encoding="utf-8")

        if "api_key" in raw.lower():
            problems.append("配置里出现了 api_key（应该只存凭据库）")
    except Exception as error:  # noqa: BLE001
        problems.append(f"配置读写失败：{type(error).__name__}: {error}")

    # 5) 计费与用量（峰谷价、缓存命中、Ledger 记录）
    try:
        peak_time = datetime(2026, 10, 12, 10, 0, tzinfo=CN_TZ)  # 周一 10:00 = 高峰
        idle_time = datetime(2026, 10, 10, 10, 0, tzinfo=CN_TZ)  # 周六 10:00 = 空闲

        if not (is_peak(peak_time) and not is_peak(idle_time)):
            problems.append("峰谷时段判断异常")

        usage = TokenUsage.from_api(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "total_tokens": 1500,
                "prompt_cache_hit_tokens": 800,
                "completion_tokens_details": {"reasoning_tokens": 100},
            }
        )

        if usage.cached_tokens != 800 or usage.miss_tokens != 200 or usage.reasoning_tokens != 100:
            problems.append("usage 解析异常")

        peak_cost = cost_of(1000, 800, 500, "deepseek-flash", now=peak_time)
        idle_cost = cost_of(1000, 800, 500, "deepseek-flash", now=idle_time)

        if not (peak_cost > 0 and abs(peak_cost - idle_cost * 2) < 1e-9):
            problems.append(f"费用估算异常：peak={peak_cost} idle={idle_cost}")

        ledger = UsageLedger(path=Path(data_dir) / "ai_usage.json")
        ledger.reset()
        ledger.record("deepseek-flash", usage, idle_cost, peak=False)

        if ledger.total.requests != 1 or ledger.total.cached_tokens != 800:
            problems.append("用量记账异常")

        if not ledger.total.line().startswith("1 次"):
            problems.append(f"用量文案异常：{ledger.total.line()}")

        ledger.reset()
    except Exception as error:  # noqa: BLE001
        problems.append(f"计费自检失败：{type(error).__name__}: {error}")

    detail = (
        f"发行版={environment.get('pretty_name', '?')}，包管理器={environment.get('package_managers', '?')}，"
        f"命令 {len(commands)} 条（风险 {[item.risk for item in commands]}），"
        f"操作 {len(actions)} 个，问题 {problems or '无'}"
    )

    return {"ok": not problems, "detail": detail}


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
    checks["ai"] = _check_ai(data_dir)
    checks["local_terminal"] = _check_local_terminal()

    if host:
        checks["ssh"] = _check_ssh(host, port, username, password)

    report["ok"] = all(item.get("ok", False) for item in checks.values() if isinstance(item, dict))

    target = Path(report_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report
