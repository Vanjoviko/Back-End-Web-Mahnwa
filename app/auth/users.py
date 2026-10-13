"""Akun pembaca: pencarian, nama pengguna (L5), dan penautan identitas sosial.

Aturan penting (PRD §10):

* Identitas ``(provider, provider_user_id)`` unik dan **tidak pernah** digabung
  otomatis ke akun lain hanya karena emailnya sama.
* Username selalu huruf kecil, hanya ``a-z 0-9 _``, panjang 3–20; bila bentrok
  ditambah angka acak.
"""
from __future__ import annotations

import re
import secrets
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.auth.models import User, UserIdentity
from app.auth.sessions import utcnow

USERNAME_MIN = 3
USERNAME_MAX = 20
_INVALID_USERNAME = re.compile(r"[^a-z0-9_]")

PROVIDER_LABELS = {"google": "Google", "discord": "Discord", "x": "X"}


def username_from_display(display_name: str | None) -> str:
    """Bentuk dasar username dari nama tampilan penyedia (L5)."""
    raw = (display_name or "").strip().lower()
    raw = raw.replace(" ", "_")
    base = _INVALID_USERNAME.sub("", raw).strip("_")
    if len(base) < USERNAME_MIN:
        base = (base or "user").ljust(USERNAME_MIN, "0")
    return base[:USERNAME_MAX]


def _random_digits(n: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(n))


def unique_username(db: DbSession, base: str) -> str:
    """Kembalikan username bebas; tambahkan angka acak bila sudah dipakai (L5)."""
    base = base[:USERNAME_MAX] or "user"
    if db.scalar(select(User.id).where(User.username == base)) is None:
        return base
    for _ in range(50):
        suffix = _random_digits(4)
        candidate = (base[: USERNAME_MAX - len(suffix)] + suffix)[:USERNAME_MAX]
        if db.scalar(select(User.id).where(User.username == candidate)) is None:
            return candidate
    # Praktis tak tercapai; tetap hasilkan kandidat unik yang valid.
    return ("u" + _random_digits(USERNAME_MAX - 1))[:USERNAME_MAX]


def resolve_role(
    email: str | None, email_verified: bool, admin_emails: tuple[str, ...] | set[str]
) -> str:
    """Tentukan role dari email terverifikasi + allowlist master (RBAC).

    Email hanya dipakai bila penyedia menyatakan terverifikasi, dan hanya untuk
    menentukan **role** — bukan identitas akun (itu tetap ``(provider, provider_user_id)``).
    """
    if email and email_verified:
        allowed = {e.strip().lower() for e in admin_emails if e and e.strip()}
        if email.strip().lower() in allowed:
            return "admin"
    return "user"


def find_user_by_username(db: DbSession, username: str) -> User | None:
    normalized = username.strip().lower()
    if not normalized:
        return None
    return db.scalar(select(User).where(User.username == normalized))


def find_identity(db: DbSession, provider: str, provider_user_id: str) -> UserIdentity | None:
    return db.scalar(
        select(UserIdentity).where(
            UserIdentity.provider == provider,
            UserIdentity.provider_user_id == provider_user_id,
        )
    )


def get_or_create_social_user(
    db: DbSession,
    *,
    provider: str,
    provider_user_id: str,
    display_name: str | None,
    email: str | None,
    avatar_url: str | None,
    role: str = "user",
    now: datetime | None = None,
) -> tuple[User, bool]:
    """Masuk ke akun identitas itu, atau buat akun pembaca baru (L3, L4).

    Kembalikan ``(user, created)``. Tidak ada penggabungan lewat email.
    """
    now = now or utcnow()
    identity = find_identity(db, provider, provider_user_id)
    if identity is not None:
        identity.last_login_at = now
        if email and not identity.email:
            identity.email = email
        user = db.get(User, identity.user_id)
        if user is not None:
            # Perbarui avatar/email dari penyedia bila akun belum punya.
            if avatar_url and not user.avatar_url:
                user.avatar_url = avatar_url
            if email and not user.email:
                user.email = email
            # Sinkronkan role tiap login: promosi/demosi dari allowlist langsung berlaku.
            if user.role != role:
                user.role = role
            db.commit()
            return user, False
        # Baris identitas yatim (user terhapus): buang lalu buat ulang.
        db.delete(identity)
        db.commit()

    user = User(
        id=str(uuid.uuid4()),
        username=unique_username(db, username_from_display(display_name)),
        password_hash=None,
        email=email or None,
        avatar_url=avatar_url or None,
        role=role,
        created_at=now,
    )
    db.add(user)
    db.add(
        UserIdentity(
            id=str(uuid.uuid4()),
            user_id=user.id,
            provider=provider,
            provider_user_id=provider_user_id,
            email=email or None,
            created_at=now,
            last_login_at=now,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Balapan: identitas dibuat proses lain lebih dulu -> pakai akun itu.
        db.rollback()
        existing = find_identity(db, provider, provider_user_id)
        if existing is None:
            raise
        existing.last_login_at = now
        db.commit()
        return db.get(User, existing.user_id), False  # type: ignore[return-value]
    return user, True


def providers_of(db: DbSession, user_id: str) -> list[str]:
    rows = db.scalars(
        select(UserIdentity.provider)
        .where(UserIdentity.user_id == user_id)
        .order_by(UserIdentity.provider)
    ).all()
    return list(rows)


def public_user(db: DbSession, user: User) -> dict:
    """Bentuk jawaban ``GET /me`` (PRD §9). Tidak pernah memuat hash kata sandi."""
    return {
        "id": user.id,
        "username": user.username,
        "avatar_url": user.avatar_url,
        "email": user.email,
        "role": user.role,
        "has_password": bool(user.password_hash),
        "providers": providers_of(db, user.id),
    }


__all__ = [
    "PROVIDER_LABELS",
    "USERNAME_MAX",
    "USERNAME_MIN",
    "find_identity",
    "find_user_by_username",
    "get_or_create_social_user",
    "providers_of",
    "public_user",
    "resolve_role",
    "unique_username",
    "username_from_display",
]
