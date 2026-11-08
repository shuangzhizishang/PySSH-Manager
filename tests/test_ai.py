"""AI 助手单元测试：命令抽取、风险识别、环境解析、配置、SSE 客户端。

流式客户端用本地 mock 服务器验证（不需要真实 API Key）。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from sshmanager.ai.actions import extract_actions
from sshmanager.ai.client import AiError, DeepSeekClient
from sshmanager.ai.commands import (
    SuggestedCommand,
    clean_code,
    extract_commands,
    insert_plan,
    risk_of,
    strip_code_blocks,
)
from sshmanager.ai.config import AiConfig
from sshmanager.ai.context import parse_environment_output, parse_os_release
from sshmanager.ai.prompts import DISTRO_PLAYBOOK, build_system_prompt


# ----------------------------------------------------------------------
# 命令抽取
# ----------------------------------------------------------------------

def test_extract_commands_from_fenced_blocks():
    reply = (
        "先看磁盘：\n\n```bash\ndf -h\n```\n\n"
        "再清理日志：\n\n```shell\n# 清空旧日志\njournalctl --vacuum-time=7d\n```\n"
    )

    commands = extract_commands(reply)

    assert [command.code for command in commands] == ["df -h", "# 清空旧日志\njournalctl --vacuum-time=7d"]
    assert commands[0].title == "df -h"
    assert commands[1].title == "清空旧日志"


def test_extract_commands_dedupes_and_skips_non_shell_languages():
    reply = """
```bash
systemctl status sshd
```

```bash
systemctl status sshd
```

```python
print("hello")
```

```json
{"a": 1}
```
"""

    commands = extract_commands(reply)

    assert len(commands) == 1
    assert commands[0].code == "systemctl status sshd"


def test_clean_code_strips_prompts_and_blank_lines():
    code = "\n\n$ ls -l\n# yum install -y vim\n\n"

    assert clean_code(code) == "ls -l\n# yum install -y vim"


def test_extract_commands_handles_untagged_block():
    commands = extract_commands("```\nfree -m\n```")

    assert len(commands) == 1
    assert commands[0].code == "free -m"


def test_strip_code_blocks_keeps_explanation():
    text = strip_code_blocks("先执行：\n```bash\ndf -h\n```\n再告诉我结果")

    assert "df -h" not in text
    assert "下方有可执行命令" in text
    assert "再告诉我结果" in text


# ----------------------------------------------------------------------
# 风险识别
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    ("code", "level"),
    [
        ("ls -l", "safe"),
        ("df -h && free -m", "safe"),
        ("yum install -y vim", "safe"),
        ("rm -rf /", "danger"),
        ("rm -rf /*", "danger"),
        ("mkfs.ext4 /dev/sdb1", "danger"),
        ("dd if=/dev/zero of=/dev/sda bs=1M", "danger"),
        ("reboot", "danger"),
        ("shutdown -h now", "danger"),
        ("chmod -R 777 /", "danger"),
        (":(){ :|:& };:", "danger"),
        ("rm -rf /var/log/old", "careful"),
        ("yum remove -y mysql-server", "careful"),
        ("systemctl stop firewalld", "careful"),
        ("kill -9 1234", "careful"),
        ("chown -R app:app /data", "careful"),
    ],
)
def test_risk_of(code, level):
    assert risk_of(code)[0] == level


def test_risk_reason_is_explained():
    level, reason = risk_of("rm -rf /")

    assert level == "danger"
    assert reason


def test_suggested_command_risk_label():
    command = SuggestedCommand(code="reboot", title="重启", risk="danger")

    assert command.risk_label == "危险操作"


# ----------------------------------------------------------------------
# 软件操作（pyssh-action）
# ----------------------------------------------------------------------

def test_extract_actions_from_blocks():
    reply = (
        "我可以帮你打开文件面板：\n\n"
        '```pyssh-action\n{"action": "open_sftp"}\n```\n\n'
        '```pyssh-action\n{"action": "split_vertical", "reason": "想并排看两台"}\n```\n'
    )

    actions = extract_actions(reply)

    assert [item.action.name for item in actions] == ["open_sftp", "split_vertical"]
    assert actions[1].reason == "想并排看两台"


def test_extract_actions_ignores_unknown_and_bad_json():
    reply = '```pyssh-action\n{"action": "rm_rf_everything"}\n```\n```pyssh-action\nnot json\n```'

    assert extract_actions(reply) == []


# ----------------------------------------------------------------------
# 环境解析
# ----------------------------------------------------------------------

SAMPLE_ENV_OUTPUT = """###OS
NAME="CentOS Linux"
VERSION="7 (Core)"
ID="centos"
ID_LIKE="rhel fedora"
PRETTY_NAME="CentOS Linux 7 (Core)"
###KERNEL
Linux 3.10.0-1160.el7.x86_64 x86_64
###HOST
shuangxiudao
###SHELL
/bin/bash
###USER
root
0
###PM
yum
###VIRT
kvm
"""


def test_parse_environment_output():
    environment = parse_environment_output(SAMPLE_ENV_OUTPUT)

    assert environment["distro_id"] == "centos"
    assert environment["pretty_name"] == "CentOS Linux 7 (Core)"
    assert environment["package_managers"] == "yum"
    assert environment["host"] == "shuangxiudao"
    assert environment["shell"] == "/bin/bash"
    assert environment["is_root"] == "是（root）"
    assert "kvm" in environment["note"]


def test_parse_os_release_quotes():
    parsed = parse_os_release('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="22.04"\n')

    assert parsed["NAME"] == "Ubuntu"
    assert parsed["ID"] == "ubuntu"
    assert parsed["VERSION_ID"] == "22.04"


# ----------------------------------------------------------------------
# 提示词
# ----------------------------------------------------------------------

def test_system_prompt_mentions_distro_playbook():
    prompt = build_system_prompt(
        AiConfig(system_extra="用中文"),
        {"distro_id": "centos", "pretty_name": "CentOS 7"},
    )

    assert "yum" in prompt
    assert "python" in prompt.lower()
    assert "pyssh-action" in prompt


def test_system_prompt_covers_all_playbooks():
    for key, hint in DISTRO_PLAYBOOK.items():
        prompt = build_system_prompt(AiConfig(), {"distro_id": key})
        assert hint.split("：")[0] in prompt or key in prompt.lower(), f"发行版 {key} 没进提示词"


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------

def test_config_roundtrip_and_normalized_url(tmp_path: Path):
    path = tmp_path / "ai.json"
    config = AiConfig(base_url="https://api.deepseek.com/", model="deepseek-reasoner", temperature=0.1, include_terminal_tail=30)
    config.save(path)

    loaded = AiConfig.load(path)

    assert loaded.model == "deepseek-reasoner"
    assert loaded.temperature == pytest.approx(0.1)
    assert loaded.include_terminal_tail == 30
    assert loaded.normalized_base_url() == "https://api.deepseek.com"


def test_config_load_missing_file_returns_defaults(tmp_path: Path):
    loaded = AiConfig.load(tmp_path / "nope.json")

    assert loaded.model == "deepseek-chat"
    assert loaded.base_url.startswith("https://")


def test_endpoint_forms():
    assert DeepSeekClient("k", AiConfig(base_url="https://api.deepseek.com")).endpoint() == (
        "https://api.deepseek.com/chat/completions"
    )
    assert DeepSeekClient("k", AiConfig(base_url="https://proxy.local")).endpoint() == (
        "https://proxy.local/v1/chat/completions"
    )
    assert DeepSeekClient("k", AiConfig(base_url="https://proxy.local/v1")).endpoint() == (
        "https://proxy.local/v1/chat/completions"
    )


# ----------------------------------------------------------------------
# mock SSE 服务器
# ----------------------------------------------------------------------

class _SseHandler(BaseHTTPRequestHandler):
    #: 由测试注入：返回 (status, chunks)
    responder = None
    last_request: dict = {}

    def log_message(self, *args) -> None:  # noqa: D102 - 静默
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        payload = json.loads(body.decode("utf-8")) if body else {}

        _SseHandler.last_request = {
            "path": self.path,
            "auth": self.headers.get("Authorization"),
            "body": payload,
        }

        status, chunks = _SseHandler.responder(payload)

        self.send_response(status)

        if status != 200:
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": {"message": "bad key"}}).encode())
            return

        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        for chunk in chunks:
            self.wfile.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()

        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


@pytest.fixture()
def sse_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield server

    server.shutdown()
    server.server_close()


def _chunk(text: str, reasoning: str | None = None) -> dict:
    delta: dict = {"content": text}

    if reasoning:
        delta["reasoning_content"] = reasoning

    return {"id": "x", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def test_client_streams_sse_deltas(sse_server):
    _SseHandler.responder = lambda payload: (200, [_chunk("你好"), _chunk("，"), _chunk("世界")])

    port = sse_server.server_address[1]
    client = DeepSeekClient("sk-test", AiConfig(base_url=f"http://127.0.0.1:{port}", timeout=10))

    events = list(client.chat([{"role": "user", "content": "hi"}], stream=True))
    text = "".join(event.text for event in events if event.kind == "text")

    assert text == "你好，世界"
    assert events[-1].kind == "done"
    assert _SseHandler.last_request["path"] == "/v1/chat/completions"
    assert _SseHandler.last_request["auth"] == "Bearer sk-test"
    assert _SseHandler.last_request["body"]["model"] == "deepseek-chat"
    assert _SseHandler.last_request["body"]["stream"] is True


def test_client_emits_reasoning_and_usage(sse_server):
    usage = {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}

    def responder(payload):
        return 200, [_chunk("", reasoning="先想一想"), _chunk("答案"), {"choices": [], "usage": usage}]

    _SseHandler.responder = responder
    port = sse_server.server_address[1]
    client = DeepSeekClient("sk-test", AiConfig(base_url=f"http://127.0.0.1:{port}", timeout=10))

    events = list(client.chat([{"role": "user", "content": "hi"}], stream=True))

    assert any(event.kind == "reasoning" and event.text == "先想一想" for event in events)
    assert any(event.kind == "usage" and event.usage == usage for event in events)


def test_client_maps_auth_error(sse_server):
    _SseHandler.responder = lambda payload: (401, [])
    port = sse_server.server_address[1]
    client = DeepSeekClient("sk-bad", AiConfig(base_url=f"http://127.0.0.1:{port}", timeout=10))

    with pytest.raises(AiError) as info:
        list(client.chat([{"role": "user", "content": "hi"}], stream=True))

    assert "API Key" in str(info.value)


def test_client_requires_api_key():
    client = DeepSeekClient("", AiConfig(base_url="http://127.0.0.1:1", timeout=1))

    with pytest.raises(AiError) as info:
        list(client.chat([{"role": "user", "content": "hi"}]))

    assert "API Key" in str(info.value)


def test_client_handles_error_inside_stream(sse_server):
    _SseHandler.responder = lambda payload: (200, [{"error": {"message": "余额不足"}}])
    port = sse_server.server_address[1]
    client = DeepSeekClient("sk-test", AiConfig(base_url=f"http://127.0.0.1:{port}", timeout=10))

    with pytest.raises(AiError) as info:
        list(client.chat([{"role": "user", "content": "hi"}], stream=True))

    assert "余额不足" in str(info.value)


# ----------------------------------------------------------------------
# 「插入终端」的多行拆分
# ----------------------------------------------------------------------

def test_insert_plan_single_line():
    assert insert_plan("systemctl restart sshd") == ("systemctl restart sshd", 0)


def test_insert_plan_multiline_only_first_line():
    code = "# 装社区版\nrpm -ivh mysql80.rpm\nsystemctl start mysqld"

    assert insert_plan(code) == ("# 装社区版", 2)


def test_insert_plan_ignores_blank_lines():
    assert insert_plan("\n  \nsystemctl status sshd\n\n") == ("systemctl status sshd", 0)


def test_insert_plan_empty():
    assert insert_plan("") == ("", 0)
    assert insert_plan(None) == ("", 0)  # type: ignore[arg-type]
