"""Pengamanan log (NFR-11, QA D-05): query string / token pada URL tidak boleh tercetak ke log mana pun.

Dua lapis:
1. logger pustaka HTTP (`httpx`, `httpcore`) diturunkan ke WARNING — pada INFO httpx mencetak URL permintaan lengkap beserta query.
2. `install_log_redaction()` membungkus pembuat LogRecord: setiap pesan (termasuk dari uvicorn.access dan pustaka lain)
   diformat lalu bagian `?query` dan `user:pass@` pada URL diganti penanda, sehingga pelanggaran di masa depan tetap tertutup.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping

QUIET_LOGGERS = ("httpx", "httpcore")

# `?...` hingga spasi/kutip/penutup; minimal satu karakter setelah '?' agar tanda tanya biasa dalam kalimat tidak terkena.
_QUERY = re.compile(r"\?(?!\[diredaksi\])[^\s\"'<>)\]]+")  # idempoten: penanda yang sudah ada tidak diredaksi lagi
_USERINFO = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@\"']+@")
_MARK = "?[diredaksi]"


def redact_text(text: str) -> str:
    text = _USERINFO.sub(r"\g<scheme>[diredaksi]@", text)
    return _QUERY.sub(_MARK, text)


_installed = False
_PLAIN = (int, float, bool, type(None), bytes)


def _redact_arg(value):
    """Redaksi SATU argumen log tanpa mengubah tipe/jumlahnya (formatter seperti uvicorn.access butuh 5 argumen)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, _PLAIN):
        return value
    try:
        text = str(value)
    except Exception:  # noqa: BLE001
        return value
    redacted = redact_text(text)
    return redacted if redacted != text else value  # objek seperti httpx.URL: ganti hanya bila memuat rahasia


def redact_args(args):
    """Redaksi argumen log dengan mempertahankan bentuk: tuple tetap tuple (panjang sama), dict tetap dict (kunci sama)."""
    if isinstance(args, tuple):
        return tuple(_redact_arg(a) for a in args)
    if isinstance(args, Mapping):
        return {k: _redact_arg(v) for k, v in args.items()}
    return args


def install_log_redaction() -> None:
    """Bungkus pembuat LogRecord agar `msg` dan `args` diredaksi DI DALAM bentuk aslinya (D-05, D-09).

    Pesan tidak diformat-ulang dan `args` tidak dikosongkan, sehingga formatter khusus (uvicorn `AccessFormatter`
    memecah `record.args` menjadi 5 nilai) tetap bekerja. Hanya bila gabungan msg+args ternyata masih membocorkan
    rahasia lintas-batas (mis. msg berakhir `?` dan rahasia ada di argumen), jatuh ke pesan yang sudah diformat+diredaksi.
    """
    global _installed
    if _installed:
        return
    previous = logging.getLogRecordFactory()

    def factory(*factory_args, **kwargs):
        record = previous(*factory_args, **kwargs)
        try:
            original = record.getMessage()
            if redact_text(original) == original:
                return record  # tidak ada rahasia: jangan sentuh record sama sekali
            # Templat `msg` tidak diredaksi bila ada args (mis. "GET %s?%s": "?%s" akan terbaca sebagai query dan merusak placeholder).
            safe_msg = record.msg if record.args else (redact_text(record.msg) if isinstance(record.msg, str) else record.msg)
            safe_args = redact_args(record.args) if record.args else record.args
            candidate = (safe_msg % safe_args) if record.args else safe_msg
            if isinstance(candidate, str) and redact_text(candidate) == candidate:
                record.msg, record.args = safe_msg, safe_args
            else:  # rahasia lintas-batas (templat + argumen) atau templat berisi rahasia: format penuh lalu redaksi
                record.msg, record.args = redact_text(original), ()
        except Exception:  # noqa: BLE001 - jangan pernah merusak logging
            try:
                record.msg, record.args = redact_text(record.getMessage()), ()
            except Exception:  # noqa: BLE001
                pass
        return record

    logging.setLogRecordFactory(factory)
    _installed = True


def quiet_http_loggers() -> None:
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
