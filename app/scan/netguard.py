"""SSRF guard tunggal untuk worker (NFR-02).

Aturan: skema https (http hanya mode dev), tanpa userinfo, port default atau
allowlist, tolak IP/hostname privat/lokal/reserved (termasuk IPv4-mapped IPv6,
notasi desimal/heksa), validasi setiap hop redirect, dan IP hasil resolve dipakai
untuk koneksi (pinning) supaya tahan DNS rebinding.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from urllib.parse import SplitResult, urlsplit

from .errors import ScanError

MAX_URL_LENGTH = 2048
BLOCKED_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost"}
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
_NUMERIC_TOKEN = re.compile(r"^(0[xX][0-9a-fA-F]*|\d+)$")

Resolver = Callable[[str, int], Awaitable[list[str]]]


async def default_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ScanError("INVALID_URL", "Host sumber tidak dapat ditemukan.", status=400) from exc
    seen: list[str] = []
    for info in infos:
        address = info[4][0].split("%", 1)[0]
        if address not in seen:
            seen.append(address)
    return seen


@dataclass
class GuardPolicy:
    allowed_hosts: frozenset = frozenset()
    allow_http: bool = False
    # Mode dev: host yang SUDAH ada di allowlist boleh berupa loopback/privat (server fixture lokal).
    allow_private_for_allowlisted: bool = False
    resolver: Resolver = field(default=default_resolver)


@dataclass
class Target:
    scheme: str
    host: str
    port: int
    ip_literal: ipaddress._BaseAddress | None
    split: SplitResult

    @property
    def hostport(self) -> str:
        default = 443 if self.scheme == "https" else 80
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == default else f"{host}:{self.port}"


def _parse_loose_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Notasi IPv4 ala inet_aton: "2130706433", "0x7f.0.0.1", "0177.0.0.1", "127.1"."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4 or not all(_NUMERIC_TOKEN.match(p) for p in parts):
        return None
    numbers: list[int] = []
    for part in parts:
        try:
            if part.lower().startswith("0x"):
                numbers.append(int(part[2:] or "0", 16))
            elif len(part) > 1 and part.startswith("0"):
                numbers.append(int(part, 8))
            else:
                numbers.append(int(part, 10))
        except ValueError:
            return None
    *head, last = numbers
    if any(n > 255 for n in head) or last >= 256 ** (5 - len(numbers)):
        return None
    value = 0
    for n in head:
        value = (value << 8) | n
    value = (value << (8 * (5 - len(numbers)))) | last
    return ipaddress.IPv4Address(value)


def parse_ip_literal(host: str) -> ipaddress._BaseAddress | None:
    candidate = host.strip("[]")
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    return _parse_loose_ipv4(candidate)


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    packed = ip.packed
    if packed[:12] == bytes.fromhex("0064ff9b0000000000000000"):  # NAT64 64:ff9b::/96
        return ipaddress.IPv4Address(packed[12:])
    if ip in ipaddress.ip_network("::/96") and int(ip) > 1:  # IPv4-compatible (usang)
        return ipaddress.IPv4Address(packed[12:])
    return None


def ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None:
            return ip_blocked(embedded)
    return (not ip.is_global) or ip.is_multicast or ip.is_unspecified


def _blocked_name(host: str) -> bool:
    return host in BLOCKED_NAMES or host.endswith(BLOCKED_SUFFIXES)


_ENTRY_RE = re.compile(r"^(?P<host>.+?)(?::(?P<port>\d+))?$")


def host_allowed(host: str, port: int, scheme: str, allowed: frozenset) -> bool:
    """Entri allowlist: "host" (hanya port default skema) atau "host:port"."""
    default = 443 if scheme == "https" else 80
    host = host.lower().rstrip(".")
    for entry in allowed:
        match = _ENTRY_RE.match(entry)
        if not match or match.group("host").lower().rstrip(".") != host:
            continue
        entry_port = match.group("port")
        if (int(entry_port) if entry_port else default) == port:
            return True
    return False


def check_url(url: str, policy: GuardPolicy, *, require_allowlist: bool = True) -> Target:
    """Validasi statis (tanpa DNS, tanpa koneksi). Melempar INVALID_URL / SSRF_BLOCKED / HOST_NOT_ALLOWED."""
    if not isinstance(url, str) or not url.strip():
        raise ScanError("INVALID_URL", "URL wajib diisi.")
    if len(url) > MAX_URL_LENGTH:
        raise ScanError("INVALID_URL", f"URL terlalu panjang (maksimal {MAX_URL_LENGTH} karakter).")
    if any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise ScanError("INVALID_URL", "URL mengandung spasi atau karakter kontrol.")
    try:
        split = urlsplit(url)
        port_value = split.port
    except ValueError as exc:
        raise ScanError("INVALID_URL", "Format URL tidak valid.") from exc
    scheme = split.scheme.lower()
    allowed_schemes = {"https"} | ({"http"} if policy.allow_http else set())
    if scheme not in allowed_schemes:
        label = "HTTPS" if not policy.allow_http else "HTTP(S)"
        raise ScanError("INVALID_URL", f"URL harus memakai {label}.")
    if split.username is not None or split.password is not None or "@" in split.netloc:
        raise ScanError("INVALID_URL", "URL tidak boleh memuat nama pengguna atau kata sandi.")
    raw_host = split.hostname
    if not raw_host:
        raise ScanError("INVALID_URL", "URL tidak memiliki host.")
    host = raw_host.lower().rstrip(".")
    port = port_value or (443 if scheme == "https" else 80)

    literal = parse_ip_literal(host)
    if literal is None and host.split(".")[-1].isdigit():
        # Label terakhir numerik tetapi bukan IPv4 valid: ambigu, tolak.
        raise ScanError("SSRF_BLOCKED", "Alamat host tidak valid atau mengarah ke jaringan privat.", status=400)
    if literal is None and _blocked_name(host):
        raise ScanError("SSRF_BLOCKED", "URL mengarah ke host lokal atau internal.", status=400)

    allowlisted = host_allowed(host, port, scheme, policy.allowed_hosts)
    dev_exempt = policy.allow_private_for_allowlisted and allowlisted
    if literal is not None and ip_blocked(literal) and not dev_exempt:
        raise ScanError("SSRF_BLOCKED", "URL mengarah ke alamat jaringan privat atau terlarang.", status=400)
    if require_allowlist and not allowlisted:
        raise ScanError(
            "HOST_NOT_ALLOWED",
            "Host sumber tidak ada di daftar yang diizinkan (SCAN_ALLOWED_HOSTS).",
            status=422,
        )
    try:
        host_ascii = host.encode("idna").decode("ascii") if literal is None else host
    except UnicodeError as exc:
        raise ScanError("INVALID_URL", "Nama host tidak valid.") from exc
    return Target(scheme, host_ascii, port, literal, split)


async def resolve_target(target: Target, policy: GuardPolicy) -> list[str]:
    """Resolve DNS lalu pastikan SEMUA alamat publik. Hasilnya dipakai untuk koneksi (pinning)."""
    if target.ip_literal is not None:
        return [str(target.ip_literal)]
    addresses = await policy.resolver(target.host, target.port)
    if not addresses:
        raise ScanError("INVALID_URL", "Host sumber tidak dapat ditemukan.")
    allowlisted = host_allowed(target.host, target.port, target.scheme, policy.allowed_hosts)
    exempt = policy.allow_private_for_allowlisted and allowlisted
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip_blocked(ip) and not exempt:
            raise ScanError("SSRF_BLOCKED", "Host sumber di-resolve ke alamat jaringan privat.", status=400)
    return addresses
