"""Sesi server & kode handoff sekali-pakai.

* **Sesi** bertahan ``AUTH_SESSION_TTL_DAYS`` hari sejak aktivitas terakhir
  (sliding, L12). Token mentah hanya ada di pemanggil; database menyimpan hash.
* **Handoff** adalah kode berumur pendek, sekali pakai, untuk menyerahkan sesi
  dari backend ke front-end (BFF) tanpa menaruh token sesi di URL.

Semua waktu dibandingkan dalam UTC dan tahan terhadap driver yang mengembalikan
``datetime`` naive (mis. SQLite pada pengujian).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.auth.models import HandoffCode, SessionToken, User
from app.auth.tokens import new_token, token_hash

HANDOFF_TTL_SECONDS = 120


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def create_session(db: DbSession, user_id: str, ttl_days: int, *, now: datetime | None = None) -> str:
    """Buat sesi baru dan kembalikan token mentah (dikirim ke FE, bukan browser)."""
    now = now or utcnow()
    token = new_token()
    db.add(
        SessionToken(
            token_hash=token_hash(token),
            user_id=user_id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=ttl_days),
        )
    )
    db.commit()
    return token


def resolve_session(
    db: DbSession, token: str | None, ttl_days: int, *, now: datetime | None = None
) -> User | None:
    """Kembalikan pengguna bila token sesi masih berlaku; perpanjang masa berlaku."""
    if not token:
        return None
    now = now or utcnow()
    row = db.get(SessionToken, token_hash(token))
    if row is None:
        return None
    expires = _aware(row.expires_at)
    if expires is not None and expires <= now:
        db.delete(row)
        db.commit()
        return None
    row.last_seen_at = now
    row.expires_at = now + timedelta(days=ttl_days)
    db.commit()
    return db.get(User, row.user_id)


def revoke_session(db: DbSession, token: str | None) -> bool:
    """Hapus sesi di server (L11). ``True`` bila ada yang dihapus."""
    if not token:
        return False
    row = db.get(SessionToken, token_hash(token))
    if row is None:
        return False
    db.delete(row)
    db.commit()
    return True


def create_handoff(
    db: DbSession, user_id: str, *, ttl_seconds: int = HANDOFF_TTL_SECONDS, now: datetime | None = None
) -> str:
    """Terbitkan kode handoff sekali-pakai dan kembalikan kode mentahnya."""
    now = now or utcnow()
    code = new_token(24)
    db.add(
        HandoffCode(
            code_hash=token_hash(code),
            user_id=user_id,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
    )
    db.commit()
    return code


def consume_handoff(db: DbSession, code: str | None, *, now: datetime | None = None) -> User | None:
    """Tukar kode handoff dengan pengguna; kode langsung hangus (sekali pakai)."""
    if not code:
        return None
    now = now or utcnow()
    row = db.get(HandoffCode, token_hash(code))
    if row is None:
        return None
    expires = _aware(row.expires_at)
    if row.used_at is not None or (expires is not None and expires <= now):
        return None
    row.used_at = now
    db.commit()
    return db.get(User, row.user_id)


def purge_expired(db: DbSession, *, now: datetime | None = None) -> int:
    """Hapus sesi & handoff kedaluwarsa (dipanggil berkala). Kembalikan jumlah baris."""
    now = now or utcnow()
    removed = 0
    for row in db.scalars(select(SessionToken)).all():
        expires = _aware(row.expires_at)
        if expires is not None and expires <= now:
            db.delete(row)
            removed += 1
    for row in db.scalars(select(HandoffCode)).all():
        expires = _aware(row.expires_at)
        if expires is not None and expires <= now:
            db.delete(row)
            removed += 1
    if removed:
        db.commit()
    return removed


__all__ = [
    "HANDOFF_TTL_SECONDS",
    "consume_handoff",
    "create_handoff",
    "create_session",
    "purge_expired",
    "resolve_session",
    "revoke_session",
    "utcnow",
]
