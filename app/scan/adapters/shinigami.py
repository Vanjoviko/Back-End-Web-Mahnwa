import re
from bs4 import BeautifulSoup 
from app.scan.adapters.base import SourceAdapter, AdapterContext, ScanResult, ChapterEntry, PageRef
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

class ShinigamiAdapter(SourceAdapter):
    name = "shinigami"
    label = "Shinigami"

    def matches(self, url: str) -> bool:
        return "shinigami" in url.lower()

    async def _get_html(self, url: str, ctx: AdapterContext) -> str:
        # Menggunakan metode fetch_bytes yang didukung oleh GuardedHttp
        raw_bytes, _ = await ctx.http.fetch_bytes(
            url,
            max_bytes=10 * 1024 * 1024,  # Batas aman 10MB untuk HTML
            too_large=ScanError("LIMIT_BYTES", "Halaman terlalu besar"),
            timeout=30.0,
        )
        return raw_bytes.decode('utf-8', errors='ignore')

    async def scan(self, url: str, ctx: AdapterContext) -> ScanResult:
        html_content = await self._get_html(url, ctx)
        soup = BeautifulSoup(html_content, "html.parser")
        
        # Ekstrak Metadata
        title_el = soup.select_one("h1, .post-title h1, .summary-content h1")
        title = title_el.get_text(strip=True) if title_el else "Unknown Manga"
        
        cover_el = soup.select_one("img.object-cover, .summary_image img")
        cover_url = cover_el.get("src") if cover_el else None
        
        # Ekstrak Chapter
        chapters = []
        seen_refs = set()
        
        for el in soup.select("a[href*='/chapter/'], .wp-manga-chapter a"):
            href = el.get("href")
            text = el.get_text(" ", strip=True)
            
            if href and href not in seen_refs:
                seen_refs.add(href)
                chapters.append(
                    ChapterEntry(
                        ref=href, 
                        number_raw=chapter_number_from_url(href) or parse_chapter_number(text or href), 
                        title=""  # judul hanya mengikuti judul komik
                    )
                )
                
        return ScanResult(
            title=title,
            chapters=chapters,
            cover_url=cover_url
        )

    async def list_pages(self, chapter_ref: str, ctx: AdapterContext) -> list[PageRef]:
        html_content = await self._get_html(chapter_ref, ctx)
        soup = BeautifulSoup(html_content, "html.parser")
        
        pages = []
        banned_keywords = ["banner", "logo", "avatar", "icon", "discord", "thumbnail", "badge", "static", "promo", "iklan"]
        
        for img in soup.select("img"): 
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            
            if src:
                src_lower = src.lower()
                is_img_ext = any(ext in src_lower for ext in [".jpg", ".jpeg", ".png", ".webp"])
                is_not_ad = not any(banned in src_lower for banned in banned_keywords)
                
                if is_img_ext and is_not_ad:
                    pages.append(PageRef(url=src))
                
        return pages