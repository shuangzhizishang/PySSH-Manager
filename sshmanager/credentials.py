"""凭据加密存储。

Windows 上优先使用 DPAPI（CryptProtectData），密钥由系统按当前用户派生，
不落盘、不可跨用户解密。其他平台回退到 ``cryptography.Fernet``，
密钥保存在数据目录下的 ``.keyfile``（权限 0600），安全性弱于 DPAPI。
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

from . import settings


class CredentialStore:
    """按 ``key``（通常是主机 id）保存密钥/口令。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else settings.credentials_file()
        self._cache: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def get(self, key: str) -> str | None:
        return self._cache.get(key)

    def set(self, key: str, secret: str) -> None:
        self._cache[key] = secret
        self._save()

    def delete(self, key: str) -> None:
        if key in self._cache:
            del self._cache[key]
            self._save()

    def rename(self, old_key: str, new_key: str) -> None:
        """主机 id 变化时迁移对应凭据。"""

        if old_key in self._cache and old_key != new_key:
            self._cache[new_key] = self._cache.pop(old_key)
            self._save()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        for key, token in raw.items():
            try:
                self._cache[key] = self._decrypt(token)
            except Exception:
                # 单条损坏不应拖垮整个凭据库
                continue

    def _save(self) -> None:
        payload = {key: self._encrypt(value) for key, value in self._cache.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # 加解密
    # ------------------------------------------------------------------

    def _encrypt(self, plain: str) -> str:
        if sys.platform == "win32":
            return "dpapi:" + _dpapi_protect(plain)
        return "fernet:" + self._fernet().encrypt(plain.encode("utf-8")).decode("ascii")

    def _decrypt(self, token: str) -> str:
        scheme, _, body = token.partition(":")

        if scheme == "dpapi":
            return _dpapi_unprotect(body)

        if scheme == "fernet":
            return self._fernet().decrypt(body.encode("ascii")).decode("utf-8")

        raise ValueError(f"未知的凭据格式: {scheme}")

    def _fernet(self):
        from cryptography.fernet import Fernet

        key_path = self.path.parent / ".keyfile"

        if key_path.exists():
            key = key_path.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            key_path.write_bytes(key)
            try:
                key_path.chmod(0o600)
            except OSError:
                pass

        return Fernet(key)


# ----------------------------------------------------------------------
# Windows DPAPI
# ----------------------------------------------------------------------

if sys.platform == "win32":
    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    def _blob_to_bytes(blob: "_DataBlob") -> bytes:
        return ctypes.string_at(blob.pbData, blob.cbData)

    def _dpapi_protect(plain: str) -> str:
        raw = plain.encode("utf-8")
        blob_in = _DataBlob(len(raw), ctypes.cast(ctypes.create_string_buffer(raw), ctypes.POINTER(ctypes.c_char)))
        blob_out = _DataBlob()

        ok = _crypt32.CryptProtectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )

        if not ok:
            raise OSError("CryptProtectData 失败")

        try:
            return base64.b64encode(_blob_to_bytes(blob_out)).decode("ascii")
        finally:
            _kernel32.LocalFree(blob_out.pbData)

    def _dpapi_unprotect(token: str) -> str:
        raw = base64.b64decode(token.encode("ascii"))
        blob_in = _DataBlob(len(raw), ctypes.cast(ctypes.create_string_buffer(raw), ctypes.POINTER(ctypes.c_char)))
        blob_out = _DataBlob()

        ok = _crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )

        if not ok:
            raise OSError("CryptUnprotectData 失败")

        try:
            return _blob_to_bytes(blob_out).decode("utf-8")
        finally:
            _kernel32.LocalFree(blob_out.pbData)

else:  # pragma: no cover - 非 Windows 平台不会走这里
    def _dpapi_protect(plain: str) -> str:
        raise RuntimeError("DPAPI 仅在 Windows 可用")

    def _dpapi_unprotect(token: str) -> str:
        raise RuntimeError("DPAPI 仅在 Windows 可用")
