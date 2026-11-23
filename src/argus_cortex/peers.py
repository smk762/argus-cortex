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

Standard library only. The clients take the URL as an argument, as before; this module only finds it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from argus_cortex import settings

__all__ = ["OFF", "Peer", "PeerRefused", "peer_env", "peer_url", "resolve_peer"]

_log = logging.getLogger(__name__)

OFF = frozenset({"off", "none"})
_PEER_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


class PeerRefused(ValueError):
    """A peer's URL is set but unusable. The message names the variable, never the value."""


@dataclass(frozen=True)
class Peer:
    """A resolved peer: its base *url* (no trailing slash) and the variable it came from.

    *source* is ``env:<NAME>``, ``file:<NAME>`` or ``default``.
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


def resolve_peer(name: str, *env: str, default: str | None = None) -> Peer | None:
    """Find peer *name*: each variable in *env*, then ``ARGUS_<PEER>_URL``, then *default*; ``None`` if none is set.

    Raises :class:`PeerRefused` when the first value found is not a usable URL.
    """
    for var in (*env, peer_env(name)):
        value = settings.get(var)
        if value is None:
            continue
        if value.lower() in OFF:
            _log.info("peer %s: turned off by %s", name, var)
            return None
        where = "env" if settings.source(var) == "env" else "file"
        peer = Peer(name, _checked(value, var), f"{where}:{var}")
        _log.debug("peer %s: %s", name, peer.source)
        return peer
    if default:
        return Peer(name, _checked(default, f"the default for peer {name!r}"), "default")
    return None


def peer_url(name: str, *env: str, default: str | None = None) -> str | None:
    """:func:`resolve_peer`'s URL, or ``None`` when the peer is not configured."""
    peer = resolve_peer(name, *env, default=default)
    return peer.url if peer else None
