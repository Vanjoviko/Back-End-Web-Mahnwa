"""Jalankan worker: `python -m app` (bind default 127.0.0.1:8000).

Sebelum membuat setting, muat file env yang sesuai ke `os.environ` supaya
`python -m app` cukup dijalankan tanpa `set -a; . ./.env; set +a`.

Pemilihan file (pertama yang cocok):

1. `ENV_FILE` (bila diisi; relatif dihitung dari root repo),
2. `.env.<APP_ENV>` (mis. `.env.dev` / `.env.prod`, `APP_ENV` default `dev`),
3. `.env` (cadangan).

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
_BASE_DIR = Path(__file__).resolve().parent.parent


def env_file_path() -> Path:
    """Tentukan path file env yang dipakai (`ENV_FILE` > `.env.<APP_ENV>` > `.env`)."""
    explicit = (os.environ.get("ENV_FILE") or "").strip()
    if explicit:
        candidate = Path(explicit)
        return candidate if candidate.is_absolute() else _BASE_DIR / candidate
    app_env = (os.environ.get("APP_ENV") or "dev").strip() or "dev"
    for name in (f".env.{app_env}", ".env"):
        candidate = _BASE_DIR / name
        if candidate.exists():
            return candidate
    return _BASE_DIR / ".env"


def load_dotenv(path: Path | None = None) -> Path:
    """Muat pasangan KEY=VALUE dari `path` tanpa menimpa env yang sudah ada.

    Mengembalikan path yang dipakai (meski filenya tidak ada).
    """
    target = path or env_file_path()
    try:
        raw = target.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return target
    for line in raw.splitlines():
        match = _ENV_LINE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return target


if __name__ == "__main__":
    load_dotenv()
    settings = Settings.from_env()
    uvicorn.run(create_app(settings), host=settings.worker_bind, port=settings.worker_port)
