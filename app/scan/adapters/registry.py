from __future__ import annotations
from typing import Iterable

from ...config import Settings
from ..errors import ScanError
from .base import SourceAdapter
from .shinigami import ShinigamiAdapter
from .kiryuu import KiryuuAdapter


class AdapterRegistry:
    """Memetakan URL -> adapter. Adapter jaringan aktif untuk host apa pun (tanpa allowlist)."""

    def __init__(self, adapters: Iterable[SourceAdapter], settings: Settings):
        self.adapters = list(adapters)

        # --- INJEKSI ADAPTER MANUAL ---
        # Memastikan Shinigami dan Kiryuu selalu dimuat meskipun tidak dikirim oleh service
        existing_names = [a.name for a in self.adapters]
        if "shinigami" not in existing_names:
            self.adapters.append(ShinigamiAdapter())
        if "kiryuu" not in existing_names:
            self.adapters.append(KiryuuAdapter())

        self.settings = settings

    def active_names(self) -> list[str]:
        return [a.name for a in self.adapters if self._enabled(a)]

    def _enabled(self, adapter: SourceAdapter) -> bool:
        if adapter.is_fixture:
            return self.settings.enable_fixture
        return True

    def resolve(self, url: str) -> SourceAdapter:
        candidates = [a for a in self.adapters if a.matches(url) and self._enabled(a)]

        if not candidates:
            active = self.active_names()
            hint = ", ".join(active) if active else "tidak ada"
            raise ScanError(
                "SOURCE_NOT_SUPPORTED",
                f"Tidak ada adapter sumber untuk URL ini. Adapter aktif: {hint}.",
                status=422,
                extra={"adapters": active},
            )

        if len(candidates) > 1:
            raise ScanError(
                "AMBIGUOUS_ADAPTER",
                "Lebih dari satu adapter cocok untuk URL ini: " + ", ".join(a.name for a in candidates) + ".",
                status=409,
                extra={"adapters": [a.name for a in candidates]},
            )

        return candidates[0]

    def by_name(self, name: str) -> SourceAdapter:
        for adapter in self.adapters:
            if adapter.name == name:
                return adapter
        raise ScanError("SOURCE_NOT_SUPPORTED", f"Adapter '{name}' tidak dikenal.", status=422)
