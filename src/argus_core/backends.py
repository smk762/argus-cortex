"""The local / remote AI-backend contract, shared across the suite.

Generalised from argus-lens's captioner backends (``CaptionBackend`` →
``LocalBackend`` / ``CloudBackend``) so every stage that calls a model — lens
captioning, proof scoring — shares one lifecycle, one retry policy, and one way
to point at a **hosted service by IP/port** instead of loading weights locally.

* :class:`Backend` — lifecycle + capability attributes (``load`` / ``unload`` /
  ``is_available``); the domain call (caption an image, score an image) is added
  by the subclass.
* :class:`LocalBackend` — runs inference in-process on a resolved device.
* :class:`RemoteBackend` — calls a hosted HTTP endpoint (``base_url`` or
  ``host``/``port``, or a known :class:`RemoteProvider`); httpx lives behind the
  ``argus-core[remote]`` extra and is imported lazily.

Concrete, domain-specific adapters (a CLIP scorer, an OpenAI captioner) subclass
these in the consuming package; this module stays domain-agnostic.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from typing import TypeVar

T = TypeVar("T")


class BackendError(RuntimeError):
    """A backend failure: model unavailable, remote endpoint unreachable."""


class BackendKind(StrEnum):
    """Where a backend runs — locally in-process, or a remote hosted service."""

    LOCAL = "local"
    REMOTE = "remote"


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call *fn*, retrying on *retry_on* with exponential backoff.

    Re-raises the last error after *attempts* tries. ``sleep`` is injectable so
    tests exercise the backoff without wall-clock delay.
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            if attempt == attempts - 1:
                break
            sleep(min(max_delay, base_delay * (2**attempt)))
    assert last is not None
    raise last


def resolve_device(device: str = "auto") -> str:
    """Resolve ``"auto"`` to ``"cuda"``/``"mps"``/``"cpu"`` (or pass a fixed device through).

    Uses torch only if it's importable; with no torch present, ``"auto"`` becomes
    ``"cpu"`` so a device-free environment still works.
    """
    if device and device != "auto":
        return device
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        mps = getattr(torch.backends, "mps", None)
        if mps is not None and mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


class Backend:
    """Base for any model-backed backend: lifecycle + capability descriptor.

    Subclasses set the class attributes and add the domain call. ``load`` /
    ``unload`` default to no-ops so a trivial backend need not override them;
    ``is_available`` / ``availability_reason`` let a registry skip a backend that
    can't run right now (missing weights, unset API key) with a clear reason.
    """

    name: str = "base"
    kind: BackendKind = BackendKind.LOCAL
    requires_gpu: bool = False

    def load(self, device: str = "auto") -> None:
        """Load weights / init the client. Called lazily on first use."""

    def unload(self) -> None:
        """Release resources (GPU memory, HTTP connections)."""

    def is_available(self) -> bool:
        """Whether this backend can be used right now."""
        return True

    def availability_reason(self) -> str | None:
        """Human-readable reason this backend is unavailable, or ``None``."""
        return None


class LocalBackend(Backend):
    """Base for backends that run inference in-process on a device.

    The target device is supplied once via :meth:`load` and remembered, so the
    domain call stays device-free. Subclasses read ``self.device``.
    """

    kind = BackendKind.LOCAL
    requires_gpu = True

    def __init__(self) -> None:
        self.device: str | None = None

    def load(self, device: str = "auto") -> None:
        self.device = resolve_device(device)


class RemoteProvider(StrEnum):
    """Known hosted inference providers (base URLs in :data:`REMOTE_PROVIDER_URLS`)."""

    HF_INFERENCE = "hf_inference"
    NVIDIA_NIM = "nvidia_nim"
    OPENAI_COMPAT = "openai_compat"
    REPLICATE = "replicate"


# Default base URLs per provider. OPENAI_COMPAT has none — a local/self-hosted
# OpenAI-compatible server is addressed by an explicit base_url / host+port.
REMOTE_PROVIDER_URLS: dict[RemoteProvider, str | None] = {
    RemoteProvider.HF_INFERENCE: "https://api-inference.huggingface.co",
    RemoteProvider.NVIDIA_NIM: "https://integrate.api.nvidia.com/v1",
    RemoteProvider.OPENAI_COMPAT: None,
    RemoteProvider.REPLICATE: "https://api.replicate.com/v1",
}


class RemoteBackend(Backend):
    """Base for backends that call a hosted HTTP endpoint.

    Point it at a service by ``base_url``, by ``host``/``port`` via
    :meth:`from_host`, or at a known provider via :meth:`from_provider`. An
    ``api_key`` becomes a bearer ``Authorization`` header unless one is already
    supplied. httpx is imported lazily (``argus-core[remote]``); an injected
    ``_http`` client (tests) bypasses the import.
    """

    kind = BackendKind.REMOTE
    requires_gpu = False

    def __init__(
        self,
        base_url: str | None = None,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        provider: RemoteProvider | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.api_key = api_key
        self.timeout = timeout
        self.provider = provider
        self.headers = dict(headers or {})
        if api_key and not any(k.lower() == "authorization" for k in self.headers):
            self.headers["Authorization"] = f"Bearer {api_key}"
        self._http = None  # lazily-created httpx.Client (or an injected fake)

    @classmethod
    def from_host(cls, host: str, port: int, *, scheme: str = "http", **kwargs: object) -> RemoteBackend:
        """Address a service by host + port, e.g. a self-hosted model server."""
        return cls(f"{scheme}://{host}:{port}", **kwargs)  # type: ignore[arg-type]

    @classmethod
    def from_provider(cls, provider: RemoteProvider | str, **kwargs: object) -> RemoteBackend:
        """Address a known hosted provider (its default base URL)."""
        provider = RemoteProvider(provider)
        base = REMOTE_PROVIDER_URLS[provider]
        if base is None and "base_url" not in kwargs:
            raise BackendError(f"provider {provider.value} needs an explicit base_url (host+port of your server)")
        return cls(kwargs.pop("base_url", base), provider=provider, **kwargs)  # type: ignore[arg-type]

    def is_available(self) -> bool:
        return bool(self.base_url)

    def availability_reason(self) -> str | None:
        return None if self.base_url else "no base_url configured (set base_url or host/port)"

    def _client(self):  # noqa: ANN202  (httpx is optional; avoid importing at module load)
        if self._http is None:
            try:
                import httpx
            except ImportError as exc:  # pragma: no cover - exercised only without the extra
                raise BackendError("remote backends need: pip install argus-core[remote]") from exc
            if not self.base_url:
                raise BackendError("remote backend has no base_url")
            self._http = httpx.Client(base_url=self.base_url, timeout=self.timeout, headers=self.headers)
        return self._http

    @staticmethod
    def _http_errors() -> tuple[type[BaseException], ...]:
        try:
            import httpx

            return (httpx.HTTPError,)
        except ImportError:  # pragma: no cover
            return (Exception,)

    def post_json(self, path: str, payload: dict, *, attempts: int = 3) -> dict:
        """POST JSON and return the JSON response, retrying transient failures."""
        client = self._client()

        def call() -> dict:
            resp = client.post(path, json=payload)
            resp.raise_for_status()
            return resp.json()

        return with_retries(call, attempts=attempts, retry_on=self._http_errors())

    def get_json(self, path: str, *, attempts: int = 3) -> dict:
        """GET and return the JSON response, retrying transient failures."""
        client = self._client()

        def call() -> dict:
            resp = client.get(path)
            resp.raise_for_status()
            return resp.json()

        return with_retries(call, attempts=attempts, retry_on=self._http_errors())

    def unload(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None
