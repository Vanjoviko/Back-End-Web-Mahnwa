"""Akses database lewat SQLAlchemy ORM.

Engine dibuat *lazy* (saat pertama dipakai), bukan saat import, supaya:

* import modul ini tidak pernah menyentuh/membuat file database, dan
* aplikasi yang tidak memakai database (mis. jalur scan-import murni) tetap
  bisa di-import walau ``DATABASE_URL`` belum diisi.

URL diambil dari environment ``DATABASE_URL``. Produksi memakai PostgreSQL
(``postgresql+psycopg://...``); SQLite hanya dipertahankan untuk kode legacy
yang dijalankan di balik flag ``ENABLE_LEGACY_API``.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Base ORM tunggal untuk semua tabel (auth, bookmark, katalog legacy)."""


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def database_url() -> str:
    """URL database dari environment (kosong bila belum diisi)."""
    return (os.getenv("DATABASE_URL") or "").strip()


def get_engine() -> Engine:
    """Engine SQLAlchemy bersama; dibuat sekali lalu di-cache."""
    global _engine
    if _engine is None:
        url = database_url()
        if not url:
            raise RuntimeError(
                "DATABASE_URL kosong. Isi di .env.dev/.env.prod "
                "(contoh: postgresql+psycopg://user:pass@db:5432/mahnwa)."
            )
        kwargs: dict[str, object] = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            # Hanya untuk kode legacy; produksi memakai PostgreSQL.
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(url, **kwargs)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    """Factory ``Session`` yang terikat pada engine bersama."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(), autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _session_factory


def SessionLocal() -> Session:
    """Nama kompatibel dengan kode lama; mengembalikan ``Session`` baru."""
    return get_sessionmaker()()


def reset_engine() -> None:
    """Buang engine/factory yang di-cache (dipakai saat pengujian/migrasi)."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def get_db() -> Iterator[Session]:
    """Dependency FastAPI: satu ``Session`` per request, selalu ditutup."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
