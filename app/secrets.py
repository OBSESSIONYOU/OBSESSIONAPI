"""渠道密钥的本地加密存储。"""
import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


PREFIX = "enc:v1:"
SECRET_FILE = "obsessionapi.secret"


def _data_dir() -> Path:
    from . import database

    return database.DATA_DIR


def _key() -> bytes:
    configured = os.getenv("OBSESSIONAPI_SECRET_KEY", "").strip()
    if configured:
        return hashlib.sha256(configured.encode("utf-8")).digest()
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / SECRET_FILE
    try:
        raw = base64.urlsafe_b64decode(path.read_text(encoding="ascii").strip())
    except (FileNotFoundError, ValueError, UnicodeDecodeError):
        raw = os.urandom(32)
        path.write_text(base64.urlsafe_b64encode(raw).decode("ascii"), encoding="ascii")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    if len(raw) != 32:
        raise ValueError(f"{path} 不是有效的 OBSESSIONAPI 密钥文件")
    return raw


def is_encrypted(value: str) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt_secret(value: str | None) -> str:
    value = str(value or "")
    if not value or is_encrypted(value):
        return value
    nonce = os.urandom(12)
    payload = AESGCM(_key()).encrypt(nonce, value.encode("utf-8"), None)
    return PREFIX + base64.urlsafe_b64encode(nonce + payload).decode("ascii")


def decrypt_secret(value: str | None) -> str:
    value = str(value or "")
    if not value or not is_encrypted(value):
        return value
    try:
        payload = base64.urlsafe_b64decode(value[len(PREFIX):].encode("ascii"))
        return AESGCM(_key()).decrypt(payload[:12], payload[12:], None).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ValueError("无法解密渠道密钥，请检查 OBSESSIONAPI_SECRET_KEY 或 data/obsessionapi.secret") from exc


def migrate_channel_secrets(conn) -> int:
    """把旧库中的明文渠道密钥一次性转换为密文。"""
    changed = 0
    for row in conn.execute("SELECT id, api_key FROM channels").fetchall():
        value = row[1] or ""
        if value and not is_encrypted(value):
            conn.execute("UPDATE channels SET api_key=? WHERE id=?", (encrypt_secret(value), row[0]))
            changed += 1
    return changed
