"""Cookie transaksi OAuth bertanda tangan + validasi ``return_to``.

Nilai ``state`` dan PKCE ``code_verifier`` disimpan di cookie sementara yang
ditandatangani HMAC, sehingga callback bisa memeriksanya tanpa penyimpanan
server (PRD §7 langkah 3, §10).

``return_to`` **hanya** menerima jalur relatif di situs sendiri (diawali satu
``/``). Alamat lain diganti beranda ``/`` supaya tidak bisa mengalihkan pembaca
ke situs lain (PRD §10).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

TXN_COOKIE = "akuma_oauth_txn"
TXN_TTL_SECONDS = 600


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: str, payload: dict[str, Any]) -> str:
    """Tanda tangani payload JSON: ``<base64url json>.<hmac sha256 hex>``."""
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def unsign(secret: str, value: str | None) -> dict[str, Any] | None:
    """Verifikasi & uraikan nilai bertanda tangan; ``None`` bila tidak sah."""
    if not value or "." not in value:
        return None
    body, sig = value.rsplit(".", 1)
    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_b64d(body))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def sanitize_return_to(raw: str | None) -> str:
    """Kembalikan jalur relatif yang aman, atau ``/`` bila mencurigakan."""
    value = (raw or "").strip()
    if not value or not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or any(ch in value for ch in "\r\n\t"):
        return "/"
    return value


__all__ = ["TXN_COOKIE", "TXN_TTL_SECONDS", "sanitize_return_to", "sign", "unsign"]
