"""Shared error type + optional-driver helpers for the store package.

Every store here talks to an **optional, external** service through a driver
that lives behind an extra (``[postgres]``, ``[qdrant]``, …). Two concerns recur
across all of them, so they live in one place instead of being re-implemented per
store (as the Postgres store first did, duplicating ``backends``' pattern):

* :func:`require_extra` — import a driver module or raise a clear "install the
  extra" :class:`StoreError`;
* :func:`wrap_errors` — run a driver call and fold its operational failures into
  :class:`StoreError`, so callers catch one type regardless of which backend.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from types import ModuleType

T = TypeVar("T")


class StoreError(RuntimeError):
    """A persistence failure: an optional driver is missing, or the service failed.

    The single exception type the store package raises, so a caller can ``except
    StoreError`` whether the backend is Postgres, Qdrant, or a future one.
    """


def require_extra(module: str, extra: str, *, feature: str) -> ModuleType:
    """Import optional driver *module*, or raise :class:`StoreError` naming the extra.

    ``feature`` is the human name used in the message (e.g. ``"postgres lineage"``
    → ``"postgres lineage needs: pip install argus-cortex[postgres]"``).
    """
    try:
        return importlib.import_module(module)
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise StoreError(f"{feature} needs: pip install argus-cortex[{extra}]") from exc


def resolve_error_types(specs: Iterable[tuple[str, str | tuple[str, ...]]]) -> tuple[type[BaseException], ...]:
    """Resolve a driver's operational exception classes, tolerating a missing driver.

    Each spec is ``(module, attr)`` or ``(module, (attr, …))`` naming exception
    classes to catch (e.g. ``("psycopg", "Error")``, ``("httpx", "HTTPError")``).
    A module that isn't importable, or an attribute that isn't there, is skipped —
    so the result is empty exactly when no driver is installed (the missing-driver
    path has already raised :class:`StoreError` before any op runs). Shared by every
    store's cached ``_<driver>_error_types()`` so the lazy-import discipline lives
    in one place. Non-exception attributes are ignored defensively.
    """
    found: list[type[BaseException]] = []
    for module, attrs in specs:
        try:
            mod = importlib.import_module(module)
        except ImportError:  # pragma: no cover - exercised only without the extra
            continue
        for attr in (attrs,) if isinstance(attrs, str) else attrs:
            cls = getattr(mod, attr, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                found.append(cls)
    return tuple(found)


def wrap_errors(op: Callable[[], T], *, errors: tuple[type[BaseException], ...], label: str) -> T:
    """Run *op*, folding any exception in *errors* into a :class:`StoreError`.

    Mirrors ``backends._send``: a bad DSN/URL, unreachable server, constraint
    violation, or malformed payload surfaces as :class:`StoreError` (``"{label}
    failed: …"``) rather than a raw driver exception the caller would have to
    import the optional driver to name. *errors* is supplied lazily by each store
    (its driver's exception base) so nothing driver-specific is imported here.
    """
    try:
        return op()
    except errors as exc:
        raise StoreError(f"{label} failed: {exc}") from exc
