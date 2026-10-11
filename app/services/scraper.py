import os
import re
import time
from typing import Tuple, List, Dict, Any
from playwright.sync_api import sync_playwright

import requests
from typing import Tuple, List, Dict, Any
from DrissionPage import ChromiumPage, ChromiumOptions
from app.scan.adapters.registry import get_adapter_for_url

STORAGE_BASE = "storage/manga"

STORAGE_BASE = "storage/manga"

def parse_chapter_number(text: str) -> float:
    match = re.search(r'chapter\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
    if match: return float(match.group(1))
    digits = re.findall(r'\d+(?:\.\d+)?', text)
    return float(digits[0]) if digits else 0.0

class ScraperService:
    @staticmethod
    def scan_manga(series_url: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        # Panggil adapter berdasarkan domain target
        adapter = get_adapter_for_url(series_url)
        
        co = ChromiumOptions()
        co.set_argument('--start-maximized')
        page = ChromiumPage(co)
        
        try:
            page.get(series_url)
            title_el = page.ele(adapter.title_selector, timeout=20)
            time.sleep(2)
            title = title_el.text.strip() if title_el else "Unknown Manga"
            
            cover_el = page.ele(adapter.cover_selector)
            cover_url = cover_el.attr("src") if cover_el else None
            
            raw_chapters = []
            seen_urls = set()
            
            while True:
                elements = page.eles(adapter.chapter_selector, timeout=10)
                if not elements: break
                
                first_href_before_click = elements[0].attr("href")
                
                for el in elements:
                    href = el.attr("href")
                    text = el.text.strip()
                    if href and href not in seen_urls:
                        seen_urls.add(href)
                        raw_chapters.append({
                            "chapter_number": parse_chapter_number(text or href),
                            "title": text.split("\n")[0].strip(),
                            "source_url": href
                        })
                
                # Coba klik halaman selanjutnya untuk pagination AJAX
                try:
                    page.scroll.to_bottom()
                    time.sleep(1)
                    next_button = page.ele(adapter.next_page_selector, timeout=2)
                    if next_button and "cursor-not-allowed" not in next_button.attr("class"):
                        next_button.click(by_js=True)
                        is_page_changed = False
                        for _ in range(15):
                            time.sleep(1)
                            new_elements = page.eles(adapter.chapter_selector)
                            if new_elements and new_elements[0].attr("href") != first_href_before_click:
                                is_page_changed = True
                                break
                        if not is_page_changed: break
                    else:
                        break
                except Exception:
                    break
        finally:
            page.quit()

        raw_chapters.sort(key=lambda x: x["chapter_number"])
        return {"title": title, "cover_image_url": cover_url}, raw_chapters

    @staticmethod
    def download_chapter_images(chapter_url: str, manga_id: int, chapter_number: float) -> List[Dict[str, Any]]:
        adapter = get_adapter_for_url(chapter_url)
        
        ch_folder_name = str(int(chapter_number)) if chapter_number.is_integer() else str(chapter_number)
        target_dir = os.path.join(STORAGE_BASE, str(manga_id), "chapters", ch_folder_name)
        os.makedirs(target_dir, exist_ok=True)
        
        co = ChromiumOptions()
        co.set_argument('--start-maximized')
        page = ChromiumPage(co)
        
        try:
            page.get(chapter_url)
            page.ele("tag:img", timeout=20)
            time.sleep(2)
            
            # Simulasi lazy-load scroll berdasarkan adapter
            for _ in range(adapter.lazy_scroll_times):
                page.scroll.down(1200)
                time.sleep(0.5)
                
            all_imgs = page.run_js("""
                return Array.from(document.querySelectorAll('img'))
                    .map(i => i.src || i.dataset.src || i.getAttribute('data-lazy-src') || '')
                    .filter(Boolean);
            """)
            
            valid_urls = []
            banned_keywords = ["banner", "logo", "avatar", "icon", "discord", "thumbnail", "badge", "static", "histats", "promo", "iklan"]
            
            for u in all_imgs:
                u_lower = u.lower()
                is_img_ext = any(ext in u_lower for ext in [".jpg", ".jpeg", ".png", ".webp"])
                is_not_ad = not any(ad in u_lower for ad in banned_keywords)
                if is_img_ext and is_not_ad and u not in valid_urls:
                    valid_urls.append(u)
                    
            if not valid_urls:
                raise ValueError(f"Panel gambar komik tidak ditemukan pada {chapter_url} setelah di-filter.")
                
            # Request menggunakan cookies asli agar tidak kena blokir
            cookies_dict = page.cookies(as_dict=True)
            user_agent = page.user_agent
            saved_pages = []
            
            for page_idx, img_url in enumerate(valid_urls, start=1):
                ext = ".webp" if ".webp" in img_url.lower() else ".jpg"
                filename = f"{page_idx:03d}{ext}"
                file_disk_path = os.path.join(target_dir, filename)
                
                resp = requests.get(
                    img_url, 
                    headers={"Referer": chapter_url, "User-Agent": user_agent},
                    cookies=cookies_dict,
                    timeout=15
                )
                
                if resp.status_code == 200:
                    with open(file_disk_path, "wb") as f:
                        f.write(resp.content)
                    saved_pages.append({
                        "page_number": page_idx,
                        "image_url": f"/static/manga/{manga_id}/chapters/{ch_folder_name}/{filename}"
                    })
            return saved_pages
        except Exception as e:
            raise ValueError(f"Gagal mendownload chapter: {str(e)}")
        finally:
            page.quit()


def parse_chapter_number(text: str) -> float:
    match = re.search(r'chapter\s*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
    if match:
        return float(match.group(1))
    digits = re.findall(r'\d+(?:\.\d+)?', text)
    return float(digits[0]) if digits else 0.0


class ScraperService:

    @staticmethod
    def scan_manga(series_url: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        """Discovery metadata komik dan daftar seluruh chapter dari situs sumber."""
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768}
            )
            page = context.new_page()

            page.goto(series_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)

            # Klik tab 'Chapters' jika ada (seperti pada tema Kiryuu)
            try:
                page.locator("text=/Chapters/i").first.click(timeout=6000)
                time.sleep(1.5)
            except Exception:
                pass

            page.mouse.wheel(0, 1000)
            time.sleep(1)

            # Ambil judul komik
            title_el = page.query_selector("h1, .entry-title")
            title = title_el.inner_text().strip() if title_el else "Unknown Manga"

            # Ambil cover komik
            cover_el = page.query_selector(".thumb img, .poster img")
            cover_url = cover_el.get_attribute("src") if cover_el else None

            # Ambil semua tautan chapter
            elements = page.query_selector_all(
                "#chapterlist a, .bxcl a, .eph-num a, ul.clstyle a, [id*='chapter'] a"
            )
            raw_chapters = []
            seen_urls = set()

            for el in elements:
                href = el.get_attribute("href")
                text = el.inner_text().strip()
                if href and ("chapter" in href.lower() or "ch-" in href.lower()) and href not in seen_urls:
                    seen_urls.add(href)
                    raw_chapters.append({
                        "chapter_number": parse_chapter_number(text or href),
                        "title": text.split("\n")[0].strip(),
                        "source_url": href
                    })

            # Fallback jika selector spesifik di atas tidak membaca
            if not raw_chapters:
                all_a = page.query_selector_all("a")
                for a in all_a:
                    href = a.get_attribute("href")
                    text = a.inner_text().strip()
                    if href and "/chapter-" in href.lower() and href not in seen_urls:
                        seen_urls.add(href)
                        raw_chapters.append({
                            "chapter_number": parse_chapter_number(text or href),
                            "title": text.split("\n")[0].strip(),
                            "source_url": href
                        })

            browser.close()

            # Urutkan dari chapter kecil ke besar (1, 2, 3...)
            raw_chapters.sort(key=lambda x: x["chapter_number"])
            metadata = {"title": title, "cover_image_url": cover_url}
            return metadata, raw_chapters

    @staticmethod
    def download_chapter_images(chapter_url: str, manga_id: int, chapter_number: float) -> List[Dict[str, Any]]:
        """
        Download semua panel komik, simpan per file gambar di storage/manga/{manga_id}/chapters/{ch}/
        dan kembalikan daftar urutan gambarnya.
        """
        ch_folder_name = str(int(chapter_number)) if chapter_number.is_integer() else str(chapter_number)
        target_dir = os.path.join(STORAGE_BASE, str(manga_id), "chapters", ch_folder_name)
        os.makedirs(target_dir, exist_ok=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768}
            )
            page = context.new_page()

            page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)

            # Scroll ke bawah untuk memicu lazy loading gambar
            for _ in range(12):
                page.mouse.wheel(0, 1200)
                time.sleep(0.2)

            # Ekstrak seluruh sumber gambar dari halaman
            all_imgs = page.evaluate("""() => {
                return Array.from(document.querySelectorAll('img'))
                    .map(i => i.src || i.dataset.src || i.getAttribute('data-lazy-src') || '')
                    .filter(Boolean);
            }""")

            # Filter khusus CDN komik (yuucdn/imgsc) dan abaikan banner iklan
            valid_urls = []
            for u in all_imgs:
                if ("yuucdn.com" in u or "imgsc" in u) and not any(ad in u for ad in ["banner", "logo", "avatar", "icon"]):
                    if u not in valid_urls:
                        valid_urls.append(u)

            # Fallback jika URL tidak memakai kata yuucdn/imgsc
            if not valid_urls:
                for u in all_imgs:
                    is_img_ext = any(ext in u.lower() for ext in [".jpg", ".jpeg", ".png", ".webp"])
                    is_not_ad = not any(ad in u.lower() for ad in ["banner", "logo", "avatar", "icon", "static"])
                    if is_img_ext and is_not_ad and u not in valid_urls:
                        valid_urls.append(u)

            if not valid_urls:
                browser.close()
                raise ValueError("Panel gambar komik tidak ditemukan pada halaman.")

            saved_pages = []
            for page_idx, img_url in enumerate(valid_urls, start=1):
                ext = ".webp" if ".webp" in img_url.lower() else ".jpg"
                filename = f"{page_idx:03d}{ext}"  # Contoh: 001.webp, 002.webp
                file_disk_path = os.path.join(target_dir, filename)

                resp = context.request.get(img_url, headers={"Referer": chapter_url})
                if resp.status == 200:
                    with open(file_disk_path, "wb") as f:
                        f.write(resp.body())

                    # Path URL relatif untuk diakses browser via endpoint /static
                    web_accessible_url = f"/static/manga/{manga_id}/chapters/{ch_folder_name}/{filename}"
                    saved_pages.append({
                        "page_number": page_idx,
                        "image_url": web_accessible_url
                    })

            browser.close()
            return saved_pages