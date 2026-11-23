"""The one HTTP path the moderation clients share. Stdlib only."""

from __future__ import annotations

import base64
import http.client
import json
import math
import re
import urllib.error
import urllib.request
import uuid
from urllib.parse import unquote, urlsplit, urlunsplit

# Ignore HTTP(S)_PROXY: images go straight to the configured server, never
# through whatever proxy happens to be in the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ModerationConfigError(ValueError):
    """A moderation client was configured with an unusable URL or option."""


class CheckFailed(RuntimeError):
    """One image could not be checked (network error, bad response)."""


def service_url(base: str, path: str, service: str) -> str:
    """*base* (a server's origin) with *path* appended to its path; a *base*
    whose path already ends in *path* is used as is. A query string is kept.

    The error names *service* but not *base*: a URL can carry a credential, and
    callers surface this message.
    """
    parts = urlsplit(base.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ModerationConfigError(f"the {service} URL must be an http(s) URL")
    base_path = parts.path.rstrip("/")
    if not base_path.endswith(path):
        base_path += path
    return urlunsplit((parts.scheme, parts.netloc, base_path, parts.query, ""))


def sibling_url(url: str, path: str, sibling: str) -> str:
    """*url* (from :func:`service_url`) with its trailing *path* swapped for *sibling*: ``/infer`` -> ``/health``."""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=parts.path.removesuffix(path) + sibling))


def is_number(value: object) -> bool:
    """A finite int or float, not a bool.

    json.loads accepts bare NaN/Infinity; a NaN score would compare False against every threshold.
    It also returns an integer literal of any length exactly; one too large for a float is not a score.
    """
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:  # an int past the float range
        return False


def multipart_image(data: bytes, filename: str, field: str = "f1") -> tuple[bytes, str]:
    """One image as a ``multipart/form-data`` body in *field*, and its content type."""
    # The name lands in a header: keep it to characters that cannot break it.
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)[-100:] or "image"
    boundary = uuid.uuid4().hex
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    return head + data + f"\r\n--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def _split_credentials(url: str) -> tuple[str, dict[str, str]]:
    """*url* without its ``user:password@`` part, plus the Basic auth header it
    stood for (urllib would otherwise take the userinfo for part of the host)."""
    parts = urlsplit(url)
    if parts.username is None:
        return url, {}
    netloc = parts.netloc.rpartition("@")[2]
    userinfo = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
    auth = f"Basic {base64.b64encode(userinfo.encode()).decode('ascii')}"
    return urlunsplit(parts._replace(netloc=netloc)), {"Authorization": auth}


def post_json(
    url: str, body: bytes, content_type: str, timeout: float, service: str, failed: type[CheckFailed]
) -> object:
    """POST *body* to *url* and return the decoded JSON answer.

    Every transport or decoding problem is raised as *failed*, so a caller
    checking many images can record it against the one image.
    """
    return _request_json("POST", url, body, {"Content-Type": content_type}, timeout, service, failed)


def get_json(url: str, timeout: float, service: str, failed: type[CheckFailed], *, missing_ok: bool = False) -> object:
    """GET *url* and return the decoded JSON answer; ``None`` on a 404 when *missing_ok*.

    Failures are raised as *failed*, as in :func:`post_json`.
    """
    return _request_json("GET", url, None, {}, timeout, service, failed, missing_ok=missing_ok)


def _request_json(
    method: str,
    url: str,
    body: bytes | None,
    headers: dict[str, str],
    timeout: float,
    service: str,
    failed: type[CheckFailed],
    *,
    missing_ok: bool = False,
) -> object:
    url, auth = _split_credentials(url)
    req = urllib.request.Request(url, data=body, headers={**headers, **auth}, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:  # noqa: S310 - caller-configured URL
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        exc.close()  # the exception outlives the request as __cause__; its socket need not
        if missing_ok and exc.code == 404:
            return None
        raise failed(f"{service} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
        raise failed(f"{service} request failed: {exc}") from exc
    except ValueError as exc:
        raise failed(f"{service} returned a body that is not JSON") from exc
