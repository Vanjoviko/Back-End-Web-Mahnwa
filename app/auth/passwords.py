"""Verifikasi & pembuatan hash kata sandi.

Format hash mengikuti akun pembaca lama yang dibuat Node:

* ``crypto.scrypt(password, salt, 64)`` menulis ``"<salt hex>:<hash hex>"``.
* Parameter scrypt bawaan Node: ``N=16384, r=8, p=1``, salt 16 byte, hash 64 byte.

Python ``hashlib.scrypt`` memakai parameter yang sama, jadi hash lama langsung
bisa diverifikasi tanpa memaksa pengguna mengganti kata sandi (PRD §1.2, L6).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 64
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8


def _scrypt(password: str, salt: bytes, dklen: int = SCRYPT_DKLEN) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=dklen,
    )


def verify_password(password: str, record: str | None) -> bool:
    """``True`` bila ``password`` cocok dengan hash ``salt:hash`` gaya Node."""
    if not record or ":" not in record:
        return False
    salt_hex, hash_hex = record.split(":", 1)
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    if not salt or len(expected) < 16:
        return False
    try:
        actual = _scrypt(password, salt, len(expected))
    except (ValueError, MemoryError):  # pragma: no cover - pertahanan terhadap record rusak
        return False
    return hmac.compare_digest(actual, expected)


def hash_password(password: str) -> str:
    """Buat hash baru (dipakai P1 / pengaturan akun dan oleh pengujian)."""
    salt = secrets.token_bytes(SALT_BYTES)
    return f"{salt.hex()}:{_scrypt(password, salt).hex()}"


__all__ = [
    "MIN_PASSWORD_LENGTH",
    "hash_password",
    "verify_password",
]
