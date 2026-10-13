"""Uji skema auth/bookmark (hasil Alembic) dan pemetaan ORM.

Modul ini hanya berjalan bila ``DATABASE_URL`` diset dan terjangkau; pada suite
default tanpa database ia di-skip, sehingga 264 tes lama tetap bisa jalan.
Migrasi harus sudah dijalankan (``alembic upgrade head``) sebelum tes ini.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.auth.models import Bookmark, HandoffCode, SessionToken, User, UserIdentity
from app.database import SessionLocal, get_engine, reset_engine

pytestmark = pytest.mark.skipif(
    not (os.getenv("DATABASE_URL") or "").strip(), reason="DATABASE_URL tidak diset"
)

EXPECTED_TABLES = {"users", "user_identities", "bookmarks", "sessions", "handoffs"}


@pytest.fixture(scope="module")
def engine():
    reset_engine()
    eng = get_engine()
    try:
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - bergantung lingkungan
        pytest.skip(f"DATABASE_URL tidak terjangkau: {exc}")
    return eng


def test_tables_and_indexes_exist(engine):
    inspector = inspect(engine)
    names = set(inspector.get_table_names())
    assert EXPECTED_TABLES <= names, f"tabel kurang: {EXPECTED_TABLES - names}"

    users_indexes = {idx["name"] for idx in inspector.get_indexes("users")}
    assert "ix_users_username" in users_indexes

    bookmark_indexes = {idx["name"] for idx in inspector.get_indexes("bookmarks")}
    assert "ix_bookmarks_user_created" in bookmark_indexes

    identity_uniques = {uc["name"] for uc in inspector.get_unique_constraints("user_identities")}
    assert "uq_user_identities_provider_user" in identity_uniques


def test_orm_roundtrip_and_cascade(engine):
    db = SessionLocal()
    user_id = str(uuid.uuid4())
    username = f"uji_{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    try:
        db.add(User(id=user_id, username=username, password_hash=None, email="a@b.c", avatar_url=None))
        db.add(
            UserIdentity(
                id=str(uuid.uuid4()),
                user_id=user_id,
                provider="google",
                provider_user_id="g-123",
                email="a@b.c",
                last_login_at=now,
            )
        )
        db.add(Bookmark(user_id=user_id, series_id="s_101"))
        db.add(
            SessionToken(
                token_hash=uuid.uuid4().hex,
                user_id=user_id,
                created_at=now,
                last_seen_at=now,
                expires_at=now + timedelta(days=30),
            )
        )
        db.add(
            HandoffCode(
                code_hash=uuid.uuid4().hex,
                user_id=user_id,
                created_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )
        db.flush()

        loaded = db.get(User, user_id)
        assert loaded is not None
        assert loaded.username == username
        assert loaded.password_hash is None
        assert [b.series_id for b in loaded.bookmarks] == ["s_101"]
        assert [i.provider for i in loaded.identities] == ["google"]

        # Kolom waktu harus tz-aware (Postgres timestamptz).
        assert loaded.created_at.tzinfo is not None
    finally:
        db.rollback()
        db.close()


def test_bookmark_composite_pk_is_unique(engine):
    db = SessionLocal()
    user_id = str(uuid.uuid4())
    try:
        db.add(User(id=user_id, username=f"uji_{uuid.uuid4().hex[:8]}"))
        db.add(Bookmark(user_id=user_id, series_id="s_dup"))
        db.flush()
        db.add(Bookmark(user_id=user_id, series_id="s_dup"))
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()


def test_identity_provider_user_unique(engine):
    db = SessionLocal()
    user_a = str(uuid.uuid4())
    user_b = str(uuid.uuid4())
    try:
        db.add(User(id=user_a, username=f"uji_{uuid.uuid4().hex[:8]}"))
        db.add(User(id=user_b, username=f"uji_{uuid.uuid4().hex[:8]}"))
        db.flush()
        db.add(
            UserIdentity(
                id=str(uuid.uuid4()), user_id=user_a, provider="discord", provider_user_id="d-1"
            )
        )
        db.flush()
        db.add(
            UserIdentity(
                id=str(uuid.uuid4()), user_id=user_b, provider="discord", provider_user_id="d-1"
            )
        )
        with pytest.raises(IntegrityError):
            db.flush()
    finally:
        db.rollback()
        db.close()
