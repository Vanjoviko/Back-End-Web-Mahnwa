"""Deteksi format gambar dari magic bytes (BE-05) + konversi ke WebP.

Ekstensi tidak pernah diambil dari URL. Konversi WebP (opsional, nonaktif secara
default) dipakai jalur unduhan halaman supaya berkas tersimpan lebih kecil tanpa
mengubah kontrak integritas: `bytes`/`sha256` manifest dihitung dari hasil konversi.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO

log = logging.getLogger("scan.imaging")

PNG_SIGNATURE = bytes([137, 80, 78, 71, 13, 10, 26, 10])


@dataclass(frozen=True)
class ImageFormat:
    name: str
    extension: str
    content_type: str


JPEG = ImageFormat("jpeg", ".jpg", "image/jpeg")
PNG = ImageFormat("png", ".png", "image/png")
WEBP = ImageFormat("webp", ".webp", "image/webp")

MIN_SNIFF_BYTES = 12


def sniff(head: bytes) -> ImageFormat | None:
    if head[:3] == b"\xff\xd8\xff":
        return JPEG
    if head[:8] == PNG_SIGNATURE:
        return PNG
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return WEBP
    return None


_CT_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-png": "image/png"}


def content_type_conflicts(header_value: str | None, detected: ImageFormat) -> bool:
    """True bila Content-Type bertentangan dengan format hasil magic bytes."""
    if not header_value:
        return False
    media = header_value.split(";", 1)[0].strip().lower()
    media = _CT_ALIASES.get(media, media)
    if media.startswith("image/"):
        return media != detected.content_type
    if media.startswith("text/") or media in {"application/json", "application/xml", "application/xhtml+xml", "application/javascript"}:
        return True
    return False  # octet-stream / tidak dikenal: percaya magic bytes


WEBP_QUALITY_DEFAULT = 80
WEBP_ENCODE_METHOD = 4  # 4 = kualitas/kecepatan seimbang; 6 = paling lambat & paling kecil


def _rgb_mode(im) -> str:
    """Mode target WebP: pertahankan transparansi bila ada, jika tidak RGB."""
    if im.mode in {"RGBA", "LA", "PA"} or (im.mode == "P" and "transparency" in im.info):
        return "RGBA"
    return "RGB"


def encode_webp(data: bytes, quality: int = WEBP_QUALITY_DEFAULT) -> bytes | None:
    """Enkode `data` (JPEG/PNG/WebP apa pun) menjadi WebP lossy.

    Mengembalikan bytes WebP, atau `None` bila data bukan gambar yang dapat
    didekode (mis. berkas rusak/bukan gambar). Pemanggil bertanggung jawab
    memutuskan apakah hasilnya layak dipakai (biasanya hanya bila lebih kecil).
    """
    try:
        from PIL import Image  # impor lokal: Pillow opsional, fitur gagal-aman
    except Exception:  # pragma: no cover - Pillow adalah dependensi wajib di requirements
        log.warning("pillow_tidak_tersedia")
        return None
    try:
        with Image.open(BytesIO(data)) as im:
            im.load()
            mode = _rgb_mode(im)
            if im.mode != mode:
                im = im.convert(mode)
            out = BytesIO()
            im.save(out, "WEBP", quality=int(quality), method=WEBP_ENCODE_METHOD)
            return out.getvalue()
    except Exception as exc:
        log.info("encode_webp_gagal error=%s", type(exc).__name__)
        return None


CHALLENGE_MARKERS = (
    b"captcha",
    b"verify you are human",
    b"checking your browser",
    b"cf-challenge",
    b"attention required",
    b"access denied",
)


def looks_like_challenge(sample: bytes) -> bool:
    lowered = sample[:4096].lower()
    return any(marker in lowered for marker in CHALLENGE_MARKERS)
