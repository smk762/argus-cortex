"""Paths from outside stay under their root (the suite's paved road for path traversal).

Any path built from request input, a manifest, an archive member or another service's answer
goes through :func:`safe_join` before it touches the filesystem, and any id or slug that becomes
a path segment goes through :func:`safe_name`. Both refuse with :class:`PathRefused` (a
``ValueError``), never a framework exception, so a server maps it to a ``400`` at its edge.

Design and the reasoning behind each choice: ``docs/paved-roads.md``. Stdlib only.

What these helpers don't cover: the check-then-use race. A path is checked when it's resolved;
a symlink swapped in afterwards by someone who can write under the root is not caught. Callers
writing into a directory others can write to need ``O_NOFOLLOW`` / ``openat``.
"""

from __future__ import annotations

import errno
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

__all__ = ["SAFE_NAME", "PathRefused", "is_within", "safe_basename", "safe_join", "safe_name"]

#: A single path segment: starts alphanumeric (so never ``.`` or ``..``), then letters, digits,
#: ``.``, ``_`` or ``-``. Used with ``fullmatch``, so a trailing newline can't slip through.
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Upload filenames: Unicode letters and digits (people upload "фото.jpg"), plus spaces and
# ``._()+,=@~-``; never a separator, a control character or a leading dot.
_BASENAME = re.compile(r"[^\W_][\w .()+,=@~-]*")


class PathRefused(ValueError):
    """A path or name from outside was refused. The message names the *label*, not the value."""


def _refuse(label: str, why: str) -> PathRefused:
    return PathRefused(f"{label}: {why}")


def _resolve(path: Path) -> Path:
    """*path* with every symlink followed, or ``OSError`` when that cannot be finished.

    ``Path.resolve(strict=False)`` stops at a symlink loop and returns the rest of the path as written, so
    ``loop/../link`` came back as an unfollowed ``link`` that could point anywhere. A resolved path resolves to
    itself; one that does not is refused. From Python 3.13 a loop resolves to itself (``loop``, ``loop/x``) or is
    stepped back out of (``loop/../link``), so the OS is asked about both paths, and ``ELOOP`` is refused. Before
    3.13 a loop is a ``RuntimeError``, raised here as the ``OSError`` it stands for.
    """
    looped = OSError(errno.ELOOP, f"{path.name!r} does not resolve to a fixed path (a symlink loop?)")
    try:
        resolved = path.resolve(strict=False)
        fixed = resolved.resolve(strict=False) == resolved
    except RuntimeError as exc:
        raise looped from exc
    if not fixed:
        raise looped
    for spelling in (path, resolved):
        try:
            os.stat(spelling)
        except OSError as exc:  # any other failure (it need not exist yet) is the caller's to meet
            if exc.errno == errno.ELOOP:
                raise looped from exc
    return resolved


def safe_join(
    root: str | os.PathLike[str],
    user_path: str | os.PathLike[str],
    *,
    allow_root: bool = False,
    allow_absolute: bool = False,
    label: str = "path",
) -> Path:
    """*user_path* under *root*, resolved, or :class:`PathRefused`.

    The **resolved** path (symlinks followed) must be under the resolved *root*, and that
    resolved path is what's returned, so what was checked is what gets used.

    * The root itself is refused unless ``allow_root=True``.
    * An absolute *user_path* is refused unless ``allow_absolute=True``, and then it must still
      resolve under *root*.
    * Empty input, a NUL byte, or a backslash (a Windows separator) is refused.
    * ``~`` is not expanded.
    * A path that can't be resolved (a symlink loop, an OS error) is refused.
    """
    raw = os.fspath(user_path)
    if not isinstance(raw, str):
        raise _refuse(label, "must be text")
    if not raw:
        raise _refuse(label, "is empty")
    if "\x00" in raw:
        raise _refuse(label, "contains a NUL byte")
    if "\\" in raw:
        raise _refuse(label, "contains a backslash")
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() and not allow_absolute:
        raise _refuse(label, "must be relative")
    try:
        base = _resolve(Path(root))
        resolved = _resolve(base / candidate)
    except (OSError, RuntimeError, ValueError) as exc:
        raise _refuse(label, "could not be resolved") from exc
    if resolved == base:
        if allow_root:
            return resolved
        raise _refuse(label, "is the root itself")
    if not resolved.is_relative_to(base):
        raise _refuse(label, "is outside its root")
    return resolved


def is_within(root: str | os.PathLike[str], path: str | os.PathLike[str]) -> bool:
    """Whether *path* resolves to *root* or somewhere under it. Never raises."""
    try:
        base = _resolve(Path(root))
        p = _resolve(Path(path))
    except (OSError, RuntimeError, ValueError):
        return False
    return p == base or p.is_relative_to(base)


def safe_name(value: str, *, pattern: re.Pattern[str] = SAFE_NAME, max_len: int = 128, label: str = "name") -> str:
    """*value* if it is one safe path segment (an id, a slug), else :class:`PathRefused`.

    The default pattern must start alphanumeric, so ``.``, ``..`` and hidden names never match.
    It uses ``fullmatch``: ``re.match`` with ``$`` accepts a trailing newline. A custom *pattern*
    is applied the same way, and ``.`` / ``..`` are refused whatever it allows.
    """
    if not isinstance(value, str) or not value:
        raise _refuse(label, "is empty")
    if len(value) > max_len:
        raise _refuse(label, f"is longer than {max_len} characters")
    if value in {".", ".."} or not pattern.fullmatch(value):
        raise _refuse(label, "is not a safe name")
    return value


def safe_basename(filename: str, *, max_len: int = 255, label: str = "filename") -> str:
    """The last component of a client-supplied filename, if it's safe to use as one segment.

    Both ``/`` and ``\\`` count as separators (browsers send either), so ``..\\..\\x.png`` gives
    ``x.png``. The result must start with a letter or digit (Unicode included) and contain no
    control characters; letters, digits, ``_``, spaces and ``.()+,=@~-`` are allowed, and it is at
    most *max_len* bytes in UTF-8 (the filesystem's limit). Otherwise :class:`PathRefused`.
    """
    if not isinstance(filename, str):
        raise _refuse(label, "must be text")
    name = PureWindowsPath(PurePosixPath(filename.strip()).name).name.strip()
    if not name:
        raise _refuse(label, "has no name")
    if len(name.encode("utf-8")) > max_len:  # filesystems cap a name in bytes (NAME_MAX), not characters
        raise _refuse(label, f"is longer than {max_len} bytes")
    if not _BASENAME.fullmatch(name):
        raise _refuse(label, "is not a safe filename")
    return name
