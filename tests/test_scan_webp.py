"""Konversi WebP halaman (SCAN_CONVERT_WEBP/SCAN_WEBP_QUALITY).

Berbeda dari berkas pengujian lain yang memakai magic-bytes palsu, di sini gambar
dibuat sungguhan lewat Pillow supaya jalur enkode benar-benar dieksekusi.
"""
from __future__ import annotations

import io
import hashlib

import pytest

from app.config import Settings
from app.scan import imaging
from .conftest import simple_manifest

WEBP_MAGIC = b"RIFF"


def real_image(fmt: str, size=(64, 64)) -> bytes:
    Image = pytest.importorskip("PIL.Image")
    # Derau + blok warna: PNG (lossless) jadi besar, WebP lossy menang -> menguji konversi nyata.
    im = Image.effect_noise(size, 64).convert("RGB")
    block = max(8, size[0] // 4)
    for x in range(0, size[0], block):
        for y in range(0, size[1], block):
            for dx in range(min(block, size[0] - x)):
                for dy in range(min(block, size[1] - y)):
                    im.putpixel((x + dx, y + dy), ((x * 7) % 255, (y * 5) % 255, 90))
    out = io.BytesIO()
    im.save(out, fmt, **({"quality": 90} if fmt in {"JPEG", "WEBP"} else {}))
    return out.getvalue()


def is_webp(data: bytes) -> bool:
    return data[:4] == WEBP_MAGIC and data[8:12] == b"WEBP"


# ------------------------------------------------------------------ unit
def test_encode_webp_jpeg_smaller_and_valid():
    src = real_image("JPEG", (96, 96))
    out = imaging.encode_webp(src, 80)
    assert out is not None and is_webp(out)
    assert len(out) < len(src)


def test_encode_webp_png_to_webp():
    src = real_image("PNG", (96, 96))
    out = imaging.encode_webp(src, 80)
    assert out is not None and is_webp(out)


def test_encode_webp_returns_none_for_garbage():
    assert imaging.encode_webp(b"\xff\xd8\xff\xe0" + b"\x00" * 200, 80) is None
    assert imaging.encode_webp(b"bukan gambar sama sekali", 80) is None


# ------------------------------------------------------------------ config
def test_settings_from_env_webp_defaults_and_overrides():
    on = Settings.from_env({"SCAN_DEV_MODE": "true", "SCAN_CONVERT_WEBP": "true", "SCAN_WEBP_QUALITY": "70"})
    assert on.convert_webp is True and on.webp_quality == 70
    off = Settings.from_env({"SCAN_DEV_MODE": "true"})
    assert off.convert_webp is False and off.webp_quality == 80
    assert Settings.from_env({"SCAN_DEV_MODE": "true", "SCAN_WEBP_QUALITY": "999"}).webp_quality == 100
    assert Settings.from_env({"SCAN_DEV_MODE": "true", "SCAN_WEBP_QUALITY": "0"}).webp_quality == 1


# ------------------------------------------------------- end-to-end engine
async def _run_one_page(w, source, image_bytes, ctype, *, extra=None):
    source.add_image("/p/001.png", image_bytes, ctype)
    manifest = simple_manifest(source, [("Chapter 1", ["p/001.png"])])
    if extra:
        manifest.update(extra)
    source.add_manifest("/s.json", manifest)
    url = source.url("/s.json")
    info = (await w.scan(url)).json()
    await w.start_job("j1", url, [{"key": "c1", "ref": info["chapters"][0]["ref"]}])
    final = await w.wait("j1")
    assert final["chapters"][0]["status"] == "COMPLETED", final
    m = (await w.client.get("/worker/v1/jobs/j1/chapters/c1/manifest", headers=w.h())).json()
    body = (await w.client.get("/worker/v1/jobs/j1/chapters/c1/pages/1", headers=w.h())).content
    return m["pages"][0], body


async def test_page_converted_to_webp_when_enabled(make_worker, source):
    w = await make_worker(source, convert_webp=True, webp_quality=80)
    page, body = await _run_one_page(w, source, real_image("JPEG", (120, 120)), "image/jpeg")
    assert page["content_type"] == "image/webp"
    assert is_webp(body)
    # integritas: manifest harus cocok dengan byte yang disajikan
    assert page["bytes"] == len(body) and page["sha256"] == hashlib.sha256(body).hexdigest()
    # berkas di staging benar-benar .webp
    files = [p.name for p in w.state.staging.chapter_dir("j1", "c1").iterdir()]
    assert files == ["001.webp"]


async def test_page_keeps_original_format_when_disabled(make_worker, source):
    w = await make_worker(source, convert_webp=False)
    page, body = await _run_one_page(w, source, real_image("PNG", (120, 120)), "image/png")
    assert page["content_type"] == "image/png" and body[:8] == imaging.PNG_SIGNATURE


async def test_page_already_webp_not_reencoded(make_worker, source):
    w = await make_worker(source, convert_webp=True)
    src = real_image("WEBP", (120, 120))
    page, body = await _run_one_page(w, source, src, "image/webp")
    assert page["content_type"] == "image/webp"
    assert body == src  # tidak dienkode ulang


async def test_chapter_budget_reflects_converted_size(make_worker, source):
    """Batas byte chapter dihitung dari ukuran SETELAH konversi (WebP)."""
    src = real_image("PNG", (400, 400))
    w = await make_worker(source, convert_webp=True, max_bytes_per_chapter=len(src) + 10,
                          max_bytes_per_image=len(src) + 10)
    page, body = await _run_one_page(w, source, src, "image/png")
    assert page["content_type"] == "image/webp" and page["bytes"] == len(body) < len(src)
