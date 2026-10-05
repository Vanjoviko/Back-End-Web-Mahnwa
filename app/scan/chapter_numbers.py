"""Kebijakan nomor chapter (BE-13). Satu implementasi; aturannya menjadi kontrak bersama dengan FE.

Nomor disimpan sebagai *string kanonik* `^(0|[1-9]\\d*)(\\.\\d+)?$` atau `None`.
Perbandingan memakai Decimal, bukan float.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable, Sequence

CANONICAL_RE = re.compile(r"^(0|[1-9]\d*)(\.\d+)?$")

_KEYWORD_NUMBER = re.compile(
    r"(?<![A-Za-z])(?:chapter|chap|ch|bab|episode|ep)(?![A-Za-z])\.?\s*#?\s*(\d{1,6}(?:\.\d{1,4})?)(?!\d)(\s*[-–—~]\s*\d)?",
    re.IGNORECASE,
)
_SPECIAL = re.compile(r"(?<![A-Za-z])(extra|special|side|oneshot|one-shot|epilog|epilogue|omake|bonus)(?![A-Za-z])", re.IGNORECASE)
_PROLOG = re.compile(r"(?<![A-Za-z])(prolog|prologue)(?![A-Za-z])", re.IGNORECASE)
_FIRST_NUMBER = re.compile(r"(?<![\d.])(\d{1,6}(?:\.\d{1,4})?)(?!\d)(\s*[-–—~]\s*\d)?")


def canonicalize(value: str | int | float | Decimal | None) -> str | None:
    """Ubah "007" -> "7", "10.0" -> "10", "10.50" -> "10.5". Mengembalikan None bila bukan angka."""
    if value is None:
        return None
    text = str(value).strip()
    if not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    integer, _, fraction = text.partition(".")
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    result = f"{integer}.{fraction}" if fraction else integer
    return result if CANONICAL_RE.match(result) else None


def to_decimal(number: str | None) -> Decimal | None:
    return Decimal(number) if number is not None and CANONICAL_RE.match(number) else None


@dataclass
class ParsedNumber:
    number_raw: str
    number: str | None
    issues: list[str] = field(default_factory=list)
    is_prolog: bool = False


def parse_single(raw: str) -> ParsedNumber:
    """Parse satu label tanpa konteks batch (prolog belum dicek bentrokannya)."""
    text = (raw or "").strip()
    keyword = _KEYWORD_NUMBER.search(text)
    if keyword:
        if keyword.group(2):  # "Chapter 1-2": rentang -> keputusan manual (Q4, pilihan konservatif)
            return ParsedNumber(raw, None, ["needs_number", "ambiguous_number"])
        return ParsedNumber(raw, canonicalize(keyword.group(1)), [])
    if _SPECIAL.search(text):
        return ParsedNumber(raw, None, ["needs_number"])
    if _PROLOG.search(text):
        return ParsedNumber(raw, "0", [], is_prolog=True)
    first = _FIRST_NUMBER.search(text)
    if first:
        if first.group(2):
            return ParsedNumber(raw, None, ["needs_number", "ambiguous_number"])
        return ParsedNumber(raw, canonicalize(first.group(1)), [])
    return ParsedNumber(raw, None, ["needs_number"])


def analyze(raws: Sequence[str]) -> list[ParsedNumber]:
    """Parse satu batch: prolog yang bentrok dengan "0" lain -> null; nomor kembar diberi flag."""
    parsed = [parse_single(r) for r in raws]
    explicit_zero = any(p.number == "0" and not p.is_prolog for p in parsed)
    prologs = [p for p in parsed if p.is_prolog]
    for p in prologs:
        if explicit_zero or len(prologs) > 1:
            p.number = None
            p.issues = ["needs_number"]
    counts: dict[str, int] = {}
    for p in parsed:
        if p.number is not None:
            counts[p.number] = counts.get(p.number, 0) + 1
    for p in parsed:
        if p.number is not None and counts[p.number] > 1:
            p.issues.append("duplicate_number")
    return parsed


def sort_desc(items: Iterable, key=lambda item: item["number"]) -> list:
    """Urut menurun numerik, null di akhir; stabil terhadap urutan sumber."""
    items = list(items)
    numeric = [(to_decimal(key(i)), idx, i) for idx, i in enumerate(items)]
    with_number = sorted((t for t in numeric if t[0] is not None), key=lambda t: (-t[0], t[1]))
    without = [t for t in numeric if t[0] is None]
    return [t[2] for t in with_number] + [t[2] for t in without]
