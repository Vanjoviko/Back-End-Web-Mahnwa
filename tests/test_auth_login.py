"""Uji login kata sandi, sesi server, `GET /me`, logout, dan handoff (PRD L6–L8, L11, L12)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app import database
from app.auth import sessions
from app.auth.models import SessionToken, User
from app.auth.passwords import hash_password
from app.auth.api import INVALID_CREDENTIALS_MESSAGE, INVALID_SESSION_MESSAGE, THROTTLE_MESSAGE


def _create_user(username: str = "budi", password: str = "secret123") -> str:
    db = database.SessionLocal()
    try:
        user = User(
            id=str(uuid.uuid4()),
            username=username,
            password_hash=hash_password(password),
            created_at=datetime.now(timezone.utc),
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


async def _login(client, username: str, password: str):
    return await client.post("/auth/login", json={"username": username, "password": password})


async def test_me_requires_session(make_auth_app):
    _app, client, _settings = await make_auth_app()
    resp = await client.get("/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "UNAUTHORIZED"
    assert resp.json()["error"] == INVALID_SESSION_MESSAGE


async def test_password_login_then_me(make_auth_app):
    _create_user("budi", "secret123")
    _app, client, _settings = await make_auth_app()

    resp = await _login(client, "budi", "secret123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["authenticated"] is True
    token = body["session_token"]
    assert body["user"]["username"] == "budi"
    assert body["user"]["has_password"] is True
    assert body["user"]["providers"] == []
    assert "password_hash" not in body["user"]

    me = await client.get("/me", headers={"authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "budi"


async def test_login_is_case_insensitive_on_username(make_auth_app):
    _create_user("budi", "secret123")
    _app, client, _settings = await make_auth_app()
    resp = await _login(client, "BUDI", "secret123")
    assert resp.status_code == 200


async def test_wrong_password_uses_generic_message(make_auth_app):
    _create_user("budi", "secret123")
    _app, client, _settings = await make_auth_app()
    resp = await _login(client, "budi", "salah-banget")
    assert resp.status_code == 401
    assert resp.json()["error"] == INVALID_CREDENTIALS_MESSAGE


async def test_unknown_user_same_generic_message(make_auth_app):
    _app, client, _settings = await make_auth_app()
    resp = await _login(client, "tidak-ada", "apa-saja")
    assert resp.status_code == 401
    assert resp.json()["error"] == INVALID_CREDENTIALS_MESSAGE


async def test_throttle_after_five_failures(make_auth_app):
    _create_user("budi", "secret123")
    _app, client, _settings = await make_auth_app()
    for _ in range(5):
        resp = await _login(client, "budi", "salah")
        assert resp.status_code == 401
    blocked = await _login(client, "budi", "salah")
    assert blocked.status_code == 429
    assert blocked.json()["error"] == THROTTLE_MESSAGE
    assert blocked.headers.get("retry-after")
    # Bahkan kata sandi benar ikut ditolak selama jendela pembatasan berjalan.
    still_blocked = await _login(client, "budi", "secret123")
    assert still_blocked.status_code == 429


async def test_successful_login_resets_throttle(make_auth_app):
    _create_user("budi", "secret123")
    _app, client, _settings = await make_auth_app()
    for _ in range(4):
        assert (await _login(client, "budi", "salah")).status_code == 401
    assert (await _login(client, "budi", "secret123")).status_code == 200
    for _ in range(4):
        assert (await _login(client, "budi", "salah")).status_code == 401
    assert (await _login(client, "budi", "salah")).status_code == 401  # 5 gagal
    assert (await _login(client, "budi", "salah")).status_code == 429  # ke-6 ditolak


async def test_logout_revokes_session(make_auth_app):
    _create_user("budi", "secret123")
    _app, client, _settings = await make_auth_app()
    token = (await _login(client, "budi", "secret123")).json()["session_token"]
    headers = {"authorization": f"Bearer {token}"}

    assert (await client.get("/me", headers=headers)).status_code == 200
    assert (await client.post("/auth/logout", headers=headers)).status_code == 200
    assert (await client.get("/me", headers=headers)).status_code == 401


async def test_expired_session_rejected_and_removed(make_auth_app):
    _create_user("budi", "secret123")
    _app, client, _settings = await make_auth_app()
    token = (await _login(client, "budi", "secret123")).json()["session_token"]

    db = database.SessionLocal()
    try:
        row = db.scalar(select(SessionToken))
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
        assert db.scalar(select(SessionToken)) is not None
    finally:
        db.close()

    resp = await client.get("/me", headers={"authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    db = database.SessionLocal()
    try:
        assert db.scalar(select(SessionToken)) is None
    finally:
        db.close()


async def test_handoff_exchange_is_single_use(make_auth_app):
    user_id = _create_user("budi", "secret123")
    _app, client, _settings = await make_auth_app()

    db = database.SessionLocal()
    try:
        code = sessions.create_handoff(db, user_id)
    finally:
        db.close()

    first = await client.post("/auth/handoff", json={"code": code})
    assert first.status_code == 200
    assert first.json()["user"]["username"] == "budi"
    token = first.json()["session_token"]
    assert (await client.get("/me", headers={"authorization": f"Bearer {token}"})).status_code == 200

    second = await client.post("/auth/handoff", json={"code": code})
    assert second.status_code == 400
    assert second.json()["code"] == "INVALID_HANDOFF"


async def test_handoff_unknown_code_rejected(make_auth_app):
    _app, client, _settings = await make_auth_app()
    resp = await client.post("/auth/handoff", json={"code": "tidak-ada"})
    assert resp.status_code == 400


async def test_logout_without_session_is_idempotent(make_auth_app):
    _app, client, _settings = await make_auth_app()
    resp = await client.post("/auth/logout")
    assert resp.status_code == 200
