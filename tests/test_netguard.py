import pytest

from app.scan.errors import ScanError
from app.scan.netguard import GuardPolicy, check_url, resolve_target

STRICT = GuardPolicy()
WITH_HTTP = GuardPolicy(allow_http=True)

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
        ("http://sumber.example/", "INVALID_URL"),  # http ditolak tanpa mode dev
    ],
)
def test_invalid_urls(url, code):
    with pytest.raises(ScanError) as e:
        check_url(url, STRICT)
    assert e.value.code == code


def test_any_public_https_host_is_accepted():
    t = check_url("https://Sumber.Example./series/x", STRICT)
    assert t.host == "sumber.example" and t.port == 443
    assert check_url("https://sumber.example:8443/x", STRICT).port == 8443
    assert check_url("https://lain.example/x", STRICT).host == "lain.example"


async def test_hostname_resolving_to_private_ip_is_blocked():
    async def resolver(host, port):
        return ["10.0.0.5"]

    policy = GuardPolicy(resolver=resolver)
    target = check_url("https://rebind.example/x", policy)
    with pytest.raises(ScanError) as e:
        await resolve_target(target, policy)
    assert e.value.code == "SSRF_BLOCKED"


async def test_any_private_among_multiple_records_is_blocked():
    async def resolver(host, port):
        return ["93.184.216.34", "127.0.0.1"]

    policy = GuardPolicy(resolver=resolver)
    with pytest.raises(ScanError):
        await resolve_target(check_url("https://mixed.example/", policy), policy)


async def test_public_resolution_passes_and_returns_ip_for_pinning():
    async def resolver(host, port):
        return ["93.184.216.34"]

    policy = GuardPolicy(resolver=resolver)
    assert await resolve_target(check_url("https://ok.example/", policy), policy) == ["93.184.216.34"]


def test_private_hosts_allowed_only_with_allow_private():
    dev = GuardPolicy(allow_http=True, allow_private=True)
    assert check_url("http://127.0.0.1:9999/x", dev).port == 9999
    assert check_url("http://10.0.0.5/x", dev).host == "10.0.0.5"
    with pytest.raises(ScanError) as e:
        check_url("http://10.0.0.5/x", WITH_HTTP)  # http diizinkan, tetapi privat ditolak
    assert e.value.code == "SSRF_BLOCKED"
