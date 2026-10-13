"""Uji alur login sosial (Google/Discord/X) tanpa jaringan: state, PKCE, return_to,
pembuatan akun (L3, L5), pemakaian ulang akun (L4), batal (L9), dan tanpa merge email.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.auth import oauth
from app.auth.state import TXN_COOKIE, unsign


class FakeOAuthTransport:
    """Transport palsu: balas token lalu profil yang sudah diprogram per penyedia."""

    def __init__(self, **profiles: Any) -> None:
        self.profiles = {k: (v if isinstance(v, list) else [v]) for k, v in profiles.items()}
        self.index = {k: 0 for k in profiles}
        self.posts: list[tuple[str, dict]] = []

    async def post_form(self, url: str, data: dict, headers: dict) -> dict:
        self.posts.append((url, data))
        return {"access_token": "tok-123"}

    async def get_json(self, url: str, headers: dict, params: dict | None = None) -> dict:
        for name, provider in oauth.PROVIDERS.items():
            if provider.profile_url == url:
                seq = self.profiles[name]
                position = min(self.index[name], len(seq) - 1)
                self.index[name] += 1
                return seq[position]
        raise AssertionError(f"tidak ada profil palsu untuk {url}")


def _query(location: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(location).query)


async def _social_login(client, provider: str, code: str = "code-1", return_to: str = "/#detail/abc"):
    start = await client.get(f"/auth/{provider}/start", params={"return_to": return_to})
    assert start.status_code == 302, start.text
    state = _query(start.headers["location"])["state"][0]
    callback = await client.get(f"/auth/{provider}/callback", params={"code": code, "state": state})
    assert callback.status_code == 302, callback.text
    location = callback.headers["location"]
    handoff = _query(location).get("handoff")
    assert handoff, location
    exchange = await client.post("/auth/handoff", json={"code": handoff[0]})
    assert exchange.status_code == 200, exchange.text
    return exchange.json(), location


# --- /start -------------------------------------------------------------------

async def test_start_builds_google_authorize_redirect(make_auth_app):
    _app, client, settings = await make_auth_app()
    resp = await client.get("/auth/google/start", params={"return_to": "/#detail/abc"})
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    query = _query(location)
    assert query["client_id"] == ["gid"]
    assert query["scope"] == ["openid email profile"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]
    assert query["redirect_uri"] == ["http://127.0.0.1:8000/auth/google/callback"]
    assert query["state"][0]

    set_cookie = resp.headers.get("set-cookie", "")
    assert f"{TXN_COOKIE}=" in set_cookie
    assert "httponly" in set_cookie.lower()
    txn = unsign(settings.auth_secret, client.cookies.get(TXN_COOKIE))
    assert txn["provider"] == "google"
    assert txn["return_to"] == "/#detail/abc"
    assert txn["state"] == query["state"][0]


async def test_discord_start_has_no_pkce(make_auth_app):
    _app, client, _settings = await make_auth_app()
    resp = await client.get("/auth/discord/start")
    query = _query(resp.headers["location"])
    assert "code_challenge" not in query
    assert query["scope"] == ["identify email"]


async def test_external_return_to_is_sanitized(make_auth_app):
    _app, client, settings = await make_auth_app()
    for hostile in ("https://evil.example/x", "//evil.example/x", "javascript:alert(1)", "\\evil"):
        resp = await client.get("/auth/google/start", params={"return_to": hostile})
        txn = unsign(settings.auth_secret, client.cookies.get(TXN_COOKIE))
        assert txn["return_to"] == "/", f"{hostile!r} tidak disanitasi"


async def test_unknown_provider_is_404(make_auth_app):
    _app, client, _settings = await make_auth_app()
    resp = await client.get("/auth/myspace/start")
    assert resp.status_code == 404
    assert resp.json()["code"] == "UNKNOWN_PROVIDER"


async def test_unconfigured_provider_redirects_with_error(make_auth_app):
    _app, client, _settings = await make_auth_app(google_client_id="")
    resp = await client.get("/auth/google/start")
    assert resp.status_code == 302
    assert resp.headers["location"].startswith("http://127.0.0.1:3000/api/auth/callback?")
    assert _query(resp.headers["location"])["error"] == ["unconfigured"]


# --- callback ----------------------------------------------------------------

async def test_callback_without_transaction_cookie_is_rejected(make_auth_app):
    _app, client, _settings = await make_auth_app()
    resp = await client.get("/auth/google/callback", params={"code": "x", "state": "y"})
    assert resp.status_code == 302
    assert _query(resp.headers["location"])["error"] == ["failed"]


async def test_callback_with_tampered_state_is_rejected(make_auth_app):
    _app, client, transport = await make_auth_app(
        transport=FakeOAuthTransport(google={"sub": "g-1", "name": "Jane"})
    )
    start = await client.get("/auth/google/start")
    del transport  # tidak dipakai
    assert start.status_code == 302
    resp = await client.get("/auth/google/callback", params={"code": "x", "state": "state-palsu"})
    assert resp.status_code == 302
    assert _query(resp.headers["location"])["error"] == ["failed"]


async def test_callback_without_state_is_rejected(make_auth_app):
    _app, client, transport = await make_auth_app(
        transport=FakeOAuthTransport(google={"sub": "g-1", "name": "Jane"})
    )
    del transport
    await client.get("/auth/google/start")
    resp = await client.get("/auth/google/callback", params={"code": "x"})
    assert _query(resp.headers["location"])["error"] == ["failed"]


async def test_callback_provider_mismatch_is_rejected(make_auth_app):
    _app, client, transport = await make_auth_app(
        transport=FakeOAuthTransport(google={"sub": "g-1", "name": "Jane"})
    )
    del transport
    start = await client.get("/auth/google/start")
    state = _query(start.headers["location"])["state"][0]
    resp = await client.get("/auth/discord/callback", params={"code": "x", "state": state})
    assert _query(resp.headers["location"])["error"] == ["failed"]


async def test_cancel_returns_cancelled(make_auth_app):
    _app, client, _settings = await make_auth_app()
    start = await client.get("/auth/google/start", params={"return_to": "/#detail/abc"})
    state = _query(start.headers["location"])["state"][0]
    resp = await client.get("/auth/google/callback", params={"error": "access_denied", "state": state})
    assert resp.status_code == 302
    query = _query(resp.headers["location"])
    assert query["error"] == ["cancelled"]
    assert query["return_to"] == ["/#detail/abc"]


# --- akun sosial -------------------------------------------------------------

async def test_first_social_login_creates_account_then_reuses_it(make_auth_app):
    transport = FakeOAuthTransport(
        google={"sub": "g-42", "name": "Jane Doe", "email": "jane@example.com", "picture": "https://img/g.png"}
    )
    _app, client, _settings = await make_auth_app(transport=transport)

    first, _loc = await _social_login(client, "google")
    assert first["user"]["username"] == "jane_doe"
    assert first["user"]["providers"] == ["google"]
    assert first["user"]["has_password"] is False
    assert first["user"]["avatar_url"] == "https://img/g.png"
    assert first["user"]["email"] == "jane@example.com"
    first_id = first["user"]["id"]

    second, _loc = await _social_login(client, "google")
    assert second["user"]["id"] == first_id
    assert second["user"]["username"] == "jane_doe"


async def test_username_collision_numbered(make_auth_app):
    transport = FakeOAuthTransport(
        google=[
            {"sub": "g-1", "name": "Jane Doe"},
            {"sub": "g-2", "name": "Jane Doe"},
        ]
    )
    _app, client, _settings = await make_auth_app(transport=transport)

    first, _ = await _social_login(client, "google")
    second, _ = await _social_login(client, "google")
    assert first["user"]["username"] == "jane_doe"
    assert second["user"]["username"] != "jane_doe"
    assert second["user"]["username"].startswith("jane_doe")
    assert len(second["user"]["username"]) <= 20
    assert second["user"]["username"].isalnum() or "_" in second["user"]["username"]


async def test_no_email_merge_across_providers(make_auth_app):
    transport = FakeOAuthTransport(
        google={"sub": "g-1", "name": "Jane", "email": "same@example.com"},
        discord={"id": "d-1", "username": "jane", "email": "same@example.com"},
    )
    _app, client, _settings = await make_auth_app(transport=transport)

    google, _ = await _social_login(client, "google")
    discord, _ = await _social_login(client, "discord")
    assert google["user"]["id"] != discord["user"]["id"]
    assert google["user"]["username"] != discord["user"]["username"]


async def test_discord_avatar_url_is_built(make_auth_app):
    transport = FakeOAuthTransport(discord={"id": "d-9", "username": "budi", "avatar": "abc123"})
    _app, client, _settings = await make_auth_app(transport=transport)
    result, _ = await _social_login(client, "discord")
    assert result["user"]["avatar_url"] == "https://cdn.discordapp.com/avatars/d-9/abc123.png"


async def test_x_profile_is_normalized(make_auth_app):
    transport = FakeOAuthTransport(
        x={"data": {"id": "x-7", "name": "Senja", "username": "senja", "profile_image_url": "https://img/x.jpg"}}
    )
    _app, client, _settings = await make_auth_app(transport=transport)
    result, _ = await _social_login(client, "x")
    assert result["user"]["username"] == "senja"
    assert result["user"]["avatar_url"] == "https://img/x.jpg"
    assert result["user"]["providers"] == ["x"]


async def test_social_login_session_works_for_me(make_auth_app):
    transport = FakeOAuthTransport(google={"sub": "g-7", "name": "Senja"})
    _app, client, _settings = await make_auth_app(transport=transport)
    result, _ = await _social_login(client, "google")
    token = result["session_token"]
    me = await client.get("/me", headers={"authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "senja"


class BrokenTokenTransport:
    """Transport yang gagal menukar kode (respons tanpa ``access_token``)."""

    async def post_form(self, url: str, data: dict, headers: dict) -> dict:
        return {}

    async def get_json(self, url: str, headers: dict, params: dict | None = None) -> dict:  # pragma: no cover
        raise AssertionError("profil tidak boleh diambil saat penukaran token gagal")


async def test_x_start_uses_pkce_and_x_endpoints(make_auth_app):
    _app, client, _settings = await make_auth_app()
    resp = await client.get("/auth/x/start")
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith("https://x.com/i/oauth2/authorize?")
    query = _query(location)
    assert query["client_id"] == ["xid"]
    assert query["scope"] == ["users.read tweet.read"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0]


async def test_callback_provider_token_failure_redirects_failed(make_auth_app):
    _app, client, _settings = await make_auth_app(transport=BrokenTokenTransport())
    start = await client.get("/auth/google/start", params={"return_to": "/#detail/abc"})
    state = _query(start.headers["location"])["state"][0]
    resp = await client.get("/auth/google/callback", params={"code": "x", "state": state})
    assert resp.status_code == 302
    query = _query(resp.headers["location"])
    assert query["error"] == ["failed"]
    assert query["return_to"] == ["/#detail/abc"]


async def test_callback_missing_code_with_valid_state_is_failed(make_auth_app):
    _app, client, _settings = await make_auth_app()
    start = await client.get("/auth/google/start")
    state = _query(start.headers["location"])["state"][0]
    resp = await client.get("/auth/google/callback", params={"state": state})
    assert resp.status_code == 302
    assert _query(resp.headers["location"])["error"] == ["failed"]


async def test_successful_callback_clears_transaction_cookie_and_returns_handoff(make_auth_app):
    transport = FakeOAuthTransport(google={"sub": "g-9", "name": "Senja"})
    _app, client, _settings = await make_auth_app(transport=transport)
    start = await client.get("/auth/google/start", params={"return_to": "/#detail/abc"})
    state = _query(start.headers["location"])["state"][0]
    assert client.cookies.get(TXN_COOKIE)
    resp = await client.get("/auth/google/callback", params={"code": "c", "state": state})
    assert resp.status_code == 302
    # Cookie transaksi dibersihkan setelah dipakai (sekali pakai).
    assert f"{TXN_COOKIE}=" in resp.headers.get("set-cookie", "")
    location = resp.headers["location"]
    assert location.startswith("http://127.0.0.1:3000/api/auth/callback?")
    handoff = _query(location)["handoff"][0]
    session = await client.post("/auth/handoff", json={"code": handoff})
    assert session.status_code == 200
    assert session.json()["user"]["username"] == "senja"


class OAuthProviderStub:
    """Server HTTP lokal yang meniru ``/token`` (POST) dan ``/userinfo`` (GET) penyedia."""

    def __init__(self) -> None:
        outer = self
        self.token_calls: list[tuple[str, dict]] = []
        self.profile_authorization: str | None = None

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: D401 - senyap saat pengujian
                return

            def _json(self, code: int, obj: dict) -> None:
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length).decode()
                outer.token_calls.append((self.path, parse_qs(raw)))
                self._json(200, {"access_token": "tok-real"})

            def do_GET(self):  # noqa: N802
                if self.path.split("?", 1)[0] == "/userinfo":
                    outer.profile_authorization = self.headers.get("authorization")
                    self._json(
                        200,
                        {
                            "sub": "g-100",
                            "name": "Budi Nyata",
                            "email": "budi@example.com",
                            "picture": "https://img/p.png",
                        },
                    )
                else:
                    self._json(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


async def test_complete_login_over_real_http_transport(make_auth_app):
    """Jalur produksi ``HttpxTransport``: tukar kode + ambil profil via HTTP nyata."""
    _app, _client, settings = await make_auth_app()
    stub = OAuthProviderStub()
    try:
        provider = oauth.Provider(
            name="google",
            label="Google",
            authorize_url=f"{stub.base}/authorize",
            token_url=f"{stub.base}/token",
            scopes="openid email profile",
            uses_pkce=True,
            client_id_attr="google_client_id",
            client_secret_attr="google_client_secret",
            profile_url=f"{stub.base}/userinfo",
        )
        transport = oauth.HttpxTransport()
        try:
            profile = await oauth.complete_login(
                provider,
                settings,
                transport,
                code="kode-1",
                code_verifier="verifier-1",
                redirect_uri="https://backend.example/auth/google/callback",
            )
        finally:
            await transport.aclose()
    finally:
        stub.stop()

    assert profile.provider == "google"
    assert profile.provider_user_id == "g-100"
    assert profile.display_name == "Budi Nyata"
    assert profile.email == "budi@example.com"
    assert profile.avatar_url == "https://img/p.png"

    assert stub.profile_authorization == "Bearer tok-real"
    path, form = stub.token_calls[0]
    assert path == "/token"
    assert form["grant_type"] == ["authorization_code"]
    assert form["code"] == ["kode-1"]
    assert form["client_id"] == ["gid"]
    assert form["client_secret"] == ["gsec"]
    assert form["code_verifier"] == ["verifier-1"]
    assert form["redirect_uri"] == ["https://backend.example/auth/google/callback"]


# --- RBAC: role admin dari allowlist ADMIN_EMAILS -------------------------------

async def test_admin_email_gets_admin_role_and_me_exposes_it(make_auth_app):
    transport = FakeOAuthTransport(
        google={"sub": "g-admin", "name": "Boss", "email": "boss@example.com", "email_verified": True}
    )
    _app, client, _settings = await make_auth_app(
        transport=transport, admin_emails=("boss@example.com",)
    )
    result, _ = await _social_login(client, "google")
    assert result["user"]["role"] == "admin"
    me = await client.get("/me", headers={"authorization": f"Bearer {result['session_token']}"})
    assert me.status_code == 200
    assert me.json()["role"] == "admin"


async def test_non_admin_email_is_user(make_auth_app):
    transport = FakeOAuthTransport(
        google={"sub": "g-plain", "name": "Plain", "email": "plain@example.com", "email_verified": True}
    )
    _app, client, _settings = await make_auth_app(
        transport=transport, admin_emails=("boss@example.com",)
    )
    result, _ = await _social_login(client, "google")
    assert result["user"]["role"] == "user"


async def test_unverified_admin_email_is_not_promoted(make_auth_app):
    transport = FakeOAuthTransport(
        google={"sub": "g-unv", "name": "Unv", "email": "boss@example.com", "email_verified": False}
    )
    _app, client, _settings = await make_auth_app(
        transport=transport, admin_emails=("boss@example.com",)
    )
    result, _ = await _social_login(client, "google")
    assert result["user"]["role"] == "user"


async def test_role_syncs_on_repeat_login(make_auth_app):
    transport = FakeOAuthTransport(
        google={"sub": "g-sync", "name": "Sync", "email": "boss@example.com", "email_verified": True}
    )
    _app, client, _settings = await make_auth_app(transport=transport)
    first, _ = await _social_login(client, "google")
    assert first["user"]["role"] == "user"

    # Login berikutnya dengan allowlist -> akun yang sama dipromosikan jadi admin.
    _app2, client2, _settings2 = await make_auth_app(
        transport=transport, admin_emails=("boss@example.com",)
    )
    second, _ = await _social_login(client2, "google")
    assert second["user"]["id"] == first["user"]["id"]
    assert second["user"]["role"] == "admin"
