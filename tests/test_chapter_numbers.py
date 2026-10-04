import re

import pytest

from app.scan import chapter_numbers as cn


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Chapter 10", "10"),
        ("Chapter 10.5", "10.5"),
        ("Ch. 007", "7"),
        ("Chapter 10.0", "10"),
        ("Prolog", "0"),
        ("Vol.2 Chapter 5", "5"),
        ("Season 2 Ch. 5", "5"),
        ("Bab 12", "12"),
        ("Episode 3", "3"),
        ("Extra", None),
        ("Special Chapter", None),
        ("Oneshot", None),
        ("Epilog", None),
        ("", None),
        ("Tanpa angka", None),
        ("10.50", "10.5"),
        ("7", "7"),
        ("Chapter 1-2", None),  # Q4: rentang -> keputusan manual
    ],
)
def test_parse_single_table(raw, expected):
    assert cn.parse_single(raw).number == expected


def test_never_returns_zero_float_for_missing_number():
    # Memperbaiki scraper.py:15 (tanpa angka -> 0.0)
    result = cn.parse_single("Extra")
    assert result.number is None and "needs_number" in result.issues


def test_prolog_collides_with_chapter_zero_becomes_null():
    parsed = cn.analyze(["Prolog", "Chapter 0", "Chapter 1"])
    assert [p.number for p in parsed] == [None, "0", "1"]
    assert "needs_number" in parsed[0].issues


def test_prolog_alone_is_zero():
    assert [p.number for p in cn.analyze(["Prolog", "Chapter 1"])] == ["0", "1"]


def test_duplicate_numbers_flag_both():
    parsed = cn.analyze(["Chapter 5", "Chapter 5", "Chapter 6"])
    assert parsed[0].issues == ["duplicate_number"] and parsed[1].issues == ["duplicate_number"] and parsed[2].issues == []


def test_1_10_and_1_1_become_duplicates():
    parsed = cn.analyze(["1.10", "1.1"])
    assert [p.number for p in parsed] == ["1.1", "1.1"]
    assert all("duplicate_number" in p.issues for p in parsed)


def test_canonical_regex_property():
    for raw in ["Chapter 007", "Ch 10.50", "Chapter 0", "Chapter 100.001", "9"]:
        number = cn.parse_single(raw).number
        assert re.fullmatch(r"^(0|[1-9]\d*)(\.\d+)?$", number), (raw, number)


def test_canonicalize_rejects_non_numbers():
    assert cn.canonicalize("abc") is None and cn.canonicalize("1e5") is None and cn.canonicalize("-1") is None
    assert cn.canonicalize("010.100") == "10.1"


def test_sort_desc_numeric_not_lexicographic_with_nulls_last():
    items = [{"number": n} for n in ["2", "10", None, "10.5", "9", "0"]]
    assert [i["number"] for i in cn.sort_desc(items)] == ["10.5", "10", "9", "2", "0", None]
