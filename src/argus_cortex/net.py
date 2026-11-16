"""Outbound fetches that can't be steered at the inside (the suite's paved road for SSRF).

When a URL comes from outside (a request, a manifest, a third-party catalogue), fetch it with
:func:`fetch` / :func:`afetch` rather than a bare ``httpx`` call:

* **Resolved addresses are checked, not the name.** The host is resolved once, and *every*
  address must be global: private, loopback, link-local, CGNAT, multicast, reserved and
  unspecified ranges are refused. IPv4 addresses embedded in IPv6 (mapped, compatible, 6to4,
  Teredo, NAT64) are unwrapped and checked as IPv4.
* **The connection is pinned** to the address that was checked, with the original ``Host`` header
  and TLS SNI, so DNS rebinding can't swap in another address between check and connect.
* **Redirects are followed by hand** (GET and HEAD only, at most *max_redirects*), and every hop is
  checked again. Any other method gets the 3xx back, so a request body is never re-sent to a host
  nobody checked.
* **The body is capped** (*max_bytes*; a declared ``Content-Length`` over it is refused before
  reading), and the whole fetch has a *deadline* across every hop and the body (*timeout* alone is
  per read, so a slow drip would never end).
* ``http``/``https`` only, no ``user:password@`` in the URL, and environment proxies are ignored.
* An *allow_hosts* entry (``host`` or ``host:port``) lets an internal service by name skip the
  global-address rule; it is still resolved and pinned.

Everything refused raises :class:`UrlRefused` (a ``ValueError``) with a reason that names the host
but never a resolved address. Design: ``docs/paved-roads.md``. httpx is behind ``[remote]``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

__all__ = [
    "MAX_BYTES",
    "CheckedUrl",
    "FetchFailed",
    "Reply",
    "UrlRefused",
    "afetch",
    "check_url",
    "fetch",
    "is_public_ip",
]

MAX_BYTES = 50 * 1024 * 1024
TIMEOUT_S = 30.0
DEADLINE_S = 120.0
MAX_REDIRECTS = 5
_PORTS = {"http": 80, "https": 443}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_FOLLOW = {"GET", "HEAD"}

IP = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]


class UrlRefused(ValueError):
    """The URL, or a redirect hop, is not one that may be fetched."""


class FetchFailed(RuntimeError):
    """The fetch was allowed but the server answered with an error status (see ``status``)."""

    def __init__(self, status: int, host: str) -> None:
        super().__init__(f"{host} answered {status}")
        self.status = status


@dataclass(frozen=True)
class CheckedUrl:
    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return host if self.port == _PORTS[self.scheme] else f"{host}:{self.port}"

    def pinned(self, ip: str) -> str:
        parts = urlsplit(self.url)
        literal = f"[{ip}]" if ":" in ip else ip
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        return f"{self.scheme}://{literal}:{self.port}{path}"


@dataclass
class Reply:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str
    redirects: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def raise_for_status(self) -> Reply:
        if self.status >= 400:
            raise FetchFailed(self.status, urlsplit(self.url).hostname or "")
        return self


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64 or ip in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    # IPv4-compatible (::a.b.c.d, deprecated): anything in ::/96 except :: and ::1
    if int(ip) >> 32 == 0 and int(ip) > 1:
        return ipaddress.IPv4Address(int(ip))
    return None


def is_public_ip(ip: IP | str) -> bool:
    """Whether *ip* (and any IPv4 address embedded in it) is globally routable unicast."""
    addr = ipaddress.ip_address(str(ip).split("%", 1)[0])
    if isinstance(addr, ipaddress.IPv6Address):
        inner = _embedded_v4(addr)
        if inner is not None and not is_public_ip(inner):
            return False
    return addr.is_global and not addr.is_multicast and not addr.is_unspecified


def _system_resolve(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(i[4][0]) for i in infos))


def _allowed(host: str, port: int, allow_hosts: Iterable[str]) -> bool:
    for entry in allow_hosts:
        e = entry.strip().lower().rstrip(".")
        if not e:
            continue
        if e.startswith("["):  # [v6]:port or [v6]
            h, _, p = e[1:].partition("]")
            p = p.lstrip(":")
        elif e.count(":") == 1:
            h, p = e.split(":")
        else:
            h, p = e, ""
        if h == host and (not p or p == str(port)):
            return True
    return False


def _parse(url: str) -> tuple[str, str, int]:
    if not isinstance(url, str) or not url.strip():
        raise UrlRefused("no URL")
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _PORTS:
        raise UrlRefused(f"only http(s) URLs are fetched, not {scheme or 'a URL without a scheme'}")
    if "@" in parts.netloc:
        raise UrlRefused("URLs with credentials are not fetched")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UrlRefused("the URL has no host")
    try:
        port = parts.port or _PORTS[scheme]
    except ValueError as exc:
        raise UrlRefused("the URL has an invalid port") from exc
    return scheme, host, port


def _judge(url: str, scheme: str, host: str, port: int, addresses: list[str], allow_hosts: Iterable[str]) -> CheckedUrl:
    if not addresses:
        raise UrlRefused(f"cannot resolve {host}")
    if not _allowed(host, port, allow_hosts):
        for a in addresses:
            try:
                public = is_public_ip(a)
            except ValueError:
                public = False
            if not public:
                raise UrlRefused(f"{host} resolves to a non-public address")
    return CheckedUrl(url.strip(), scheme, host, port, tuple(addresses))


def check_url(url: str, *, allow_hosts: Iterable[str] = (), resolve: Resolver | None = None) -> CheckedUrl:
    """Check *url* under the policy above; :class:`UrlRefused` if it may not be fetched."""
    scheme, host, port = _parse(url)
    try:
        addresses = (resolve or _system_resolve)(host, port)
    except (OSError, UnicodeError) as exc:
        raise UrlRefused(f"cannot resolve {host}") from exc
    return _judge(url, scheme, host, port, addresses, allow_hosts)


async def _acheck_url(url: str, allow_hosts: Iterable[str], resolve: Resolver | None) -> CheckedUrl:
    scheme, host, port = _parse(url)
    try:
        if resolve is not None:
            addresses = resolve(host, port)
        else:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
            addresses = list(dict.fromkeys(str(i[4][0]) for i in infos))
    except (OSError, UnicodeError) as exc:
        raise UrlRefused(f"cannot resolve {host}") from exc
    return _judge(url, scheme, host, port, addresses, allow_hosts)


def _too_big(resp, max_bytes: int) -> bool:
    declared = resp.headers.get("content-length", "")
    return declared.isdigit() and int(declared) > max_bytes


def fetch(
    url: str,
    *,
    method: str = "GET",
    content: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    allow_hosts: Iterable[str] = (),
    timeout: float = TIMEOUT_S,
    max_bytes: int = MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    deadline: float = DEADLINE_S,
    resolve: Resolver | None = None,
    transport=None,
) -> Reply:
    """Fetch *url* under the SSRF policy. Returns a :class:`Reply` (call ``raise_for_status()`` to
    turn a 4xx/5xx into :class:`FetchFailed`); refusals raise :class:`UrlRefused`."""
    import httpx

    method = method.upper()
    allow_hosts = list(allow_hosts)
    end = time.monotonic() + deadline
    current, hops = url, []
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, transport=transport) as client:
        for _ in range(max_redirects + 1):
            if time.monotonic() > end:
                raise UrlRefused(f"the fetch took longer than {deadline:g}s")
            checked = check_url(current, allow_hosts=allow_hosts, resolve=resolve)
            resp = None
            for ip in checked.addresses:
                req = client.build_request(
                    method,
                    checked.pinned(ip),
                    content=content,
                    headers={**(headers or {}), "Host": checked.host_header},
                    extensions={"sni_hostname": checked.host},
                )
                try:
                    resp = client.send(req, stream=True)
                    break
                except httpx.ConnectError:
                    continue
                except httpx.TransportError as exc:
                    raise UrlRefused(f"could not fetch from {checked.host}") from exc
            if resp is None:
                raise UrlRefused(f"could not connect to {checked.host}")
            try:
                if resp.is_redirect and method in _FOLLOW:
                    location = resp.headers.get("location")
                    if not location:
                        raise UrlRefused("a redirect without a Location header")
                    hops.append(current)
                    current = urljoin(current, location)
                    continue
                if _too_big(resp, max_bytes):
                    raise UrlRefused(f"the response is larger than {max_bytes} bytes")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise UrlRefused(f"the response is larger than {max_bytes} bytes")
                    if time.monotonic() > end:
                        raise UrlRefused(f"the fetch took longer than {deadline:g}s")
                return Reply(resp.status_code, dict(resp.headers), bytes(body), current, hops)
            finally:
                resp.close()
    raise UrlRefused(f"more than {max_redirects} redirects")


async def afetch(
    url: str,
    *,
    method: str = "GET",
    content: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    allow_hosts: Iterable[str] = (),
    timeout: float = TIMEOUT_S,
    max_bytes: int = MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
    deadline: float = DEADLINE_S,
    resolve: Resolver | None = None,
    transport=None,
) -> Reply:
    """:func:`fetch`, async (resolution runs in the event loop's resolver, not blocking it)."""
    import httpx

    method = method.upper()
    allow_hosts = list(allow_hosts)
    end = time.monotonic() + deadline
    current, hops = url, []
    async with httpx.AsyncClient(
        timeout=timeout, follow_redirects=False, trust_env=False, transport=transport
    ) as client:
        for _ in range(max_redirects + 1):
            if time.monotonic() > end:
                raise UrlRefused(f"the fetch took longer than {deadline:g}s")
            checked = await _acheck_url(current, allow_hosts, resolve)
            resp = None
            for ip in checked.addresses:
                req = client.build_request(
                    method,
                    checked.pinned(ip),
                    content=content,
                    headers={**(headers or {}), "Host": checked.host_header},
                    extensions={"sni_hostname": checked.host},
                )
                try:
                    resp = await client.send(req, stream=True)
                    break
                except httpx.ConnectError:
                    continue
                except httpx.TransportError as exc:
                    raise UrlRefused(f"could not fetch from {checked.host}") from exc
            if resp is None:
                raise UrlRefused(f"could not connect to {checked.host}")
            try:
                if resp.is_redirect and method in _FOLLOW:
                    location = resp.headers.get("location")
                    if not location:
                        raise UrlRefused("a redirect without a Location header")
                    hops.append(current)
                    current = urljoin(current, location)
                    continue
                if _too_big(resp, max_bytes):
                    raise UrlRefused(f"the response is larger than {max_bytes} bytes")
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise UrlRefused(f"the response is larger than {max_bytes} bytes")
                    if time.monotonic() > end:
                        raise UrlRefused(f"the fetch took longer than {deadline:g}s")
                return Reply(resp.status_code, dict(resp.headers), bytes(body), current, hops)
            finally:
                await resp.aclose()
    raise UrlRefused(f"more than {max_redirects} redirects")
