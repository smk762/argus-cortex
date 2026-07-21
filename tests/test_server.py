"""Tests for the shared ASGI write-guard + env-flag scaffolding."""

from __future__ import annotations

import logging

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from argus_cortex.server import (
    FALSY,
    TRUTHY,
    UNSAFE_METHODS,
    WriteGuard,
    constant_refuse,
    cross_site_refuse,
    env_flag,
)

# ----- a tiny app that echoes the method, exposed under the guard ----------


async def _echo(request):  # noqa: ANN001,ANN202
    return PlainTextResponse(f"{request.method} ok")


async def _stream(request):  # noqa: ANN001,ANN202
    async def gen():  # noqa: ANN202
        for i in range(3):
            yield f"chunk-{i}\n"

    return StreamingResponse(gen(), media_type="text/plain")


def _app(refuse) -> Starlette:  # noqa: ANN001
    routes = [
        Route("/", _echo, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        Route("/stream", _stream, methods=["GET"]),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(WriteGuard, refuse=refuse)
    return app


# ----- constants -----------------------------------------------------------


def test_unsafe_methods_are_the_state_changers() -> None:
    assert set(UNSAFE_METHODS) == {"POST", "PUT", "PATCH", "DELETE"}
    assert "GET" not in UNSAFE_METHODS and "OPTIONS" not in UNSAFE_METHODS


def test_truthy_and_falsy_are_disjoint() -> None:
    assert not (TRUTHY & FALSY)
    assert "" in FALSY  # unset/empty is explicitly off, not "unrecognised"


# ----- env_flag ------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "  yes  ", "on"])
def test_env_flag_truthy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ARGUS_TEST_FLAG", value)
    assert env_flag("ARGUS_TEST_FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_env_flag_falsy(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("ARGUS_TEST_FLAG", value)
    assert env_flag("ARGUS_TEST_FLAG") is False


def test_env_flag_unset_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARGUS_TEST_FLAG", raising=False)
    assert env_flag("ARGUS_TEST_FLAG") is False


def test_env_flag_unrecognised_warns_and_is_off(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A typo (e.g. ``=enabled``) must resolve OFF *and* be visible in the logs,
    so a mistyped protection flag doesn't silently leave the guard disabled."""
    monkeypatch.setenv("ARGUS_TEST_FLAG", "enabled")
    with caplog.at_level(logging.WARNING, logger="argus_cortex.server"):
        assert env_flag("ARGUS_TEST_FLAG") is False
    assert any("unrecognised" in r.message for r in caplog.records)


# ----- WriteGuard + constant_refuse (read-only mode) -----------------------


def test_constant_refuse_blocks_every_write_but_serves_reads() -> None:
    client = TestClient(_app(constant_refuse("read-only")))
    assert client.get("/").status_code == 200
    for method in ("post", "put", "patch", "delete"):
        resp = getattr(client, method)("/")
        assert resp.status_code == 403, method
        assert resp.json()["detail"] == "read-only"


def test_constant_refuse_beats_body_validation() -> None:
    """The method gate runs before routing, so a write is 403'd regardless of the
    (here nonexistent) route or body — future write routes are covered too."""
    client = TestClient(_app(constant_refuse("nope")))
    assert client.post("/does-not-exist").status_code == 403


def test_guard_leaves_streaming_responses_intact() -> None:
    """Pure-ASGI: the guard forwards the send channel untouched, so a streaming
    read passes straight through (a BaseHTTPMiddleware guard would buffer it)."""
    client = TestClient(_app(constant_refuse("read-only")))
    resp = client.get("/stream")
    assert resp.status_code == 200
    assert resp.text == "chunk-0\nchunk-1\nchunk-2\n"


# ----- WriteGuard + cross_site_refuse (cross-site protection) ---------------


def test_cross_site_refuse_allows_absent_origin() -> None:
    """curl / CLI / server-to-server send no Origin and must be unaffected."""
    client = TestClient(_app(cross_site_refuse(["https://studio.example"])))
    assert client.post("/").status_code == 200


def test_cross_site_refuse_blocks_untrusted_origin() -> None:
    client = TestClient(_app(cross_site_refuse(["https://studio.example"])))
    resp = client.post("/", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 403
    assert "cross-site" in resp.json()["detail"]
    assert "https://evil.example" in resp.json()["detail"]


def test_cross_site_refuse_allows_trusted_origin() -> None:
    client = TestClient(_app(cross_site_refuse(["https://studio.example"])))
    assert client.post("/", headers={"Origin": "https://studio.example"}).status_code == 200


def test_cross_site_refuse_allows_same_host() -> None:
    """A UI proxied onto this host is same-origin (host:port match), no allow-list
    entry needed — even when the proxy rewrote the scheme to https."""
    client = TestClient(_app(cross_site_refuse([])))
    assert client.post("/", headers={"Origin": "https://testserver"}).status_code == 200


def test_cross_site_read_is_never_gated() -> None:
    client = TestClient(_app(cross_site_refuse([])))
    assert client.get("/", headers={"Origin": "https://evil.example"}).status_code == 200
