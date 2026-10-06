"""密钥生成/加载的验证：Ed25519 与 RSA，含 passphrase 加密。"""

from __future__ import annotations

import base64
import os
import struct
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("PYSSH_DATA_DIR", tempfile.mkdtemp(prefix="pyssh-keygen-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sshmanager import settings  # noqa: E402
from sshmanager.keygen import KeyStore, load_private_key, public_key_text  # noqa: E402

import paramiko  # noqa: E402


def main() -> int:
    store = KeyStore()
    results: dict[str, bool] = {}

    # 1) Ed25519（无 passphrase）
    ed = store.generate("test-ed25519")
    ed_loaded = load_private_key(ed.path, None)
    results["ed25519_generated"] = ed.path.exists() and ed.public_path.exists()
    results["ed25519_name"] = ed_loaded.get_name() == "ssh-ed25519"
    results["ed25519_loads"] = bool(ed_loaded.asbytes())

    ed_text = store.public_text(ed, None)
    results["ed25519_public_text"] = ed_text.startswith("ssh-ed25519 ")
    print(f"1. Ed25519 公钥：{ed_text[:60]}...")

    # 公钥能被 paramiko 按 OpenSSH 文本解析回对象（用正规的 Message 路线）
    blob = base64.b64decode(ed_text.split()[1])
    parsed_ok = False

    try:
        message = paramiko.Message(blob)
        key_name = message.get_text()
        key_body = message.get_binary()
        parsed_ok = key_name == "ssh-ed25519" and len(key_body) == 32
    except Exception as error:
        print("   公钥解析失败:", error)

    results["ed25519_public_parsable"] = parsed_ok

    # 2) Ed25519（带 passphrase）
    ed2 = store.generate("test-ed25519-locked", passphrase="pw-123")
    results["encrypted_flag"] = ed2.encrypted is True

    try:
        load_private_key(ed2.path, None)
        wrong_ok = False
    except Exception:
        wrong_ok = True

    results["encrypted_needs_passphrase"] = wrong_ok

    try:
        loaded2 = load_private_key(ed2.path, "pw-123")
        right_ok = loaded2.get_name() == "ssh-ed25519"
    except Exception as error:
        print("   带 passphrase 加载失败:", error)
        right_ok = False

    results["encrypted_with_passphrase"] = right_ok
    print(f"2. 加密 Ed25519：无口令被拒={wrong_ok}，带口令可加载={right_ok}")

    # 3) RSA
    rsa = store.generate("test-rsa", key_type="rsa", bits=2048)
    rsa_loaded = load_private_key(rsa.path, None)
    results["rsa_generated"] = rsa_loaded.get_name() == "ssh-rsa"
    results["rsa_bits"] = rsa_loaded.get_bits() == 2048
    print(f"3. RSA 公钥：{store.public_text(rsa, None)[:60]}...")

    # 4) 登记表持久化与删除
    reloaded = KeyStore()
    results["index_roundtrip"] = len(reloaded.keys) == 3
    results["index_get"] = reloaded.get(ed.id) is not None

    reloaded.remove(ed2.id)
    results["removed"] = KeyStore().get(ed2.id) is None and not ed2.path.exists()

    # 5) OpenSSH blob 结构校验
    blob = base64.b64decode(ed_text.split()[1])
    length = struct.unpack(">I", blob[:4])[0]
    results["openssh_blob_format"] = blob[4 : 4 + length] == b"ssh-ed25519"

    print("--- 判定 ---")

    for name, value in results.items():
        print(f"{name}: {'OK' if value else 'FAIL'}")

    ok = all(results.values())
    print("KEYGEN_SMOKE_OK" if ok else "KEYGEN_SMOKE_FAILED")
    print("keys dir:", settings.keys_dir())

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
