"""Uji adapter Shinigami berbasis JSON API (`api.shngm.io`).

Adapter ini tidak menyentuh HTML situs (SPA + Cloudflare), hanya API JSON. Tes
memakai `_FakeHttp` untuk menyuntikkan respons detail/list/detail-chapter tanpa
jaringan, mengikuti pola `test_scraper_adapters.py`.
"""
from __future__ import annotations

import json
import logging
import re

import pytest

from app.config import Settings
from app.scan.adapters.base import AdapterContext
from app.scan.adapters.shinigami import ShinigamiAdapter, series_id_from_url
from app.scan.errors import ScanError

SERIES_URL = "https://11.shinigami.asia/series/f4eb0724-0874-4ce5-b9ae-b0c3ca832b50"


class _FakeHttp:
    """fetch_bytes sederhana: pilih route pertama yang cocok dengan substring URL."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[str] = []

    async def fetch_bytes(self, url, *, max_bytes, too_large, timeout, accept="*/*", headers=None, breaker=None):
        self.calls.append(url)
        for needle, value in self.routes:
            if needle in url:
                payload = value(url) if callable(value) else value
                return json.dumps(payload).encode("utf-8"), {"content-type": "application/json"}
        raise AssertionError(f"URL tak terduga pada tes: {url}")


def _ctx(http: _FakeHttp) -> AdapterContext:
    return AdapterContext(
        http=http,
        settings=Settings(),
        logger=logging.getLogger("test.shinigami"),
        source_url=SERIES_URL,
    )


def _ok(data):
    return {"retcode": 0, "message": "success", "data": data}


DETAIL = _ok({
    "manga_id": "f4eb0724-0874-4ce5-b9ae-b0c3ca832b50",
    "title": "Demon Life Starting From A Baby",
    "alternative_title": "새끼부터 시작하는 악마생활",
    "description": "Baris satu.\n\nBaris dua.",
    "release_year": "2026",
    "status": 2,
    "user_rate": 8.5,
    "cover_portrait_url": "https://assets.shngm.id/thumbnail/image/portrait.jpg",
    "cover_image_url": "https://assets.shngm.id/thumbnail/cover/banner.jpg",
    "taxonomy": {
        "Format": [{"name": "Manhua", "slug": "manhua"}],
        "Genre": [{"name": "Action", "slug": "action"}, {"name": "Fantasy", "slug": "fantasy"}],
        "Author": [{"name": "Pengarang A"}, {"name": "Pengarang B"}],
        "Artist": [{"name": "Artis C"}],
        "Type": [{"name": "Project", "slug": "project"}],
    },
})


def _chapter_list(pages: dict[int, list[dict]]):
    def route(url: str):
        page = int(re.search(r"[?&]page=(\d+)", url).group(1))
        return {
            "retcode": 0,
            "message": "success",
            "meta": {"page": page, "page_size": 100, "total_page": max(pages)},
            "data": pages.get(page, []),
        }
    return route


def _routes(pages):
    return [
        ("/manga/detail/", DETAIL),
        ("/chapter/", _chapter_list(pages)),
    ]


# --- URL & pencocokan -------------------------------------------------------

@pytest.mark.parametrize(
    "url,expected",
    [
        (SERIES_URL, True),
        ("https://shinigami.asia/series/abc123def", True),
        ("https://api.shngm.io/v1/manga/detail/f4eb0724-0874-4ce5-b9ae-b0c3ca832b50", False),
        ("https://v7.kiryuu.to/manga/seri/", False),
        ("https://shinigami.asia/manga/abc123", False),
        ("https://example.com/series/abc123", False),
        ("https://shinigami.asia/series/", False),
    ],
)
def test_matches(url, expected):
    assert ShinigamiAdapter().matches(url) is expected


@pytest.mark.parametrize(
    "url,expected",
    [
        (SERIES_URL, "f4eb0724-0874-4ce5-b9ae-b0c3ca832b50"),
        ("https://shinigami.asia/series/abc123def/", "abc123def"),
        ("https://shinigami.asia/manga/abc123", None),
        ("https://shinigami.asia/series/", None),
        ("https://shinigami.asia/series/abcdef/extra", "abcdef"),  # parser path (host dicek oleh matches)
        ("https://example.com/series/abc123", "abc123"),  # parser murni path
    ],
)
def test_series_id_from_url(url, expected):
    assert series_id_from_url(url) == expected


# --- scan ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_maps_metadata_and_chapters():
    pages = {
        1: [
            {"chapter_id": "c1", "chapter_number": 1, "chapter_title": "", "release_date": "2026-10-09T16:36:05Z"},
            {"chapter_id": "c2", "chapter_number": 2, "chapter_title": "SEASON 1 END", "release_date": "2026-10-03T06:52:33Z"},
        ],
        2: [
            {"chapter_id": "c3", "chapter_number": 8.5, "release_date": None},
            {"chapter_id": "c2", "chapter_number": 2, "release_date": "2026-10-03T06:52:33Z"},  # duplikat -> dibuang
        ],
    }
    http = _FakeHttp(_routes(pages))
    result = await ShinigamiAdapter().scan(SERIES_URL, _ctx(http))

    assert result.title == "Demon Life Starting From A Baby"
    assert result.alt == "새끼부터 시작하는 악마생활"
    assert result.synopsis == "Baris satu. Baris dua."  # whitespace dirapikan
    assert result.status == "Tamat"
    assert result.type == "Manhua"
    assert result.genres == ["Action", "Fantasy"]
    assert result.author == "Pengarang A, Pengarang B"
    assert result.year == 2026
    assert result.cover_url == "https://assets.shngm.id/thumbnail/image/portrait.jpg"

    # paginasi: halaman 1 & 2 diminta
    assert sum("/chapter/" in call and "/list" in call for call in http.calls) == 2
    assert [c.ref for c in result.chapters] == ["c1", "c2", "c3"]  # duplikat c2 dibuang
    assert [c.number_raw for c in result.chapters] == ["1", "2", "8.5"]
    assert result.chapters[0].title == ""
    assert result.chapters[1].title == "SEASON 1 END"
    assert result.chapters[0].date == "2026-10-09"
    assert result.chapters[0].released_at is not None and result.chapters[0].released_at.startswith("2026-10-09T16:36:05")
    assert result.chapters[2].date is None


@pytest.mark.parametrize("code,label", [(1, "Berjalan"), (2, "Tamat"), (3, "Hiatus")])
@pytest.mark.asyncio
async def test_scan_status_mapping(code, label):
    detail = json.loads(json.dumps(DETAIL))
    detail["data"]["status"] = code
    http = _FakeHttp([("/manga/detail/", detail), ("/chapter/", _route_empty)])
    result = await ShinigamiAdapter().scan(SERIES_URL, _ctx(http))
    assert result.status == label


def _route_empty(url: str):
    return {"retcode": 0, "message": "success", "meta": {"page": 1, "total_page": 1}, "data": []}


@pytest.mark.asyncio
async def test_scan_rejects_non_series_url():
    http = _FakeHttp([])
    with pytest.raises(ScanError) as excinfo:
        await ShinigamiAdapter().scan("https://shinigami.asia/manga/abc123", _ctx(http))
    assert excinfo.value.code == "INVALID_URL"


@pytest.mark.asyncio
async def test_scan_rejects_error_retcode():
    http = _FakeHttp([("/manga/detail/", {"retcode": 1, "message": "nope"})])
    with pytest.raises(ScanError) as excinfo:
        await ShinigamiAdapter().scan(SERIES_URL, _ctx(http))
    assert excinfo.value.code == "HTTP_ERROR"


@pytest.mark.asyncio
async def test_scan_rejects_missing_detail():
    http = _FakeHttp([("/manga/detail/", {"retcode": 0, "message": "success", "data": None})])
    with pytest.raises(ScanError) as excinfo:
        await ShinigamiAdapter().scan(SERIES_URL, _ctx(http))
    assert excinfo.value.code == "SCAN_PARSE_ERROR"


# --- list_pages -------------------------------------------------------------

CHAPTER_DETAIL = _ok({
    "chapter_id": "c1",
    "manga_id": "f4eb0724-0874-4ce5-b9ae-b0c3ca832b50",
    "base_url": "https://assets.shngm.id",
    "base_url_low": "https://assets.shngm.id",
    "chapter": {
        "path": "/chapter/manga_f4eb0724/chapter_e7a3f21c/",
        "data": ["000-b96b3e.jpg", "01-336d33.jpg", "9999-148bff.jpg"],
    },
})


@pytest.mark.asyncio
async def test_list_pages_builds_absolute_urls_in_order():
    http = _FakeHttp([("/chapter/detail/", CHAPTER_DETAIL)])
    pages = await ShinigamiAdapter().list_pages("c1", _ctx(http))
    assert [p.url for p in pages] == [
        "https://assets.shngm.id/chapter/manga_f4eb0724/chapter_e7a3f21c/000-b96b3e.jpg",
        "https://assets.shngm.id/chapter/manga_f4eb0724/chapter_e7a3f21c/01-336d33.jpg",
        "https://assets.shngm.id/chapter/manga_f4eb0724/chapter_e7a3f21c/9999-148bff.jpg",
    ]


@pytest.mark.asyncio
async def test_list_pages_without_files_raises_no_pages():
    payload = _ok({"chapter_id": "c1", "base_url": "https://assets.shngm.id", "chapter": {"path": "/x/", "data": []}})
    http = _FakeHttp([("/chapter/detail/", payload)])
    with pytest.raises(ScanError) as excinfo:
        await ShinigamiAdapter().list_pages("c1", _ctx(http))
    assert excinfo.value.code == "NO_PAGES"
