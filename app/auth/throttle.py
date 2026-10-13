"""Pembatas percobaan login kata sandi (L8, PRD §10).

Setelah 5 kegagalan untuk satu nama pengguna dalam 15 menit, percobaan
berikutnya ditolak. Ada juga batas per alamat IP (lebih longgar) agar satu
sumber tidak bisa menyapu banyak nama pengguna.

Disimpan di memori proses: cukup untuk satu instance backend. Bila kelak jalan
banyak instance, ganti penyimpanannya (mis. Redis) tanpa mengubah antarmuka.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

WINDOW_SECONDS = 15 * 60
MAX_FAILURES_PER_USER = 5
MAX_FAILURES_PER_IP = 20


@dataclass
class LoginThrottle:
    max_per_user: int = MAX_FAILURES_PER_USER
    max_per_ip: int = MAX_FAILURES_PER_IP
    window_seconds: float = WINDOW_SECONDS
    _by_user: dict[str, list[float]] = field(default_factory=dict)
    _by_ip: dict[str, list[float]] = field(default_factory=dict)

    def _recent(self, bucket: dict[str, list[float]], key: str | None, now: float) -> list[float]:
        if not key:
            return []
        times = [t for t in bucket.get(key, []) if now - t < self.window_seconds]
        if times:
            bucket[key] = times
        else:
            bucket.pop(key, None)
        return times

    def retry_after(self, username: str | None, ip: str | None, *, now: float | None = None) -> int:
        """Detik sampai percobaan boleh dicoba lagi; 0 bila belum diblokir."""
        now = time.monotonic() if now is None else now
        for bucket, key, limit in (
            (self._by_user, username, self.max_per_user),
            (self._by_ip, ip, self.max_per_ip),
        ):
            times = self._recent(bucket, key, now)
            if times and len(times) >= limit:
                remaining = self.window_seconds - (now - min(times))
                return max(1, int(remaining) + 1)
        return 0

    def record_failure(self, username: str | None, ip: str | None, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for bucket, key in ((self._by_user, username), (self._by_ip, ip)):
            if key:
                bucket.setdefault(key, []).append(now)

    def reset(self, username: str | None = None, ip: str | None = None) -> None:
        if username:
            self._by_user.pop(username, None)
        if ip:
            self._by_ip.pop(ip, None)


__all__ = [
    "MAX_FAILURES_PER_IP",
    "MAX_FAILURES_PER_USER",
    "WINDOW_SECONDS",
    "LoginThrottle",
]
