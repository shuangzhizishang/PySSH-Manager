"""SFTP 辅助函数与端口转发规则的单元测试（不需要网络）。"""

from __future__ import annotations

import socket

import pytest

from sshmanager.forwarding import ForwardRule, load_rules, save_rules
from sshmanager.hosts import Host, HostStore
from sshmanager.sftpcore import (
    RemoteEntry,
    format_size,
    normalize_remote_path,
    parent_remote_path,
)


def test_normalize_remote_path():
    assert normalize_remote_path("") == "/"
    assert normalize_remote_path("/") == "/"
    assert normalize_remote_path("root") == "/root"
    assert normalize_remote_path("/home/root/../root") == "/home/root"
    assert normalize_remote_path("//home//root/") == "/home/root"


def test_parent_remote_path():
    assert parent_remote_path("/home/root") == "/home"
    assert parent_remote_path("/home") == "/"
    assert parent_remote_path("/") == "/"


def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(512) == "512 B"
    assert format_size(1536) == "1.5 KB"
    assert format_size(5 * 1024**3) == "5.0 GB"


def test_remote_entry_texts():
    directory = RemoteEntry(name="etc", path="/etc", is_dir=True)
    assert directory.size_text == "<目录>"
    assert directory.kind_text == "目录"

    link = RemoteEntry(name="ln", path="/ln", is_link=True, modified=0)
    assert link.kind_text == "链接"
    assert link.modified_text == ""


def test_forward_rule_roundtrip():
    rule = ForwardRule(bind="0.0.0.0", local_port="18080", remote_host="10.0.0.5", remote_port="3306", label="db")

    restored = ForwardRule.from_dict(rule.to_dict())

    assert restored.local_port == 18080
    assert restored.remote_port == 3306
    assert restored.remote_host == "10.0.0.5"
    assert restored.summary == "0.0.0.0:18080 → 10.0.0.5:3306"


def test_forward_rules_persist_with_host(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path))

    store = HostStore()
    host = store.add(Host(hostname="10.0.0.9"))

    save_rules(host, [ForwardRule(local_port=18080, remote_port=22, label="测试")])
    store.save()

    reloaded = HostStore().get(host.id)
    rules = load_rules(reloaded)

    assert len(rules) == 1
    assert rules[0].local_port == 18080
    assert rules[0].label == "测试"


def test_load_rules_ignores_garbage():
    host = Host()
    host.forwards = ["不是字典", {"local_port": 1, "remote_port": 1}, {"未知字段": 1}]

    rules = load_rules(host)

    # 字符串被跳过；字典（含只有未知字段的）都能解析出规则
    assert len(rules) == 2
    assert rules[0].local_port == 1
    assert rules[1].local_port == 8080  # 未知字段用默认值


def test_local_port_can_be_bound_and_released():
    """本地转发依赖能正常监听/释放端口，先验证这一步。"""

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    port = listener.getsockname()[1]

    with socket.create_connection(("127.0.0.1", port), timeout=1):
        pass

    listener.close()
    listener.close()  # 幂等
