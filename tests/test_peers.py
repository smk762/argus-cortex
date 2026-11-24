"""Peer resolution: a stage's own variable, then the suite-wide one; a bad URL is refused, never skipped."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from argus_cortex import peers, settings
from argus_cortex.peers import (
    Peer,
    PeerRefused,
    apeer_url,
    aresolve_peer,
    peer_env,
    peer_url,
    resolve_peer,
)
from argus_cortex.service import DIRECTORY_VERSION, PeerLookup, service_directory_schema

SECRET = "hunter2"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ARGUS_NUDENET_URL", "ARGUS_CURATOR_NUDENET_URL", "PROOF_NUDENET_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("ARGUS_GPU_BROKER_URL", raising=False)
    monkeypatch.delenv("ARGUS_GPU_BROKER_TOKEN", raising=False)
    for cache in (peers._found, peers._last_good, peers._said):
        cache.clear()


def test_peer_env_names() -> None:
    assert peer_env("nudenet") == "ARGUS_NUDENET_URL" and peer_env("echo-xtts") == "ARGUS_ECHO_XTTS_URL"
    for bad in ("", "Nudenet", "nude_net", "a--b", "-a", "a/b", "ARGUS_X"):
        with pytest.raises(ValueError):
            peer_env(bad)


def test_unset_is_none_and_default_is_last(monkeypatch: pytest.MonkeyPatch) -> None:
    assert peer_url("nudenet", "ARGUS_CURATOR_NUDENET_URL") is None
    assert resolve_peer("nudenet", default="http://nudenet:8080/") == Peer("nudenet", "http://nudenet:8080", "default")
    monkeypatch.setenv("ARGUS_NUDENET_URL", "http://suite:8130")
    assert peer_url("nudenet", default="http://nudenet:8080") == "http://suite:8130"


def test_the_stages_own_variable_wins_then_the_suite_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_NUDENET_URL", "http://suite:8130/")
    assert resolve_peer("nudenet", "ARGUS_CURATOR_NUDENET_URL") == Peer(
        "nudenet", "http://suite:8130", "env:ARGUS_NUDENET_URL"
    )
    monkeypatch.setenv("ARGUS_CURATOR_NUDENET_URL", "  ")  # blank (compose's ${VAR:-}) is unset
    assert peer_url("nudenet", "ARGUS_CURATOR_NUDENET_URL") == "http://suite:8130"
    monkeypatch.setenv("ARGUS_CURATOR_NUDENET_URL", "http://mine:8080")
    assert resolve_peer("nudenet", "PROOF_NUDENET_URL", "ARGUS_CURATOR_NUDENET_URL").source == (
        "env:ARGUS_CURATOR_NUDENET_URL"
    )


def test_the_suite_file_supplies_a_peer_and_off_opts_one_service_out(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "suite.env"
    path.write_text("ARGUS_NUDENET_URL=http://retina.test:8130\n", encoding="utf-8")
    path.chmod(0o600)
    monkeypatch.setenv(settings.ENV_FILE_VAR, str(path))
    assert resolve_peer("nudenet", "ARGUS_CURATOR_NUDENET_URL") == Peer(
        "nudenet", "http://retina.test:8130", "file:ARGUS_NUDENET_URL"
    )
    monkeypatch.setenv("ARGUS_CURATOR_NUDENET_URL", "OFF")
    assert peer_url("nudenet", "ARGUS_CURATOR_NUDENET_URL") is None


@pytest.mark.parametrize(
    "value",
    [
        "/api/lens",  # studio's browser-side path prefix is not a service URL
        "retina:8130",
        "ftp://host/",
        "http://",
        "http:///path",
        f"http://u:{SECRET}@host:notaport",
        f"http://u:{SECRET}@host/?q=1",
        f"http://u:{SECRET}@host/#frag",
        f"http://[::1/{SECRET}",
    ],
)
def test_a_bad_url_is_refused_not_skipped_and_never_echoed(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    """Falling through to None would read as "not configured" and switch a guard off on a typo."""
    monkeypatch.setenv("ARGUS_CURATOR_NUDENET_URL", value)
    monkeypatch.setenv("ARGUS_NUDENET_URL", "http://good:8130")
    with pytest.raises(PeerRefused) as exc:
        peer_url("nudenet", "ARGUS_CURATOR_NUDENET_URL")
    assert "ARGUS_CURATOR_NUDENET_URL" in str(exc.value)
    assert SECRET not in str(exc.value) and value not in str(exc.value)
    # No chained parser error, explicit or implicit, which could quote the value in a traceback.
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None or exc.value.__suppress_context__


def test_credentials_are_kept_in_the_url_and_out_of_the_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_NUDENET_URL", f"https://user:{SECRET}@retina.test/nudenet/")
    peer = resolve_peer("nudenet")
    assert peer is not None and peer.url == f"https://user:{SECRET}@retina.test/nudenet"
    assert SECRET not in repr(peer) and "retina.test" in repr(peer)


# ── discovery through the directory (argus-iris) ─────────────────────────────────────────────────────────────────

TOKEN = "caller-token-for-tests"
ROOT = Path(__file__).resolve().parents[1]


def entry(url: str | None = "http://10.1.2.3:8130", **over: object) -> dict:
    """One entry as argus-iris serves it today (its DirectoryEntry), including fields cortex does not model."""
    return {
        "peer": "nudenet",
        "origin": "label",
        "service": "argus-retina",
        "node": "morpheus",
        "container": "argus-retina-nudenet",
        "running": True,
        "url": url,
        "reason": None,
        "health_path": "/health",
        "health": {
            "checked_at": "2026-11-23T10:00:00Z",
            "status": "ok",
            "http_status": 200,
            "contract": True,
            "service": "argus-retina",
            "version": "0.3.0",
            "wire": {},
            "capabilities": [],
            "checks": {},
            "error": None,
            "a_later_field": 1,
        },
        "a_later_field": True,
        **over,
    }


class Directory:
    """A fake argus-iris: counts the lookups and records what each carried."""

    def __init__(self, *entries: dict, status: int = 200, body: object = None) -> None:
        self.entries, self.status, self.body, self.requests = list(entries), status, body, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, headers={"location": "http://elsewhere.test/"}, json={"detail": "no"})
        if self.body is not None:
            return (
                httpx.Response(200, content=self.body)
                if isinstance(self.body, bytes)
                else httpx.Response(200, json=self.body)
            )
        return httpx.Response(200, json={"peer": "nudenet", "urls": self.entries})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_GPU_BROKER_URL", "http://iris.test:8120/")
    monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", TOKEN)


def test_committed_directory_schema_is_current() -> None:
    committed = (ROOT / "service-directory-schema.json").read_text(encoding="utf-8")
    assert committed == service_directory_schema(), "service-directory-schema.json is stale: run make schema"


def test_the_reply_iris_serves_today_is_a_peer_lookup() -> None:
    lookup = PeerLookup.model_validate({"peer": "nudenet", "urls": [entry(), entry(None, reason="loopback only")]})
    assert lookup.directory_version == DIRECTORY_VERSION and lookup.urls[0].health.version == "0.3.0"
    assert lookup.urls[1].url is None and "a_later_field" not in lookup.model_dump()["urls"][0]


def test_discovery_is_opt_in_and_asks_with_the_caller_token(broker: None) -> None:
    directory = Directory(entry())
    assert resolve_peer("nudenet", transport=directory.transport) is None and not directory.requests
    found = resolve_peer("nudenet", "ARGUS_CURATOR_NUDENET_URL", discover=True, transport=directory.transport)
    assert found == Peer("nudenet", "http://10.1.2.3:8130", "broker")
    (request,) = directory.requests
    assert str(request.url) == "http://iris.test:8120/directory/nudenet"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


def test_a_variable_wins_over_the_directory_and_off_stops_before_it(broker: None, monkeypatch) -> None:
    directory = Directory(entry())
    monkeypatch.setenv("ARGUS_NUDENET_URL", "http://mine:8130")
    assert resolve_peer("nudenet", discover=True, transport=directory.transport).source == "env:ARGUS_NUDENET_URL"
    monkeypatch.setenv("ARGUS_NUDENET_URL", "off")
    assert resolve_peer("nudenet", discover=True, default="http://d", transport=directory.transport) is None
    assert not directory.requests


def test_no_token_means_no_discovery(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """The answer decides where a moderation check is sent. Without a token anyone on the LAN could have set it."""
    directory = Directory(entry())
    assert resolve_peer("nudenet", discover=True, transport=directory.transport) is None  # no broker at all
    monkeypatch.setenv("ARGUS_GPU_BROKER_URL", "http://iris.test:8120")
    with caplog.at_level("WARNING"):
        for _ in range(3):
            assert resolve_peer("nudenet", discover=True, transport=directory.transport) is None
    assert not directory.requests and caplog.text.count("ARGUS_GPU_BROKER_TOKEN is not set") == 1


def test_a_found_peer_is_cached_and_the_default_fills_a_miss(broker: None) -> None:
    directory = Directory(entry())
    for _ in range(5):
        assert resolve_peer("nudenet", discover=True, transport=directory.transport).url == "http://10.1.2.3:8130"
    assert len(directory.requests) == 1

    missing = Directory(status=404)
    for _ in range(3):
        found = resolve_peer("ocr", discover=True, default="http://ocr:8134", transport=missing.transport)
        assert found == Peer("ocr", "http://ocr:8134", "default")
    assert len(missing.requests) == 1  # a miss is cached too, for a shorter time


@pytest.mark.parametrize(
    "directory",
    [
        Directory(status=401),
        Directory(status=403),
        Directory(status=500),
        Directory(status=302),  # a redirect is not followed: the token goes nowhere else
        Directory(body=b"<html>not json</html>"),
        Directory(body={"peer": "nudenet", "urls": "nope"}),
        Directory(body={"peer": "nudenet", "urls": [], "directory_version": "2.0"}),
        Directory(body=[1, 2]),
    ],
    ids=["401", "403", "500", "redirect", "not-json", "not-the-model", "later-major", "a-list"],
)
def test_a_directory_that_cannot_answer_is_not_found_never_an_error(
    broker: None, directory: Directory, caplog: pytest.LogCaptureFixture
) -> None:
    directory.requests.clear()
    with caplog.at_level("DEBUG"):
        assert resolve_peer("nudenet", discover=True, transport=directory.transport) is None
    assert len(directory.requests) == 1 and TOKEN not in caplog.text


def test_an_unreachable_directory_is_not_found(broker: None, caplog: pytest.LogCaptureFixture) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url} with {request.headers['authorization']}")

    with caplog.at_level("DEBUG"):
        assert resolve_peer("nudenet", discover=True, transport=httpx.MockTransport(refuse)) is None
    assert "ConnectError" in caplog.text and TOKEN not in caplog.text


def test_only_a_usable_entry_for_this_peer_is_taken(broker: None) -> None:
    directory = Directory(
        entry("http://10.9.9.9:1", peer="ocr"),  # another peer's entry
        entry(None, reason="published on loopback only"),
        entry("/api/nudenet"),
        entry("ftp://10.1.2.3/"),
        entry("http://user:pw@10.1.2.3:8130"),  # a directory names a place, never credentials
        entry("http://10.1.2.3:8130/?x=1"),
        entry("http://10.1.2.4:8130/"),
        entry("http://10.1.2.5:8130"),
    )
    assert resolve_peer("nudenet", discover=True, transport=directory.transport).url == "http://10.1.2.4:8130"
    peers._found.clear()
    peers._last_good.clear()
    none_usable = Directory(entry(None), entry("http://user:pw@h"))
    assert resolve_peer("nudenet", discover=True, transport=none_usable.transport) is None


def test_a_peer_found_once_is_not_unfound_when_the_directory_fails(broker: None, monkeypatch, caplog) -> None:
    """A guard must not read as "not configured" because the directory blinked. A peer that has really gone then
    fails its calls, which is a failed check, not a skipped one."""
    monkeypatch.setattr(peers, "DISCOVERY_TTL_S", 0.0)
    monkeypatch.setattr(peers, "DISCOVERY_MISS_TTL_S", 0.0)
    up = Directory(entry())
    assert resolve_peer("nudenet", discover=True, transport=up.transport).url == "http://10.1.2.3:8130"
    with caplog.at_level("WARNING"):
        for down in (Directory(status=500), Directory(status=404), Directory(body=b"x")):
            kept = resolve_peer("nudenet", discover=True, default="http://default", transport=down.transport)
            assert kept == Peer("nudenet", "http://10.1.2.3:8130", "broker") and len(down.requests) == 1
    assert caplog.text.count("keeping the last one found") == 1
    moved = Directory(entry("http://10.1.2.9:8130"))
    assert resolve_peer("nudenet", discover=True, transport=moved.transport).url == "http://10.1.2.9:8130"


def test_discovery_without_httpx_is_off_not_a_crash(broker: None, monkeypatch, caplog) -> None:
    def no_httpx() -> None:
        raise ImportError("no httpx")

    monkeypatch.setattr(peers, "_httpx", no_httpx)
    with caplog.at_level("WARNING"):
        assert peer_url("nudenet", discover=True, default="http://d:1") == "http://d:1"
    assert "httpx is not installed" in caplog.text


async def test_the_async_form_finds_the_same_peer_and_shares_the_cache(broker: None) -> None:
    directory = Directory(entry())
    found = await aresolve_peer("nudenet", discover=True, transport=directory.transport)
    assert found == Peer("nudenet", "http://10.1.2.3:8130", "broker")
    assert resolve_peer("nudenet", discover=True).url == found.url  # from the cache: no transport, no request
    assert await apeer_url("nudenet", discover=True) == found.url and len(directory.requests) == 1
    assert await apeer_url("ocr", "ARGUS_T_OCR_URL", default="http://ocr:1") == "http://ocr:1"
