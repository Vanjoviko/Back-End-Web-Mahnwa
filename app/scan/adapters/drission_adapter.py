import asyncio
import re
import time
from typing import Mapping

from DrissionPage import ChromiumPage, ChromiumOptions

from app.scan.adapters.base import SourceAdapter, AdapterContext, ScanResult, ChapterEntry, PageRef
from app.scan.adapters.manifest_schema import extract_date_text, normalize_date, normalize_released_at

def parse_chapter_number_raw(text: str) -> str:
    match = re.search(r'chapter\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
    if match:
        return match.group(1)
    digits = re.findall(r'\d+(?:\.\d+)?', text)
    return digits[0] if digits else "0"

def get_site_rules(url: str) -> dict:
    url_lower = url.lower()
    if "shinigami" in url_lower:
        return {
            "title_selector": "css:h1, .post-title h1, .summary-content h1",
            "cover_selector": "css:img.object-cover, .summary_image img",
            "chapter_selector": "css:a[href*='/chapter/'], .wp-manga-chapter a",
            "next_page_selector": "xpath://button[.//img[contains(@src, 'arrow-right')]] | //*[contains(@class, 'pagination')]//button[last()]",
            "lazy_scroll_times": 20
        }
    
    return {
        "title_selector": "css:h1, .entry-title",
        "cover_selector": "css:.thumb img, .poster img",
        "chapter_selector": "css:#chapterlist a, .bxcl a, .eph-num a, ul.clstyle a, [id*='chapter'] a",
        "next_page_selector": "css:.next-page, a.next",
        "lazy_scroll_times": 14
    }

class DrissionMangaAdapter(SourceAdapter):
    name = "drission_manga"
    label = "Shinigami & Kiryuu (Cloudflare Bypass)"

    def matches(self, url: str) -> bool:
        u = url.lower()
        return any(domain in u for domain in ["shinigami", "kiryuu.to", "kiryuu.id"])

    def _init_page(self) -> ChromiumPage:
        co = ChromiumOptions()
        co.set_argument('--no-sandbox')
        co.set_argument('--headless=new')  # Wajib untuk backend worker agar browser tidak pop-up
        return ChromiumPage(co)

    def _sync_scan(self, url: str) -> ScanResult:
        """Berjalan di background thread secara sinkronus."""
        rules = get_site_rules(url)
        page = self._init_page()

        try:
            page.get(url)
            title_el = page.ele(rules["title_selector"], timeout=20)
            time.sleep(2)

            title = title_el.text.strip() if title_el else "Unknown Manga"
            cover_el = page.ele(rules["cover_selector"])
            cover_url = cover_el.attr("src") if cover_el else None

            raw_chapters = []
            seen_urls = set()

            while True:
                elements = page.eles(rules["chapter_selector"], timeout=10)
                if not elements:
                    break

                first_href_before_click = elements[0].attr("href")

                for el in elements:
                    href = el.attr("href")
                    text = el.text.strip()
                    if href and href not in seen_urls:
                        seen_urls.add(href)
                        date_text = extract_date_text(text)
                        raw_chapters.append(
                            ChapterEntry(
                                ref=href,  # ref diisi dengan URL chapter
                                number_raw=parse_chapter_number_raw(text or href),
                                title=text.split("\n")[0].strip(),
                                date=normalize_date(date_text),
                                released_at=normalize_released_at(date_text),
                            )
                        )

                try:
                    page.scroll.to_bottom()
                    time.sleep(1)

                    next_button = page.ele(rules["next_page_selector"], timeout=2)
                    if next_button and "cursor-not-allowed" not in (next_button.attr("class") or ""):
                        next_button.click(by_js=True)
                        is_page_changed = False
                        for _ in range(15):
                            time.sleep(1)
                            new_elements = page.eles(rules["chapter_selector"])
                            if new_elements and new_elements[0].attr("href") != first_href_before_click:
                                is_page_changed = True
                                break
                        if not is_page_changed:
                            break
                    else:
                        break
                except Exception:
                    break

        except Exception as e:
            raise ValueError(f"Gagal melakukan scan pada {url}: {str(e)}")
        finally:
            page.quit()

        # Urutkan berdasarkan chapter_number as float
        raw_chapters.sort(key=lambda x: float(x.number_raw) if x.number_raw.replace('.', '', 1).isdigit() else 0.0)
        
        return ScanResult(
            title=title,
            cover_url=cover_url,
            chapters=raw_chapters,
            permission_note="Membutuhkan bypass Cloudflare (diizinkan via allowlist)."
        )

    def _sync_list_pages(self, chapter_ref: str, ctx_cache: dict) -> list[PageRef]:
        """Ekstrak URL gambar dan curi cookies dari browser lalu simpan ke ctx_cache."""
        rules = get_site_rules(chapter_ref)
        page = self._init_page()

        try:
            page.get(chapter_ref)
            page.ele("tag:img", timeout=20)
            time.sleep(2)

            for _ in range(rules["lazy_scroll_times"]):
                page.scroll.down(1200)
                time.sleep(0.5)

            all_imgs = page.run_js("""
                return Array.from(document.querySelectorAll('img'))
                    .map(i => i.src || i.dataset.src || i.getAttribute('data-lazy-src') || '')
                    .filter(Boolean);
            """)

            banned_keywords = ["banner", "logo", "avatar", "icon", "discord", "thumbnail", "badge", "static", "histats", "promo", "iklan"]
            valid_urls = []
            for u in all_imgs:
                u_lower = u.lower()
                is_img_ext = any(ext in u_lower for ext in [".jpg", ".jpeg", ".png", ".webp"])
                is_not_ad = not any(ad in u_lower for ad in banned_keywords)
                if is_img_ext and is_not_ad and u not in valid_urls:
                    valid_urls.append(u)

            if not valid_urls:
                raise ValueError(f"Panel gambar tidak ditemukan pada {chapter_ref}")

            # Ambil User-Agent & Cookies yang lolos Cloudflare, simpan ke AdapterContext cache
            ua = page.user_agent
            cookies_dict = page.cookies(as_dict=True)
            cookie_str = "; ".join([f"{k}={v}" for k, v in cookies_dict.items()])
            
            ctx_cache['drission_headers'] = {
                "User-Agent": ua,
                "Cookie": cookie_str,
                "Referer": chapter_ref
            }

            return [PageRef(url=u) for u in valid_urls]
        finally:
            page.quit()

    async def scan(self, url: str, ctx: AdapterContext) -> ScanResult:
        # Jalankan DrissionPage di background thread agar tidak memblokir async event-loop FastAPI
        return await asyncio.to_thread(self._sync_scan, url)

    async def list_pages(self, chapter_ref: str, ctx: AdapterContext) -> list[PageRef]:
        return await asyncio.to_thread(self._sync_list_pages, chapter_ref, ctx.cache)

    def fetch_headers(self, ctx: AdapterContext) -> Mapping[str, str]:
        # Engine akan memanggil fungsi ini saat menggunakan GuardedHttp.
        # Kita suplai cookies Cloudflare yang tadi dicuri pada proses list_pages.
        return ctx.cache.get('drission_headers', {})