from __future__ import annotations

import importlib.util

import pytest

from argus_cortex.backends import (
    Backend,
    BackendError,
    BackendKind,
    LocalBackend,
    RemoteBackend,
    RemoteProvider,
    resolve_device,
    with_retries,
)

_HAS_HTTPX = importlib.util.find_spec("httpx") is not None


# --------------------------------------------------------------------------
# retry
# --------------------------------------------------------------------------


def test_with_retries_returns_on_first_success() -> None:
    assert with_retries(lambda: 42) == 42


def test_with_retries_retries_then_succeeds_with_backoff() -> None:
    calls = {"n": 0}
    delays: list[float] = []

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("boom")
        return "ok"

    result = with_retries(flaky, attempts=3, base_delay=1.0, retry_on=(ValueError,), sleep=delays.append)
    assert result == "ok"
    assert calls["n"] == 3
    assert delays == [1.0, 2.0]  # exponential: base*2^0, base*2^1


def test_with_retries_reraises_last_after_exhausting() -> None:
    def always() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        with_retries(always, attempts=2, retry_on=(ValueError,), sleep=lambda _d: None)


def test_with_retries_does_not_catch_unlisted_exception() -> None:
    def boom() -> None:
        raise KeyError("x")

    with pytest.raises(KeyError):
        with_retries(boom, attempts=3, retry_on=(ValueError,), sleep=lambda _d: None)


def test_with_retries_rejects_non_positive_attempts() -> None:
    # attempts<1 must fail loudly, never `raise None` (which the old assert did under -O)
    with pytest.raises(ValueError, match="attempts must be >= 1"):
        with_retries(lambda: 1, attempts=0)


# --------------------------------------------------------------------------
# device
# --------------------------------------------------------------------------


def test_resolve_device_passes_explicit_through() -> None:
    assert resolve_device("cuda:1") == "cuda:1"
    assert resolve_device("cpu") == "cpu"


def test_resolve_device_auto_falls_back_to_cpu_without_torch() -> None:
    # torch isn't a dependency of argus-cortex, so "auto" resolves to cpu here.
    if importlib.util.find_spec("torch") is None:
        assert resolve_device("auto") == "cpu"


# --------------------------------------------------------------------------
# Backend base + LocalBackend
# --------------------------------------------------------------------------


def test_backend_defaults() -> None:
    b = Backend()
    assert b.is_available() is True
    assert b.availability_reason() is None
    assert b.load() is None  # no-op
    assert b.unload() is None


def test_local_backend_load_sets_device() -> None:
    b = LocalBackend()
    assert b.kind == BackendKind.LOCAL
    assert b.device is None
    b.load("cpu")
    assert b.device == "cpu"


# --------------------------------------------------------------------------
# RemoteBackend
# --------------------------------------------------------------------------


def test_remote_backend_normalizes_url_and_sets_auth() -> None:
    b = RemoteBackend("http://10.0.0.5:9000/", api_key="secret")
    assert b.base_url == "http://10.0.0.5:9000"
    assert b.headers["Authorization"] == "Bearer secret"
    assert b.kind == BackendKind.REMOTE
    assert b.is_available() is True


def test_remote_backend_from_host() -> None:
    b = RemoteBackend.from_host("192.168.1.20", 8188)
    assert b.base_url == "http://192.168.1.20:8188"


def test_remote_backend_from_provider_uses_default_url() -> None:
    b = RemoteBackend.from_provider(RemoteProvider.NVIDIA_NIM, api_key="k")
    assert b.base_url == "https://integrate.api.nvidia.com/v1"
    assert b.provider == RemoteProvider.NVIDIA_NIM


def test_openai_compat_provider_requires_base_url() -> None:
    with pytest.raises(BackendError, match="explicit base_url"):
        RemoteBackend.from_provider(RemoteProvider.OPENAI_COMPAT)
    # ...but works when you supply one (your self-hosted server)
    b = RemoteBackend.from_provider(RemoteProvider.OPENAI_COMPAT, base_url="http://localhost:1234/v1")
    assert b.base_url == "http://localhost:1234/v1"


def test_remote_backend_unavailable_without_url() -> None:
    b = RemoteBackend()
    assert b.is_available() is False
    assert "base_url" in (b.availability_reason() or "")


class _FakeResp:
    def __init__(self, data: dict) -> None:
        self._data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._data


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.closed = False

    def post(self, path: str, json: dict) -> _FakeResp:
        self.calls.append((path, json))
        return _FakeResp({"echo": json})

    def get(self, path: str) -> _FakeResp:
        self.calls.append((path, None))
        return _FakeResp({"path": path})

    def close(self) -> None:
        self.closed = True


def test_remote_post_and_get_via_injected_client() -> None:
    b = RemoteBackend("http://host:1")
    fake = _FakeClient()
    b._http = fake
    assert b.post_json("/score", {"a": 1}) == {"echo": {"a": 1}}
    assert b.get_json("/health") == {"path": "/health"}
    assert fake.calls == [("/score", {"a": 1}), ("/health", None)]


def test_remote_wraps_request_failures_in_backend_error() -> None:
    class Failing(_FakeClient):
        def post(self, path: str, json: dict) -> _FakeResp:
            raise ValueError("bad json body")

    b = RemoteBackend("http://host:1")
    b._http = Failing()
    with pytest.raises(BackendError, match="POST /score failed"):
        b.post_json("/score", {"a": 1})


def test_remote_unload_closes_client() -> None:
    b = RemoteBackend("http://host:1")
    fake = _FakeClient()
    b._http = fake
    b.unload()
    assert fake.closed is True
    assert b._http is None


@pytest.mark.skipif(_HAS_HTTPX, reason="httpx installed; the missing-extra path can't be exercised")
def test_client_without_httpx_raises_helpful_error() -> None:
    with pytest.raises(BackendError, match=r"argus-cortex\[remote\]"):
        RemoteBackend("http://host:1")._client()
