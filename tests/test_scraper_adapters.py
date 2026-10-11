"""Uji adapter scraper (Kiryuu/Shinigami) untuk ekstraksi nomor chapter.

Regresi yang dicegah: link chapter menampilkan teks "Chapter 14 8 hours ago";
parse berbasis teks merangkainya menjadi "148"/"138"/dst. Nomor diambil dari URL.
"""
from __future__ import annotations

import logging

import pytest

from app.config import Settings
from app.scan.adapters.base import AdapterContext
from app.scan.adapters.kiryuu import KiryuuAdapter, chapter_number_from_url as kiryuu_number
from app.scan.adapters.shinigami import ShinigamiAdapter, chapter_number_from_url as shinigami_number


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
    assert shinigami_number(href) == expected


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
    assert [c.number_raw for c in result.chapters] == ["14", "13"]


SHINIGAMI_HTML = """
<html><body>
<h1>Seri Uji</h1>
<div class="wp-manga-chapter">
  <a href="https://shinigami.example/chapter/14/">Chapter 14 <span>3 hours ago</span></a>
  <a href="https://shinigami.example/chapter/13/">Chapter 13 <span>5 days ago</span></a>
</div>
</body></html>
"""


@pytest.mark.asyncio
async def test_shinigami_scan_takes_number_from_url_without_titles():
    result = await ShinigamiAdapter().scan("https://shinigami.example/manga/seri/", _ctx(SHINIGAMI_HTML))
    assert [c.number_raw for c in result.chapters] == ["14", "13"]
    assert all(c.title == "" for c in result.chapters)
