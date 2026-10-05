"""Klien HTTP ter-guard: SSRF guard + IP pinning, validasi tiap hop redirect,
pembatas konkurensi/jeda per host (NFR-04), deteksi penolakan sumber (BE-14),
dan unduhan gambar streaming dengan batas ukuran (NFR-01, BE-05).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import AsyncIterator, Mapping
from urllib.parse import urljoin, urlsplit

import httpx

from ..config import Settings
from . import imaging
from .errors import ScanError, redact_url
from .netguard import GuardPolicy, check_url, resolve_target

log = logging.getLogger("scan.http")

REFUSAL_STATUSES = {401, 403, 407, 451}
SAFE_HEADER_NAMES = {"referer", "accept", "accept-language"}
CHUNK = 64 * 1024


class Breaker:
    """Circuit-breaker per job: setelah sumber menolak, tidak ada request lagi ke host itu."""

    def __init__(self) -> None:
        self.tripped: dict[str, str] = {}

    def check(self, host: str) -> None:
        if host in self.tripped:
            raise ScanError("SOURCE_REFUSED", self.tripped[host], status=403)

    def trip(self, host: str, message: str) -> None:
        self.tripped.setdefault(host, message)


class ByteBudget:
    """Total byte per chapter; dicek saat streaming agar berhenti begitu melewati batas."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0

    def add(self, n: int) -> None:
        self.used += n
        if self.used > self.limit:
            raise ScanError(
                "LIMIT_CHAPTER_BYTES",
                f"Total ukuran chapter melebihi batas {self.limit // (1024 * 1024)} MB.",
            )

    def sub(self, n: int) -> None:
        self.used = max(0, self.used - n)


class HostLimiter:
    def __init__(self, max_concurrency: int, min_delay_ms: int) -> None:
        self.max_concurrency = max_concurrency
        self.min_delay = min_delay_ms / 1000
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        sem = self._sems.setdefault(host, asyncio.Semaphore(self.max_concurrency))
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with sem:
            if self.min_delay > 0:
                async with lock:
                    wait = self._last.get(host, 0) + self.min_delay - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    self._last[host] = time.monotonic()
            yield


@dataclass
class DownloadedImage:
    path: Path
    fmt: imaging.ImageFormat
    size: int
    sha256: str


class _Response:
    """Pembungkus tipis agar jalur fixture lokal dan httpx memakai antarmuka yang sama."""

    def __init__(self, status: int, headers: Mapping[str, str], chunks, closer):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self._chunks = chunks
        self._closer = closer

    def chunks(self):
        return self._chunks

    async def aclose(self) -> None:
        await self._closer()


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


class GuardedHttp:
    def __init__(self, settings: Settings, policy: GuardPolicy, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.policy = policy
        self.limiter = HostLimiter(settings.host_max_concurrency, settings.host_min_delay_ms)
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(settings.image_timeout_s, connect=settings.connect_timeout_s),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    # ------------------------------------------------------------------ inti
    def _headers(self, extra: Mapping[str, str] | None, accept: str) -> dict[str, str]:
        headers = {"User-Agent": self.settings.http_user_agent, "Accept": accept}
        for key, value in (extra or {}).items():
            if key.lower() in SAFE_HEADER_NAMES and isinstance(value, str):
                headers[key] = value
        return headers

    async def _send(self, target, ips: list[str], headers: dict[str, str]) -> httpx.Response:
        last_error: Exception | None = None
        for ip in ips:
            host = f"[{ip}]" if ":" in ip else ip
            pinned = target.split._replace(netloc=f"{host}:{target.port}").geturl()
            request_headers = dict(headers)
            request_headers["Host"] = target.hostport
            extensions = {"sni_hostname": target.host} if target.scheme == "https" else {}
            request = self.client.build_request("GET", pinned, headers=request_headers, extensions=extensions)
            try:
                return await self.client.send(request, stream=True)
            except httpx.ConnectError as exc:
                last_error = exc
                continue
        raise last_error or httpx.ConnectError("tidak ada alamat yang dapat dihubungi")

    def _fixture_response(self, url: str) -> _Response:
        if not self.settings.enable_fixture:
            raise ScanError("INVALID_URL", "Sumber fixture tidak diaktifkan.")
        parts = urlsplit(url)
        name = parts.netloc
        relative = parts.path.lstrip("/")
        root = (self.settings.fixture_dir / name).resolve()
        target = (root / relative).resolve()
        if not name or root not in target.parents or not target.is_file():
            raise ScanError("HTTP_ERROR", "Berkas fixture tidak ditemukan.", status=404)

        async def gen():
            with open(target, "rb") as handle:
                while chunk := handle.read(CHUNK):
                    yield chunk

        async def closer() -> None:
            return None

        return _Response(200, {}, gen(), closer)

    @asynccontextmanager
    async def open(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        accept: str = "*/*",
        breaker: Breaker | None = None,
    ) -> AsyncIterator[_Response]:
        if url.lower().startswith("fixture://"):
            response = self._fixture_response(url)
            try:
                yield response
            finally:
                await response.aclose()
            return

        current = url
        for _ in range(self.settings.max_redirects + 1):
            target = check_url(current, self.policy)
            ips = await resolve_target(target, self.policy)
            if breaker:
                breaker.check(target.host)
            async with self.limiter.slot(target.host):
                try:
                    raw = await self._send(target, ips, self._headers(headers, accept))
                except httpx.TimeoutException as exc:
                    raise ScanError("TIMEOUT", "Sumber tidak merespons dalam batas waktu.", status=504, retryable=True) from exc
                except httpx.TransportError as exc:
                    raise ScanError("NETWORK_ERROR", "Koneksi ke sumber gagal.", status=502, retryable=True) from exc
                closed = False

                async def closer(raw=raw) -> None:
                    nonlocal closed
                    if not closed:
                        closed = True
                        await raw.aclose()

                try:
                    status = raw.status_code
                    if status in (301, 302, 303, 307, 308) and raw.headers.get("location"):
                        current = urljoin(current, raw.headers["location"])
                        await closer()
                        continue
                    self._raise_for_status(status, raw.headers, target.host, breaker)

                    async def gen(raw=raw):
                        try:
                            async for chunk in raw.aiter_bytes(CHUNK):
                                yield chunk
                        except httpx.TimeoutException as exc:
                            raise ScanError("TIMEOUT", "Sumber berhenti mengirim data.", status=504, retryable=True) from exc
                        except httpx.TransportError as exc:
                            raise ScanError("NETWORK_ERROR", "Koneksi ke sumber terputus.", status=502, retryable=True) from exc

                    yield _Response(status, raw.headers, gen(), closer)
                finally:
                    await closer()
                return
        raise ScanError("TOO_MANY_REDIRECTS", "Terlalu banyak pengalihan (redirect) dari sumber.", status=502)

    @staticmethod
    def _raise_for_status(status: int, headers, host: str, breaker: Breaker | None) -> None:
        if 200 <= status < 300:
            return
        retry_after = parse_retry_after(headers.get("retry-after"))
        if status in REFUSAL_STATUSES:
            message = f"Sumber menolak akses (HTTP {status})."
            if retry_after is not None:
                message += f" Retry-After: {int(retry_after)} detik."
            if breaker:
                breaker.trip(host, message)
            raise ScanError("SOURCE_REFUSED", message, status=403, retry_after=retry_after)
        if status == 429:
            raise ScanError("RATE_LIMITED", "Sumber membatasi laju permintaan (HTTP 429).", status=429, retryable=True, retry_after=retry_after)
        if status >= 500:
            raise ScanError("HTTP_ERROR", f"Sumber membalas HTTP {status}.", status=502, retryable=True, retry_after=retry_after)
        raise ScanError("HTTP_ERROR", f"Sumber membalas HTTP {status}.", status=502)

    # ------------------------------------------------------------ pembantu
    async def fetch_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        too_large: ScanError,
        timeout: float,
        accept: str = "*/*",
        headers: Mapping[str, str] | None = None,
        breaker: Breaker | None = None,
    ) -> tuple[bytes, Mapping[str, str]]:
        async def run() -> tuple[bytes, Mapping[str, str]]:
            async with self.open(url, headers=headers, accept=accept, breaker=breaker) as resp:
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise too_large
                buf = bytearray()
                async for chunk in resp.chunks():
                    buf += chunk
                    if len(buf) > max_bytes:
                        raise too_large
                return bytes(buf), resp.headers

        try:
            return await asyncio.wait_for(run(), timeout)
        except asyncio.TimeoutError as exc:
            raise ScanError("TIMEOUT", "Sumber tidak merespons dalam batas waktu.", status=504, retryable=True) from exc

    async def download_image(
        self,
        url: str,
        directory: Path,
        stem: str,
        *,
        max_bytes: int,
        budget: ByteBudget | None = None,
        headers: Mapping[str, str] | None = None,
        breaker: Breaker | None = None,
    ) -> DownloadedImage:
        """Unduh satu gambar secara streaming ke `directory/stem.<ext>` (ext dari magic bytes)."""
        directory.mkdir(parents=True, exist_ok=True)
        part = directory / f"{stem}.part"
        charged = 0

        async def run() -> DownloadedImage:
            nonlocal charged
            digest = hashlib.sha256()
            size = 0
            head = b""
            fmt: imaging.ImageFormat | None = None
            with open(part, "wb") as handle:
                async with self.open(url, headers=headers, accept="image/jpeg,image/png,image/webp,*/*;q=0.5", breaker=breaker) as resp:
                    declared = resp.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise ScanError("LIMIT_IMAGE_BYTES", f"Ukuran gambar melebihi {max_bytes // (1024 * 1024)} MB.")
                    content_type = resp.headers.get("content-type")
                    async for chunk in resp.chunks():
                        if fmt is None:
                            head += chunk
                            if len(head) < imaging.MIN_SNIFF_BYTES:
                                continue
                            fmt = imaging.sniff(head)
                            if fmt is None:
                                self._raise_not_image(head, content_type, breaker, url)
                            if imaging.content_type_conflicts(content_type, fmt):
                                raise ScanError("UNSUPPORTED_IMAGE_FORMAT", "Content-Type tidak sesuai dengan isi gambar.")
                            chunk, head = head, b""
                        size += len(chunk)
                        if size > max_bytes:
                            raise ScanError("LIMIT_IMAGE_BYTES", f"Ukuran gambar melebihi {max_bytes // (1024 * 1024)} MB.")
                        if budget:
                            budget.add(len(chunk))
                            charged += len(chunk)
                        digest.update(chunk)
                        handle.write(chunk)
                    if fmt is None:
                        if size == 0 and not head:
                            raise ScanError("UNSUPPORTED_IMAGE_FORMAT", "Respons gambar kosong.")
                        self._raise_not_image(head, content_type, breaker, url)
            final = directory / f"{stem}{fmt.extension}"
            part.replace(final)
            return DownloadedImage(final, fmt, size, digest.hexdigest())

        try:
            return await asyncio.wait_for(run(), self.settings.image_timeout_s)
        except asyncio.TimeoutError as exc:
            raise ScanError("TIMEOUT", "Unduhan gambar melewati batas waktu.", status=504, retryable=True) from exc
        except BaseException:
            if budget and charged:
                budget.sub(charged)
            part.unlink(missing_ok=True)
            raise

    def _raise_not_image(self, sample: bytes, content_type: str | None, breaker: Breaker | None, url: str) -> None:
        if imaging.looks_like_challenge(sample):
            host = urlsplit(url).hostname or ""
            message = "Sumber menampilkan halaman tantangan (CAPTCHA/anti-bot); unduhan dihentikan."
            if breaker:
                breaker.trip(host.lower(), message)
            raise ScanError("SOURCE_REFUSED", message, status=403)
        log.info("bukan_gambar url=%s content_type=%s", redact_url(url), content_type)
        raise ScanError("UNSUPPORTED_IMAGE_FORMAT", "Format gambar tidak didukung (hanya JPEG, PNG, dan WebP).")
