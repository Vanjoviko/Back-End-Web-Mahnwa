import json
import re

from bs4 import BeautifulSoup

from app.scan.adapters.base import SourceAdapter, AdapterContext, ScanResult, ChapterEntry, PageRef
from app.scan.adapters.manifest_schema import STATUS_MAP
from app.scan.errors import ScanError

def parse_chapter_number(text: str) -> str:
    """Mengekstrak angka chapter dari teks string."""
    match = re.search(r'chapter\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
    if match:
        return match.group(1)
    digits = re.findall(r'\d+(?:\.\d+)?', text)
    return digits[0] if digits else "0"


def chapter_number_from_url(href: str) -> str | None:
    """Nomor chapter dari URL tautan (mis. /chapter-14.816865/ -> '14').

    Teks tautan umumnya tercampur waktu relatif ("Chapter 14 8 hours ago"),
    sehingga nomor diambil dari URL yang lebih andal. Post-id WordPress yang
    menempel setelah titik (mis. ".816865") dibuang lebih dulu.
    """
    if not href:
        return None
    path = href.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    path = re.sub(r"\.\d{4,}$", "", path)  # buang post-id wordpress (.816865)
    m = re.search(r"(?:chapter|chap|bab|episode)[-_/.](\d+(?:\.\d+)?)", path, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def _clean_text(text: str) -> str:
    """Rapikan whitespace (baris baru -> spasi tunggal)."""
    return " ".join(text.split()) if text else ""


def _pick_synopsis(soup: BeautifulSoup) -> str:
    """Sinopsis dari halaman detail.

    Tema kiryuu v7 server-render dua blok ``[itemprop=description]``: yang
    terpotong (``data-show=true``, berakhir "[…]") dan yang lengkap
    (``data-show=false``). Ambil yang teksnya paling panjang, lalu hanya
    ``<p>`` pertama supaya link "Original Webtoon"/"Official Translations"
    tidak ikut. Catatan sumber di ekor (mis. "(Source: WEBTOON)") dibuang.
    """
    text = ""
    blocks = soup.select('div[itemprop="description"]')
    if blocks:
        node = max(blocks, key=lambda el: len(el.get_text(" ", strip=True)))
        first_p = node.find("p") if node else None
        text = first_p.get_text(" ", strip=True) if first_p else node.get_text(" ", strip=True)
    else:
        node = soup.select_one(
            ".entry-content[itemprop='description'], #sinopsis, "
            ".summary__content, .description-summary"
        )
        if node is not None:
            first_p = node.find("p")
            text = first_p.get_text(" ", strip=True) if first_p else node.get_text(" ", strip=True)
    if not text:
        for meta in soup.select('meta[name="description"], meta[property="og:description"]'):
            content = (meta.get("content") or "").strip()
            if content:
                text = content
                break
    # "...in the shadows... (Source: WEBTOON)" -> "...in the shadows..."
    text = re.split(r"\(\s*(?:source|sumber)\s*:", text, flags=re.IGNORECASE)[0]
    return _clean_text(text)[:2000]


def _ld_json(soup: BeautifulSoup) -> list[dict]:
    """Semua blok JSON-LD pada halaman."""
    out: list[dict] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict):
            out.append(payload)
        elif isinstance(payload, list):
            out.extend(item for item in payload if isinstance(item, dict))
    return out


def _comic_series(soup: BeautifulSoup) -> dict | None:
    """Objek JSON-LD tipe ComicSeries/Book (metadata terstruktur kiryuu)."""
    for item in _ld_json(soup):
        typ = item.get("@type")
        names = {typ} if isinstance(typ, str) else set(typ or [])
        if names & {"ComicSeries", "ComicStory", "Book"}:
            return item
    return None


def _extract_genres(soup: BeautifulSoup, ld: dict | None) -> list[str]:
    genres = [el.get_text(" ", strip=True) for el in soup.select('[itemprop="genre"]')]
    genres = [g for g in genres if g]
    if not genres and ld and isinstance(ld.get("genre"), list):
        genres = [str(g).strip() for g in ld["genre"] if str(g).strip()]
    return [_clean_text(g)[:40] for g in genres][:20]


def _label_value(soup: BeautifulSoup, labels: set[str]) -> str | None:
    """Nilai pada baris info: <h4>…Label…</h4> + <div class="inline">…nilai…</div>."""
    for h in soup.select("h4"):
        if h.get_text(" ", strip=True) in labels:
            sibling = h.find_next_sibling("div")
            if sibling is not None:
                value = sibling.get_text(" ", strip=True)
                if value:
                    return value
    return None


def _extract_status(soup: BeautifulSoup, ld: dict | None) -> str | None:
    raw = None
    if ld:
        raw = ld.get("creativeWorkStatus")
        if not raw and ld.get("isCompleted") is True:
            raw = "Tamat"
    if not raw:
        raw = _label_value(soup, {"Status", "status"})
    if isinstance(raw, str):
        return STATUS_MAP.get(raw.strip().lower())
    return None


class KiryuuAdapter(SourceAdapter):
    name = "kiryuu"
    label = "Kiryuu"

    def matches(self, url: str) -> bool:
        return "kiryuu" in url.lower()

    async def _get_html(self, url: str, ctx: AdapterContext) -> str:
        try:
            raw_bytes, _ = await ctx.http.fetch_bytes(
                url,
                max_bytes=10 * 1024 * 1024,
                too_large=ScanError("LIMIT_BYTES", "Halaman terlalu besar"),
                timeout=30.0,
            )
            return raw_bytes.decode('utf-8', errors='ignore')
        except AttributeError as e:
            raise Exception(f"Sistem GuardedHttp tidak merespons dengan benar: {e}")

    async def scan(self, url: str, ctx: AdapterContext) -> ScanResult:
        html_content = await self._get_html(url, ctx)
        soup = BeautifulSoup(html_content, "html.parser")
        
        # 1. Ekstrak Title yang lebih tahan banting
        title_el = soup.select_one("h1.entry-title, h1[itemprop='name'], .post-title h1, .infox h1, h1")
        title = title_el.get_text(strip=True) if title_el else "Unknown Manga"
        
        # 2. Ekstrak Cover (Mendukung data-src / atribut lazy-load)
        cover_el = soup.select_one(".thumb img, .poster img, .wp-post-image, .ts-post-image, img[itemprop='image'], .infomanga img")
        cover_url = None
        if cover_el:
            cover_url = cover_el.get("src") or cover_el.get("data-src") or cover_el.get("data-lazy-src")
        
        # 3. Ekstrak Chapter
        chapters = []
        seen_refs = set()
        
        # Coba selektor MangaStream/Madara standar
        chapter_elements = soup.select("#chapterlist a, .bxcl a, .eph-num a, ul.clstyle a, [id*='chapter'] a, .chbox a, .wp-manga-chapter a, .lchx a")
        
        # FALLBACK: Jika tidak ditemukan, cari semua link <a> yang memiliki pola tautan chapter
        if not chapter_elements:
            for a in soup.find_all("a", href=True):
                href = a["href"].lower()
                if "/chapter-" in href or "-chapter-" in href or "ch-" in href or "/ch/" in href:
                    chapter_elements.append(a)

        for el in chapter_elements:
            href = el.get("href")
            
            # Lewati tautan mati atau tautan komentar
            if not href or href == "#" or "comment" in href or "reply" in href:
                continue
                
            text = el.get_text(" ", strip=True)
            if not text:
                span = el.find("span", class_="chapternum")
                text = span.get_text(" ", strip=True) if span else ""
            
            if href not in seen_refs:
                seen_refs.add(href)
                chapters.append(
                    ChapterEntry(
                        ref=href, 
                        number_raw=chapter_number_from_url(href) or parse_chapter_number(text or href), 
                        title=""  # judul hanya mengikuti judul komik
                    )
                )
                
        ld = _comic_series(soup)
        return ScanResult(
            title=title,
            chapters=chapters,
            cover_url=cover_url,
            synopsis=_pick_synopsis(soup),
            genres=_extract_genres(soup, ld),
            status=_extract_status(soup, ld),
        )

    async def list_pages(self, chapter_ref: str, ctx: AdapterContext) -> list[PageRef]:
        html_content = await self._get_html(chapter_ref, ctx)
        soup = BeautifulSoup(html_content, "html.parser")
        
        pages = []
        banned_keywords = ["banner", "logo", "avatar", "icon", "discord", "thumbnail", "badge", "static", "promo", "iklan"]
        
        # Ambil gambar hanya dari area pembaca (mengurangi false positive dari iklan sidebar)
        reader_area = soup.select_one("#readerarea") or soup
        
        for img in reader_area.select("img"): 
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            
            if src:
                src_lower = src.lower()
                is_img_ext = any(ext in src_lower for ext in [".jpg", ".jpeg", ".png", ".webp"])
                is_not_ad = not any(banned in src_lower for banned in banned_keywords)
                
                if is_img_ext and is_not_ad:
                    pages.append(PageRef(url=src))
                
        return pages