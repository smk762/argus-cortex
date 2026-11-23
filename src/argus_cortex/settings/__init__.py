"""One owner-only file for the settings every Argus service on a host shares.

Each service keeps its own configuration. What repeats across them (the GPU broker's URL and token, where the
detector servers are, store credentials) goes in one **suite file** instead of a copy per repo::

    # ~/.config/argus/suite.env  (mode 600)
    ARGUS_GPU_BROKER_URL=http://iris.lan:8120
    ARGUS_GPU_BROKER_TOKEN=...
    ARGUS_NUDENET_URL=http://retina.lan:8130

``get(name)`` reads the process environment first and the suite file second, so anything set for one service still
wins. Everything cortex itself reads (``gpu``, ``store``, ``auth``, ``server.env_flag``, ``peers``) goes through it,
so a service picks the file up with no code change. ``apply()`` copies the file's values into ``os.environ`` for
code that reads the environment directly (a service's own settings, ``HF_TOKEN``).

* **Where.** ``ARGUS_ENV_FILE`` names the file (``off`` or ``none`` disables it). Unset, the first of
  ``$XDG_CONFIG_HOME/argus/suite.env`` (``~/.config/argus/suite.env``) and ``/etc/argus/suite.env`` that exists.
  In a container, mount the file read-only at ``/etc/argus/suite.env``.
* **Blank is unset.** ``NAME=`` in the environment (what compose's ``${NAME:-}`` produces) falls through to the file.
* **Owner-only, or it is not read.** On POSIX the file must belong to this user or root, must not be writable by
  group or others, and must not be readable by them unless root owns it (a root-owned ``0444`` secret mount is the
  one accepted exception). A refused, unreadable or missing file is the same as no file: a warning, then every
  feature it would have configured is off, and a token it would have supplied leaves its route closed.
* **Values are never logged.** Warnings and ``python -m argus_cortex.settings`` name variables and the file, and
  nothing else.
* **Re-read when it changes** (one ``stat`` per lookup), so rotating a token needs no restart.

Standard library only.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
import threading
from collections.abc import Collection, Iterator, Mapping
from pathlib import Path

__all__ = [
    "ENV_FILE_VAR",
    "MAX_FILE_BYTES",
    "SettingsRefused",
    "apply",
    "environ",
    "get",
    "parse_env",
    "read_suite_file",
    "source",
    "suite_file",
    "suite_file_candidates",
    "suite_settings",
]

_log = logging.getLogger(__name__)

ENV_FILE_VAR = "ARGUS_ENV_FILE"
SUITE_DIR = "argus"
SUITE_FILE = "suite.env"
SYSTEM_SUITE_FILE = "/etc/argus/suite.env"
DISABLED = frozenset({"off", "none"})
MAX_FILE_BYTES = 64 * 1024

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Variables apply() never sets: they change which code a process or its children load.
_NOT_APPLIED = re.compile(r"(PATH|IFS|ENV|BASH_ENV|LD_.*|DYLD_.*|PYTHON.*|" + ENV_FILE_VAR + ")")


class SettingsRefused(ValueError):
    """The suite file exists but will not be read. The message names the file and the reason, never a value."""


def parse_env(text: str) -> tuple[dict[str, str], list[int]]:
    """Parse ``NAME=value`` lines into a dict, with the 1-based numbers of the lines that were not understood.

    Blank lines and ``#`` comments are skipped, a leading ``export`` is allowed, and one pair of matching quotes
    around a value is removed. There is no interpolation, escaping or line continuation: a value is literal. An
    unquoted value ends at `` #``. A later line wins.
    """
    values: dict[str, str] = {}
    bad: list[int] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        name, sep, value = line.partition("=")
        name = name.strip()
        if not sep or not _NAME.fullmatch(name) or "\x00" in value:
            bad.append(number)
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[name] = value
    return values, bad


def suite_file_candidates() -> tuple[Path, ...]:
    """Where the suite file is looked for, in order. Empty when ``ARGUS_ENV_FILE`` is ``off`` or ``none``."""
    named = os.environ.get(ENV_FILE_VAR, "").strip()
    if named.lower() in DISABLED:
        return ()
    if named:
        return (Path(named).expanduser(),)
    out: list[Path] = []
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if config_home:
        out.append(Path(config_home) / SUITE_DIR / SUITE_FILE)
    else:
        with contextlib.suppress(RuntimeError):  # no home directory (a bare container user)
            out.append(Path.home() / ".config" / SUITE_DIR / SUITE_FILE)
    out.append(Path(SYSTEM_SUITE_FILE))
    return tuple(out)


def suite_file() -> Path | None:
    """The suite file that would be read: the first candidate that exists, or ``None``."""
    for path in suite_file_candidates():
        if path.exists():
            return path
    return None


def _check_owner_only(path: Path, st: os.stat_result) -> None:
    if not stat.S_ISREG(st.st_mode):
        raise SettingsRefused(f"{path} is not a regular file")
    if st.st_size > MAX_FILE_BYTES:
        raise SettingsRefused(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    if os.name != "posix":
        return
    mode = stat.S_IMODE(st.st_mode)
    if st.st_uid not in (0, os.geteuid()):
        raise SettingsRefused(f"{path} belongs to another user (uid {st.st_uid}); it must be yours or root's")
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SettingsRefused(f"{path} is writable by group or others (mode {mode:o}); run: chmod 600 {path}")
    if st.st_uid != 0 and mode & (stat.S_IRGRP | stat.S_IROTH):
        raise SettingsRefused(f"{path} is readable by group or others (mode {mode:o}); run: chmod 600 {path}")


def read_suite_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Read one suite file, strictly: raises :class:`SettingsRefused` if it is not owner-only, and ``OSError`` if it
    cannot be opened. Lines that do not parse are skipped with a warning that gives their numbers."""
    path = Path(path)
    with open(path, "rb") as handle:
        # Checked on the open descriptor, so the file that was checked is the file that is read.
        _check_owner_only(path, os.fstat(handle.fileno()))
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise SettingsRefused(f"{path} is larger than {MAX_FILE_BYTES} bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SettingsRefused(f"{path} is not UTF-8 text") from exc
    values, bad = parse_env(text)
    if bad:
        _warn_once(path, f"skipped lines that are not NAME=value: {', '.join(map(str, bad))}")
    return values


_lock = threading.Lock()
_cache: tuple[tuple[object, ...], dict[str, str]] | None = None
_warned: set[tuple[str, str]] = set()


def _warn_once(path: Path, message: str) -> None:
    key = (str(path), message)
    if key not in _warned:
        _warned.add(key)
        _log.warning("suite settings file %s: %s", path, message)


def suite_settings() -> Mapping[str, str]:
    """The suite file's values, or an empty mapping when there is no usable file. Never raises.

    A file that is refused or cannot be read is reported once in the log and then treated as absent.
    """
    global _cache
    explicit = bool(os.environ.get(ENV_FILE_VAR, "").strip())
    for path in suite_file_candidates():
        try:
            st = os.stat(path)
        except FileNotFoundError:
            if explicit:
                _warn_once(path, f"named by {ENV_FILE_VAR} but does not exist")
            continue
        except OSError as exc:
            _warn_once(path, f"not read ({type(exc).__name__})")
            continue
        key = (str(path), st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode, st.st_uid)
        with _lock:
            if _cache is not None and _cache[0] == key:
                return _cache[1]
        try:
            values = read_suite_file(path)
        except SettingsRefused as exc:
            _warn_once(path, f"not read: {exc}")
            values = {}
        except OSError as exc:
            _warn_once(path, f"not read ({type(exc).__name__})")
            values = {}
        with _lock:
            _cache = (key, values)
        return values
    return {}


def get(name: str, default: str | None = None) -> str | None:
    """The setting *name*, stripped: from the process environment, else the suite file, else *default*.

    A blank value counts as unset at both levels.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    value = suite_settings().get(name, "").strip()
    return value or default


def source(name: str) -> str | None:
    """Where :func:`get` finds *name*: ``"env"``, ``"file"`` or ``None``."""
    if os.environ.get(name, "").strip():
        return "env"
    if suite_settings().get(name, "").strip():
        return "file"
    return None


class _Environ(Mapping[str, str]):
    """The process environment with the suite file behind it, as a read-only mapping. Blank values are absent."""

    def __getitem__(self, name: str) -> str:
        value = get(name)
        if value is None:
            raise KeyError(name)
        return value

    def __iter__(self) -> Iterator[str]:
        seen = {k for k, v in os.environ.items() if v.strip()}
        seen |= {k for k, v in suite_settings().items() if v.strip()}
        return iter(sorted(seen))

    def __len__(self) -> int:
        return sum(1 for _ in self)


environ: Mapping[str, str] = _Environ()
"""``os.environ`` with the suite file behind it, for code that takes an ``env`` mapping."""


def apply(names: Collection[str] | None = None) -> list[str]:
    """Copy suite-file values into ``os.environ`` where the variable is unset or blank; returns the names set.

    For code that reads the environment directly: call it once at startup, before that code runs. *names* limits it
    to those variables. Variables that change what a process loads (``PATH``, ``LD_*``, ``DYLD_*``, ``PYTHON*``) are
    never set from the file.
    """
    applied: list[str] = []
    for name, value in suite_settings().items():
        if names is not None and name not in names:
            continue
        if not value.strip() or os.environ.get(name, "").strip():
            continue
        if _NOT_APPLIED.fullmatch(name):
            _log.warning("suite settings: %s is not applied to the environment from a file", name)
            continue
        os.environ[name] = value.strip()
        applied.append(name)
    return sorted(applied)
