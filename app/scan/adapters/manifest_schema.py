"""Normalisasi manifest JSON bersama untuk ManifestAdapter dan FixtureAdapter.

Skema = ekstensi dari fixture demo FE (`public/demo-source/manhwa.json`):
  id, title, alt, type, cover, status, genres, year, author, synopsis, permission,
  chapters[{chapter, title, date, pages[]}]

`date` boleh berupa tanggal (YYYY-MM-DD) maupun waktu relatif ("3 jam lalu",
"8 hours ago"); `released_at` menormalkannya menjadi ISO 8601 lengkap dengan jam
bila memungkinkan (lihat `normalize_released_at`).
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
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


MONTHS: dict[str, int] = {
    "jan": 1, "januari": 1, "january": 1,
    "feb": 2, "februari": 2, "february": 2,
    "mar": 3, "maret": 3, "march": 3,
    "apr": 4, "april": 4,
    "mei": 5, "may": 5,
    "jun": 6, "juni": 6, "june": 6,
    "jul": 7, "juli": 7, "july": 7,
    "agu": 8, "ags": 8, "agustus": 8, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10, "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "des": 12, "desember": 12, "dec": 12, "december": 12,
}

# Detik per satuan waktu relatif (Inggris & Indonesia).
RELATIVE_UNITS: dict[str, int] = {
    "second": 1, "seconds": 1, "sec": 1, "secs": 1, "detik": 1, "dtk": 1,
    "minute": 60, "minutes": 60, "min": 60, "mins": 60, "menit": 60,
    "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600, "jam": 3600,
    "day": 86400, "days": 86400, "hari": 86400,
    "week": 604800, "weeks": 604800, "minggu": 604800, "pekan": 604800,
    "month": 2592000, "months": 2592000, "bulan": 2592000,
    "year": 31536000, "years": 31536000, "tahun": 31536000,
}

# Sumber yang didukung berbahasa Indonesia (WIB, UTC+7); dipakai saat waktu tanpa zona.
JAKARTA = timezone(timedelta(hours=7))


def _parse_tz(tz: str | None) -> timezone | None:
    if not tz:
        return None
    if tz == "Z":
        return timezone.utc
    sign = 1 if tz[0] == "+" else -1
    body = tz[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(body[:2]), minutes=int(body[2:4])))


def normalize_released_at(value: Any, now: datetime | None = None) -> str | None:
    """Normalkan tanggal/waktu sumber menjadi ISO 8601 lengkap dengan jam.

    Menerima: ISO ("2026-09-28T09:00:00+07:00"), "2026-09-28 09:00",
    "28 Sep 2026 09:00", serta waktu relatif ("3 jam lalu", "8 hours ago",
    "kemarin"). Kembalikan `None` bila tidak ada jam yang bisa dipastikan
    (mis. hanya "2026-09-28") — pemanggil dapat memakai waktu publikasi.
    """
    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    if not text:
        return None
    now = now or datetime.now(timezone.utc)

    # 1) ISO-ish: YYYY-MM-DD[ T]HH:MM[:SS][Z|±HH:MM]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?\s*(Z|[+-]\d{2}:?\d{2})?$", text)
    if m:
        y, mo, d, hh, mm, ss, tz = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0), tzinfo=_parse_tz(tz))
        except ValueError:
            return None
        if tz is None:
            dt = dt.replace(tzinfo=JAKARTA)
        return dt.isoformat()

    # 2) DD Mon YYYY [HH:MM[:SS]]
    m = re.match(r"^(\d{1,2})\s+([A-Za-z]+)\.?\s+(\d{4})(?:[,\s]+(\d{1,2}):(\d{2})(?::(\d{2}))?)?$", text)
    if m:
        d, mon, y, hh, mm, ss = m.groups()
        month = MONTHS.get(mon.lower())
        if not month:
            return None
        try:
            dt = datetime(int(y), month, int(d), int(hh or 0), int(mm or 0), int(ss or 0), tzinfo=JAKARTA)
        except ValueError:
            return None
        return dt.isoformat()

    # 3) Waktu relatif
    low = text.lower()
    if low in {"baru saja", "just now", "saat ini", "now"}:
        return now.astimezone(timezone.utc).isoformat()
    if low in {"kemarin", "yesterday"}:
        return (now - timedelta(days=1)).astimezone(timezone.utc).isoformat()
    if low in {"hari ini", "today"}:
        return now.astimezone(timezone.utc).isoformat()
    m = re.match(r"^(\d+)\s*([A-Za-z]+)(?:\s+(?:ago|lalu|yang lalu))?$", text)
    if m:
        seconds = RELATIVE_UNITS.get(m.group(2).lower().rstrip("."))
        if seconds is not None:
            return (now - timedelta(seconds=int(m.group(1)) * seconds)).astimezone(timezone.utc).isoformat()
    return None


def extract_date_text(text: Any) -> str | None:
    """Ambil sisa teks setelah penanda chapter; umumnya berisi tanggal/waktu rilis.

    Contoh: "Chapter 14 8 hours ago" -> "8 hours ago"; "Bab 3 - 28 Sep 2026" -> "28 Sep 2026".
    """
    if not isinstance(text, str):
        return None
    cleaned = re.sub(r"(?i)\b(?:chapter|chap|bab|episode|ep)\b\s*[\d.]*", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—|·:,•")
    return cleaned or None


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
                released_at=normalize_released_at(item.get("date")),
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
