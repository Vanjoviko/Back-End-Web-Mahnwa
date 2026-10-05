from __future__ import annotations

from typing import Iterable
from urllib.parse import urlsplit

from ...config import Settings
from ..errors import ScanError
from ..netguard import host_allowed
from .base import SourceAdapter


class AdapterRegistry:
    """Memetakan URL -> adapter. Adapter berbasis jaringan hanya aktif bila host ada di allowlist."""

    def __init__(self, adapters: Iterable[SourceAdapter], settings: Settings):
        self.adapters = list(adapters)
        self.settings = settings

    def active_names(self) -> list[str]:
        return [a.name for a in self.adapters if self._enabled(a)]

    def _enabled(self, adapter: SourceAdapter) -> bool:
        if adapter.requires_allowlist:
            return bool(self.settings.allowed_hosts)
        return self.settings.enable_fixture

    def _host_ok(self, adapter: SourceAdapter, url: str) -> bool:
        if not adapter.requires_allowlist:
            return self.settings.enable_fixture
        parts = urlsplit(url)
        try:
            port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        except ValueError:
            return False
        return bool(parts.hostname) and host_allowed(parts.hostname, port, parts.scheme.lower(), self.settings.allowed_hosts)

    def resolve(self, url: str) -> SourceAdapter:
        candidates = [a for a in self.adapters if a.matches(url) and self._host_ok(a, url)]
        if not candidates:
            active = self.active_names()
            hint = ", ".join(active) if active else "tidak ada (SCAN_ALLOWED_HOSTS kosong)"
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
