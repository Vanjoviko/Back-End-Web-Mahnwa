"""Migrasi satu kali: akun pembaca lama dari `db.json` FE ke tabel `users` BE.

Latar (PRD §1.2, L6): akun pembaca lama hidup di `db.json` front-end dengan
hash kata sandi gaya Node (`crypto.scrypt`). Setelah login pindah ke BE, akun
itu harus tetap bisa masuk lewat tab "Kata sandi".

Aturan:
- Hanya peran pembaca (`role != "admin"`); login admin tidak disentuh (PRD §1.3).
- `id`, `username`, dan `passwordHash` dipertahankan apa adanya sehingga sesi dan
  tautan lama tetap valid. Hash Node langsung terverifikasi oleh `hashlib.scrypt`.
- Username yang sudah ada di BE dilewati (idempoten, aman dijalankan ulang).
- `createdAt` dipertahankan bila ada.

Default DRY-RUN (tidak mengubah apa pun). Tambahkan `--apply` untuk menulis.
Pastikan migrasi Alembic sudah dijalankan dan `DATABASE_URL` sudah diisi.

Contoh:
    python -m tools.migrate_fe_users                     # tinjau (dry-run)
    python -m tools.migrate_fe_users --apply             # tulis ke database
    python -m tools.migrate_fe_users --db-json X --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):  # jalankan sebagai skrip langsung
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.auth.models import User  # noqa: E402
from app.database import SessionLocal, database_url  # noqa: E402


def default_db_json() -> Path | None:
    """Cari `.dev-data/db.json` repo FE di sebelah repo ini (dev lokal)."""
    here = Path(__file__).resolve().parent.parent
    for name in ("Front-End-Web-Mahnwa", "fe", "frontend"):
        cand = here.parent / name / ".dev-data" / "db.json"
        if cand.is_file():
            return cand
    return None


def parse_created_at(raw: object) -> datetime:
    if isinstance(raw, str) and raw.strip():
        try:
            return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def load_fe_users(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    users = data.get("users")
    return [u for u in users if isinstance(u, dict)] if isinstance(users, list) else []


def plan(db: Session, fe_users: list[dict]) -> tuple[list[User], list[tuple[str, str]]]:
    """Kembalikan (baris baru, dilewati). Tidak menulis apa pun."""
    existing_usernames = set(db.scalars(select(User.username)).all())
    existing_ids = set(db.scalars(select(User.id)).all())
    to_insert: list[User] = []
    skipped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in fe_users:
        if str(raw.get("role") or "user") == "admin":
            skipped.append((str(raw.get("username") or "?"), "peran admin"))
            continue
        username = str(raw.get("username") or "").strip().lower()
        if not username:
            skipped.append(("?", "tanpa username"))
            continue
        user_id = str(raw.get("id") or "").strip()
        if username in existing_usernames or username in seen:
            skipped.append((username, "username sudah ada di BE"))
            continue
        if user_id and user_id in existing_ids:
            skipped.append((username, "id sudah ada di BE"))
            continue
        seen.add(username)
        to_insert.append(
            User(
                id=user_id or username,
                username=username,
                password_hash=(str(raw.get("passwordHash")).strip() if raw.get("passwordHash") else None),
                email=(str(raw["email"]).strip() if raw.get("email") else None),
                avatar_url=(str(raw["avatarUrl"]).strip() if raw.get("avatarUrl") else None),
                created_at=parse_created_at(raw.get("createdAt")),
            )
        )
    return to_insert, skipped


def run(db_json: Path, *, apply: bool) -> dict:
    fe_users = load_fe_users(db_json)
    db = SessionLocal()
    try:
        to_insert, skipped = plan(db, fe_users)
        if apply:
            for user in to_insert:
                db.add(user)
            db.commit()
        return {
            "total": len(fe_users),
            "inserted": len(to_insert) if apply else 0,
            "would_insert": len(to_insert),
            "skipped": skipped,
            "applied": apply,
        }
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrasi akun pembaca FE -> BE.")
    parser.add_argument("--db-json", type=Path, default=None, help="lokasi db.json FE (otomatis bila kosong)")
    parser.add_argument("--apply", action="store_true", help="tulis ke database (default: dry-run)")
    args = parser.parse_args(argv)

    db_json = args.db_json or default_db_json()
    if db_json is None or not db_json.is_file():
        print("db.json FE tidak ditemukan; isi --db-json secara eksplisit.", file=sys.stderr)
        return 2
    if not database_url():
        print("DATABASE_URL kosong; isi di .env.dev lalu coba lagi.", file=sys.stderr)
        return 2

    report = run(db_json, apply=args.apply)
    mode = "APPLY" if report["applied"] else "DRY-RUN"
    print(f"[{mode}] db.json: {db_json}")
    print(f"  user pembaca dibaca : {report['total']}")
    print(f"  {'dimasukkan' if report['applied'] else 'akan dimasukkan'}        : {report['would_insert']}")
    for username, reason in report["skipped"]:
        print(f"  dilewati            : {username} ({reason})")
    if not report["applied"] and report["would_insert"]:
        print("Jalankan ulang dengan --apply untuk menulis.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
