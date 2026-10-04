"""Normalisasi manifest JSON bersama untuk ManifestAdapter dan FixtureAdapter.

Skema = ekstensi dari fixture demo FE (`public/demo-source/manhwa.json`):
  id, title, alt, type, cover, status, genres, year, author, synopsis, permission,
  chapters[{chapter, title, date, pages[]}]
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urljoin

from ..errors import ScanError
from .base import ChapterEntry, ScanResult

STATUS_MAP = {
    "berjalan": "Berjalan", "ongoing": "Berjalan", "running": "Berjalan", "publishing": "Berjalan",
    "tamat": "Tamat", "completed": "Tamat", "complete": "Tamat", "finished": "Tamat", "end": "Tamat",
    "hiatus": "Hiatus", "on hold": "Hiatus", "paused": "Hiatus",
}
TYPE_MAP = {"manhwa": "Manhwa", "manhua": "Manhua", "manga": "Manga"}


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def normalize_date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:$|[T ])", value.strip())
    if not match:
        return None
    try:
        datetime(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None
    return f"{match[1]}-{match[2]}-{match[3]}"


def chapter_ref(index: int, label: str, title: str) -> str:
    return f"{index}-{hashlib.sha1(f'{label}|{title}'.encode()).hexdigest()[:8]}"


def parse_ref(ref: str) -> tuple[int, str]:
    match = re.fullmatch(r"(\d{1,6})-([0-9a-f]{8})", ref or "")
    if not match:
        raise ScanError("SCAN_PARSE_ERROR", "Referensi chapter tidak valid.")
    return int(match[1]), match[2]


def parse_manifest(data: Any, resolve: Callable[[str], str]) -> ScanResult:
    if not isinstance(data, dict):
        raise ScanError("SCAN_PARSE_ERROR", "Manifest harus berupa objek JSON.", status=422)
    title = _text(data.get("title"), 140)
    if not title:
        raise ScanError("SCAN_PARSE_ERROR", "Manifest tidak memiliki judul.", status=422)
    raw_chapters = data.get("chapters")
    if raw_chapters is None:
        raw_chapters = []
    if not isinstance(raw_chapters, list):
        raise ScanError("SCAN_PARSE_ERROR", "Daftar chapter pada manifest tidak valid.", status=422)

    chapters: list[ChapterEntry] = []
    for index, item in enumerate(raw_chapters):
        if not isinstance(item, dict):
            raise ScanError("SCAN_PARSE_ERROR", f"Chapter ke-{index + 1} pada manifest tidak valid.", status=422)
        label = item.get("chapter", item.get("number", ""))
        label = str(label).strip() if isinstance(label, (str, int, float)) else ""
        ch_title = _text(item.get("title"), 140)
        chapters.append(
            ChapterEntry(
                ref=chapter_ref(index, label, ch_title),
                number_raw=label or ch_title,
                title=ch_title,
                date=normalize_date(item.get("date")),
            )
        )

    genres_raw = data.get("genres")
    genres = [g.strip()[:40] for g in genres_raw if isinstance(g, str) and g.strip()][:20] if isinstance(genres_raw, list) else []
    year = data.get("year")
    year = year if isinstance(year, int) and not isinstance(year, bool) and 1900 <= year <= 2100 else None
    status_raw = data.get("status")
    cover = data.get("cover")
    permission = _text(data.get("permission"), 300) or None
    return ScanResult(
        title=title,
        alt=_text(data.get("alt"), 140),
        synopsis=_text(data.get("synopsis"), 2000),
        status=STATUS_MAP.get(status_raw.strip().lower()) if isinstance(status_raw, str) else None,
        author=_text(data.get("author"), 100) or None,
        type=TYPE_MAP.get(str(data.get("type", "")).strip().lower()),
        genres=genres,
        year=year,
        cover_url=resolve(cover) if isinstance(cover, str) and cover.strip() else None,
        chapters=chapters,
        permission_note=permission,
    )


def page_urls(data: dict, index: int, expected_hash: str, resolve: Callable[[str], str]) -> list[str]:
    chapters = data.get("chapters") or []
    if index >= len(chapters) or not isinstance(chapters[index], dict):
        raise ScanError("SCAN_PARSE_ERROR", "Chapter tidak ditemukan pada manifest (manifest berubah?).", status=422)
    item = chapters[index]
    label = item.get("chapter", item.get("number", ""))
    label = str(label).strip() if isinstance(label, (str, int, float)) else ""
    if chapter_ref(index, label, _text(item.get("title"), 140)).split("-")[1] != expected_hash:
        raise ScanError("SCAN_PARSE_ERROR", "Manifest berubah sejak scan; lakukan scan ulang.", status=422)
    pages = item.get("pages")
    if not isinstance(pages, list):
        raise ScanError("SCAN_PARSE_ERROR", "Daftar halaman chapter tidak valid.", status=422)
    urls = []
    for page in pages:
        if isinstance(page, dict):
            page = page.get("url") or page.get("image_url")
        if not isinstance(page, str) or not page.strip():
            raise ScanError("SCAN_PARSE_ERROR", "Entri halaman pada manifest tidak valid.", status=422)
        urls.append(resolve(page.strip()))
    return urls
