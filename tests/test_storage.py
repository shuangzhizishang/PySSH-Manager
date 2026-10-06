"""主机存储与凭据加密测试。"""

from __future__ import annotations

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore


def test_host_roundtrip(data_dir):
    store = HostStore()
    host = Host(label="测试机", hostname="10.0.0.9", port=2222, username="dev")
    store.add(host)

    reloaded = HostStore()
    found = reloaded.get(host.id)

    assert found is not None
    assert found.display_name == "测试机"
    assert found.target == "dev@10.0.0.9:2222"


def test_host_update_and_remove(data_dir):
    store = HostStore()
    host = store.add(Host(hostname="10.0.0.1"))

    host.label = "改名"
    store.update(host)

    assert HostStore().get(host.id).label == "改名"

    store.remove(host.id)

    assert HostStore().get(host.id) is None


def test_from_dict_ignores_unknown_and_bad_types():
    host = Host.from_dict({"hostname": "h", "port": "not-a-port", "font_size": None, "未知字段": 1})

    assert host.port == 22
    assert host.hostname == "h"


def test_credential_roundtrip(data_dir):
    store = CredentialStore()
    store.set("host-1", "s3cret-中文-!@#")

    assert CredentialStore().get("host-1") == "s3cret-中文-!@#"


def test_credential_rename_and_delete(data_dir):
    store = CredentialStore()
    store.set("old", "value")
    store.rename("old", "new")

    reloaded = CredentialStore()

    assert reloaded.get("old") is None
    assert reloaded.get("new") == "value"

    reloaded.delete("new")

    assert CredentialStore().get("new") is None


def test_credentials_not_plaintext_on_disk(data_dir):
    store = CredentialStore()
    store.set("host-1", "plaintext-marker")

    raw = (data_dir / "credentials.json").read_text(encoding="utf-8")

    assert "plaintext-marker" not in raw
