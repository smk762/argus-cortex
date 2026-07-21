"""Shared ASGI write-guard + env-flag scaffolding for the suite's micro-servers.

Every Argus micro-server (proof, curator, forge, lens) is a small FastAPI app
that must fence off state-changing requests in some deployment mode:

* **read-only / replay** (argus-proof #45) — a GPU-less public demo keeps serving
  stored reports but refuses every write and every live-eval trigger.
* **cross-site protection** (argus-curator #3) — CORS is not a write boundary, so
  unsafe methods carrying an untrusted ``Origin`` are refused before the routes.

Both are the same shape: gate the *unsafe* HTTP methods, refuse with a ``403``
JSON ``{"detail": ...}``, let reads through. :class:`WriteGuard` is that one
gate, parameterised by a *refuse* predicate; :func:`constant_refuse` and
:func:`cross_site_refuse` are the two predicates the suite needs today.

The guard is deliberately **pure ASGI**, not Starlette's ``BaseHTTPMiddleware``:
``BaseHTTPMiddleware`` runs the response through an anyio memory stream, which
buffers the SSE / NDJSON streams these servers return and adds a hop to every
read on a read-heavy demo host. A pure-ASGI guard forwards the untouched ``send``
channel, so streaming endpoints stream and reads pay nothing.

Starlette ships with FastAPI, so this module lives behind the ``argus-cortex``
``[server]`` extra and is never imported at ``import argus_cortex`` time.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable

try:
    from starlette.datastructures import Headers
    from starlette.responses import JSONResponse
    from starlette.types import ASGIApp, Receive, Scope, Send
except ImportError as exc:  # pragma: no cover
    raise ImportError("argus_cortex.server requires Starlette: pip install argus-cortex[server]") from exc

__all__ = [
    "FALSY",
    "TRUTHY",
    "UNSAFE_METHODS",
    "Refuse",
    "WriteGuard",
    "constant_refuse",
    "cross_site_refuse",
    "env_flag",
]

# HTTP methods that change server state (or trigger compute). Idempotent reads
# (GET/HEAD/OPTIONS) are never gated — they stay CORS's business.
UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Recognised env-flag spellings. A *set* value in neither set is a
# misconfiguration (a typo like ``...=enabled``), surfaced by :func:`env_flag`.
TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})
FALSY: frozenset[str] = frozenset({"0", "false", "no", "off", ""})

_log = logging.getLogger(__name__)


def env_flag(name: str) -> bool:
    """Whether env var *name* is set to a truthy string (``1``/``true``/``yes``/``on``).

    A set-but-unrecognised value (a typo like ``ARGUS_PROOF_READ_ONLY=enabled``)
    logs a warning and is treated as **off**, so a mistyped protection flag is
    visible in the logs rather than silently leaving the guard disabled.
    """
    raw = os.environ.get(name, "").strip().lower()
    if raw in TRUTHY:
        return True
    if raw not in FALSY:
        _log.warning("env flag %s set to an unrecognised value %r; treating as off", name, raw)
    return False


# A refusal predicate: given the scope + headers of an *unsafe* request, return a
# ``403`` detail message to refuse it, or ``None`` to let it through.
Refuse = Callable[[Scope, Headers], "str | None"]


class WriteGuard:
    """Pure-ASGI gate on state-changing HTTP methods.

    For every request whose method is in :data:`UNSAFE_METHODS`, consult
    ``refuse(scope, headers)``: a returned string becomes a ``403`` JSON
    ``{"detail": ...}`` and the wrapped app is never called; ``None`` lets the
    request through. Reads (and every non-``http`` scope) are forwarded untouched.

    Register it *before* ``CORSMiddleware`` (``add_middleware`` inserts at the top
    of the stack, so the last added is outermost) — that keeps CORS the outer
    layer and lets it still annotate a refused cross-origin write with its
    headers, so the caller sees a readable ``403`` rather than an opaque CORS
    error.
    """

    def __init__(self, app: ASGIApp, refuse: Refuse) -> None:
        self.app = app
        self.refuse = refuse

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            detail = self.refuse(scope, Headers(scope=scope))
            if detail is not None:
                await JSONResponse({"detail": detail}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def constant_refuse(message: str) -> Refuse:
    """A :class:`WriteGuard` predicate refusing *every* unsafe request with the
    same *message* — read-only / replay mode, where no write is ever allowed.

    Method-based, so it covers every write route — current and future — without
    per-route guards to forget.
    """

    def _refuse(scope: Scope, headers: Headers) -> str | None:
        return message

    return _refuse


def cross_site_refuse(trusted_origins: Iterable[str]) -> Refuse:
    """A :class:`WriteGuard` predicate refusing untrusted cross-origin writes.

    CORS is not a write boundary: a ``multipart/form-data`` POST is a
    CORS-safelisted content type, so a browser sends it with **no preflight** and
    any page the user visits can drive it — the same-origin policy only stops
    that page from *reading* the reply. For an unauthenticated server commonly
    bound on localhost or a LAN address the attacker's own server cannot reach,
    that is enough to poison data (or move files about). So unsafe methods are
    gated on ``Origin``:

    * absent -> allowed. Non-browser clients (curl, the CLI, server-to-server)
      never send it, and browsers always do for cross-origin state changes.
    * same host:port as the request -> allowed, so a UI proxied onto this host
      keeps working un-allow-listed. Compared host:port (not full origin) because
      behind a TLS-terminating proxy the app sees ``http://`` while the browser
      reports ``https://`` — same host either way.
    * in *trusted_origins* -> allowed. That is the operator's own allow-list.
    * anything else -> refused.
    """
    trusted = set(trusted_origins)

    def _refuse(scope: Scope, headers: Headers) -> str | None:
        origin = headers.get("origin")
        if origin is None or origin in trusted:
            return None
        host = headers.get("host")
        if host and origin.partition("://")[2] == host:
            return None
        return f"cross-site {scope['method']} from origin {origin} is not allowed"

    return _refuse
