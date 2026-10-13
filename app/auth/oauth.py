"""Login sosial: definisi penyedia, alur authorize, dan penukaran kode.

Desain:

* Daftar penyedia statis (Google, Discord, X) dengan endpoint, cakupan minimal,
  dan dukungan PKCE.
* ``OAuthTransport`` memisahkan I/O jaringan. Produksi memakai ``HttpxTransport``;
  pengujian menyuntikkan transport palsu lewat ``AuthState`` sehingga tidak
  pernah menyentuh jaringan.
* Client secret hanya dibaca dari ``Settings`` (environment) dan hanya dipakai
  di sisi server (PRD §10, §11).
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.config import Settings


@dataclass(frozen=True)
class Provider:
    name: str
    label: str
    authorize_url: str
    token_url: str
    scopes: str
    uses_pkce: bool
    client_id_attr: str
    client_secret_attr: str
    profile_url: str | None = None
    profile_params: dict[str, str] | None = None


PROVIDERS: dict[str, Provider] = {
    "google": Provider(
        name="google",
        label="Google",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes="openid email profile",
        uses_pkce=True,
        client_id_attr="google_client_id",
        client_secret_attr="google_client_secret",
        profile_url="https://openidconnect.googleapis.com/v1/userinfo",
    ),
    "discord": Provider(
        name="discord",
        label="Discord",
        authorize_url="https://discord.com/oauth2/authorize",
        token_url="https://discord.com/api/oauth2/token",
        scopes="identify email",
        uses_pkce=False,
        client_id_attr="discord_client_id",
        client_secret_attr="discord_client_secret",
        profile_url="https://discord.com/api/users/@me",
    ),
    "x": Provider(
        name="x",
        label="X",
        authorize_url="https://x.com/i/oauth2/authorize",
        token_url="https://api.x.com/2/oauth2/token",
        scopes="users.read tweet.read",
        uses_pkce=True,
        client_id_attr="x_client_id",
        client_secret_attr="x_client_secret",
        profile_url="https://api.x.com/2/users/me",
        profile_params={"user.fields": "profile_image_url"},
    ),
}


@dataclass(frozen=True)
class Profile:
    """Profil dasar yang dibutuhkan untuk membuat/menemukan akun (PRD §7 langkah 5)."""

    provider: str
    provider_user_id: str
    display_name: str | None
    email: str | None
    avatar_url: str | None
    # Hanya email yang terverifikasi penyedia boleh dipakai untuk penetapan role admin.
    email_verified: bool = False


class OAuthTransport(Protocol):
    """I/O jaringan minimal yang dibutuhkan alur OAuth."""

    async def post_form(self, url: str, data: dict[str, str], headers: dict[str, str]) -> dict[str, Any]:
        ...

    async def get_json(
        self, url: str, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        ...


class HttpxTransport:
    """Transport jaringan nyata (produksi)."""

    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def post_form(self, url: str, data: dict[str, str], headers: dict[str, str]) -> dict[str, Any]:
        response = await self._client.post(url, data=data, headers={"accept": "application/json", **headers})
        response.raise_for_status()
        return response.json()

    async def get_json(
        self, url: str, headers: dict[str, str], params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        response = await self._client.get(url, headers={"accept": "application/json", **headers}, params=params)
        response.raise_for_status()
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()


def client_id_for(settings: Settings, provider: Provider) -> str:
    return str(getattr(settings, provider.client_id_attr, "") or "")


def client_secret_for(settings: Settings, provider: Provider) -> str:
    return str(getattr(settings, provider.client_secret_attr, "") or "")


def is_configured(settings: Settings, provider: Provider) -> bool:
    """Penyedia dianggap aktif bila client id (dan rahasianya) tersedia."""
    return bool(client_id_for(settings, provider) and client_secret_for(settings, provider))


def code_challenge_for(verifier: str) -> str:
    """PKCE S256 (PRD §10)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def build_authorize_url(
    provider: Provider,
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str | None,
) -> str:
    params: dict[str, str] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": provider.scopes,
        "state": state,
    }
    if provider.name == "google":
        params.update({"access_type": "online", "prompt": "select_account"})
    if code_challenge:
        params.update({"code_challenge": code_challenge, "code_challenge_method": "S256"})
    return f"{provider.authorize_url}?{urlencode(params)}"


async def complete_login(
    provider: Provider,
    settings: Settings,
    transport: OAuthTransport,
    *,
    code: str,
    code_verifier: str | None,
    redirect_uri: str,
) -> Profile:
    """Tukar kode dengan token lalu ambil profil dasar penyedia."""
    data: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id_for(settings, provider),
    }
    secret = client_secret_for(settings, provider)
    if secret:
        data["client_secret"] = secret
    if code_verifier:
        data["code_verifier"] = code_verifier

    token = await transport.post_form(provider.token_url, data, {})
    access_token = token.get("access_token")
    if not access_token:
        raise OAuthError("Penyedia tidak mengembalikan access token.")
    if not provider.profile_url:
        raise OAuthError("Penyedia tidak punya endpoint profil.")

    headers = {"authorization": f"Bearer {access_token}"}
    raw = await transport.get_json(provider.profile_url, headers, provider.profile_params)
    return _normalize(provider, raw)


class OAuthError(RuntimeError):
    """Kegagalan alur OAuth (pesan aman untuk dicatat, bukan untuk browser)."""


def _normalize(provider: Provider, raw: dict[str, Any]) -> Profile:
    name = provider.name
    if name == "google":
        user_id = str(raw.get("sub") or "")
        display = raw.get("name") or raw.get("email")
        email = raw.get("email")
        avatar = raw.get("picture")
        verified = bool(raw.get("email_verified"))
    elif name == "discord":
        user_id = str(raw.get("id") or "")
        display = raw.get("global_name") or raw.get("username")
        email = raw.get("email")
        verified = bool(raw.get("verified"))
        avatar_hash = raw.get("avatar")
        avatar = (
            f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png"
            if user_id and avatar_hash
            else None
        )
    elif name == "x":
        data = raw.get("data") or {}
        user_id = str(data.get("id") or "")
        display = data.get("name") or data.get("username")
        email = data.get("email")
        avatar = data.get("profile_image_url")
        verified = False  # X tidak mengembalikan email terverifikasi pada scope dasar.
    else:  # pragma: no cover - penyedia tak dikenal
        raise OAuthError(f"Penyedia {name!r} tidak dikenal.")

    if not user_id:
        raise OAuthError(f"Profil dari {name} tidak memuat pengenal pengguna.")

    return Profile(
        provider=name,
        provider_user_id=user_id,
        display_name=str(display).strip() if display else None,
        email=str(email).strip() if email else None,
        avatar_url=str(avatar).strip() if avatar else None,
        email_verified=verified,
    )


__all__ = [
    "PROVIDERS",
    "HttpxTransport",
    "OAuthError",
    "OAuthTransport",
    "Profile",
    "Provider",
    "build_authorize_url",
    "client_secret_for",
    "client_id_for",
    "code_challenge_for",
    "complete_login",
    "is_configured",
]
