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
    from collections.abc import Callable
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
