"""主机导入 / 导出测试。"""

from __future__ import annotations

import json

from sshmanager.credentials import CredentialStore
from sshmanager.hostio import (
    export_json,
    export_openssh_config,
    import_json,
    import_json_credentials,
    parse_openssh_config,
)
from sshmanager.hosts import Host, HostStore

SSH_CONFIG = """
# 注释行
Host web
    HostName 10.0.0.10
    User deploy
    Port 2222
    IdentityFile ~/.ssh/id_ed25519

Host db
    HostName 10.0.0.20
    User root
    StrictHostKeyChecking no

Host *
    StrictHostKeyChecking no
    ServerAliveInterval 30
"""


def test_parse_openssh_config_basic():
    result = parse_openssh_config(SSH_CONFIG)

    assert result.count == 2
    names = [host.label for host in result.hosts]
    assert names == ["web", "db"]

    web = result.hosts[0]
    assert web.hostname == "10.0.0.10"
    assert web.port == 2222
    assert web.username == "deploy"
    assert web.auth_type == "key"
    assert "id_ed25519" in web.key_path
    assert web.verify_host_key is True

    db = result.hosts[1]
    assert db.port == 22
    assert db.username == "root"
    assert db.verify_host_key is False  # StrictHostKeyChecking no
    assert db.auth_type == "password"


def test_parse_openssh_config_skips_wildcards():
    result = parse_openssh_config(SSH_CONFIG)

    assert "*" in result.skipped


def test_parse_openssh_config_tolerates_garbage():
    result = parse_openssh_config(
        """
        Host
        HostName
        Host onlyname
        Port not-a-number
        """
    )

    # "Host onlyname" 会用别名当主机名，"Port not-a-number" 被忽略并给出提示
    assert result.count == 1
    assert result.hosts[0].hostname == "onlyname"
    assert any("端口" in warning for warning in result.warnings)


def test_parse_openssh_config_equals_syntax():
    result = parse_openssh_config("Host eq\n  HostName=10.1.1.1\n  User=someone\n  Port=2200\n")

    assert result.count == 1
    assert result.hosts[0].hostname == "10.1.1.1"
    assert result.hosts[0].username == "someone"
    assert result.hosts[0].port == 2200


def test_export_openssh_config_roundtrip():
    hosts = [
        Host(label="web", hostname="10.0.0.10", port=2222, username="deploy", key_path="/home/u/.ssh/id_rsa"),
        Host(label="db", hostname="10.0.0.20", username="root", verify_host_key=False),
    ]

    text = export_openssh_config(hosts)
    parsed = parse_openssh_config(text)

    assert parsed.count == 2
    by_name = {host.label: host for host in parsed.hosts}

    assert by_name["web"].hostname == "10.0.0.10"
    assert by_name["web"].port == 2222
    assert by_name["web"].username == "deploy"
    assert by_name["web"].auth_type == "key"
    assert by_name["db"].verify_host_key is False


def test_json_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path))

    store = HostStore()
    host = store.add(Host(label="测试", hostname="10.0.0.30", port=22, username="dev"))
    credentials = CredentialStore()
    credentials.set(host.id, "s3cret")

    text = export_json([host], credentials=credentials, include_credentials=True)
    payload = json.loads(text)

    assert payload["version"] == 1
    assert len(payload["hosts"]) == 1

    result = import_json(text)
    assert result.count == 1
    assert result.hosts[0].hostname == "10.0.0.30"

    secrets = import_json_credentials(text)
    assert secrets[host.id] == "s3cret"


def test_json_export_without_credentials_has_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path))

    credentials = CredentialStore()
    host = Host(label="x", hostname="10.0.0.31")
    credentials.set(host.id, "topsecret")

    text = export_json([host], credentials=credentials, include_credentials=False)

    assert "topsecret" not in text
    assert "credentials" not in json.loads(text)


def test_import_json_rejects_bad_payload():
    assert import_json("not json").warnings
    assert import_json('{"hosts": "nope"}').warnings
    assert import_json("[]").count == 0
