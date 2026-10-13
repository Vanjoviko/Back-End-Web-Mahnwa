"""Router `app.auth` untuk login kata sandi, login sosial, sesi, dan `GET /me`.

Dipasang hanya bila ``AUTH_ENABLED=true``. Endpoint bersifat backend-murni; FE
berperan sebagai BFF (proxy) dan memegang cookie browser (PRD §1.6, keputusan
arsitektur). Token sesi hanya beredar server-to-server.
"""
from __future__ import annotations

import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from app.config import Settings
from app.database import get_db
from app.auth import oauth
from app.auth.passwords import verify_password
from app.auth.sessions import consume_handoff, create_handoff, create_session, resolve_session, revoke_session
from app.auth.state import TXN_COOKIE, TXN_TTL_SECONDS, sanitize_return_to, sign, unsign
from app.auth.throttle import LoginThrottle
from app.auth.users import find_user_by_username, get_or_create_social_user, public_user, resolve_role

ROUTE_PREFIX = ""
THROTTLE_MESSAGE = "Terlalu banyak percobaan. Coba lagi dalam 15 menit."
INVALID_CREDENTIALS_MESSAGE = "Nama pengguna atau kata sandi salah."
INVALID_SESSION_MESSAGE = "Sesi tidak valid atau sudah berakhir."


class AuthError(Exception):
    """Kesalahan auth dengan pesan siap tampil dan status HTTP."""

    def __init__(self, status: int, message: str, *, code: str = "AUTH_ERROR", retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.retry_after = retry_after


class AuthState:
    """Komponen auth milik satu instance aplikasi."""

    def __init__(self, settings: Settings, transport: oauth.OAuthTransport | None = None) -> None:
        self.settings = settings
        self.transport: oauth.OAuthTransport = transport or oauth.HttpxTransport()
        self.throttle = LoginThrottle()

    @property
    def secret(self) -> str:
        return self.settings.auth_secret or self.settings.worker_token or "dev-insecure-auth-secret"


def auth_state(request: Request) -> AuthState:
    state = getattr(request.app.state, "auth", None)
    if state is None:  # pragma: no cover - hanya bila router dipasang tanpa state
        raise AuthError(503, "Fitur login tidak aktif.")
    return state


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=200)


class HandoffBody(BaseModel):
    code: str = Field(min_length=1, max_length=200)


router = APIRouter()


def _client_ip(request: Request) -> str:
    client = request.client
    return client.host if client else ""


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def current_user(request: Request, db: DbSession = Depends(get_db)):
    state = auth_state(request)
    token = _bearer(request)
    user = resolve_session(db, token, state.settings.auth_session_ttl_days)
    if user is None:
        raise AuthError(401, INVALID_SESSION_MESSAGE, code="UNAUTHORIZED")
    return user


@router.post("/auth/login")
def login(body: LoginBody, request: Request, db: DbSession = Depends(get_db)):
    state = auth_state(request)
    username = body.username.strip().lower()
    ip = _client_ip(request)

    retry = state.throttle.retry_after(username, ip)
    if retry:
        raise AuthError(429, THROTTLE_MESSAGE, code="TOO_MANY_ATTEMPTS", retry_after=retry)

    user = find_user_by_username(db, username)
    if user is None or not verify_password(body.password, user.password_hash):
        state.throttle.record_failure(username, ip)
        raise AuthError(401, INVALID_CREDENTIALS_MESSAGE, code="INVALID_CREDENTIALS")

    state.throttle.reset(username, ip)
    token = create_session(db, user.id, state.settings.auth_session_ttl_days)
    return {"authenticated": True, "session_token": token, "user": public_user(db, user)}


@router.post("/auth/logout")
def logout(request: Request, db: DbSession = Depends(get_db)):
    revoke_session(db, _bearer(request))
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: DbSession = Depends(get_db), user=Depends(current_user)):
    return public_user(db, user)


@router.post("/auth/handoff")
def handoff(body: HandoffBody, request: Request, db: DbSession = Depends(get_db)):
    """Tukar kode handoff (dari alur sosial) dengan sesi baru; kode sekali pakai."""
    state = auth_state(request)
    user = consume_handoff(db, body.code)
    if user is None:
        raise AuthError(400, "Kode handoff tidak valid atau sudah dipakai.", code="INVALID_HANDOFF")
    token = create_session(db, user.id, state.settings.auth_session_ttl_days)
    return {"authenticated": True, "session_token": token, "user": public_user(db, user)}


def _redirect_uri(state: AuthState, provider: str) -> str:
    return f"{state.settings.auth_public_base_url}/auth/{provider}/callback"


def _frontend_error_redirect(state: AuthState, kind: str, return_to: str) -> RedirectResponse:
    base = state.settings.auth_frontend_base_url
    query = urlencode({"error": kind, "return_to": return_to})
    return RedirectResponse(f"{base}/api/auth/callback?{query}", status_code=302)


@router.get("/auth/{provider}/start")
def start(provider: str, request: Request, return_to: str = "/"):
    state = auth_state(request)
    config = oauth.PROVIDERS.get(provider)
    if config is None:
        raise AuthError(404, "Penyedia login tidak dikenal.", code="UNKNOWN_PROVIDER")
    return_to = sanitize_return_to(return_to)
    if not oauth.is_configured(state.settings, config):
        return _frontend_error_redirect(state, "unconfigured", return_to)

    oauth_state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = oauth.code_challenge_for(verifier) if config.uses_pkce else None
    url = oauth.build_authorize_url(
        config,
        client_id=oauth.client_id_for(state.settings, config),
        redirect_uri=_redirect_uri(state, provider),
        state=oauth_state,
        code_challenge=challenge,
    )
    payload = {
        "provider": provider,
        "state": oauth_state,
        "verifier": verifier,
        "return_to": return_to,
    }
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        TXN_COOKIE,
        sign(state.secret, payload),
        max_age=TXN_TTL_SECONDS,
        httponly=True,
        secure=state.settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


@router.get("/auth/{provider}/callback")
async def callback(
    provider: str,
    request: Request,
    code: str | None = None,
    error: str | None = None,
    db: DbSession = Depends(get_db),
):
    state = auth_state(request)
    provided_state = request.query_params.get("state")
    config = oauth.PROVIDERS.get(provider)
    txn = unsign(state.secret, request.cookies.get(TXN_COOKIE))
    if config is None or txn is None or txn.get("provider") != provider:
        return _frontend_error_redirect(state, "failed", "/")

    return_to = sanitize_return_to(txn.get("return_to"))
    if error:
        return _frontend_error_redirect(state, "cancelled", return_to)
    if not code or not provided_state or provided_state != txn.get("state"):
        return _frontend_error_redirect(state, "failed", return_to)

    verifier = txn.get("verifier") if config.uses_pkce else None
    try:
        profile = await oauth.complete_login(
            config,
            state.settings,
            state.transport,
            code=code,
            code_verifier=verifier,
            redirect_uri=_redirect_uri(state, provider),
        )
    except Exception:  # noqa: BLE001 - jangan bocorkan detail ke browser
        return _frontend_error_redirect(state, "failed", return_to)

    user, _created = get_or_create_social_user(
        db,
        provider=config.name,
        provider_user_id=profile.provider_user_id,
        display_name=profile.display_name,
        email=profile.email,
        avatar_url=profile.avatar_url,
        role=resolve_role(profile.email, profile.email_verified, state.settings.admin_emails),
    )
    handoff_code = create_handoff(db, user.id)

    params = urlencode({"handoff": handoff_code, "return_to": return_to})
    response = RedirectResponse(f"{state.settings.auth_frontend_base_url}/api/auth/callback?{params}", status_code=302)
    response.delete_cookie(TXN_COOKIE, path="/")
    return response


async def _handle_auth_error(_: Request, exc: AuthError) -> JSONResponse:
    headers = {"retry-after": str(exc.retry_after)} if exc.retry_after else None
    return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status, headers=headers)


def install_auth(app: FastAPI, state: AuthState) -> None:
    """Pasang state + router auth ke aplikasi."""
    app.state.auth = state
    app.add_exception_handler(AuthError, _handle_auth_error)  # type: ignore[arg-type]
    app.include_router(router)


__all__ = ["AuthError", "AuthState", "install_auth", "router"]
