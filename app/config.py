"""Konfigurasi worker scan-import (dibaca dari environment).

Semua nilai default mengikuti spesifikasi scan-import (NFR-01, NFR-04, NFR-05,
NFR-08). Tidak ada rahasia yang ditanam di repo: token worker wajib datang dari
environment kecuali mode dev.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

BASE_DIR = Path(__file__).resolve().parent.parent

TRUE_VALUES = {"1", "true", "yes", "on"}


def _bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in TRUE_VALUES


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:  # pragma: no cover - dilaporkan jelas saat start
        raise RuntimeError(f"Environment {name} harus berupa bilangan bulat, bukan {raw!r}.") from exc


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError as exc:  # pragma: no cover
        raise RuntimeError(f"Environment {name} harus berupa angka, bukan {raw!r}.") from exc


@dataclass(frozen=True)
class Settings:
    worker_token: str = ""
    dev_mode: bool = False
    worker_bind: str = "127.0.0.1"
    worker_port: int = 8000
    public_base_url: str = ""
    staging_dir: Path = field(default_factory=lambda: BASE_DIR / "staging")
    allowed_hosts: frozenset = frozenset()
    enable_fixture: bool = False
    fixture_dir: Path = field(default_factory=lambda: BASE_DIR / "fixtures")
    legacy_enabled: bool = False
    # Swagger/ReDoc/OpenAPI. Default nyala agar `python -m app` menampilkan /docs.
    enable_docs: bool = True

    # Limit khusus jalur scan-import (NFR-01). "150 MB" dibaca sebagai 150 MiB.
    max_pages_per_chapter: int = 300
    max_bytes_per_chapter: int = 157_286_400
    max_bytes_per_image: int = 15_728_640
    max_cover_bytes: int = 5_000_000
    max_manifest_bytes: int = 5_000_000

    # Konkurensi & kesopanan (NFR-04)
    chapter_concurrency: int = 2
    image_concurrency: int = 4
    host_max_concurrency: int = 4
    host_min_delay_ms: int = 500
    max_active_jobs: int = 2

    # Retry (BE-06)
    image_retries: int = 2
    retry_backoff_base_s: float = 1.0
    retry_after_cap_s: float = 60.0

    # Timeout (NFR-05)
    scan_timeout_s: float = 90.0
    html_timeout_s: float = 30.0
    image_timeout_s: float = 60.0
    connect_timeout_s: float = 10.0
    max_redirects: int = 3

    # Disk & pembersihan (NFR-10)
    staging_ttl_hours: float = 24.0
    min_free_disk_bytes: int = 1_073_741_824
    cancelled_cleanup_s: float = 60.0
    sweep_interval_s: float = 600.0

    user_agent: str = "LembarScan/1.0"
    contact: str = ""

    @property
    def http_user_agent(self) -> str:
        return f"{self.user_agent} (+{self.contact})" if self.contact else self.user_agent

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        env = os.environ if env is None else env
        hosts = frozenset(
            h.strip().lower().rstrip(".")
            for h in (env.get("SCAN_ALLOWED_HOSTS") or "").split(",")
            if h.strip()
        )
        legacy_raw = env.get("ENABLE_LEGACY_API", env.get("LEGACY_API_ENABLED"))
        defaults = cls()
        return cls(
            worker_token=(env.get("WORKER_TOKEN") or "").strip(),
            dev_mode=_bool(env, "SCAN_DEV_MODE"),
            worker_bind=(env.get("WORKER_BIND") or defaults.worker_bind).strip(),
            worker_port=_int(env, "WORKER_PORT", defaults.worker_port),
            public_base_url=(env.get("PUBLIC_BASE_URL") or "").strip().rstrip("/"),
            staging_dir=Path(env.get("STAGING_DIR") or defaults.staging_dir).resolve(),
            allowed_hosts=hosts,
            enable_fixture=_bool(env, "SCAN_ENABLE_FIXTURE"),
            fixture_dir=Path(env.get("SCAN_FIXTURE_DIR") or defaults.fixture_dir).resolve(),
            legacy_enabled=(legacy_raw or "").strip().lower() in TRUE_VALUES,
            enable_docs=_bool(env, "SCAN_ENABLE_DOCS", default=True),
            max_pages_per_chapter=_int(env, "SCAN_MAX_PAGES_PER_CHAPTER", defaults.max_pages_per_chapter),
            max_bytes_per_chapter=_int(env, "SCAN_MAX_BYTES_PER_CHAPTER", defaults.max_bytes_per_chapter),
            max_bytes_per_image=_int(env, "SCAN_MAX_BYTES_PER_IMAGE", defaults.max_bytes_per_image),
            max_cover_bytes=_int(env, "SCAN_MAX_COVER_BYTES", defaults.max_cover_bytes),
            max_manifest_bytes=_int(env, "SCAN_MAX_MANIFEST_BYTES", defaults.max_manifest_bytes),
            chapter_concurrency=max(1, _int(env, "SCAN_CHAPTER_CONCURRENCY", defaults.chapter_concurrency)),
            image_concurrency=max(1, _int(env, "SCAN_IMAGE_CONCURRENCY", defaults.image_concurrency)),
            host_max_concurrency=max(1, _int(env, "SCAN_HOST_MAX_CONCURRENCY", defaults.host_max_concurrency)),
            host_min_delay_ms=max(0, _int(env, "SCAN_HOST_MIN_DELAY_MS", defaults.host_min_delay_ms)),
            max_active_jobs=max(1, _int(env, "SCAN_MAX_ACTIVE_JOBS", defaults.max_active_jobs)),
            image_retries=max(0, _int(env, "SCAN_IMAGE_RETRIES", defaults.image_retries)),
            retry_backoff_base_s=_float(env, "SCAN_RETRY_BACKOFF_BASE_S", defaults.retry_backoff_base_s),
            retry_after_cap_s=_float(env, "SCAN_RETRY_AFTER_CAP_S", defaults.retry_after_cap_s),
            scan_timeout_s=_float(env, "SCAN_TIMEOUT_S", defaults.scan_timeout_s),
            html_timeout_s=_float(env, "SCAN_HTML_TIMEOUT_S", defaults.html_timeout_s),
            image_timeout_s=_float(env, "SCAN_IMAGE_TIMEOUT_S", defaults.image_timeout_s),
            connect_timeout_s=_float(env, "SCAN_CONNECT_TIMEOUT_S", defaults.connect_timeout_s),
            max_redirects=_int(env, "SCAN_MAX_REDIRECTS", defaults.max_redirects),
            staging_ttl_hours=_float(env, "SCAN_STAGING_TTL_HOURS", defaults.staging_ttl_hours),
            min_free_disk_bytes=_int(env, "SCAN_MIN_FREE_DISK_BYTES", defaults.min_free_disk_bytes),
            cancelled_cleanup_s=_float(env, "SCAN_CANCELLED_CLEANUP_S", defaults.cancelled_cleanup_s),
            sweep_interval_s=_float(env, "SCAN_SWEEP_INTERVAL_S", defaults.sweep_interval_s),
            user_agent=(env.get("SCAN_USER_AGENT") or defaults.user_agent).strip(),
            contact=(env.get("SCAN_CONTACT") or "").strip(),
        )

    def validate(self) -> None:
        """Gagalkan start bila konfigurasi tidak aman (NFR-08)."""
        if not self.worker_token and not self.dev_mode:
            raise RuntimeError(
                "WORKER_TOKEN kosong. Set WORKER_TOKEN (atau SCAN_DEV_MODE=true hanya untuk pengembangan lokal)."
            )
        if self.enable_fixture and not self.dev_mode:
            raise RuntimeError("SCAN_ENABLE_FIXTURE hanya boleh aktif bersama SCAN_DEV_MODE=true.")
