"""SSH 密钥生成与管理。

- 生成 Ed25519 / RSA 密钥对，私钥可选 passphrase 加密后写入 ``data_dir/keys``
- 读取已有私钥（含加密的），导出 OpenSSH 公钥文本
- 把公钥部署到远端 ``~/.ssh/authorized_keys``（权限 700/600）

私钥文件权限在 Windows 上不能靠 chmod 表达，因此这里明确记录在
``keys/index.json`` 中仅作登记；真正的秘密保护依赖 DPAPI 存储的 passphrase
与 Windows 账户隔离。
"""

from __future__ import annotations

import json
import os
import stat
import struct
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import settings

DEFAULT_KEY_TYPE = "ed25519"
KEY_TYPES = ("ed25519", "rsa")


class KeyError_(Exception):
    """密钥操作失败。"""


@dataclass
class ManagedKey:
    """一条受管理的密钥记录。"""

    name: str = ""
    key_type: str = DEFAULT_KEY_TYPE
    comment: str = ""
    #: 私钥文件名（不含目录）
    filename: str = ""
    #: 是否用 passphrase 加密
    encrypted: bool = False
    bits: int = 0
    created_at: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def path(self) -> Path:
        return settings.keys_dir() / self.filename

    @property
    def public_path(self) -> Path:
        return settings.keys_dir() / f"{self.filename}.pub"

    @property
    def display_name(self) -> str:
        return self.name or self.filename or "未命名密钥"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ManagedKey":
        known = {name for name in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{key: value for key, value in data.items() if key in known})


class KeyStore:
    """``data_dir/keys`` 下的密钥登记表。"""

    def __init__(self, path: Path | None = None) -> None:
        self.directory = settings.keys_dir()
        self.index_path = Path(path) if path else self.directory / "index.json"
        self.keys: list[ManagedKey] = []
        self.load()

    # ------------------------------------------------------------------

    def load(self) -> None:
        self.keys = []

        if not self.index_path.exists():
            return

        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        if not isinstance(raw, list):
            return

        for item in raw:
            if not isinstance(item, dict):
                continue

            try:
                key = ManagedKey.from_dict(item)
            except TypeError:
                continue

            if key.filename:
                self.keys.append(key)

    def save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = [key.to_dict() for key in self.keys]
        self.index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------

    def get(self, key_id: str) -> ManagedKey | None:
        for key in self.keys:
            if key.id == key_id:
                return key
        return None

    def add(self, key: ManagedKey) -> ManagedKey:
        self.keys.append(key)
        self.save()
        return key

    def remove(self, key_id: str, delete_files: bool = True) -> None:
        key = self.get(key_id)

        if key is None:
            return

        if delete_files:
            for path in (key.path, key.public_path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

        self.keys = [item for item in self.keys if item.id != key_id]
        self.save()

    # ------------------------------------------------------------------

    def generate(
        self,
        name: str,
        key_type: str = DEFAULT_KEY_TYPE,
        passphrase: str | None = None,
        bits: int = 4096,
        comment: str | None = None,
    ) -> ManagedKey:
        """生成密钥对并写入磁盘，返回登记记录。"""

        import paramiko

        self.directory.mkdir(parents=True, exist_ok=True)

        key_type = (key_type or DEFAULT_KEY_TYPE).lower()

        if key_type == "rsa":
            private_key = paramiko.RSAKey.generate(bits=max(2048, int(bits)))
        elif key_type == "ed25519":
            private_key = _generate_ed25519()
        else:
            raise KeyError_(f"不支持的密钥类型：{key_type}")

        safe_name = _safe_filename(name) or f"key-{uuid.uuid4().hex[:8]}"
        filename = safe_name if not (self.directory / safe_name).exists() else f"{safe_name}-{uuid.uuid4().hex[:4]}"

        key = ManagedKey(
            name=name or filename,
            key_type=key_type,
            comment=comment or name or filename,
            filename=filename,
            encrypted=bool(passphrase),
            bits=bits if key_type == "rsa" else 256,
        )

        try:
            private_key.write_private_key_file(str(key.path), password=passphrase or None)
        except Exception as error:  # noqa: BLE001
            raise KeyError_(f"写入私钥失败：{error}") from error

        try:
            key.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

        public_text = f"{public_key_text(private_key)} {key.comment}\n"
        key.public_path.write_text(public_text, encoding="utf-8")

        return self.add(key)

    def public_text(self, key: ManagedKey, passphrase: str | None = None) -> str:
        """读取公钥文本；没有 .pub 时从私钥推导（需要 passphrase）。"""

        if key.public_path.exists():
            return key.public_path.read_text(encoding="utf-8").strip()

        private_key = load_private_key(key.path, passphrase)

        return public_key_text(private_key)

    def deploy_public_key(self, ssh_client, key: ManagedKey, passphrase: str | None = None) -> str:
        """把公钥追加到远端 ``~/.ssh/authorized_keys``（幂等）。"""

        public_text = self.public_text(key, passphrase)
        command = _DEPLOY_SCRIPT.replace("__KEY__", _shell_single_quote(public_text))

        stdin, stdout, stderr = ssh_client.exec_command(command, timeout=20)
        output = stdout.read().decode("utf-8", errors="replace").strip()
        error = stderr.read().decode("utf-8", errors="replace").strip()

        if error:
            raise KeyError_(error)

        return output or "已部署"


def public_key_text(private_key) -> str:
    """导出 OpenSSH 公钥文本（``ssh-ed25519 AAAA...``）。"""

    raw = private_key.asbytes()

    if private_key.get_name() == "ssh-ed25519":
        return "ssh-ed25519 " + _ed25519_openssh_public(raw)
    return private_key.get_name() + " " + _b64(raw)


def load_private_key(path: Path, passphrase: str | None = None):
    """按类型加载私钥（与 sshclient.load_private_key 同样的坑：不要用 PKey.from_path）。"""

    import paramiko

    path = Path(path)
    classes = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
    dss = getattr(paramiko, "DSSKey", None)

    if dss is not None:
        classes.append(dss)

    last_error: Exception | None = None

    for key_class in classes:
        try:
            return key_class.from_private_key_file(str(path), password=passphrase)
        except Exception as error:  # noqa: BLE001
            last_error = error

    raise KeyError_(f"无法加载私钥 {path.name}：{last_error}")


# ----------------------------------------------------------------------
# Ed25519 支持
# ----------------------------------------------------------------------

def _generate_ed25519():
    """paramiko 的 Ed25519Key 只能解析、不能生成，这里用 cryptography 造一个。

    返回的对象实现 paramiko 期望的接口：``get_name`` / ``asbytes`` /
    ``get_fingerprint`` / ``sign_ssh_data`` / ``write_private_key_file``，
    因此既能直接用于 ``Transport.auth_publickey``，也能写出 OpenSSH 私钥文件。
    """

    import hashlib

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    class GeneratedEd25519:
        """鸭子类型的 Ed25519 私钥。"""

        def __init__(self) -> None:
            self._key = Ed25519PrivateKey.generate()
            self._raw = self._key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )

        # --- paramiko 需要的接口 -----------------------------------

        def get_name(self) -> str:
            return "ssh-ed25519"

        def asbytes(self) -> bytes:
            return self._raw

        def get_base64(self) -> str:
            return _b64(self.public_blob())

        def public_blob(self) -> bytes:
            return _ed25519_blob(self._raw)

        def get_fingerprint(self) -> bytes:
            return hashlib.md5(self.public_blob()).digest()  # noqa: S324 - 与 paramiko 指纹格式一致

        def sign_ssh_data(self, data: bytes, algorithm: str | None = None) -> paramiko.Message:
            import paramiko

            signature = self._key.sign(data)
            message = paramiko.Message()
            message.add_string("ssh-ed25519")
            message.add_string(signature)
            return message

        def write_private_key_file(self, filename, password=None) -> None:
            encryption = (
                serialization.BestAvailableEncryption(
                    password.encode("utf-8") if isinstance(password, str) else password
                )
                if password
                else serialization.NoEncryption()
            )

            data = self._key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.OpenSSH,
                encryption_algorithm=encryption,
            )

            with open(filename, "wb") as handle:
                handle.write(data)

        def __eq__(self, other) -> bool:
            return hasattr(other, "asbytes") and other.asbytes() == self._raw

        def __hash__(self) -> int:
            return hash(self._raw)

    return GeneratedEd25519()


def _ed25519_blob(raw_public: bytes) -> bytes:
    """OpenSSH 公钥 blob：``string "ssh-ed25519" + string key``。"""

    return struct.pack(">I", len(b"ssh-ed25519")) + b"ssh-ed25519" + struct.pack(">I", len(raw_public)) + raw_public


def _ed25519_openssh_public(raw_public: bytes) -> str:
    return _b64(_ed25519_blob(raw_public))


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _safe_filename(name: str) -> str:
    cleaned = "".join(char for char in (name or "") if char.isalnum() or char in "-_.").strip("._")

    return cleaned[:48]


def _shell_single_quote(text: str) -> str:
    """把文本安全地放进 shell 单引号里。"""

    return "'" + text.replace("'", "'\"'\"'") + "'"


_DEPLOY_SCRIPT = r"""
set -e
mkdir -p ~/.ssh
chmod 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
KEY=__KEY__
grep -qxF "$KEY" ~/.ssh/authorized_keys || printf '%s\n' "$KEY" >> ~/.ssh/authorized_keys
echo "已写入 ~/.ssh/authorized_keys（$(wc -l < ~/.ssh/authorized_keys) 行）"
"""
