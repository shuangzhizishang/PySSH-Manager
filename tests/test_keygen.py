"""密钥生成与 ProxyJump / 标签解析的单元测试（不需要网络）。"""

from __future__ import annotations

import base64
import struct

import pytest

from sshmanager.hostio import export_openssh_config, parse_openssh_config, resolve_proxy_jumps
from sshmanager.hosts import Host, HostStore
from sshmanager.keygen import KeyError_, KeyStore, load_private_key

PROXY_CONFIG = """
Host jump
    HostName 10.0.0.1
    User root

Host prod-web
    HostName 10.0.0.9
    User deploy
    ProxyJump jump

Host prod-db
    HostName 10.0.0.10
    ProxyJump root@jump:22
"""


@pytest.fixture()
def key_store(data_dir) -> KeyStore:
    return KeyStore()


def test_generate_ed25519_and_public_text(key_store: KeyStore):
    key = key_store.generate("unit-ed25519")

    assert key.path.exists()
    assert key.public_path.exists()
    assert not key.encrypted

    private_key = load_private_key(key.path, None)
    assert private_key.get_name() == "ssh-ed25519"

    text = key_store.public_text(key)
    assert text.startswith("ssh-ed25519 ")

    blob = base64.b64decode(text.split()[1])
    length = struct.unpack(">I", blob[:4])[0]
    assert blob[4 : 4 + length] == b"ssh-ed25519"


def test_generate_encrypted_ed25519(key_store: KeyStore):
    key = key_store.generate("unit-locked", passphrase="pw-1", comment="hello")

    assert key.encrypted is True

    with pytest.raises(Exception):
        load_private_key(key.path, None)

    loaded = load_private_key(key.path, "pw-1")
    assert loaded.get_name() == "ssh-ed25519"


def test_generate_rsa(key_store: KeyStore):
    key = key_store.generate("unit-rsa", key_type="rsa", bits=2048)

    assert key.key_type == "rsa"
    assert key.bits == 2048

    loaded = load_private_key(key.path, None)
    assert loaded.get_name() == "ssh-rsa"
    assert loaded.get_bits() == 2048


def test_unsupported_key_type(key_store: KeyStore):
    with pytest.raises(KeyError_):
        key_store.generate("bad", key_type="dsa-420")


def test_index_persists_and_removes(key_store: KeyStore):
    key = key_store.generate("unit-persist")

    reloaded = KeyStore()
    assert reloaded.get(key.id) is not None

    reloaded.remove(key.id)
    assert KeyStore().get(key.id) is None
    assert not key.path.exists()


def test_duplicate_names_get_unique_files(key_store: KeyStore):
    first = key_store.generate("same-name")
    second = key_store.generate("same-name")

    assert first.filename != second.filename
    assert first.path.exists() and second.path.exists()


def test_parse_proxy_jump_directives():
    result = parse_openssh_config(PROXY_CONFIG)

    assert result.count == 3
    assert result.proxy_jumps["prod-web"] == "jump"
    assert result.proxy_jumps["prod-db"] == "jump"  # user@host:port 形式也能解析

    resolved = resolve_proxy_jumps(result, result.hosts)
    assert resolved == 2

    by_label = {host.label: host for host in result.hosts}
    assert by_label["prod-web"].jump_host_id == by_label["jump"].id
    assert by_label["prod-db"].jump_host_id == by_label["jump"].id
    # 跳板机自己不该指向自己
    assert by_label["jump"].jump_host_id == ""


def test_tags_from_alias_and_group_property():
    result = parse_openssh_config(PROXY_CONFIG)
    by_label = {host.label: host for host in result.hosts}

    assert by_label["prod-web"].tags == ["prod"]
    assert by_label["prod-web"].group == "prod"
    assert by_label["jump"].tags == ["jump"]

    plain = Host(label="plain")
    assert plain.group == "未分组"


def test_export_keeps_tags_as_comment():
    hosts = [Host(label="a", hostname="10.0.0.1", tags=["prod", "web"])]
    text = export_openssh_config(hosts)

    assert "# Tags: prod, web" in text


def test_proxy_jump_survives_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path))

    result = parse_openssh_config(PROXY_CONFIG)
    store = HostStore()

    for host in result.hosts:
        store.add(host)

    resolve_proxy_jumps(result, store.hosts)
    store.save()

    reloaded = HostStore()
    by_label = {host.label: host for host in reloaded.hosts}
    assert by_label["prod-web"].jump_host_id == by_label["jump"].id
