"""Alur scan: validasi URL -> adapter -> metadata ternormalisasi + cover ter-stage (BE-03)."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..config import Settings
from . import chapter_numbers, imaging
from .adapters import AdapterContext, AdapterRegistry
from .errors import ScanError, redact_url
from .http import GuardedHttp
from .netguard import GuardPolicy, check_url
from .staging import Staging

log = logging.getLogger("scan.service")
TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|igshid|yclid|_ga)$", re.IGNORECASE)


def canonical_url(url: str) -> str:
    """Host lowercase, tanpa fragment/query pelacakan, tanpa trailing slash (FR-14)."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or parts.netloc).lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"
    default = {"https": 443, "http": 80}.get(scheme)
    port = f":{parts.port}" if parts.port and parts.port != default else ""
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)))
    return urlunsplit((scheme, f"{host}{port}", path, query, ""))


def make_policy(settings: Settings, resolver=None) -> GuardPolicy:
    kwargs = {"resolver": resolver} if resolver else {}
    return GuardPolicy(
        allowed_hosts=settings.allowed_hosts,
        allow_http=settings.dev_mode,
        allow_private_for_allowlisted=settings.dev_mode,
        **kwargs,
    )


def validate_scan_url(url: str, settings: Settings, policy: GuardPolicy) -> None:
    """Validasi statis tanpa request keluar (FR-02)."""
    if isinstance(url, str) and url.lower().startswith("fixture://"):
        if not settings.enable_fixture:
            raise ScanError("INVALID_URL", "URL harus memakai HTTPS.")
        return
    check_url(url, policy, require_allowlist=False)


async def stage_cover(cover_url: str | None, *, settings: Settings, http: GuardedHttp, staging: Staging, warnings: list[str]) -> dict | None:
    if not cover_url:
        warnings.append("COVER_UNAVAILABLE")
        return None
    try:
        limit = settings.max_cover_bytes
        data, headers = await http.fetch_bytes(
            cover_url,
            max_bytes=limit,
            too_large=ScanError("COVER_TOO_LARGE", f"Sampul melebihi {limit // 1_000_000} MB."),
            timeout=settings.image_timeout_s,
            accept="image/jpeg,image/png,image/webp",
        )
        fmt = imaging.sniff(data[:16])
        if fmt is None or imaging.content_type_conflicts(headers.get("content-type"), fmt):
            raise ScanError("UNSUPPORTED_IMAGE_FORMAT", "Sampul bukan JPEG/PNG/WebP yang valid.")
        staged_id = staging.save_cover(data, fmt)
        return {"staged_id": staged_id, "content_type": fmt.content_type, "bytes": len(data)}
    except ScanError as exc:
        log.info("cover_gagal code=%s url=%s", exc.code, redact_url(cover_url))
        warnings.append("COVER_UNAVAILABLE")
        if exc.code in {"SSRF_BLOCKED", "HOST_NOT_ALLOWED"}:
            warnings.append(exc.code)
        return None


async def run_scan(url: str, *, settings: Settings, registry: AdapterRegistry, http: GuardedHttp, staging: Staging) -> dict:
    policy = http.policy
    validate_scan_url(url, settings, policy)
    adapter = registry.resolve(url)

    async def work() -> dict:
        ctx = AdapterContext(http=http, settings=settings, logger=log, source_url=url)
        result = await adapter.scan(url, ctx)
        if not (result.title or "").strip():
            raise ScanError("SCAN_PARSE_ERROR", "Judul komik tidak ditemukan pada sumber.", status=422)
        warnings: list[str] = []
        cover = await stage_cover(result.cover_url, settings=settings, http=http, staging=staging, warnings=warnings)
        parsed = chapter_numbers.analyze([c.number_raw or c.title for c in result.chapters])
        chapters = [
            {
                "ref": entry.ref,
                "number_raw": entry.number_raw,
                "number": p.number,
                "title": entry.title,
                "date": entry.date,
                "issues": p.issues,
            }
            for entry, p in zip(result.chapters, parsed)
        ]
        chapters = chapter_numbers.sort_desc(chapters)
        if not chapters:
            warnings.append("NO_CHAPTERS")
        if not result.permission_note:
            warnings.append("PERMISSION_NOT_RECORDED")
        return {
            "adapter": adapter.name,
            "adapter_label": adapter.label,
            "canonical_url": canonical_url(url),
            "comic": {
                "title": result.title,
                "alt": result.alt,
                "synopsis": result.synopsis,
                "status": result.status,
                "author": result.author,
                "type": result.type,
                "genres": result.genres,
                "year": result.year,
            },
            "cover": cover,
            "chapters": chapters,
            "permission_note": result.permission_note,
            "warnings": warnings,
        }

    try:
        return await asyncio.wait_for(work(), settings.scan_timeout_s)
    except ScanError as exc:
        if exc.code == "TIMEOUT":
            raise ScanError("SCAN_TIMEOUT", "Sumber tidak merespons dalam batas waktu scan.", status=504) from exc
        raise
    except asyncio.TimeoutError as exc:
        raise ScanError("SCAN_TIMEOUT", "Scan melewati batas waktu; sumber tidak merespons.", status=504) from exc
