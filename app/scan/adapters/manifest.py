"""ManifestAdapter: sumber yang mempublikasikan manifest JSON terstruktur dan diizinkan Jovan.

Diakses lewat HTTP ter-guard dari host di SCAN_ALLOWED_HOSTS. Semua URL cover/halaman
dalam manifest dicek terhadap guard SSRF + allowlist oleh klien HTTP saat diunduh.
"""
from __future__ import annotations

import json
from urllib.parse import urljoin, urlsplit

from ..errors import ScanError
from .base import AdapterContext, PageRef, ScanResult, SourceAdapter
from .manifest_schema import page_urls, parse_manifest, parse_ref


class ManifestAdapter(SourceAdapter):
    name = "manifest"
    label = "Manifest JSON"
    requires_allowlist = True

    def matches(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.scheme.lower() in {"http", "https"} and parts.path.lower().endswith(".json")

    async def _load(self, url: str, ctx: AdapterContext) -> dict:
        cached = ctx.cache.get("manifest")
        if cached is not None:
            return cached
        limit = ctx.settings.max_manifest_bytes
        body, _ = await ctx.http.fetch_bytes(
            url,
            max_bytes=limit,
            too_large=ScanError("SCAN_PARSE_ERROR", f"Manifest melebihi batas {limit // 1_000_000} MB.", status=422),
            timeout=ctx.settings.html_timeout_s,
            accept="application/json",
            headers=self.fetch_headers(ctx),
        )
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ScanError("SCAN_PARSE_ERROR", "Manifest bukan JSON yang valid.", status=422) from exc
        if not isinstance(data, dict):
            raise ScanError("SCAN_PARSE_ERROR", "Manifest harus berupa objek JSON.", status=422)
        ctx.cache["manifest"] = data
        return data

    async def scan(self, url: str, ctx: AdapterContext) -> ScanResult:
        data = await self._load(url, ctx)
        return parse_manifest(data, lambda ref: urljoin(url, ref))

    async def list_pages(self, chapter_ref: str, ctx: AdapterContext) -> list[PageRef]:
        data = await self._load(ctx.source_url, ctx)
        index, expected = parse_ref(chapter_ref)
        return [PageRef(u) for u in page_urls(data, index, expected, lambda ref: urljoin(ctx.source_url, ref))]
