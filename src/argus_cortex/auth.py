"""One bearer-token check for admin routes, failing closed (the suite's paved road for auth).

``bearer()`` returns a FastAPI/Starlette dependency::

    from fastapi import Depends
    from argus_cortex.auth import bearer

    require_admin = bearer("ARGUS_ADMIN_TOKEN")

    @app.post("/admin/unload", dependencies=[Depends(require_admin)])
    def unload(): ...

* The token is read on every request, from the environment or else the suite file
  (:mod:`argus_cortex.settings`), so rotating it needs no restart.
* **Not configured means refused** (``503``), unless the route opts in with
  ``open_when_unset=True``. That keeps today's "open in dev" behaviour visible in the code
  instead of implicit, and logs a warning the first time it lets a request through.
* ``Authorization: Bearer <token>``: the scheme is case-insensitive and surrounding whitespace is
  ignored (RFC 6750). The comparison is ``hmac.compare_digest`` on UTF-8 bytes, so it takes the
  same time however much of a guess is right, and doesn't raise on non-ASCII input.
* A missing or wrong token is ``401`` with ``WWW-Authenticate: Bearer``.

Design: ``docs/paved-roads.md``. Starlette is behind the ``[server]`` extra (FastAPI ships it).
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Awaitable, Callable

from argus_cortex import settings

try:
    from starlette.exceptions import HTTPException
    from starlette.requests import HTTPConnection
except ImportError as exc:  # pragma: no cover
    raise ImportError("argus_cortex.auth requires Starlette: pip install argus-cortex[server]") from exc

__all__ = ["bearer", "check_bearer"]

_log = logging.getLogger(__name__)


def check_bearer(header: str | None, token: str) -> bool:
    """Whether an ``Authorization`` header carries *token* as a bearer token. Constant-time in
    the token; an empty *token* never matches."""
    if not token:
        return False
    scheme, _, value = (header or "").strip().partition(" ")
    if scheme.lower() != "bearer":
        # Compare anyway, so a missing scheme takes as long as a wrong token.
        hmac.compare_digest(b"", token.encode("utf-8"))
        return False
    return hmac.compare_digest(value.strip().encode("utf-8"), token.encode("utf-8"))


def bearer(
    env_var: str = "ARGUS_ADMIN_TOKEN", *, open_when_unset: bool = False
) -> Callable[[HTTPConnection], Awaitable[None]]:
    """A dependency that requires ``Authorization: Bearer $<env_var>``.

    With the variable unset or blank, requests are refused with ``503`` unless
    *open_when_unset* is true, in which case they pass (with a one-time warning).
    """
    warned = False

    async def require_bearer(conn: HTTPConnection) -> None:
        nonlocal warned
        token = settings.get(env_var, "")
        if not token:
            if open_when_unset:
                if not warned:
                    _log.warning("%s is not set: admin routes are open (open_when_unset=True)", env_var)
                    warned = True
                return
            raise HTTPException(status_code=503, detail=f"{env_var} is not configured, so this route is closed")
        if not check_bearer(conn.headers.get("authorization"), token):
            raise HTTPException(status_code=401, detail="bearer token required", headers={"WWW-Authenticate": "Bearer"})

    require_bearer.__name__ = f"require_bearer_{env_var.lower()}"
    return require_bearer
