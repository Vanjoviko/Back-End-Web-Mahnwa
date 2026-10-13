"""Uji parsing & validasi konfigurasi database dan login sosial (`Settings`)."""
from __future__ import annotations

import pytest

from app.config import Settings


def test_from_env_parses_db_and_auth():
    s = Settings.from_env(
        {
            "WORKER_TOKEN": "t",
            "DATABASE_URL": "postgresql+psycopg://u:p@localhost:5432/db",
            "AUTH_ENABLED": "true",
            "AUTH_FRONTEND_BASE_URL": "https://fe.example/",
            "AUTH_PUBLIC_BASE_URL": "https://be.example/",
            "AUTH_SESSION_TTL_DAYS": "7",
            "AUTH_COOKIE_SECURE": "true",
            "GOOGLE_CLIENT_ID": "gid",
            "GOOGLE_CLIENT_SECRET": "gsec",
            "DISCORD_CLIENT_ID": "did",
            "DISCORD_CLIENT_SECRET": "dsec",
            "X_CLIENT_ID": "xid",
            "X_CLIENT_SECRET": "xsec",
        }
    )
    assert s.database_url == "postgresql+psycopg://u:p@localhost:5432/db"
    assert s.auth_enabled is True
    assert s.auth_frontend_base_url == "https://fe.example"  # trailing slash dibuang
    assert s.auth_public_base_url == "https://be.example"
    assert s.auth_session_ttl_days == 7
    assert s.auth_cookie_secure is True
    assert (s.google_client_id, s.google_client_secret) == ("gid", "gsec")
    assert (s.discord_client_id, s.discord_client_secret) == ("did", "dsec")
    assert (s.x_client_id, s.x_client_secret) == ("xid", "xsec")


def test_auth_defaults_off():
    s = Settings.from_env({"WORKER_TOKEN": "t"})
    assert s.auth_enabled is False
    assert s.database_url == ""
    assert s.auth_session_ttl_days == 30
    assert s.auth_cookie_secure is False


def test_validate_requires_base_urls_when_auth_enabled():
    with pytest.raises(RuntimeError, match="AUTH_FRONTEND_BASE_URL"):
        Settings(worker_token="t", auth_enabled=True).validate()


def test_validate_requires_secure_cookie_outside_dev():
    with pytest.raises(RuntimeError, match="AUTH_COOKIE_SECURE"):
        Settings(
            worker_token="t",
            auth_enabled=True,
            auth_frontend_base_url="https://fe",
            auth_public_base_url="https://be",
            auth_cookie_secure=False,
            dev_mode=False,
        ).validate()
    # dev_mode melewati aturan Secure.
    Settings(
        worker_token="t",
        auth_enabled=True,
        auth_frontend_base_url="https://fe",
        auth_public_base_url="https://be",
        auth_cookie_secure=False,
        dev_mode=True,
    ).validate()
