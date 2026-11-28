"""Where a service finds another one: one resolution order, one suite-wide name per peer.

Every stage that calls NudeNet used to name its URL itself (``ARGUS_CURATOR_NUDENET_URL``,
``ARGUS_DARKROOM_NUDENET_URL``, ``PROOF_NUDENET_URL``), so one server was configured three times. A peer now has one
suite-wide variable, ``ARGUS_<PEER>_URL``, and a stage's own variable still wins::

    from argus_cortex.peers import peer_url

    url = peer_url("nudenet", "ARGUS_CURATOR_NUDENET_URL")   # the stage's name first, then ARGUS_NUDENET_URL
    client = NudeNetClient(url) if url else None

Each name is read through :mod:`argus_cortex.settings` (process environment, then the suite file), in the order
given, then ``ARGUS_<PEER>_URL``, then *default*.

* **Blank is unset** and falls through to the next name. ``off`` (or ``none``) stops there: the peer is not
  configured for this service, whatever the suite file says.
* **A bad URL is refused, not skipped.** A value that is not an absolute ``http(s)`` URL with a host raises
  :class:`PeerRefused`, naming the variable and never the value (a URL can carry credentials). Returning ``None``
  would read as "this check is not configured", and a typo must not turn a guard off.
* **None means not configured.** Whether that is acceptable is the caller's rule: a moderation check that is
  required must refuse, not pass.

* **Discovery is opt-in.** ``peer_url(..., discover=True)`` asks the suite's directory (argus-iris,
  ``GET /directory/<peer>``) after the variables and before the default. It needs ``ARGUS_GPU_BROKER_URL`` and
  ``ARGUS_GPU_BROKER_TOKEN``, and httpx (``[remote]``). See :func:`resolve_peer`; on an event loop use
  :func:`apeer_url` / :func:`aresolve_peer`.

Standard library only without discovery. The clients take the URL as an argument, as before; this module only
finds it.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from argus_cortex import settings
from argus_cortex.peer_names import PEER_NAME_PATTERN

__all__ = [
    "OFF",
    "PEER_NAME_PATTERN",
    "Peer",
    "PeerRefused",
    "apeer_url",
    "aresolve_peer",
    "peer_env",
    "peer_url",
    "resolve_peer",
]

_log = logging.getLogger(__name__)

OFF = frozenset({"off", "none"})
_PEER_NAME = re.compile(PEER_NAME_PATTERN)


class PeerRefused(ValueError):
    """A peer's URL is set but unusable. The message names the variable, never the value."""


@dataclass(frozen=True)
class Peer:
    """A resolved peer: its base *url* (no trailing slash) and the variable it came from.

    *source* is ``env:<NAME>``, ``file:<NAME>``, ``broker`` (the directory) or ``default``.
    """

    name: str
    url: str
    source: str

    def __repr__(self) -> str:  # the URL may carry credentials
        return f"Peer(name={self.name!r}, host={urlsplit(self.url).hostname!r}, source={self.source!r})"


def peer_env(name: str) -> str:
    """The suite-wide variable for peer *name*: ``nudenet`` → ``ARGUS_NUDENET_URL``."""
    if not _PEER_NAME.fullmatch(name):
        raise ValueError(f"not a peer name: {name!r} (lower-case letters, digits and single hyphens)")
    return f"ARGUS_{name.upper().replace('-', '_')}_URL"


def _checked(value: str, origin: str) -> str:
    try:
        parts = urlsplit(value)
        host, _ = parts.hostname, parts.port  # .port raises on a non-numeric or out-of-range port
    except ValueError:
        raise PeerRefused(f"{origin} is not a URL") from None
    if parts.scheme not in ("http", "https") or not host:
        raise PeerRefused(f"{origin} must be an absolute http(s) URL with a host")
    if parts.query or parts.fragment:
        raise PeerRefused(f"{origin} must not have a query or fragment")
    return value.rstrip("/")


_OFF = object()


def _configured(name: str, env: tuple[str, ...]) -> Peer | object | None:
    """The peer the variables name, ``_OFF`` when one turns it off, or ``None`` when none is set."""
    for var in (*env, peer_env(name)):
        value = settings.get(var)
        if value is None:
            continue
        if value.lower() in OFF:
            _log.info("peer %s: turned off by %s", name, var)
            return _OFF
        where = "env" if settings.source(var) == "env" else "file"
        peer = Peer(name, _checked(value, var), f"{where}:{var}")
        _log.debug("peer %s: %s", name, peer.source)
        return peer
    return None


def _default(name: str, default: str | None) -> Peer | None:
    return Peer(name, _checked(default, f"the default for peer {name!r}"), "default") if default else None


# ── discovery: ask the suite's directory (argus-iris) ────────────────────────────────────────────────────────────

BROKER_SOURCE = "broker"
DISCOVERY_TTL_S = 60.0
"""How long a found peer is used before the directory is asked again."""
DISCOVERY_MISS_TTL_S = 15.0
"""How long "not found" or "could not ask" stands before the directory is asked again."""
DISCOVERY_TIMEOUT_S = 2.0

_lock = threading.Lock()
_found: dict[tuple[str, str], tuple[float, Peer | None]] = {}  # (broker, peer) -> (ask again after, what was found)
_last_good: dict[tuple[str, str], Peer] = {}
_said: set[tuple[str, str]] = set()


def _say_once(key: tuple[str, str], message: str, *args: object) -> None:
    if key not in _said:
        _said.add(key)
        _log.warning(message, *args)


def _directory() -> tuple[str, str] | None:
    """The directory's URL and the caller token, or ``None`` when discovery cannot be used."""
    url, token = settings.get("ARGUS_GPU_BROKER_URL"), settings.get("ARGUS_GPU_BROKER_TOKEN")
    if not url:
        return None
    if not token:
        # The directory decides where work is sent. Without a token anyone on the network could have filled it.
        _say_once(("token", ""), "peer discovery is off: ARGUS_GPU_BROKER_TOKEN is not set")
        return None
    return url.rstrip("/"), token


def _cached(key: tuple[str, str]) -> tuple[bool, Peer | None]:
    with _lock:
        hit = _found.get(key)
    if hit is not None and time.monotonic() < hit[0]:
        return True, hit[1]
    return False, None


def _httpx() -> Any:
    try:
        import httpx  # noqa: PLC0415 - optional, behind the [remote] extra
    except ImportError as exc:
        raise ImportError("peer discovery requires httpx: pip install argus-cortex[remote]") from exc
    return httpx


def _client_kwargs(token: str) -> dict[str, Any]:
    # trust_env=False: the directory is on the LAN and must not go through a desktop's proxy. Redirects are not
    # followed (httpx's default), so the token goes to the configured host and nowhere else.
    return {"headers": {"Authorization": f"Bearer {token}"}, "timeout": DISCOVERY_TIMEOUT_S, "trust_env": False}


def _from_reply(name: str, status: int, body: Callable[[], Any]) -> tuple[Peer | None, str | None]:
    """The peer a directory reply names, or the reason there is none."""
    from argus_cortex.service import PeerLookup  # noqa: PLC0415 - peers stays pydantic-free without discovery

    if status == 404:
        return None, "the directory lists no reachable one"
    if status in (401, 403):
        return None, f"the directory refused the caller token (HTTP {status})"
    if status != 200:
        return None, f"the directory answered HTTP {status}"
    try:
        lookup = PeerLookup.model_validate(body())
    except Exception as exc:  # noqa: BLE001 - not JSON, not the model, or a version this build does not read
        return None, f"the directory's reply was not understood ({type(exc).__name__})"
    for entry in lookup.urls:
        if entry.peer != name or not entry.url:
            continue
        try:
            url = _checked(entry.url, "a directory entry")
        except PeerRefused:
            continue
        if urlsplit(url).username is not None or urlsplit(url).password is not None:
            continue  # a directory names a place, never credentials
        return Peer(name, url, BROKER_SOURCE), None
    return None, "the directory lists no usable URL for it"


def _settle(key: tuple[str, str], name: str, peer: Peer | None, why: str | None) -> Peer | None:
    """Record a lookup's outcome. A peer found once is not un-found by a later failure: see :func:`resolve_peer`."""
    with _lock:
        if peer is not None:
            _last_good[key] = peer
            _found[key] = (time.monotonic() + DISCOVERY_TTL_S, peer)
            return peer
        kept = _last_good.get(key)
        _found[key] = (time.monotonic() + DISCOVERY_MISS_TTL_S, kept)
    if kept is not None:
        _say_once((name, "kept"), "peer %s: %s; keeping the last one found", name, why)
    else:
        _log.debug("peer %s: not discovered: %s", name, why)
    return kept


def _discover(name: str, transport: Any) -> Peer | None:
    where = _directory()
    if where is None:
        return None
    key = (where[0], name)
    cached, peer = _cached(key)
    if cached:
        return peer
    try:
        httpx = _httpx()
        with httpx.Client(transport=transport, **_client_kwargs(where[1])) as client:
            reply = client.get(f"{where[0]}/directory/{name}")
        peer, why = _from_reply(name, reply.status_code, reply.json)
    except ImportError:
        _say_once(("httpx", ""), "peer discovery is off: httpx is not installed (argus-cortex[remote])")
        return None
    except Exception as exc:  # noqa: BLE001 - an unreachable directory is "could not ask", never an error here
        peer, why = None, f"the directory could not be asked ({type(exc).__name__})"
    return _settle(key, name, peer, why)


async def _adiscover(name: str, transport: Any) -> Peer | None:
    where = _directory()
    if where is None:
        return None
    key = (where[0], name)
    cached, peer = _cached(key)
    if cached:
        return peer
    try:
        httpx = _httpx()
        async with httpx.AsyncClient(transport=transport, **_client_kwargs(where[1])) as client:
            reply = await client.get(f"{where[0]}/directory/{name}")
        peer, why = _from_reply(name, reply.status_code, reply.json)
    except ImportError:
        _say_once(("httpx", ""), "peer discovery is off: httpx is not installed (argus-cortex[remote])")
        return None
    except Exception as exc:  # noqa: BLE001
        peer, why = None, f"the directory could not be asked ({type(exc).__name__})"
    return _settle(key, name, peer, why)


def resolve_peer(
    name: str, *env: str, default: str | None = None, discover: bool = False, transport: Any = None
) -> Peer | None:
    """Find peer *name*: each variable in *env*, then ``ARGUS_<PEER>_URL``, then the directory when *discover* is
    set, then *default*; ``None`` if nothing names it.

    Raises :class:`PeerRefused` when the first variable set is not a usable URL.

    **Discovery** asks argus-iris, at ``ARGUS_GPU_BROKER_URL``, for ``GET /directory/<name>``: where a container
    labelled ``argus.peer: <name>`` is published. It is used only when ``ARGUS_GPU_BROKER_TOKEN`` is set, since the
    answer decides where work is sent. The answer is cached (:data:`DISCOVERY_TTL_S`), and this call blocks for at
    most :data:`DISCOVERY_TIMEOUT_S` when it has to ask: on an event loop use :func:`aresolve_peer`. A directory
    that is down, refuses the token or lists nothing is "not found", never an error. **A peer found once stays
    found** for the life of the process: if a later lookup fails, the last URL is kept, so a guard does not read as
    "not configured" because the directory blinked; a peer that has really gone then fails its calls instead.
    Needs httpx (``[remote]``). *transport* injects an httpx transport (tests).
    """
    configured = _configured(name, env)
    if configured is _OFF:
        return None
    if isinstance(configured, Peer):
        return configured
    return (_discover(name, transport) if discover else None) or _default(name, default)


async def aresolve_peer(
    name: str, *env: str, default: str | None = None, discover: bool = False, transport: Any = None
) -> Peer | None:
    """:func:`resolve_peer` for an event loop: the directory is asked without blocking it."""
    configured = _configured(name, env)
    if configured is _OFF:
        return None
    if isinstance(configured, Peer):
        return configured
    return (await _adiscover(name, transport) if discover else None) or _default(name, default)


def peer_url(name: str, *env: str, default: str | None = None, discover: bool = False) -> str | None:
    """:func:`resolve_peer`'s URL, or ``None`` when the peer is not configured."""
    peer = resolve_peer(name, *env, default=default, discover=discover)
    return peer.url if peer else None


async def apeer_url(name: str, *env: str, default: str | None = None, discover: bool = False) -> str | None:
    """:func:`aresolve_peer`'s URL, or ``None`` when the peer is not configured."""
    peer = await aresolve_peer(name, *env, default=default, discover=discover)
    return peer.url if peer else None
