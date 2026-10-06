"""录制、配置同步、PPK 导入的单元测试。"""

from __future__ import annotations

import base64
import struct

import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sshmanager.credentials import CredentialStore
from sshmanager.hosts import Host, HostStore
from sshmanager.keygen import load_private_key
from sshmanager.ppk import PPKError, convert_ppk, parse_ppk, public_key_line, write_ppk as write_ppk_file
from sshmanager.recording import CastRecorder, load_recording, recording_text, suggest_filename
from sshmanager.sync import export_package, import_package, read_package, sync_folder_path


# ----------------------------------------------------------------------
# 录制
# ----------------------------------------------------------------------

def test_recorder_writes_asciicast_v2(tmp_path):
    path = tmp_path / "session.cast"
    recorder = CastRecorder(path, width=100, height=30, title="demo")
    recorder.start()
    recorder.write_output(b"hello ")
    recorder.write_input(b"ls\r")
    recorder.write_output("中文输出\n")
    recorder.write_resize(120, 40)
    result = recorder.stop()

    assert result == path
    lines = path.read_text(encoding="utf-8").strip().splitlines()

    import json

    header = json.loads(lines[0])
    assert header["version"] == 2
    assert header["width"] == 100
    assert header["title"] == "demo"

    events = [json.loads(line) for line in lines[1:]]
    assert [event[1] for event in events] == ["o", "i", "o", "r"]
    assert events[0][2] == "hello "
    assert events[2][2] == "中文输出\n"
    assert events[3][2] == "120x40"


def test_recording_roundtrip(tmp_path):
    path = tmp_path / "round.cast"
    recorder = CastRecorder(path, width=80, height=24)
    recorder.start()
    recorder.write_output("line1\n")
    recorder.write_output("line2\n")
    recorder.stop()

    recording = load_recording(path)

    assert recording.width == 80
    assert recording.height == 24
    assert len(recording.events) == 2
    assert recording.duration >= 0
    assert "line1" in recording_text(path)
    assert recording.output_bytes == len("line1\nline2\n")


def test_recorder_ignores_inactive_writes(tmp_path):
    recorder = CastRecorder(tmp_path / "never.cast")

    recorder.write_output("should be ignored")

    assert recorder.stop() is None
    assert not (tmp_path / "never.cast").exists()


def test_suggest_filename_is_safe():
    name = suggest_filename("root@10.0.0.1:22")

    assert name.endswith(".cast")
    assert "/" not in name and ":" not in name and "@" not in name


# ----------------------------------------------------------------------
# 配置同步
# ----------------------------------------------------------------------

def test_export_then_import_adds_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path / "data"))

    shared = tmp_path / "shared"
    credentials = CredentialStore()
    hosts = [Host(label="a", hostname="10.0.0.1"), Host(label="b", hostname="10.0.0.2")]
    credentials.set(hosts[0].id, "secret")

    path = export_package(shared, hosts, credentials=credentials, include_credentials=True)

    assert path == sync_folder_path(shared)
    assert path.exists()

    local: list[Host] = []
    local_credentials = CredentialStore()
    existing_before = local_credentials.get(hosts[0].id)
    result = import_package(shared, local, local_credentials)

    assert result.added == 2
    assert {host.hostname for host in local} == {"10.0.0.1", "10.0.0.2"}

    # 凭据落库到"本地实际保留的主机 id"上（合并时本地 id 会被保留）
    local_id = result.id_map[hosts[0].id]
    assert local_credentials.get(local_id) == "secret"

    # 本次是否算"新导入"取决于导入前本地有没有：同一个进程里导出的 store 可能已经写盘
    assert result.credentials == (0 if existing_before else 1)


def test_import_without_credentials_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path / "data"))

    shared = tmp_path / "shared"
    export_package(shared, [Host(hostname="10.0.0.9")])

    hosts, secrets, warnings = read_package(shared)

    assert len(hosts) == 1
    assert secrets == {}
    assert warnings == []


def test_import_missing_package_reports_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path / "data"))

    result = import_package(tmp_path / "nope", [], None)

    assert result.added == 0
    assert result.warnings


def test_merge_prefers_recent_and_keeps_local_id(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path / "data"))

    shared = tmp_path / "shared"
    remote = Host(label="same", hostname="10.0.0.5", username="root")
    remote.last_connected_at = 999999.0
    remote.tags = ["prod"]
    export_package(shared, [remote])

    local_host = Host(label="same", hostname="10.0.0.5", username="root")
    local_host.last_connected_at = 1.0

    result = import_package(shared, [local_host], None)

    assert result.updated == 1
    assert local_host.id != remote.id  # 本地 id 被保留
    assert local_host.tags == ["prod"]  # 远端内容被合并进来
    assert local_host.last_connected_at == 999999.0


def test_merge_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("PYSSH_DATA_DIR", str(tmp_path / "data"))

    shared = tmp_path / "shared"
    export_package(shared, [Host(hostname="10.0.0.7")])

    local: list[Host] = []
    first = import_package(shared, local, None)
    second = import_package(shared, local, None)

    assert first.added == 1
    assert second.unchanged == 1
    assert len(local) == 1


# ----------------------------------------------------------------------
# PPK 导入
# ----------------------------------------------------------------------

def _mpint(value: int) -> bytes:
    if value == 0:
        return struct.pack(">I", 0)

    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")

    if raw[0] & 0x80:
        raw = b"\x00" + raw

    return struct.pack(">I", len(raw)) + raw


def _write_ppk(path, key_type: str, public_blob: bytes, private_blob: bytes, comment: str = "test@local") -> None:
    """用生产代码里的写出逻辑生成测试文件（避免测试自己拼格式拼错）。"""

    write_ppk_file(path, key_type, public_blob, private_blob, comment)


def _rsa_ppk(path, bits: int = 2048):
    private = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    numbers = private.private_numbers()

    # OpenSSH 公钥段必须是 "算法名 + e + n" 三段，漏掉算法名会导致解析错位
    public_blob = _string_param("ssh-rsa") + _mpint(65537) + _mpint(numbers.public_numbers.n)
    private_blob = _mpint(numbers.d) + _mpint(numbers.p) + _mpint(numbers.q)

    _write_ppk(path, "ssh-rsa", public_blob, private_blob)

    return private, numbers


def _string_param(text: str) -> bytes:
    raw = text.encode("ascii")

    return struct.pack(">I", len(raw)) + raw


def _ed25519_ppk(path):
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    public_blob = _string_param("ssh-ed25519") + struct.pack(">I", len(public_raw)) + public_raw
    private_blob = seed + public_raw

    _write_ppk(path, "ssh-ed25519", public_blob, private_blob)

    return private, public_raw


def test_parse_ppk_rsa(tmp_path):
    path = tmp_path / "id_rsa.ppk"
    _rsa_ppk(path)

    meta = parse_ppk(path)

    assert meta.key_type == "ssh-rsa"
    assert meta.encrypted is False
    assert meta.version == 3
    assert meta.comment == "test@local"
    assert public_key_line(meta).startswith("ssh-rsa ")


def test_convert_ppk_rsa_roundtrip(tmp_path):
    path = tmp_path / "id_rsa.ppk"
    _rsa_ppk(path)

    converted, comment = convert_ppk(path)

    assert comment == "test@local"
    assert converted.get_name() == "ssh-rsa"

    # 序列化成 PEM 应能重新解析出同一把密钥
    # （paramiko 的 RSAKey.write_private_key 写的是传统 PEM，不是 OpenSSH 格式）
    import io

    buffer = io.StringIO()
    converted.write_private_key(buffer)
    assert "PRIVATE KEY-----" in buffer.getvalue()

    reloaded = paramiko.RSAKey.from_private_key(io.StringIO(buffer.getvalue()))
    assert reloaded.get_fingerprint() == converted.get_fingerprint()


def test_convert_ppk_ed25519_matches(tmp_path):
    path = tmp_path / "id_ed25519.ppk"
    private, public_raw = _ed25519_ppk(path)

    converted, _comment = convert_ppk(path)

    assert converted.get_name() == "ssh-ed25519"

    # paramiko 的 asbytes() 给的是带长度前缀的线格式，所以比对结尾的 32 字节裸公钥
    assert converted.asbytes().endswith(public_raw)
    assert converted.get_fingerprint() == paramiko.Ed25519Key(
        msg=_ed25519_public_message(public_raw)
    ).get_fingerprint()


def _ed25519_public_message(public_raw: bytes) -> paramiko.Message:
    message = paramiko.Message()
    message.add_string("ssh-ed25519")
    message.add_string(public_raw)

    return message


def test_convert_ppk_rsa_produces_working_openssh_key(tmp_path):
    """转换后的私钥写成 OpenSSH 文件后，用项目自己的加载器能读出来。"""

    path = tmp_path / "id_rsa.ppk"
    _rsa_ppk(path)

    converted, _ = convert_ppk(path)
    target = tmp_path / "converted"
    converted.write_private_key_file(str(target), password="pass-1")

    loaded = load_private_key(target, "pass-1")

    assert loaded.get_name() == "ssh-rsa"
    assert loaded.get_fingerprint() == converted.get_fingerprint()


def test_reject_unsupported_key_type(tmp_path):
    path = tmp_path / "bad.ppk"
    _write_ppk(path, "ssh-dss", b"\x00" * 4, b"\x00" * 4)

    with pytest.raises(PPKError):
        parse_ppk(path)


def test_encrypted_ppk_without_passphrase_is_rejected(tmp_path):
    path = tmp_path / "enc.ppk"
    lines = path.write_text(
        "\n".join(
            [
                "PuTTY-User-Key-File-2: ssh-rsa",
                "Encryption: aes256-cbc",
                "Comment: enc",
                "Public-Lines: 1",
                base64.b64encode(b"\x00" * 8).decode(),
                "Private-Lines: 1",
                base64.b64encode(b"\x00" * 16).decode(),
                "Private-MAC: 00",
            ]
        ),
        encoding="utf-8",
    )

    meta = parse_ppk(path)
    assert meta.encrypted is True

    with pytest.raises(PPKError):
        convert_ppk(path, passphrase=None)
