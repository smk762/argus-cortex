"""argus_cortex.net: the SSRF policy. DNS and the network are faked: the resolver plays the
attacker's DNS, and an httpx MockTransport records where each request really went."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import ipaddress
import tracemalloc
import zlib

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from argus_cortex.net import FetchFailed, UrlRefused, afetch, check_url, fetch, is_public_ip

PUBLIC = "93.184.216.34"
PUBLIC6 = "2606:2800:220:1:248:1893:25c8:1946"


def dns(table: dict[str, list[str]]):
    def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("NXDOMAIN")
        return table[host]

    return resolve


class Net:
    """A fake internet: routes by the IP actually connected to, records every request."""

    def __init__(self, routes: dict[str, httpx.Response | list[httpx.Response]]):
        self.routes, self.seen = routes, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        r = self.routes[request.url.host]
        return r.pop(0) if isinstance(r, list) else r

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


# ── which addresses are public ───────────────────────────────────────────────

INSIDE = [
    "127.0.0.1",
    "10.1.2.3",
    "172.16.0.1",
    "192.168.1.1",
    "169.254.169.254",
    "100.64.0.1",
    "0.0.0.0",
    "224.0.0.1",
    "255.255.255.255",
    "198.18.0.1",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "ff02::1",
    "::ffff:127.0.0.1",
    "::ffff:169.254.169.254",
    "::127.0.0.1",
    "2002:7f00:1::",
    "2002:a9fe:a9fe::",
    "64:ff9b::a9fe:a9fe",
    "64:ff9b::7f00:1",
    "2001:0:4136:e378:8000:63bf:80ff:fffe",
    "2002:808:808::",  # 6to4 is a legacy transition range: refused even around a public v4
]


@pytest.mark.parametrize("ip", INSIDE)
def test_inside_addresses_are_not_public(ip: str) -> None:
    assert not is_public_ip(ip)


@pytest.mark.parametrize("ip", [PUBLIC, PUBLIC6, "8.8.8.8", "::ffff:8.8.8.8"])
def test_public_addresses(ip: str) -> None:
    assert is_public_ip(ip)


PRIVATE_NETS = [
    ipaddress.ip_network(n)
    for n in ("10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
]


@given(
    st.sampled_from(PRIVATE_NETS).flatmap(lambda n: st.integers(0, n.num_addresses - 1).map(lambda i: n[i])),
    st.sampled_from(["mapped", "compat", "6to4", "nat64", "plain"]),
)
def test_no_wrapping_makes_an_inside_v4_address_public(v4: ipaddress.IPv4Address, wrap: str) -> None:
    n = int(v4)
    ip = {
        "plain": v4,
        "mapped": ipaddress.IPv6Address((0xFFFF << 32) | n),
        "compat": ipaddress.IPv6Address(n),
        "6to4": ipaddress.IPv6Address((0x2002 << 112) | (n << 80)),
        "nat64": ipaddress.IPv6Address((0x64FF9B << 96) | n),
    }[wrap]
    assert not is_public_ip(ip)


@given(st.ip_addresses())
def test_public_means_global(ip) -> None:
    if is_public_ip(ip):
        assert ip.is_global and not ip.is_multicast


# ── check_url ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("file:///etc/passwd", "only http"),
        ("gopher://x/", "only http"),
        ("ftp://example.com/", "only http"),
        ("//example.com/x", "only http"),
        ("http://user:pw@example.com/", "credentials"),
        ("http://example.com@127.0.0.1/", "credentials"),
        ("http:///nohost", "no host"),
        ("http://example.com:99999/", "invalid port"),
        ("", "no URL"),
    ],
)
def test_malformed_urls_are_refused(url: str, why: str) -> None:
    with pytest.raises(UrlRefused, match=why):
        check_url(url, resolve=dns({"example.com": [PUBLIC]}))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost:8765/admin",
        "http://[::1]/",
        "http://2130706433/",
        "http://0x7f.1/",
        "http://127.1/",
        "http://0/",
        "http://[::ffff:127.0.0.1]/",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_numeric_and_loopback_tricks_resolve_inside_and_are_refused(url: str) -> None:
    """The system resolver turns decimal, hex and short forms into real addresses; no DNS is used."""
    with pytest.raises(UrlRefused):
        check_url(url)


def test_any_inside_record_refuses_the_host() -> None:
    mixed = dns({"rebind.example": [PUBLIC, "10.0.0.5"]})
    with pytest.raises(UrlRefused, match="non-public") as e:
        check_url("https://rebind.example/", resolve=mixed)
    assert "10.0.0.5" not in str(e.value)  # the message doesn't map the network


def test_unresolvable_host_is_refused() -> None:
    with pytest.raises(UrlRefused, match="cannot resolve"):
        check_url("https://nope.example/", resolve=dns({}))


def test_allow_list_by_host_and_port() -> None:
    inside = dns({"inference": ["10.0.0.9"]})
    assert check_url("http://inference:8000/x", resolve=inside, allow_hosts=["inference"]).addresses == ("10.0.0.9",)
    assert check_url("http://inference:8000/x", resolve=inside, allow_hosts=["INFERENCE:8000"]).port == 8000
    with pytest.raises(UrlRefused):
        check_url("http://inference:9000/x", resolve=inside, allow_hosts=["inference:8000"])
    with pytest.raises(UrlRefused):
        check_url("http://inference.evil/x", resolve=dns({"inference.evil": ["10.0.0.9"]}), allow_hosts=["inference"])


def test_trailing_dot_and_case_are_normalised() -> None:
    c = check_url("HTTPS://Example.COM./a?b=1", resolve=dns({"example.com": [PUBLIC]}))
    assert (c.scheme, c.host, c.port) == ("https", "example.com", 443)
    assert c.pinned(PUBLIC) == f"https://{PUBLIC}:443/a?b=1" and c.host_header == "example.com"


# ── fetch ────────────────────────────────────────────────────────────────────


def test_connection_is_pinned_to_the_checked_address() -> None:
    net = Net({PUBLIC: httpx.Response(200, content=b"img")})
    r = fetch("https://example.com/a.png", resolve=dns({"example.com": [PUBLIC]}), transport=net.transport)
    assert r.ok and r.body == b"img"
    req = net.seen[0]
    assert req.url.host == PUBLIC and req.headers["host"] == "example.com"
    assert req.extensions["sni_hostname"] == "example.com"


def test_an_allow_listed_host_is_still_pinned() -> None:
    """The allow-list skips the public-address rule, never the pinning."""
    answers = [["10.0.0.9"], ["10.6.6.6"]]
    net = Net({"10.0.0.9": httpx.Response(200, content=b"ok")})
    fetch(
        "http://inference:8000/x",
        allow_hosts=["inference"],
        resolve=lambda h, p: answers.pop(0),
        transport=net.transport,
    )
    req = net.seen[0]
    assert (req.url.host, req.headers["host"]) == ("10.0.0.9", "inference:8000")


def test_dns_rebinding_after_the_check_changes_nothing() -> None:
    """The resolver answers public first and inside afterwards; only the first answer is used."""
    answers = [[PUBLIC], ["127.0.0.1"]]
    net = Net({PUBLIC: httpx.Response(200, content=b"ok")})
    fetch("https://flip.example/", resolve=lambda h, p: answers.pop(0), transport=net.transport)
    assert [r.url.host for r in net.seen] == [PUBLIC]


def test_redirects_are_rechecked_and_capped() -> None:
    hop = httpx.Response(302, headers={"location": "http://metadata.example/latest"})
    table = dns({"example.com": [PUBLIC], "metadata.example": ["169.254.169.254"]})
    with pytest.raises(UrlRefused, match="non-public"):
        fetch("https://example.com/", resolve=table, transport=Net({PUBLIC: hop}).transport)

    loop = Net({PUBLIC: [httpx.Response(302, headers={"location": "/again"}) for _ in range(10)]})
    with pytest.raises(UrlRefused, match="more than 2 redirects"):
        fetch("https://example.com/", resolve=dns({"example.com": [PUBLIC]}), max_redirects=2, transport=loop.transport)


def test_good_redirects_are_followed_and_recorded() -> None:
    net = Net({PUBLIC: [httpx.Response(301, headers={"location": "/b"}), httpx.Response(200, content=b"b")]})
    r = fetch("https://example.com/a", resolve=dns({"example.com": [PUBLIC]}), transport=net.transport)
    assert r.body == b"b" and r.url == "https://example.com/b" and r.redirects == ["https://example.com/a"]


def test_a_post_is_never_redirected() -> None:
    net = Net({PUBLIC: httpx.Response(307, headers={"location": "http://10.0.0.1/"})})
    r = fetch(
        "https://example.com/hook",
        method="POST",
        content=b"{}",
        resolve=dns({"example.com": [PUBLIC]}),
        transport=net.transport,
    )
    assert r.status == 307 and len(net.seen) == 1 and net.seen[0].content == b"{}"


def test_body_cap_declared_and_streamed() -> None:
    table = dns({"example.com": [PUBLIC]})
    declared = Net({PUBLIC: httpx.Response(200, headers={"content-length": "999"}, content=b"x")})
    with pytest.raises(UrlRefused, match="larger than 10"):
        fetch("https://example.com/", resolve=table, max_bytes=10, transport=declared.transport)
    streamed = Net({PUBLIC: httpx.Response(200, content=iter([b"12345", b"67890", b"1"]))})
    with pytest.raises(UrlRefused, match="larger than 10"):
        fetch("https://example.com/", resolve=table, max_bytes=10, transport=streamed.transport)


def test_error_status_is_returned_then_raised_on_request() -> None:
    net = Net({PUBLIC: httpx.Response(404, content=b"no")})
    r = fetch("https://example.com/", resolve=dns({"example.com": [PUBLIC]}), transport=net.transport)
    assert r.status == 404 and not r.ok
    with pytest.raises(FetchFailed) as e:
        r.raise_for_status()
    assert e.value.status == 404


def test_falls_back_to_the_next_checked_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PUBLIC6:
            raise httpx.ConnectError("no v6 route")
        return httpx.Response(200, content=b"v4")

    r = fetch(
        "https://dual.example/",
        resolve=dns({"dual.example": [PUBLIC6, PUBLIC]}),
        transport=httpx.MockTransport(handler),
    )
    assert r.body == b"v4"


def test_deadline_covers_every_hop() -> None:
    net = Net({PUBLIC: httpx.Response(302, headers={"location": "/x"})})
    with pytest.raises(UrlRefused, match="longer than"):
        fetch("https://example.com/", resolve=dns({"example.com": [PUBLIC]}), deadline=-1, transport=net.transport)


def test_async_fetch_has_the_same_policy() -> None:
    net = Net({PUBLIC: [httpx.Response(302, headers={"location": "http://inside.example/"})]})
    table = dns({"example.com": [PUBLIC], "inside.example": ["10.9.9.9"]})
    with pytest.raises(UrlRefused):
        asyncio.run(afetch("https://example.com/", resolve=table, transport=net.transport))
    ok = Net({PUBLIC: httpx.Response(200, content=b"a")})
    r = asyncio.run(afetch("https://example.com/", resolve=table, transport=ok.transport))
    assert r.body == b"a" and ok.seen[0].url.host == PUBLIC


@given(st.text(max_size=60))
def test_check_url_never_raises_anything_else(url: str) -> None:
    with contextlib.suppress(UrlRefused):  # refusing is fine; any other exception fails the test
        check_url(url, resolve=dns({}))


def test_credentials_are_not_forwarded_to_another_origin() -> None:
    net = Net(
        {
            PUBLIC: [
                httpx.Response(302, headers={"location": "/same"}),
                httpx.Response(302, headers={"location": "https://other.example/"}),
            ],
            "93.184.216.35": httpx.Response(200, content=b"ok"),
        }
    )
    table = dns({"example.com": [PUBLIC], "other.example": ["93.184.216.35"]})
    secret = {"Authorization": "Bearer s3cret", "Cookie": "sid=1", "X-Trace": "t"}
    fetch("https://example.com/", headers=secret, resolve=table, transport=net.transport)
    first, same, other = net.seen
    assert same.headers["authorization"] == "Bearer s3cret" and same.headers["cookie"] == "sid=1"
    assert "authorization" not in other.headers and "cookie" not in other.headers
    assert other.headers["x-trace"] == "t" and other.headers["host"] == "other.example"


def test_a_caller_host_header_cannot_override_the_checked_host() -> None:
    net = Net({PUBLIC: httpx.Response(200)})
    fetch(
        "https://example.com/",
        headers={"host": "inside"},
        resolve=dns({"example.com": [PUBLIC]}),
        transport=net.transport,
    )
    assert net.seen[0].headers.get_list("host") == ["example.com"]


def test_a_compression_bomb_is_never_inflated_past_the_cap() -> None:
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))
    net = Net({PUBLIC: httpx.Response(200, headers={"content-encoding": "gzip"}, content=iter([bomb]))})
    tracemalloc.start()
    try:
        with pytest.raises(UrlRefused, match="larger than 1024"):
            fetch(
                "https://example.com/", max_bytes=1024, resolve=dns({"example.com": [PUBLIC]}), transport=net.transport
            )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024


@pytest.mark.parametrize("encoding", ["gzip", "deflate"])
def test_compressed_bodies_within_the_cap_are_decoded(encoding: str) -> None:
    data = b"hello " * 100
    raw = gzip.compress(data) if encoding == "gzip" else zlib.compress(data)
    net = Net({PUBLIC: httpx.Response(200, headers={"content-encoding": encoding}, content=iter([raw]))})
    r = fetch(
        "https://example.com/", max_bytes=len(data), resolve=dns({"example.com": [PUBLIC]}), transport=net.transport
    )
    assert r.body == data


def test_an_unknown_content_encoding_is_refused() -> None:
    net = Net({PUBLIC: httpx.Response(200, headers={"content-encoding": "br"}, content=iter([b"x"]))})
    with pytest.raises(UrlRefused, match="encoding"):
        fetch("https://example.com/", resolve=dns({"example.com": [PUBLIC]}), transport=net.transport)


def test_a_non_ascii_content_length_is_not_a_crash() -> None:
    net = Net({PUBLIC: httpx.Response(200, headers=[(b"content-length", "²".encode())])})
    r = fetch("https://example.com/", resolve=dns({"example.com": [PUBLIC]}), transport=net.transport)
    assert r.status == 200


def test_a_connect_timeout_falls_back_to_the_next_address() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PUBLIC6:
            raise httpx.ConnectTimeout("blackholed")
        return httpx.Response(200, content=b"v4")

    r = fetch(
        "https://dual.example/",
        resolve=dns({"dual.example": [PUBLIC6, PUBLIC]}),
        transport=httpx.MockTransport(handler),
    )
    assert r.body == b"v4"


def test_an_idn_host_is_fetched_by_its_ascii_name() -> None:
    net = Net({PUBLIC: httpx.Response(200, content=b"ok")})
    seen: list[str] = []

    def resolve(host: str, port: int) -> list[str]:
        seen.append(host)
        return [PUBLIC]

    r = fetch("https://bücher.example/", resolve=resolve, transport=net.transport)
    assert r.body == b"ok" and seen == ["xn--bcher-kva.example"]
    assert net.seen[0].headers["host"] == "xn--bcher-kva.example"
