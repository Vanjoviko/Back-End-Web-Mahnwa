"""FixtureAdapter: membaca fixture lokal (tanpa jaringan) untuk tes otomatis dan demo.

URL: fixture://<nama>  ->  <SCAN_FIXTURE_DIR>/<nama>.json ; halaman: fixture://<nama>/<path-relatif>.
Hanya aktif bila SCAN_ENABLE_FIXTURE=true bersama SCAN_DEV_MODE=true.
"""
from __future__ import annotations

import json
import re
from urllib.parse import quote, urlsplit

from ..errors import ScanError
from .base import AdapterContext, PageRef, ScanResult, SourceAdapter
from .manifest_schema import page_urls, parse_manifest, parse_ref

NAME_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


class FixtureAdapter(SourceAdapter):
    name = "fixture"
    label = "Fixture lokal"
    requires_allowlist = False

    def matches(self, url: str) -> bool:
        return url.lower().startswith("fixture://")

    @staticmethod
    def _name(url: str) -> str:
        name = urlsplit(url).netloc
        if not NAME_RE.match(name):
            raise ScanError("INVALID_URL", "Nama fixture tidak valid.")
        return name

    def _load(self, url: str, ctx: AdapterContext) -> dict:
        if "manifest" in ctx.cache:
            return ctx.cache["manifest"]
        name = self._name(url)
        path = ctx.settings.fixture_dir / f"{name}.json"
        if not path.is_file():
            raise ScanError("SCAN_PARSE_ERROR", f"Fixture '{name}' tidak ditemukan.", status=422)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScanError("SCAN_PARSE_ERROR", "Fixture bukan JSON yang valid.", status=422) from exc
        ctx.cache["manifest"] = data
        return data

    @staticmethod
    def _resolver(name: str):
        def resolve(ref: str) -> str:
            if "://" in ref:
                return ref
            return f"fixture://{name}/{quote(ref.lstrip('/'), safe='/')}"

        return resolve

    async def scan(self, url: str, ctx: AdapterContext) -> ScanResult:
        data = self._load(url, ctx)
        return parse_manifest(data, self._resolver(self._name(url)))

    async def list_pages(self, chapter_ref: str, ctx: AdapterContext) -> list[PageRef]:
        data = self._load(ctx.source_url, ctx)
        index, expected = parse_ref(chapter_ref)
        return [PageRef(u) for u in page_urls(data, index, expected, self._resolver(self._name(ctx.source_url)))]
