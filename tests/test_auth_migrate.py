"""Uji migrasi akun pembaca lama dari db.json FE ke tabel users BE (PRD §1.2, L6)."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app import database
from app.auth.models import User
from app.auth.passwords import hash_password, verify_password
from tools import migrate_fe_users


@pytest.fixture
def migrate_db(tmp_path, monkeypatch):
    import app.auth.models  # noqa: F401 - daftarkan tabel

    db_path = tmp_path / "migrate.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    database.reset_engine()
    database.Base.metadata.create_all(bind=database.get_engine())
    try:
        yield
    finally:
        database.reset_engine()


def _write_db_json(tmp_path, users) -> "object":
    path = tmp_path / "db.json"
    path.write_text(json.dumps({"users": users}), encoding="utf-8")
    return path


def _sample_users():
    return [
        {
            "id": "u-1",
            "username": "budi",
            "passwordHash": hash_password("secret123"),
            "role": "user",
            "createdAt": "2026-10-11T14:15:23.959Z",
        },
        {
            "id": "a-1",
            "username": "admin",
            "passwordHash": hash_password("admin-only"),
            "role": "admin",
            "createdAt": "2026-01-01T00:00:00.000Z",
        },
    ]


def test_dry_run_writes_nothing(migrate_db, tmp_path):
    path = _write_db_json(tmp_path, _sample_users())
    report = migrate_fe_users.run(path, apply=False)
    assert report["applied"] is False
    assert report["would_insert"] == 1
    assert report["inserted"] == 0
    db = database.SessionLocal()
    try:
        assert db.scalar(select(User)) is None
    finally:
        db.close()


def test_apply_preserves_id_username_and_hash(migrate_db, tmp_path):
    users = _sample_users()
    path = _write_db_json(tmp_path, users)
    report = migrate_fe_users.run(path, apply=True)
    assert report["inserted"] == 1
    assert any(reason == "peran admin" for _name, reason in report["skipped"])

    db = database.SessionLocal()
    try:
        user = db.scalar(select(User))
        assert user.id == "u-1"
        assert user.username == "budi"
        assert user.password_hash == users[0]["passwordHash"]
        assert user.created_at.year == 2026
        # Hash Node lama tetap bisa diverifikasi (L6).
        assert verify_password("secret123", user.password_hash) is True
    finally:
        db.close()


def test_apply_is_idempotent(migrate_db, tmp_path):
    path = _write_db_json(tmp_path, _sample_users())
    migrate_fe_users.run(path, apply=True)
    second = migrate_fe_users.run(path, apply=True)
    assert second["inserted"] == 0
    assert any("sudah ada" in reason for _name, reason in second["skipped"])


def test_skips_users_without_username(migrate_db, tmp_path):
    path = _write_db_json(tmp_path, [{"id": "x", "role": "user"}])
    report = migrate_fe_users.run(path, apply=True)
    assert report["inserted"] == 0
    assert any(reason == "tanpa username" for _name, reason in report["skipped"])
