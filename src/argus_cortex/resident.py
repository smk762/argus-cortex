"""Shared bodies for a resident's ``POST /admin/evict`` and ``POST /admin/wake``.

A resident whose manager demotes with ``demote_to_cpu()`` and loads with
``ensure_ready()`` uses these instead of copying the two handlers. The 200
body is ``{"status": ..., "model_state": ...}``, the fields every such reply
carries.

What stays in the service, on purpose:

* a route that answers 409 while work holds the model (XTTS, Whisper)
* a reply that also reports ``gpu`` or ``unloaded`` (Qwen, XTTS, Whisper)
* a manager method other than these two (``evict``, ``ensure_gpu``,
  ``ensure_loaded``, or a wake that loads a named checkpoint)

``on_error`` is the service's own failure response. Evict takes one only when
the manager raises: argus-echo's Miso swallows a failed demote inside the
manager and the route still answers 200 with the resulting state; voluptas's
raises ``MisoLoadError`` and the route maps that one type. Wake maps every
``Exception``, which is what both Miso servers do. ``CancelledError`` always
propagates.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["admin_evict", "admin_wake"]

ErrorHandler = Callable[[BaseException], Any]
Errors = type[BaseException] | tuple[type[BaseException], ...]


def _model_state(manager: Any) -> str:
    state = manager.state
    return str(getattr(state, "value", state))


async def admin_evict(
    manager: Any,
    *,
    on_error: ErrorHandler | None = None,
    errors: Errors = (Exception,),
) -> Any:
    """Demote *manager* to CPU and return ``{"status": "evicted", "model_state": ...}``.

    With *on_error*, an exception of a type in *errors* (default: ``Exception``)
    becomes that handler's return value and anything else propagates. Without
    *on_error*, ``demote_to_cpu`` is awaited directly, so every exception
    propagates.
    """
    if on_error is None:
        await manager.demote_to_cpu()
    else:
        try:
            await manager.demote_to_cpu()
        except errors as exc:
            return on_error(exc)
    return {"status": "evicted", "model_state": _model_state(manager)}


async def admin_wake(manager: Any, on_error: ErrorHandler) -> Any:
    """Load *manager* (``ensure_ready``) and return ``{"status": "ready", "model_state": ...}``.

    Any ``Exception`` becomes *on_error*'s return value. ``CancelledError`` propagates.
    """
    try:
        await manager.ensure_ready()
    except Exception as exc:
        return on_error(exc)
    return {"status": "ready", "model_state": _model_state(manager)}
