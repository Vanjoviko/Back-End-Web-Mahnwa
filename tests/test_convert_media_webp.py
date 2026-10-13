"""Tes alat migrasi media lama ke WebP (`tools/convert_media_webp.py`).

Memakai gambar sungguhan (derau) supaya WebP benar-benar lebih kecil; berkas
dibuat di direktori sementara, jadi tidak menyentuh data nyata.
"""
from __future__ import annotations

import json

import pytest

from tools import convert_media_webp as mig


def make_image(path, fmt="JPEG", size=(200, 200)):
    Image = pytest.importorskip("PIL.Image")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.effect_noise(size, 64).convert("RGB").save(path, fmt, quality=90)


def test_plan_converts_pages_and_skips_covers_and_webp(tmp_path):
    media = tmp_path / "media"
    make_image(media / "seri" / "cover-abc.jpg")                        # sampul -> tak diproses
    make_image(media / "seri" / "chapters" / "chapter-1" / "001.jpg")   # halaman -> dikonversi
    make_image(media / "seri" / "chapters" / "chapter-1" / "002.webp", fmt="WEBP")  # sudah webp -> dilewati

    todo, skipped = mig.plan(media, 80)
    assert [i["old"].name for i in todo] == ["001.jpg"]
    skipped_names = {p.name for p, _ in skipped}
    assert skipped_names == {"002.webp"}
    assert all(i["old"].name != "cover-abc.jpg" for i in todo)


def test_dry_run_changes_nothing(tmp_path):
    media = tmp_path / "media"
    db = tmp_path / "db.json"
    make_image(media / "seri" / "chapters" / "chapter-1" / "001.jpg")
    db.write_text(json.dumps({"comics": [{"chapters": [{"pages": [{"image_url": "/media/seri/chapters/chapter-1/001.jpg"}]}]}]}))

    assert mig.main(["--media-dir", str(media), "--db-json", str(db)]) == 0
    assert (media / "seri" / "chapters" / "chapter-1" / "001.jpg").exists()
    assert not (media / "seri" / "chapters" / "chapter-1" / "001.webp").exists()
    url = json.loads(db.read_text())["comics"][0]["chapters"][0]["pages"][0]["image_url"]
    assert url.endswith("001.jpg")


def test_apply_converts_updates_db_backs_up_and_leaves_cover(tmp_path):
    media = tmp_path / "media"
    db = tmp_path / "db.json"
    page = media / "seri" / "chapters" / "chapter-1" / "001.jpg"
    make_image(page)
    db.write_text(json.dumps({
        "comics": [{
            "cover": "/media/seri/cover-abc.jpg",
            "chapters": [{"pages": [{"image_url": "/media/seri/chapters/chapter-1/001.jpg"}]}],
        }],
    }))
    backup = tmp_path / "bak"

    assert mig.main(["--media-dir", str(media), "--db-json", str(db), "--apply", "--backup-dir", str(backup)]) == 0

    assert not page.exists()                                              # asli dipindah
    assert (media / "seri" / "chapters" / "chapter-1" / "001.webp").is_file()
    assert (backup / "seri" / "chapters" / "chapter-1" / "001.jpg").is_file()
    data = json.loads(db.read_text())
    assert data["comics"][0]["chapters"][0]["pages"][0]["image_url"].endswith("001.webp")
    assert data["comics"][0]["cover"] == "/media/seri/cover-abc.jpg"      # sampul tak disentuh
    assert list(tmp_path.glob("db.json.bak-*"))                           # cadangan db.json


def test_missing_media_dir_returns_error(tmp_path):
    assert mig.main(["--media-dir", str(tmp_path / "tidak-ada"), "--db-json", str(tmp_path / "db.json")]) == 2
