"""argus_cortex.auth: fail closed, constant-time compare, RFC 6750 header parsing."""

from __future__ import annotations

import hmac
import logging

import pytest
from hypothesis import given
from hypothesis import strategies as st
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from argus_cortex.auth import bearer, check_bearer


def app_with(dep) -> TestClient:
    async def unload(request: Request) -> JSONResponse:
        await dep(request)
        return JSONResponse({"ok": True})

    return TestClient(Starlette(routes=[Route("/admin/unload", unload, methods=["POST"])]))


@pytest.mark.parametrize(
    ("header", "ok"),
    [
        ("Bearer s3cret", True),
        ("bearer s3cret", True),
        ("BEARER   s3cret  ", True),
        ("  Bearer s3cret", True),
        ("Bearer s3cre", False),
        ("Bearer s3cret!", False),
        ("s3cret", False),
        ("Basic s3cret", False),
        ("Bearer", False),
        ("", False),
        (None, False),
        ("Bearer ünïcode", False),
    ],
)
def test_check_bearer(header: str | None, ok: bool) -> None:
    assert check_bearer(header, "s3cret") is ok


def test_empty_token_never_matches() -> None:
    assert not check_bearer("Bearer ", "") and not check_bearer("Bearer x", "")


def test_compare_is_constant_time(monkeypatch) -> None:
    calls = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: calls.append((a, b)) or real(a, b))
    check_bearer("Bearer guess", "s3cret")
    check_bearer("Token guess", "s3cret")
    assert len(calls) == 2 and all(isinstance(a, bytes) for a, _ in calls)


@given(st.text(max_size=40), st.text(min_size=1, max_size=40))
def test_only_the_exact_token_passes(guess: str, token: str) -> None:
    """For any token and any guess: the guess passes iff it is the token (modulo scheme case and
    the whitespace RFC 6750 allows around it); never an exception, whatever the characters."""
    assert check_bearer(f"Bearer {guess}", token) == (guess.strip() == token)


def test_unset_token_closes_the_route(monkeypatch) -> None:
    monkeypatch.delenv("ARGUS_ADMIN_TOKEN", raising=False)
    r = app_with(bearer()).post("/admin/unload", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503 and "ARGUS_ADMIN_TOKEN" in r.text
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", "   ")  # blank counts as unset
    assert app_with(bearer()).post("/admin/unload").status_code == 503


def test_open_when_unset_is_explicit_and_logged(monkeypatch, caplog) -> None:
    monkeypatch.delenv("ARGUS_ADMIN_TOKEN", raising=False)
    client = app_with(bearer(open_when_unset=True))
    with caplog.at_level(logging.WARNING, logger="argus_cortex.auth"):
        assert client.post("/admin/unload").status_code == 200
        assert client.post("/admin/unload").status_code == 200
    assert sum("open_when_unset" in m for m in caplog.messages) == 1  # warned once


def test_configured_token_is_required(monkeypatch) -> None:
    monkeypatch.setenv("ARGUS_MINT_MODERATOR_TOKEN", "s3cret")
    client = app_with(bearer("ARGUS_MINT_MODERATOR_TOKEN", open_when_unset=True))  # set: the opt-in doesn't matter
    no = client.post("/admin/unload")
    assert no.status_code == 401 and no.headers["www-authenticate"] == "Bearer"
    assert client.post("/admin/unload", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.post("/admin/unload", headers={"Authorization": "bearer s3cret"}).status_code == 200


def test_token_is_read_per_request(monkeypatch) -> None:
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", "one")
    client = app_with(bearer())
    assert client.post("/admin/unload", headers={"Authorization": "Bearer one"}).status_code == 200
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", "two")  # rotated
    assert client.post("/admin/unload", headers={"Authorization": "Bearer one"}).status_code == 401


def test_works_as_a_fastapi_dependency(monkeypatch) -> None:
    fastapi = pytest.importorskip("fastapi")
    monkeypatch.setenv("ARGUS_ADMIN_TOKEN", "s3cret")
    app = fastapi.FastAPI()

    @app.post("/admin/unload", dependencies=[fastapi.Depends(bearer())])
    def unload() -> dict:
        return {"ok": True}

    c = TestClient(app)
    assert c.post("/admin/unload").status_code == 401
    assert c.post("/admin/unload", headers={"Authorization": "Bearer s3cret"}).json() == {"ok": True}


def test_refusal_is_a_starlette_http_exception(monkeypatch) -> None:
    import asyncio

    monkeypatch.delenv("ARGUS_ADMIN_TOKEN", raising=False)
    req = Request({"type": "http", "method": "POST", "headers": [], "path": "/"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(bearer()(req))
    assert e.value.status_code == 503
