"""用户认证：密码哈希（pbkdf2）+ JWT 签发/验证。"""

from __future__ import annotations

import hashlib
import os
import secrets
import warnings
from pathlib import Path

import jwt

_SECRET: str | None = None
_LOCAL_SECRET_PATH = Path(__file__).resolve().parents[2] / "data" / ".jwt-secret"


def _load_or_create_local_secret() -> str:
    """Keep the development signing key stable across server restarts."""
    try:
        existing = _LOCAL_SECRET_PATH.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except FileNotFoundError:
        pass
    except OSError:
        pass

    generated = secrets.token_hex(32)
    try:
        _LOCAL_SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            _LOCAL_SECRET_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(generated)
        return generated
    except FileExistsError:
        # Another worker won the creation race; all workers must use its key.
        return _LOCAL_SECRET_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return generated


def _get_secret() -> str:
    global _SECRET
    if _SECRET is None:
        _SECRET = os.getenv("JWT_SECRET", "")
        if not _SECRET:
            _SECRET = _load_or_create_local_secret()
            warnings.warn(
                "JWT_SECRET 未配置，正在使用 data/.jwt-secret 中的本地开发密钥。"
                "生产环境请在 .env.local 中显式设置 JWT_SECRET。",
                stacklevel=2,
            )
    return _SECRET


def hash_password(password: str) -> str:
    """返回 'salt:hex' 格式的哈希字符串。"""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"{salt}:{h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, hex_hash = stored.split(":", 1)
    except ValueError:
        return False
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return secrets.compare_digest(h.hex(), hex_hash)


def create_token(user_id: str) -> str:
    return jwt.encode({"sub": user_id}, _get_secret(), algorithm="HS256")


def decode_token(token: str) -> str | None:
    try:
        payload = jwt.decode(token, _get_secret(), algorithms=["HS256"])
        return payload.get("sub")
    except jwt.PyJWTError:
        return None
