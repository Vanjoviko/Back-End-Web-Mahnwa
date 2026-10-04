import pytest

from app.config import Settings
from app.scan.adapters import AdapterRegistry, SourceAdapter
from app.scan.adapters.base import ScanResult
from app.scan.errors import ScanError


class Dummy(SourceAdapter):
    def __init__(self, name, pattern):
        self.name, self.pattern = name, pattern

    def matches(self, url):
        return self.pattern in url

    async def scan(self, url, ctx):
        return ScanResult(title="x", chapters=[])

    async def list_pages(self, ref, ctx):
        return []


SETTINGS = Settings(allowed_hosts=frozenset({"sumber.example"}))


def test_zero_adapters_match():
    reg = AdapterRegistry([Dummy("a", "zzz")], SETTINGS)
    with pytest.raises(ScanError) as e:
        reg.resolve("https://sumber.example/series")
    assert e.value.code == "SOURCE_NOT_SUPPORTED" and e.value.status == 422
    assert e.value.extra["adapters"] == ["a"] and "a" in e.value.message


def test_one_adapter_matches():
    reg = AdapterRegistry([Dummy("a", "series"), Dummy("b", "zzz")], SETTINGS)
    assert reg.resolve("https://sumber.example/series").name == "a"


def test_two_adapters_match_is_ambiguous():
    reg = AdapterRegistry([Dummy("a", "series"), Dummy("b", "sumber")], SETTINGS)
    with pytest.raises(ScanError) as e:
        reg.resolve("https://sumber.example/series")
    assert e.value.code == "AMBIGUOUS_ADAPTER"


def test_host_outside_allowlist_is_not_supported_even_if_pattern_matches():
    reg = AdapterRegistry([Dummy("a", "series")], SETTINGS)
    with pytest.raises(ScanError) as e:
        reg.resolve("https://lain.example/series")
    assert e.value.code == "SOURCE_NOT_SUPPORTED"


def test_default_allowlist_empty_means_no_adapter_active():
    from app.scan.adapters import build_registry

    reg = build_registry(Settings())
    assert reg.active_names() == []
    with pytest.raises(ScanError):
        reg.resolve("https://sumber.example/a.json")


def test_builtin_adapters_are_generic_only():
    from app.scan.adapters import BUILTIN_ADAPTERS

    assert sorted(c.name for c in BUILTIN_ADAPTERS) == ["fixture", "manifest"]
