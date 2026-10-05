"""Staging berkas hasil unduhan. Berada di STAGING_DIR (di luar folder yang di-mount /static, F-06)."""
from __future__ import annotations

import re
import secrets
import shutil
import time
from pathlib import Path

from . import imaging
from .errors import ScanError

STAGED_ID_RE = re.compile(r"^s_[0-9a-f]{16}$")


class Staging:
    def __init__(self, root: Path):
        self.root = root
        self.jobs_dir = root / "jobs"
        self.covers_dir = root / "covers"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.covers_dir.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_dir / job_id

    def chapter_dir(self, job_id: str, key: str) -> Path:
        return self.jobs_dir / job_id / key

    def remove_dir(self, path: Path) -> None:
        shutil.rmtree(path, ignore_errors=True)

    def save_cover(self, data: bytes, fmt: imaging.ImageFormat) -> str:
        staged_id = "s_" + secrets.token_hex(8)
        (self.covers_dir / f"{staged_id}{fmt.extension}").write_bytes(data)
        return staged_id

    def cover_path(self, staged_id: str) -> Path:
        if not STAGED_ID_RE.match(staged_id):
            raise ScanError("INVALID_REQUEST", "ID staging tidak valid.", status=400)
        for path in self.covers_dir.glob(f"{staged_id}.*"):
            if path.is_file():
                return path
        raise ScanError("STAGED_NOT_FOUND", "Berkas staging tidak ditemukan.", status=404)

    def delete_cover(self, staged_id: str) -> None:
        if STAGED_ID_RE.match(staged_id):
            for path in self.covers_dir.glob(f"{staged_id}.*"):
                path.unlink(missing_ok=True)

    def sweep(self, ttl_s: float, keep_jobs: set[str]) -> int:
        """Hapus berkas yatim yang lebih tua dari TTL. Mengembalikan jumlah entri yang dihapus."""
        removed = 0
        cutoff = time.time() - ttl_s
        for path in list(self.jobs_dir.iterdir()):
            if path.name in keep_jobs:
                continue
            if path.stat().st_mtime <= cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        for path in list(self.covers_dir.iterdir()):
            if path.stat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
