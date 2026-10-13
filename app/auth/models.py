"""ORM auth & bookmark (PostgreSQL via SQLAlchemy 2.0).

Skema dibuat lewat Alembic, bukan ``Base.metadata.create_all``. Semua tipe
portabel (``String``/``Text``/``DateTime(timezone=True)``) sehingga hanya perlu
mengganti ``DATABASE_URL`` untuk pindah dari Postgres lokal ke Postgres produksi.

Catatan desain:

* ``users`` menyatukan akun pembaca lama (punya ``password_hash``) dan akun
  hasil login sosial (``password_hash`` boleh kosong). Tidak ada sistem akun
  kedua.
* ``user_identities`` memetakan ``(provider, provider_user_id)`` ke satu akun.
  Pasangan itu unik dan TIDAK pernah digabung otomatis lewat email.
* ``bookmarks`` memakai primary key gabungan ``(user_id, series_id)`` sehingga
  menyimpan dua kali bersifat idempoten.
* ``sessions`` menyimpan hash token (bukan token mentah) + masa berlaku 30 hari.
* ``handoffs`` adalah kode sekali-pakai untuk menyerahkan sesi dari backend ke
  front-end (BFF) tanpa menaruh token sesi di URL.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    # Akun hasil login sosial belum punya kata sandi -> nullable.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Peran akses (RBAC): 'user' (default) atau 'admin'. Master ditetapkan lewat ADMIN_EMAILS.
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    identities: Mapped[list["UserIdentity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    bookmarks: Mapped[list["Bookmark"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    sessions: Mapped[list["SessionToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    handoffs: Mapped[list["HandoffCode"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class UserIdentity(Base):
    __tablename__ = "user_identities"
    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_user_identities_provider_user"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # google | discord | x
    provider_user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="identities")


class Bookmark(Base):
    __tablename__ = "bookmarks"
    __table_args__ = (
        Index("ix_bookmarks_user_created", "user_id", "created_at"),
    )

    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    series_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    user: Mapped[User] = relationship(back_populates="bookmarks")


class SessionToken(Base):
    """Sesi login. Nama tabel ``sessions``; token disimpan sebagai hash SHA-256."""

    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship(back_populates="sessions")


class HandoffCode(Base):
    """Kode sekali-pakai untuk menyerahkan sesi dari BE ke FE (BFF)."""

    __tablename__ = "handoffs"

    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="handoffs")


__all__ = ["User", "UserIdentity", "Bookmark", "SessionToken", "HandoffCode"]
