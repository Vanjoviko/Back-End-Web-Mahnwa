"""Uji adapter scraper Kiryuu untuk ekstraksi nomor chapter.

Regresi yang dicegah: link chapter menampilkan teks "Chapter 14 8 hours ago";
parse berbasis teks merangkainya menjadi "148"/"138"/dst. Nomor diambil dari URL.

Adapter Shinigami kini berbasis JSON API (bukan HTML) dan diuji di
`test_shinigami_adapter.py`.
"""
from __future__ import annotations

import logging

import pytest

from app.config import Settings
from app.scan.adapters.base import AdapterContext
from app.scan.adapters.kiryuu import KiryuuAdapter, chapter_number_from_url as kiryuu_number
from app.scan.adapters.manifest_schema import extract_date_text, normalize_released_at


@pytest.mark.parametrize(
    "href,expected",
    [
        ("https://v7.kiryuu.to/manga/i-bought-the-villains-with-money/chapter-14.816865/", "14"),
        ("https://v7.kiryuu.to/manga/seri/chapter-138.123456/", "138"),
        ("https://v7.kiryuu.to/manga/seri/chapter-1.813766/", "1"),
        ("https://contoh.example/chapter/12/", "12"),
        ("https://contoh.example/chapter/8.5/", "8.5"),
        ("https://contoh.example/bab-3.2/", "3.2"),
        ("https://contoh.example/manga/tanpa-nomor/", None),
        ("", None),
        (None, None),
    ],
)
def test_chapter_number_from_url(href, expected):
    assert kiryuu_number(href) == expected


class _FakeHttp:
    def __init__(self, html: str):
        self.html = html

    async def fetch_bytes(self, url, max_bytes, too_large, timeout):
        return self.html.encode("utf-8"), "text/html"


def _ctx(html: str) -> AdapterContext:
    return AdapterContext(
        http=_FakeHttp(html),
        settings=Settings(),
        logger=logging.getLogger("test"),
        source_url="https://v7.kiryuu.to/manga/seri/",
    )


KIRYUU_HTML = """
<html><body>
<h1 class="entry-title">I Bought the Villains With Money</h1>
<div class="listing"><ul id="chapterlist">
  <li><a href="https://v7.kiryuu.to/manga/seri/chapter-14.816865/">Chapter 14 <span>8 hours ago</span></a></li>
  <li><a href="https://v7.kiryuu.to/manga/seri/chapter-13.816862/">Chapter 13 <span>8 hours ago</span></a></li>
  <li><a href="https://v7.kiryuu.to/manga/seri/chapter-12.816005/">Chapter 12 <span>2 days ago</span></a></li>
</ul></div>
</body></html>
"""


@pytest.mark.asyncio
async def test_kiryuu_scan_takes_number_from_url_without_titles():
    result = await KiryuuAdapter().scan("https://v7.kiryuu.to/manga/seri/", _ctx(KIRYUU_HTML))
    assert [c.number_raw for c in result.chapters] == ["14", "13", "12"]
    assert all(c.title == "" for c in result.chapters)
    # "8 hours ago"/"2 days ago" -> waktu rilis relatif terisi
    assert all(c.released_at is not None for c in result.chapters)


KIRYUU_DETAIL_HTML = """
<html><body>
<h1 itemprop="name">Superhuman Era</h1>
<div class="block text-sm">Superhuman Era,초인의 시대</div>
<img class="wp-post-image" src="https://v7.kiryuu.to/wp-content/uploads/2026/08/i517302.jpg">
<div data-show="true" itemprop="description" class="p-4 hidden">Sinopsis cuplikan yang tidak lengkap...</div>
<div data-show="false" itemprop="description" class="p-4 prose">
  <p>Di dunia tempat monster datang dari langit, hanya segelintir manusia yang punya kekuatan melawan.
     Pahlawan rahasia menyembunyikan identitasnya di balik topi putih, karena kekuatan lama itu bukan
     hadiah melainkan misteri...<br /> (<em>Source: WEBTOON</em>)</p>
  <p><strong>Original Webtoon:</strong><br /><a href="https://contoh.example/webtoon">Naver Webtoon</a></p>
  <p><strong>Official Translations:</strong><br /><a href="https://contoh.example/terjemahan">English</a></p>
</div>
<a itemprop="genre" href="https://v7.kiryuu.to/genre/action/">Action</a>
<a itemprop="genre" href="https://v7.kiryuu.to/genre/fantasy/">Fantasy</a>
<a itemprop="genre" href="https://v7.kiryuu.to/genre/shounen/">Shounen</a>
<a itemprop="genre" href="https://v7.kiryuu.to/genre/supernatural/">Supernatural</a>
<div class="flex sm:justify-between justify-start items-center gap-2">
  <h4 class="font-semibold text-base text-gray-100 flex items-center justify-start">
    <span data-lucide="type-outline" class="mx-1 inline-block w-6 h-6"></span>
    <span class="font-semibold">Type</span>
  </h4>
  <div class="inline">
    <img src="https://v7.kiryuu.to/wp-content/themes/kiryuu/static/svg/manhua.svg" width="24" height="24" alt="">
    <p class="font-normal text-sm inline ml-1">Manhua</p>
  </div>
</div>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":["Book","ComicSeries"],
 "name":"Superhuman Era","creativeWorkStatus":"Ongoing","isCompleted":false,
 "genre":["Action","Fantasy"]}
</script>
<div class="listing"><ul id="chapterlist">
  <li><a href="https://v7.kiryuu.to/manga/superhuman-era/chapter-14.816865/">Chapter 14 <span>8 hours ago</span></a></li>
  <li><a href="https://v7.kiryuu.to/manga/superhuman-era/chapter-13.816862/">Chapter 13 <span>8 hours ago</span></a></li>
</ul></div>
</body></html>
"""


@pytest.mark.asyncio
async def test_kiryuu_scan_extracts_synopsis_genres_status():
    result = await KiryuuAdapter().scan("https://v7.kiryuu.to/manga/seri/", _ctx(KIRYUU_DETAIL_HTML))

    assert result.title == "Superhuman Era"
    assert result.synopsis.startswith("Di dunia tempat monster datang dari langit")
    # sinopsis hanya paragraf pertama & tanpa catatan sumber/link pendukung
    assert "(Source" not in result.synopsis
    assert "Original Webtoon" not in result.synopsis
    assert result.synopsis.endswith("melainkan misteri...")
    # pilih blok terpanjang: versi cuplikan data-show=true tidak dipakai
    assert "cuplikan" not in result.synopsis
    # genre dari [itemprop=genre] (bukan JSON-LD yang hanya berisi 2)
    assert result.genres == ["Action", "Fantasy", "Shounen", "Supernatural"]
    # status dari JSON-LD creativeWorkStatus -> ternormalisasi
    assert result.status == "Berjalan"
    # tipe dari baris info <h4>Type</h4> (bukan diasumsikan Manhwa)
    assert result.type == "Manhua"
    assert [c.number_raw for c in result.chapters] == ["14", "13"]


@pytest.mark.parametrize(
    "label,expected",
    [("Manhwa", "Manhwa"), ("Manhua", "Manhua"), ("Manga", "Manga"), ("Webtoon", None), ("", None)],
)
@pytest.mark.asyncio
async def test_kiryuu_scan_type_from_info_row(label, expected):
    html = f"""
    <html><body>
    <h1 class="entry-title">Seri Uji</h1>
    <div class="flex items-center gap-2">
      <h4 class="font-semibold"><span data-lucide="type-outline"></span><span>Type</span></h4>
      <div class="inline"><p class="inline">{label}</p></div>
    </div>
    </body></html>
    """
    result = await KiryuuAdapter().scan("https://v7.kiryuu.to/manga/seri/", _ctx(html))
    assert result.type == expected


@pytest.mark.asyncio
async def test_kiryuu_scan_type_absent_is_none():
    result = await KiryuuAdapter().scan("https://v7.kiryuu.to/manga/seri/", _ctx(KIRYUU_HTML))
    assert result.type is None



def test_normalize_released_at_variants():
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    # absolut dengan/ tanpa zona (naive dianggap WIB, UTC+7)
    assert normalize_released_at("2026-09-28T09:00:00+07:00") == "2026-09-28T09:00:00+07:00"
    assert normalize_released_at("2026-09-28 09:00") == "2026-09-28T09:00:00+07:00"
    assert normalize_released_at("28 Sep 2026 09:00") == "2026-09-28T09:00:00+07:00"
    # tanpa jam / tidak dikenali -> None (pemanggil memakai waktu publikasi)
    assert normalize_released_at("2026-09-28") is None
    assert normalize_released_at("bukan tanggal") is None
    assert normalize_released_at(None) is None
    # relatif (Inggris & Indonesia)
    assert normalize_released_at("8 hours ago", now=now) == (now - timedelta(hours=8)).isoformat()
    assert normalize_released_at("3 jam lalu", now=now) == (now - timedelta(hours=3)).isoformat()
    assert normalize_released_at("kemarin", now=now) == (now - timedelta(days=1)).isoformat()
    assert normalize_released_at("5 menit yang lalu", now=now) == (now - timedelta(minutes=5)).isoformat()


def test_extract_date_text():
    assert extract_date_text("Chapter 14 8 hours ago") == "8 hours ago"
    assert extract_date_text("Chapter 14\n8 hours ago") == "8 hours ago"
    assert extract_date_text("Bab 3 - 28 Sep 2026") == "28 Sep 2026"
    assert extract_date_text("Chapter 14") is None
