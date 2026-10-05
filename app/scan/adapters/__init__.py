from __future__ import annotations

from ...config import Settings
from .base import AdapterContext, ChapterEntry, PageRef, ScanResult, SourceAdapter
from .fixture import FixtureAdapter
from .manifest import ManifestAdapter
from .registry import AdapterRegistry

# Tambah adapter baru = satu modul + satu baris di sini.
BUILTIN_ADAPTERS: list[type[SourceAdapter]] = [
    ManifestAdapter,
    FixtureAdapter,
]


def build_registry(settings: Settings, extra: list[SourceAdapter] | None = None) -> AdapterRegistry:
    return AdapterRegistry([cls() for cls in BUILTIN_ADAPTERS] + list(extra or []), settings)


__all__ = [
    "AdapterContext", "AdapterRegistry", "ChapterEntry", "PageRef", "ScanResult", "SourceAdapter",
    "FixtureAdapter", "ManifestAdapter", "build_registry", "BUILTIN_ADAPTERS",
]
