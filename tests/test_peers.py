"""Peer resolution: a stage's own variable, then the suite-wide one; a bad URL is refused, never skipped."""

from __future__ import annotations

from pathlib import Path

import pytest

from argus_cortex import settings
from argus_cortex.peers import Peer, PeerRefused, peer_env, peer_url, resolve_peer

SECRET = "hunter2"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ARGUS_NUDENET_URL", "ARGUS_CURATOR_NUDENET_URL", "PROOF_NUDENET_URL"):
        monkeypatch.delenv(name, raising=False)


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
    assert exc.value.__cause__ is None  # no chained parser error, which could quote the value


def test_credentials_are_kept_in_the_url_and_out_of_the_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARGUS_NUDENET_URL", f"https://user:{SECRET}@retina.test/nudenet/")
    peer = resolve_peer("nudenet")
    assert peer is not None and peer.url == f"https://user:{SECRET}@retina.test/nudenet"
    assert SECRET not in repr(peer) and "retina.test" in repr(peer)
