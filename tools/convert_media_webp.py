"""Migrasi satu kali: konversi berkas media lama (halaman chapter) ke WebP + perbarui db.json.

Latar:
- Halaman BARU sudah dikonversi worker BE (SCAN_CONVERT_WEBP=true, kualitas SCAN_WEBP_QUALITY).
- Berkas yang sudah ada sebelum fitur itu perlu dikonversi mundur (backfill).

Aturan:
- Hanya berkas halaman yang dikonversi (mis. `001.jpg`, `002.png`).
- Sampul (`cover-*`) TIDAK dikonversi: kontrak BE-05 (ekstensi dari magic bytes) tetap utuh.
- Berkas yang sudah `.webp` dilewati. WebP hanya dipakai bila lebih kecil dari aslinya.
- `db.json` diperbarui (`image_url` lama -> baru) agar rujukan tetap valid.

Default DRY-RUN (tidak mengubah apa pun). Tambahkan `--apply` untuk mengeksekusi.
Berkas asli DIPINDAHKAN ke folder backup (bukan dihapus) kecuali `--no-backup`.

Contoh:
    python -m tools.convert_media_webp                      # tinjau (dry-run) folder FE otomatis
    python -m tools.convert_media_webp --apply               # eksekusi + backup otomatis
    python -m tools.convert_media_webp --media-dir X --db-json Y --quality 80 --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

if __package__ in (None, ""):  # jalankan sebagai skrip langsung
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.scan import imaging  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MEDIA_URL_PREFIX = "/media/"


def default_media_dir() -> Path | None:
    """Cari `.dev-data/media` repo FE di sebelah repo ini (dev lokal)."""
    here = Path(__file__).resolve().parent.parent
    for name in ("Front-End-Web-Mahnwa", "fe", "frontend"):
        cand = here.parent / name / ".dev-data" / "media"
        if cand.is_dir():
            return cand
    return None


def iter_pages(media_dir: Path):
    """Semua berkas halaman (kecuali sampul `cover-*`), urut deterministik."""
    for path in sorted(media_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if path.name.startswith("cover-") or path.name.endswith(".part"):
            continue
        yield path


def plan(media_dir: Path, quality: int):
    """Kembalikan (rencana, dilewati): rencana = list dict {old,new,old_size,new_size}."""
    todo, skipped = [], []
    for path in iter_pages(media_dir):
        if path.suffix.lower() == ".webp":
            skipped.append((path, "sudah webp"))
            continue
        raw = path.read_bytes()
        converted = imaging.encode_webp(raw, quality)
        if converted is None:
            skipped.append((path, "tak dapat didekode"))
            continue
        if len(converted) >= len(raw):
            skipped.append((path, "webp tidak lebih kecil"))
            continue
        target = path.with_suffix(".webp")
        todo.append({"old": path, "new": target, "old_size": len(raw), "new_size": len(converted), "data": converted})
    return todo, skipped


def build_url_map(media_dir: Path, todo) -> dict[str, str]:
    mapping = {}
    for item in todo:
        old_rel = item["old"].relative_to(media_dir).as_posix()
        new_rel = item["new"].relative_to(media_dir).as_posix()
        mapping[MEDIA_URL_PREFIX + old_rel] = MEDIA_URL_PREFIX + new_rel
    return mapping


def rewrite_urls(node, url_map: dict[str, str]) -> int:
    """Ganti semua nilai string yang persis sama dengan URL di url_map (rekursif)."""
    changed = 0
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value in url_map:
                node[key] = url_map[value]
                changed += 1
            else:
                changed += rewrite_urls(value, url_map)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            if isinstance(value, str) and value in url_map:
                node[i] = url_map[value]
                changed += 1
            else:
                changed += rewrite_urls(value, url_map)
    return changed


def human(n: float) -> str:
    return f"{n / 1048576:.1f} MiB"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Konversi halaman media lama ke WebP + perbarui db.json.")
    parser.add_argument("--media-dir", type=Path, default=None, help="Folder media (default: .dev-data/media repo FE).")
    parser.add_argument("--db-json", type=Path, default=None, help="db.json (default: <media-dir>/../db.json).")
    parser.add_argument("--quality", type=int, default=80, help="Kualitas WebP (default 80).")
    parser.add_argument("--apply", action="store_true", help="Benar-benar menulis (default tanpa: dry-run).")
    parser.add_argument("--backup-dir", type=Path, default=None, help="Backup berkas asli (default: <db.json>/../webp-backup-<ts>).")
    parser.add_argument("--no-backup", action="store_true", help="JANGAN backup; berkas asli dihapus setelah konversi.")
    parser.add_argument("--limit", type=int, default=0, help="Batasi jumlah berkas (0 = semua); untuk uji coba.")
    args = parser.parse_args(argv)

    media_dir = (args.media_dir or default_media_dir())
    if not media_dir or not Path(media_dir).is_dir():
        print("ERROR: folder media tidak ditemukan. Pakai --media-dir.", file=sys.stderr)
        return 2
    media_dir = Path(media_dir).resolve()
    db_json = Path(args.db_json).resolve() if args.db_json else (media_dir.parent / "db.json")

    print(f"media-dir : {media_dir}")
    print(f"db.json   : {db_json}  ({'ada' if db_json.is_file() else 'TIDAK ADA'})")
    print(f"kualitas  : q{args.quality}   mode: {'APPLY' if args.apply else 'DRY-RUN'}\n")

    todo, skipped = plan(media_dir, max(1, min(100, args.quality)))
    if args.limit:
        todo = todo[: args.limit]

    old_total = sum(i["old_size"] for i in todo)
    new_total = sum(i["new_size"] for i in todo)
    print(f"akan dikonversi : {len(todo)} berkas  {human(old_total)} -> {human(new_total)}  ({(new_total/old_total-1)*100:+.0f}%)" if todo else "akan dikonversi : 0 berkas")
    print(f"dilewati        : {len(skipped)} berkas")
    from collections import Counter
    why = Counter(reason for _, reason in skipped)
    for reason, count in why.most_common():
        print(f"    - {reason}: {count}")

    if todo:
        print("\ncontoh (5):")
        for item in todo[:5]:
            print(f"    {item['old'].relative_to(media_dir)}  {item['old_size']/1024:.0f}KB -> {item['new_size']/1024:.0f}KB")

    if not args.apply:
        print("\nDRY-RUN selesai. Tidak ada berkas diubah. Tambahkan --apply untuk mengeksekusi.")
        return 0

    if not todo:
        print("\nTidak ada yang perlu dikonversi.")
        return 0

    backup_dir = None
    if not args.no_backup:
        backup_dir = Path(args.backup_dir).resolve() if args.backup_dir else (db_json.parent / f"webp-backup-{time.strftime('%Y%m%d-%H%M%S')}")
        backup_dir.mkdir(parents=True, exist_ok=True)
        print(f"\nbackup asli -> {backup_dir}")

    written = 0
    for item in todo:
        old, new = item["old"], item["new"]
        new.write_bytes(item["data"])            # tulis .webp dulu
        if backup_dir is not None:
            dest = backup_dir / old.relative_to(media_dir)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(dest))     # pindahkan asli ke backup
        else:
            old.unlink(missing_ok=True)
        written += 1

    # --- perbarui db.json ---
    urls_updated = 0
    if db_json.is_file():
        url_map = build_url_map(media_dir, todo)
        data = json.loads(db_json.read_text(encoding="utf-8"))
        urls_updated = rewrite_urls(data, url_map)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_json = db_json.with_name(f"{db_json.name}.bak-{stamp}")
        shutil.copy2(db_json, backup_json)
        tmp = db_json.with_suffix(db_json.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(db_json)
        print(f"db.json diperbarui: {urls_updated} URL  (cadangan: {backup_json.name})")
    else:
        print("db.json tidak ada: URL tidak diperbarui (media saja).")

    print(f"\nAPPLY selesai: {written} berkas dikonversi, {human(old_total)} -> {human(new_total)} ({(new_total/old_total-1)*100:+.0f}%).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
