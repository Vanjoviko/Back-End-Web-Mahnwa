"""Jalankan worker: `python -m app` (bind default 127.0.0.1:8000).

Sebelum membuat setting, muat `.env` di root repo (bila ada) ke `os.environ`
supaya `python -m app` cukup dijalankan tanpa `set -a; . ./.env; set +a`.
Variabel yang sudah diset di shell TIDAK ditimpa, jadi
`WORKER_TOKEN=... python -m app` tetap meng-override.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import uvicorn

from app.config import Settings
from app.main import create_app

# Baris .env bergaya KEY=VALUE; "export KEY=VALUE" juga diterima. Komentar (#) diabaikan.
_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z\d_]*)\s*=\s*(.*)$")
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_dotenv(path: Path = _ENV_PATH) -> None:
    """Muat pasangan KEY=VALUE dari `path` tanpa menimpa env yang sudah ada."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return
    for line in raw.splitlines():
        match = _ENV_LINE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)


if __name__ == "__main__":
    load_dotenv()
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.worker_bind, port=settings.worker_port)
