"""The one HTTP path the moderation clients share. Stdlib only."""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from urllib.parse import urlsplit

# Ignore HTTP(S)_PROXY: images go straight to the configured server, never
# through whatever proxy happens to be in the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ModerationConfigError(ValueError):
    """A moderation client was configured with an unusable URL or option."""


class CheckFailed(RuntimeError):
    """One image could not be checked (network error, bad response)."""


def service_url(base: str, path: str, service: str) -> str:
    """*base* (a server's origin) with *path* appended; a *base* already ending
    in *path* is used as is.

    The error names *service* but not *base*: a URL can carry a credential, and
    callers surface this message.
    """
    base = base.strip().rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ModerationConfigError(f"the {service} URL must be an http(s) URL")
    return base if base.endswith(path) else f"{base}{path}"


def post_json(
    url: str, body: bytes, content_type: str, timeout: float, service: str, failed: type[CheckFailed]
) -> object:
    """POST *body* to *url* and return the decoded JSON answer.

    Every transport or decoding problem is raised as *failed*, so a caller
    checking many images can record it against the one image.
    """
    req = urllib.request.Request(url, data=body, headers={"Content-Type": content_type}, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - caller-configured URL
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise failed(f"{service} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise failed(f"{service} request failed: {exc}") from exc
    except ValueError as exc:
        raise failed(f"{service} returned a body that is not JSON") from exc
