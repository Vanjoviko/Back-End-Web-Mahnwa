"""Deteksi format gambar dari magic bytes (BE-05). Ekstensi tidak pernah diambil dari URL."""
from __future__ import annotations

from dataclasses import dataclass

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
