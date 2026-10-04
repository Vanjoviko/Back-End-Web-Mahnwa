import pytest

from app.scan.errors import ScanError
from app.scan.netguard import GuardPolicy, check_url, host_allowed, resolve_target

STRICT = GuardPolicy(allowed_hosts=frozenset({"sumber.example"}))
WITH_HTTP = GuardPolicy(allowed_hosts=frozenset({"sumber.example"}), allow_http=True)

# Tabel NFR-02 (harus ditolak, tanpa koneksi keluar)
BLOCKED = [
    "http://127.0.0.1",
    "http://[::1]",
    "http://10.0.0.5",
    "http://172.16.0.1",
    "http://192.168.1.1",
    "http://169.254.169.254/latest/meta-data",
    "http://100.64.0.1",
    "http://0.0.0.0",
    "http://[::ffff:10.0.0.1]",
    "http://2130706433",
    "http://0x7f.0.0.1",
    "http://localhost.",
    "http://localhost",
    "http://foo.localhost",
    "http://printer.local",
    "http://db.internal",
    "http://0177.0.0.1",
    "http://127.1",
    "http://[::ffff:7f00:1]",
    "http://[64:ff9b::7f00:1]",
    "http://[fe80::1]",
    "http://[fc00::1]",
    "http://224.0.0.1",
    "http://198.18.0.1",
    "http://192.0.0.8",
    "http://0.1.2.3",
    "http://240.0.0.1",
    "http://example.123",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked_urls_rejected_with_http_allowed(url):
    with pytest.raises(ScanError) as e:
        check_url(url, WITH_HTTP)
    assert e.value.code in {"SSRF_BLOCKED", "INVALID_URL"}, url
    assert e.value.code == "SSRF_BLOCKED", url


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked_urls_rejected_in_strict_mode(url):
    with pytest.raises(ScanError):
        check_url(url, STRICT)


@pytest.mark.parametrize(
    "url,code",
    [
        ("ftp://sumber.example/x", "INVALID_URL"),
        ("https://user:pw@sumber.example/", "INVALID_URL"),
        ("https://sumber.example@evil.example/", "INVALID_URL"),
        ("bukan url", "INVALID_URL"),
        ("", "INVALID_URL"),
        ("https://" + "a" * 2100 + ".example/", "INVALID_URL"),
        ("http://sumber.example/", "INVALID_URL"),
        ("https://lain.example/", "HOST_NOT_ALLOWED"),
        ("https://sumber.example:8443/", "HOST_NOT_ALLOWED"),
    ],
)
def test_invalid_and_not_allowlisted(url, code):
    with pytest.raises(ScanError) as e:
        check_url(url, STRICT)
    assert e.value.code == code


def test_valid_https_allowlisted():
    t = check_url("https://Sumber.Example./series/x", STRICT)
    assert t.host == "sumber.example" and t.port == 443


def test_allowlist_with_port():
    p = GuardPolicy(allowed_hosts=frozenset({"sumber.example:8443"}))
    assert check_url("https://sumber.example:8443/x", p).port == 8443
    with pytest.raises(ScanError):
        check_url("https://sumber.example/x", p)
    assert host_allowed("a.example", 443, "https", frozenset({"a.example"}))


async def test_hostname_resolving_to_private_ip_is_blocked():
    async def resolver(host, port):
        return ["10.0.0.5"]

    policy = GuardPolicy(allowed_hosts=frozenset({"rebind.example"}), resolver=resolver)
    target = check_url("https://rebind.example/x", policy)
    with pytest.raises(ScanError) as e:
        await resolve_target(target, policy)
    assert e.value.code == "SSRF_BLOCKED"


async def test_any_private_among_multiple_records_is_blocked():
    async def resolver(host, port):
        return ["93.184.216.34", "127.0.0.1"]

    policy = GuardPolicy(allowed_hosts=frozenset({"mixed.example"}), resolver=resolver)
    with pytest.raises(ScanError):
        await resolve_target(check_url("https://mixed.example/", policy), policy)


async def test_public_resolution_passes_and_returns_ip_for_pinning():
    async def resolver(host, port):
        return ["93.184.216.34"]

    policy = GuardPolicy(allowed_hosts=frozenset({"ok.example"}), resolver=resolver)
    assert await resolve_target(check_url("https://ok.example/", policy), policy) == ["93.184.216.34"]


def test_dev_mode_allows_loopback_only_for_allowlisted_hosts():
    policy = GuardPolicy(allowed_hosts=frozenset({"127.0.0.1:9999"}), allow_http=True, allow_private_for_allowlisted=True)
    assert check_url("http://127.0.0.1:9999/x", policy).port == 9999
    with pytest.raises(ScanError) as e:
        check_url("http://127.0.0.1:9998/x", policy)  # port tidak di-allowlist-kan
    assert e.value.code == "SSRF_BLOCKED"
    with pytest.raises(ScanError):
        check_url("http://10.0.0.5/x", policy)
