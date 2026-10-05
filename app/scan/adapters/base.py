"""Kontrak Source Adapter (BE-01).

Menambah adapter baru = menambah satu modul + satu baris di `adapters/__init__.py`,
tanpa mengubah engine. Tidak ada adapter yang dibundel untuk situs agregator.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Mapping

from ...config import Settings


@dataclass
class ChapterEntry:
    ref: str  # opaque; hanya bermakna bagi adapter
    number_raw: str
    title: str = ""
    date: str | None = None


@dataclass
class ScanResult:
    title: str
    chapters: list[ChapterEntry]
    alt: str = ""
    synopsis: str = ""
    status: str | None = None  # Berjalan|Tamat|Hiatus|None
    author: str | None = None
    type: str | None = None  # Manhwa|Manhua|Manga|None
    genres: list[str] = field(default_factory=list)
    year: int | None = None
    cover_url: str | None = None
    permission_note: str | None = None  # catatan izin sumber (FR-18); None = belum dicatat


@dataclass
class PageRef:
    url: str


@dataclass
class AdapterContext:
    """Dipasok engine: HTTP client ter-guard, pembatas, logger, cache per-job."""

    http: Any  # GuardedHttp
    settings: Settings
    logger: logging.Logger
    source_url: str
    cache: dict = field(default_factory=dict)


class SourceAdapter(ABC):
    name: str = ""
    label: str = ""  # nama tampilan untuk UI (FR-18)
    requires_allowlist: bool = True

    @abstractmethod
    def matches(self, url: str) -> bool: ...

    @abstractmethod
    async def scan(self, url: str, ctx: AdapterContext) -> ScanResult: ...

    @abstractmethod
    async def list_pages(self, chapter_ref: str, ctx: AdapterContext) -> list[PageRef]: ...

    def fetch_headers(self, ctx: AdapterContext) -> Mapping[str, str]:
        """Header sah (mis. Referer). Tidak boleh menyamar sebagai browser lain."""
        return {}
