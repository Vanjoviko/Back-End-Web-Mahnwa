from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


class ScanError(Exception):
    """Galat terklasifikasi. `code` mengikuti daftar kode baku di spesifikasi §4.1."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
        retry_after: float | None = None,
        extra: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.extra}


ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def redact_url(url: str) -> str:
    """Hilangkan userinfo, query, dan fragment dari URL sebelum masuk log (NFR-11)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parts.port}" if parts.port else ""
        return f"{parts.scheme}://{host}{port}{parts.path}"
    except ValueError:
        return "<url-tidak-valid>"
