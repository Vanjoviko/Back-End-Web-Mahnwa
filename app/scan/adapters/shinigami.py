"""Adapter sumber Shinigami (`shinigami.asia`) berbasis JSON API publik.

Situs `shinigami.asia` adalah SPA SvelteKit: HTML dari server hanya berisi shell
+ skeleton (tanpa data) dan dilindungi Cloudflare. Karena itu adapter ini TIDAK
mengambil HTML halaman seri, melainkan API JSON yang dipakai frontend-nya:

  GET https://api.shngm.io/v1/manga/detail/{manga_id}
  GET https://api.shngm.io/v1/chapter/{manga_id}/list?page=N&page_size=100&sort_by=chapter_number&sort_order=asc
  GET https://api.shngm.io/v1/chapter/detail/{chapter_id}

`manga_id` = UUID pada URL halaman seri `/series/{uuid}`. URL gambar disusun dari
`base_url + chapter.path + chapter.data[]` (host CDN `*.shngm.id`). Semua request
tetap lewat `GuardedHttp` (SSRF guard, jeda per host, batas ukuran).
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from ..errors import ScanError
from .base import AdapterContext, ChapterEntry, PageRef, ScanResult, SourceAdapter
from .manifest_schema import TYPE_MAP, normalize_date, normalize_released_at

API_BASE = "https://api.shngm.io/v1"
SERIES_HOST = "shinigami.asia"
# manga_id pada path `/series/<id>` (UUID pada situs aslinya; longgar agar tahan perubahan).
SERIES_ID_RE = re.compile(r"/series/([A-Za-z0-9][A-Za-z0-9_-]{5,63})")
# Pemetaan status numerik API (1/2/3) -> label Indonesia. Diverifikasi lewat filter
# API `status=completed` (semua 2) dan `status=hiatus` (semua 3).
STATUS_BY_CODE = {1: "Berjalan", 2: "Tamat", 3: "Hiatus"}
PAGE_SIZE = 100  # ukuran halaman daftar chapter yang diterima API
MAX_CHAPTERS = 5000  # pengaman agar paginasi tidak liar
MAX_JSON_BYTES = 8 * 1024 * 1024


def series_id_from_url(url: str) -> str | None:
    """Ambil `manga_id` dari URL halaman seri `/series/{uuid}` (None bila bukan)."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return None
    match = SERIES_ID_RE.search(path)
    return match.group(1) if match else None


def _text(value: Any, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _number_raw(value: Any) -> str:
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return _text(value, 32)


def _taxonomy(taxonomy: Any, key: str) -> list[dict]:
    items = taxonomy.get(key) if isinstance(taxonomy, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _taxonomy_names(taxonomy: Any, key: str) -> list[str]:
    names = []
    for item in _taxonomy(taxonomy, key):
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _format_type(taxonomy: Any) -> str | None:
    """Tipe komik dari `taxonomy.Format[].slug` (manhwa|manhua|manga)."""
    for item in _taxonomy(taxonomy, "Format"):
        mapped = TYPE_MAP.get(str(item.get("slug") or item.get("name") or "").strip().lower())
        if mapped:
            return mapped
    return None


def _status(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return STATUS_BY_CODE.get(value)
    if isinstance(value, str) and value.strip().isdigit():
        return STATUS_BY_CODE.get(int(value.strip()))
    return None


def _year(value: Any) -> int | None:
    try:
        year = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return year if 1900 <= year <= 2100 else None


class ShinigamiAdapter(SourceAdapter):
    name = "shinigami"
    label = "Shinigami"

    def matches(self, url: str) -> bool:
        try:
            host = (urlsplit(url).hostname or "").lower()
        except ValueError:
            return False
        if host != SERIES_HOST and not host.endswith("." + SERIES_HOST):
            return False
        return series_id_from_url(url) is not None

    async def _get_json(self, url: str, ctx: AdapterContext) -> dict:
        raw, _ = await ctx.http.fetch_bytes(
            url,
            max_bytes=MAX_JSON_BYTES,
            too_large=ScanError("LIMIT_BYTES", "Respons sumber terlalu besar."),
            timeout=ctx.settings.html_timeout_s,
            accept="application/json",
        )
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise ScanError("SCAN_PARSE_ERROR", "Respons sumber bukan JSON yang valid.", status=422) from exc
        if not isinstance(payload, dict):
            raise ScanError("SCAN_PARSE_ERROR", "Respons sumber tidak dikenali.", status=422)
        if payload.get("retcode") != 0:
            message = _text(payload.get("message"), 200) or "respons tidak dikenal"
            raise ScanError("HTTP_ERROR", f"Sumber menolak permintaan data ({message}).", status=502)
        return payload

    async def _fetch_chapters(self, manga_id: str, ctx: AdapterContext) -> list[ChapterEntry]:
        chapters: list[ChapterEntry] = []
        seen: set[str] = set()
        page, total_page = 1, 1
        while page <= total_page and len(chapters) < MAX_CHAPTERS:
            query = urlencode({
                "page": page,
                "page_size": PAGE_SIZE,
                "sort_by": "chapter_number",
                "sort_order": "asc",
            })
            payload = await self._get_json(f"{API_BASE}/chapter/{manga_id}/list?{query}", ctx)
            items = payload.get("data")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    chapter_id = item.get("chapter_id")
                    if not isinstance(chapter_id, str) or not chapter_id.strip() or chapter_id in seen:
                        continue
                    seen.add(chapter_id)
                    released = item.get("release_date")
                    chapters.append(
                        ChapterEntry(
                            ref=chapter_id.strip(),
                            number_raw=_number_raw(item.get("chapter_number")),
                            title=_text(item.get("chapter_title"), 140),
                            date=normalize_date(released),
                            released_at=normalize_released_at(released),
                        )
                    )
                    if len(chapters) >= MAX_CHAPTERS:
                        break
            meta = payload.get("meta")
            reported = meta.get("total_page") if isinstance(meta, dict) else None
            total_page = reported if isinstance(reported, int) and reported > 0 else page
            page += 1
        return chapters

    async def scan(self, url: str, ctx: AdapterContext) -> ScanResult:
        manga_id = series_id_from_url(url)
        if not manga_id:
            raise ScanError("INVALID_URL", "URL seri Shinigami tidak valid; harus berisi /series/<id>.", status=422)
        detail = (await self._get_json(f"{API_BASE}/manga/detail/{manga_id}", ctx)).get("data")
        if not isinstance(detail, dict):
            raise ScanError("SCAN_PARSE_ERROR", "Detail seri tidak ditemukan pada sumber.", status=422)

        taxonomy = detail.get("taxonomy")
        genres: list[str] = []
        for name in _taxonomy_names(taxonomy, "Genre"):
            if name[:40] not in genres:
                genres.append(name[:40])
        authors = _taxonomy_names(taxonomy, "Author") or _taxonomy_names(taxonomy, "Artist")
        cover = detail.get("cover_portrait_url") or detail.get("cover_image_url")

        return ScanResult(
            title=_text(detail.get("title"), 140),
            alt=_text(detail.get("alternative_title"), 140),
            synopsis=_text(detail.get("description"), 2000),
            status=_status(detail.get("status")),
            author=", ".join(authors)[:100] or None,
            type=_format_type(taxonomy),
            genres=genres[:20],
            year=_year(detail.get("release_year")),
            cover_url=cover if isinstance(cover, str) and cover.strip() else None,
            chapters=await self._fetch_chapters(manga_id, ctx),
        )

    async def list_pages(self, chapter_ref: str, ctx: AdapterContext) -> list[PageRef]:
        data = (await self._get_json(f"{API_BASE}/chapter/detail/{chapter_ref}", ctx)).get("data")
        if not isinstance(data, dict):
            raise ScanError("SCAN_PARSE_ERROR", "Data chapter tidak ditemukan pada sumber.", status=422)
        base = data.get("base_url")
        chapter = data.get("chapter")
        if not isinstance(base, str) or not base.strip() or not isinstance(chapter, dict):
            raise ScanError("SCAN_PARSE_ERROR", "Data halaman chapter tidak lengkap.", status=422)
        files = chapter.get("data")
        if not isinstance(files, list) or not files:
            raise ScanError("NO_PAGES", "Chapter tidak memiliki halaman.")
        path = chapter.get("path") if isinstance(chapter.get("path"), str) else ""
        folder = base.strip().rstrip("/") + "/" + path.lstrip("/")
        if not folder.endswith("/"):
            folder += "/"
        pages: list[PageRef] = []
        for name in files:
            if isinstance(name, str) and name.strip():
                pages.append(PageRef(url=urljoin(folder, name.strip())))
        return pages
