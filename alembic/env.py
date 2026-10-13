"""Lingkungan Alembic.

URL database diambil dari ``DATABASE_URL`` (bukan dari ``alembic.ini``) supaya
rahasia tidak ikut ter-commit dan pindah lingkungan cukup ganti env. Metadata
hanya memuat tabel fitur baru (auth/bookmark). Tabel katalog legacy tetap dikelola
jalur ``ENABLE_LEGACY_API`` (deprecated) dan tidak diikutkan di sini.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.database import Base

# Impor model agar tabel terdaftar di Base.metadata.
import app.auth.models  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL belum diset. Contoh: postgresql+psycopg://mahnwa:rahasia@localhost:5433/mahnwa"
        )
    return url


def run_migrations_offline() -> None:
    """Jalankan migrasi tanpa koneksi (menghasilkan SQL)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Jalankan migrasi lewat koneksi langsung."""
    connectable = create_engine(_database_url(), poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
