"""Pembantu token opak: token mentah acak + hash SHA-256 untuk disimpan.

Token sesi & kode handoff tidak pernah disimpan mentah di database, hanya
hash-nya (PRD §10). Token dikirim ke pemanggil sekali lalu dilupakan server.
"""
from __future__ import annotations

import hashlib
import secrets


def new_token(nbytes: int = 32) -> str:
    """Token opak URL-safe (dipakai sebagai kredensial bearer & kode handoff)."""
    return secrets.token_urlsafe(nbytes)


def token_hash(token: str) -> str:
    """Hash SHA-256 heksadesimal dari token mentah."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = ["new_token", "token_hash"]
