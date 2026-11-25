"""Symmetric encryption helpers for sensitive credentials (WABA tokens, OAuth refresh tokens)."""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

_DEV_FALLBACK_KEY = "dev-bookbot-encryption-key-change-in-production"


def _get_fernet() -> Fernet:
    raw = settings.encryption_key
    if not raw:
        if settings.environment == "production":
            raise RuntimeError("ENCRYPTION_KEY must be set in production")
        raw = _DEV_FALLBACK_KEY

    # Accept an already-valid 32-byte urlsafe base64 Fernet key, otherwise derive via SHA-256
    try:
        return Fernet(raw.encode("utf-8"))
    except Exception:
        digest = hashlib.sha256(raw.encode("utf-8")).digest()
        derived = base64.urlsafe_b64encode(digest)
        return Fernet(derived)


def encrypt_secret(plaintext: str) -> bytes:
    """Encrypt a plaintext string into Fernet ciphertext bytes."""
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_secret(ciphertext: bytes | str | None) -> str | None:
    """Decrypt Fernet ciphertext bytes back to plaintext.

    Gracefully falls back to UTF-8 decode for legacy unencrypted values stored
    before encryption was enforced.
    """
    if ciphertext is None:
        return None
    if isinstance(ciphertext, str):
        raw = ciphertext.encode("utf-8")
    else:
        raw = bytes(ciphertext)
    if not raw:
        return None

    try:
        return _get_fernet().decrypt(raw).decode("utf-8")
    except InvalidToken:
        # Backward compatibility with legacy plaintext tokens stored in DB
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
