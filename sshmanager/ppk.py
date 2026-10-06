"""PuTTY ``.ppk`` 密钥导入。

支持 ``PuTTY-User-Key-File-2`` 与 ``-3``，算法 ``ssh-rsa`` / ``ssh-ed25519``：

- **未加密**的 ppk：直接构造私钥并写出 OpenSSH 格式
- **带 passphrase** 的 ppk：
  - v2 用 SHA-1 派生（标准库即可）
  - v3 用 Argon2（需要 ``argon2-cffi``，缺失时给出明确提示）

为什么不用 paramiko：paramiko 5.0 的 ``RSAKey`` 已经没有
``from_private_numbers``，所以这里用 ``cryptography`` 构造密钥再序列化成
OpenSSH PEM，交给 :func:`paramiko.PKey.from_private_key` 解析。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import struct
from dataclasses import dataclass
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY_TYPE_RSA = "ssh-rsa"
KEY_TYPE_ED25519 = "ssh-ed25519"
SUPPORTED_TYPES = (KEY_TYPE_RSA, KEY_TYPE_ED25519)


class PPKError(Exception):
    """ppk 解析失败。"""


@dataclass
class PPKKey:
    """解析后的 ppk 内容。"""

    key_type: str
    encryption: str
    comment: str
    public_blob: bytes
    private_blob: bytes
    mac: bytes
    version: int
    encrypted: bool
    #: v3 的 Argon2 参数（Key-Derivation / Argon2-Salt / Argon2-Memory / Argon2-Passes / Argon2-Parallelism）
    argon2: dict | None = None


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------

def parse_ppk(path: Path) -> PPKKey:
    """读取 ppk 文件结构（不做解密）。

    ppk 是"键值行 + 定行数 base64 段"的格式：``Public-Lines: N`` 表示紧随其后
    有 N 行 base64。这里按声明行数读取，比状态机更贴合规范、也不容易错位。
    """

    lines = [line.rstrip("\n") for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines()]
    fields: dict[str, str] = {}
    index = 0
    version = 0
    key_type = ""
    public_blob = b""
    private_blob = b""

    while index < len(lines):
        line = lines[index].strip()
        index += 1

        if not line:
            continue

        if line.startswith("PuTTY-User-Key-File-"):
            head, _, key_name = line.partition(":")
            version_text = head.rsplit("-", 1)[-1]

            try:
                version = int(version_text)
            except ValueError as error:
                raise PPKError(f"无法识别 ppk 版本：{head}") from error

            if version not in (2, 3):
                raise PPKError(f"不支持的 ppk 版本：{version}")

            key_type = key_name.strip()
            continue

        key, separator, value = line.partition(":")
        key = key.strip()
        value = value.strip()

        if key in ("Public-Lines", "Private-Lines"):
            try:
                count = int(value)
            except ValueError as error:
                raise PPKError(f"{key} 数量非法：{value}") from error

            chunk = [item.strip() for item in lines[index : index + count]]
            index += count

            if len(chunk) < count:
                raise PPKError(f"{key} 声明的 {count} 行不完整")

            blob = _decode_lines(chunk)

            if key == "Public-Lines":
                public_blob = blob
            else:
                private_blob = blob

            continue

        if key:
            fields[key] = value

    if not public_blob:
        raise PPKError("文件不完整：缺少 Public-Lines")

    if key_type not in SUPPORTED_TYPES:
        raise PPKError(f"不支持的密钥类型：{key_type or '未知'}（仅支持 ssh-rsa / ssh-ed25519）")

    encryption = (fields.get("Encryption") or "none").strip().lower()

    return PPKKey(
        key_type=key_type,
        encryption=encryption,
        comment=fields.get("Comment", "").strip(),
        public_blob=public_blob,
        private_blob=private_blob,
        mac=bytes.fromhex(fields.get("Private-MAC", "").replace(" ", "") or ""),
        version=version,
        encrypted=encryption not in ("none", ""),
        argon2={
            "derivation": fields.get("Key-Derivation", ""),
            "salt": fields.get("Argon2-Salt", ""),
            "memory": _as_int(fields.get("Argon2-Memory")),
            "passes": _as_int(fields.get("Argon2-Passes")),
            "parallelism": _as_int(fields.get("Argon2-Parallelism")),
        },
    )


def _as_int(text) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _decode_lines(lines: list[str]) -> bytes:
    if not lines:
        return b""

    try:
        return base64.b64decode("".join(lines))
    except Exception as error:  # noqa: BLE001
        raise PPKError(f"base64 解码失败：{error}") from error


def _read_mpint(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 4 > len(data):
        raise PPKError("公钥数据不完整")

    length = struct.unpack(">I", data[offset : offset + 4])[0]
    start = offset + 4
    end = start + length

    if end > len(data):
        raise PPKError("公钥数据不完整")

    value = int.from_bytes(data[start:end], "big")

    return value, end


# ----------------------------------------------------------------------
# 解密
# ----------------------------------------------------------------------

def _decrypt_private(ppk: PPKKey, passphrase: str) -> bytes:
    if not ppk.encrypted:
        return ppk.private_blob

    if not passphrase:
        raise PPKError("该密钥已加密，需要提供 passphrase")

    if ppk.version == 2:
        return _decrypt_v2(ppk, passphrase)

    return _decrypt_v3(ppk, passphrase)


def _decrypt_v2(ppk: PPKKey, passphrase: str) -> bytes:
    """v2：AES-256-CBC，密钥与 IV 都来自 SHA-1 迭代（需要校验 MAC）。"""

    passphrase_bytes = passphrase.encode("utf-8")

    def derive(prefix: bytes, length: int) -> bytes:
        result = b""
        index = 0

        while len(result) < length:
            result += hashlib.sha1(struct.pack(">I", index) + prefix + passphrase_bytes).digest()
            index += 1

        return result[:length]

    key = derive(b"\x00\x00\x00\x00", 32)
    iv = derive(b"\x00\x00\x00\x00", 16)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plain = decryptor.update(ppk.private_blob) + decryptor.finalize()
    plain = _strip_pkcs7(plain)

    # v2 的 MAC 覆盖密钥类型/加密方式/注释/公钥/明文私钥
    mac_data = (
        _string(ppk.key_type.encode("ascii"))
        + _string(ppk.encryption.encode("ascii"))
        + _string(ppk.comment.encode("utf-8"))
        + _string(ppk.public_blob)
        + _string(plain)
    )
    expected = hashlib.sha1(
        _string(b"putty-private-key-file-mac-key") + passphrase_bytes + mac_data
    ).digest()

    if ppk.mac and expected != ppk.mac:
        raise PPKError("passphrase 不正确（MAC 校验失败）")

    return plain


def _decrypt_v3(ppk: PPKKey, passphrase: str) -> bytes:
    """v3：Argon2id 派生 AES-256-CBC 密钥，并用 HMAC-SHA-256 校验。

    v3 的加密结构（PuTTY 0.75+）::

        Private-Lines 明文 = [4 字节 aes_key_len][aes_key][4 字节 iv_len][iv][密文]
        MAC key = SHA-256(public_blob)[:32]
        MAC     = HMAC-SHA-256(mac_key, 头部字段 + 密文)

    需要 ``argon2-cffi`` 才能做密钥派生。
    """

    try:
        from argon2.low_level import Type, hash_secret_raw
    except ImportError as error:
        raise PPKError(
            "该 ppk 用 Argon2 加密，需要安装 argon2-cffi 才能导入：pip install argon2-cffi"
        ) from error

    parameters = ppk.argon2 or {}
    salt_text = (parameters.get("salt") or "").strip()

    if not salt_text:
        raise PPKError("该 ppk 缺少 Argon2-Salt，无法解密（可先用 puttygen 另存为 OpenSSH 私钥）")

    salt = _hex_or_bytes(salt_text)
    memory = parameters.get("memory") or 8192
    passes = parameters.get("passes") or 34
    parallelism = parameters.get("parallelism") or 1

    key_material = hash_secret_raw(
        secret=passphrase.encode("utf-8"),
        salt=salt,
        time_cost=max(1, passes),
        memory_cost=max(8, memory),
        parallelism=max(1, parallelism),
        hash_len=80,
        type=Type.ID,
    )

    aes_key, mac_key = key_material[:32], key_material[32:]

    # 先校验 MAC，避免用错误的口令去解密
    expected = hmac.new(mac_key, _v3_mac_data(ppk), hashlib.sha256).digest()

    if ppk.mac and not hmac.compare_digest(expected, ppk.mac):
        raise PPKError("passphrase 不正确（MAC 校验失败）")

    cursor = 0
    key_length = struct.unpack(">I", ppk.private_blob[cursor : cursor + 4])[0]
    cursor += 4
    cursor += key_length
    iv_length = struct.unpack(">I", ppk.private_blob[cursor : cursor + 4])[0]
    cursor += 4
    iv = ppk.private_blob[cursor : cursor + iv_length]
    cursor += iv_length
    ciphertext = ppk.private_blob[cursor:]

    if not iv or not ciphertext:
        raise PPKError("加密数据不完整")

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    plain = decryptor.update(ciphertext) + decryptor.finalize()

    return _strip_pkcs7(plain)


def _v3_mac_data(ppk: PPKKey) -> bytes:
    """v3 的 MAC 覆盖面：头部字段 + 加密后的私钥。"""

    parameters = ppk.argon2 or {}
    data = (
        _string(b"none")
        + _string(ppk.comment.encode("utf-8"))
        + _string(ppk.public_blob)
        + _string(ppk.encryption.encode("ascii"))
        + _string((parameters.get("derivation") or "").encode("ascii"))
        + _string((parameters.get("salt") or "").encode("ascii"))
        + _string(str(parameters.get("memory") or "").encode("ascii"))
        + _string(str(parameters.get("passes") or "").encode("ascii"))
        + _string(str(parameters.get("parallelism") or "").encode("ascii"))
        + _string(ppk.private_blob)
    )

    return data


def _strip_pkcs7(data: bytes) -> bytes:
    """去掉 AES-CBC 的 PKCS#7 填充。"""

    if not data:
        return data

    padding = data[-1]

    if 1 <= padding <= 16 and data[-padding:] == bytes([padding]) * padding:
        return data[:-padding]

    return data


def _hex_or_bytes(text: str) -> bytes:
    """salt 在 ppk 里是十六进制字符串。"""

    cleaned = text.strip()

    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        return cleaned.encode("utf-8")


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


# ----------------------------------------------------------------------
# 转换
# ----------------------------------------------------------------------

def convert_ppk(path: Path, passphrase: str | None = None) -> tuple[paramiko.PKey, str]:
    """把 ppk 转成 paramiko 私钥对象，返回 ``(私钥, 注释)``。"""

    ppk = parse_ppk(path)
    private_blob = _decrypt_private(ppk, passphrase or "")
    private_key = _build_private_key(ppk, private_blob)
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )

    return _load_openssh_key(io.BytesIO(pem)), ppk.comment

def _load_openssh_key(file_obj) -> paramiko.PKey:
    """把 OpenSSH PEM 解析成 paramiko 密钥。

    注意两点：

    1. 不能用 ``paramiko.PKey.from_private_key``——抽象基类内部会
       ``cls(file_obj=...)``，直接 TypeError，必须按具体类型逐个尝试。
    2. paramiko 5.0 的读取按文本处理（内部用字符串正则），所以要给
       ``io.StringIO``，给 BytesIO 会报 "string pattern on a bytes-like object"。
    """

    data = file_obj.read()

    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")

    classes = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
    dss = getattr(paramiko, "DSSKey", None)

    if dss is not None:
        classes.append(dss)

    last_error: Exception | None = None

    for key_class in classes:
        try:
            return key_class.from_private_key(io.StringIO(data))
        except Exception as error:  # noqa: BLE001
            last_error = error

    raise PPKError(f"无法解析转换后的私钥：{last_error}")


def _build_private_key(ppk: PPKKey, private_blob: bytes):
    """按类型构造 cryptography 私钥对象。"""

    if ppk.key_type == KEY_TYPE_RSA:
        return _build_rsa(ppk, private_blob)

    return _build_ed25519(private_blob)


def _build_rsa(ppk: PPKKey, private_blob: bytes):
    from cryptography.hazmat.primitives.asymmetric import rsa

    _, offset = _read_mpint(ppk.public_blob, 0)
    exponent, offset = _read_mpint(ppk.public_blob, offset)
    modulus, _ = _read_mpint(ppk.public_blob, offset)

    cursor = 0
    d, cursor = _read_mpint(private_blob, cursor)
    p, cursor = _read_mpint(private_blob, cursor)
    q, cursor = _read_mpint(private_blob, cursor)

    numbers = rsa.RSAPrivateNumbers(
        p=p,
        q=q,
        d=d,
        dmp1=rsa.rsa_crt_dmp1(d, p),
        dmq1=rsa.rsa_crt_dmq1(d, q),
        iqmp=rsa.rsa_crt_iqmp(p, q),
        public_numbers=rsa.RSAPublicNumbers(e=exponent, n=modulus),
    )

    return numbers.private_key()


def _build_ed25519(private_blob: bytes):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if len(private_blob) >= 64:
        # PuTTY 存的是 seed + public key
        seed = private_blob[:32]
    elif len(private_blob) == 32:
        seed = private_blob
    else:
        raise PPKError(f"Ed25519 私钥长度异常：{len(private_blob)} 字节")

    return Ed25519PrivateKey.from_private_bytes(seed)


def public_key_line(ppk_key: PPKKey) -> str:
    """导出 OpenSSH 公钥文本。"""

    return f"{ppk_key.key_type} {base64.b64encode(ppk_key.public_blob).decode('ascii')}"


# ----------------------------------------------------------------------
# 写出 ppk（用于把应用里的密钥导出给 PuTTY，也用于测试生成真值文件）
# ----------------------------------------------------------------------

def _b64_lines(blob: bytes, width: int = 64) -> list[str]:
    encoded = base64.b64encode(blob).decode("ascii")

    return [encoded[index : index + width] for index in range(0, len(encoded), width)]


def write_ppk(path: Path, key_type: str, public_blob: bytes, private_blob: bytes, comment: str = "") -> Path:
    """写一个未加密的 ppk v3 文件（MAC 用零填充，仅用于导出/测试）。"""

    public_lines = _b64_lines(public_blob)
    private_lines = _b64_lines(private_blob)

    body = [
        f"PuTTY-User-Key-File-3: {key_type}",
        "Encryption: none",
        f"Comment: {comment}",
        f"Public-Lines: {len(public_lines)}",
        *public_lines,
        f"Private-Lines: {len(private_lines)}",
        *private_lines,
        "Private-MAC: " + "00" * 32,
    ]

    path = Path(path)
    path.write_text("\n".join(body) + "\n", encoding="utf-8")

    return path
