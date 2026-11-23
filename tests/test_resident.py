"""argus_cortex.resident: one evict and one wake for managers that demote and ensure_ready."""

from __future__ import annotations

import asyncio
from enum import Enum

import pytest

import argus_cortex
from argus_cortex import admin_evict, admin_wake


class State(Enum):
    GPU = "gpu"
    CPU = "cpu"
    UNLOADED = "unloaded"


class LoadError(RuntimeError):
    pass


class Manager:
    def __init__(
        self,
        state: State = State.GPU,
        demote_error: BaseException | None = None,
        wake_error: BaseException | None = None,
    ) -> None:
        self.state = state
        self.demote_error = demote_error
        self.wake_error = wake_error
        self.demoted = False
        self.woken = False

    async def demote_to_cpu(self) -> None:
        self.demoted = True
        if self.demote_error is not None:
            raise self.demote_error
        self.state = State.CPU

    async def ensure_ready(self) -> None:
        self.woken = True
        if self.wake_error is not None:
            raise self.wake_error
        self.state = State.GPU


async def test_evict_reports_the_state_after_demotion() -> None:
    mgr = Manager()
    assert await admin_evict(mgr) == {"status": "evicted", "model_state": "cpu"}
    assert mgr.demoted


async def test_evict_without_a_handler_propagates() -> None:
    """argus-echo's Miso: a failed demote is the manager's to swallow, or it raises out of the route."""
    mgr = Manager(demote_error=RuntimeError("split"))
    with pytest.raises(RuntimeError, match="split"):
        await admin_evict(mgr)


async def test_evict_on_error_maps_exception() -> None:
    mgr = Manager(demote_error=RuntimeError("split"))
    assert await admin_evict(mgr, on_error=lambda exc: {"error": str(exc)}) == {"error": "split"}


async def test_evict_maps_only_the_named_errors() -> None:
    """voluptas's Miso: only MisoLoadError becomes the load-failed response; anything else propagates."""
    mgr = Manager(demote_error=LoadError("oom"))
    assert await admin_evict(mgr, on_error=lambda exc: {"error": type(exc).__name__}, errors=(LoadError,)) == {
        "error": "LoadError"
    }

    other = Manager(demote_error=RuntimeError("other"))
    with pytest.raises(RuntimeError, match="other"):
        await admin_evict(other, on_error=lambda exc: {"error": "mapped"}, errors=(LoadError,))


async def test_wake_reports_ready_and_maps_exceptions() -> None:
    mgr = Manager(state=State.CPU)
    assert await admin_wake(mgr, lambda exc: {"error": str(exc)}) == {"status": "ready", "model_state": "gpu"}
    assert mgr.woken

    failed = Manager(wake_error=LoadError("nope"))
    assert await admin_wake(failed, lambda exc: {"error": str(exc)}) == {"error": "nope"}


async def test_wake_does_not_swallow_cancellation() -> None:
    mgr = Manager(wake_error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await admin_wake(mgr, lambda exc: {"error": "swallowed"})


async def test_a_plain_state_is_reported_as_itself() -> None:
    mgr = Manager()
    mgr.state = "unloaded"  # type: ignore[assignment]

    async def demote() -> None:
        mgr.demoted = True

    mgr.demote_to_cpu = demote  # type: ignore[method-assign]
    assert (await admin_evict(mgr))["model_state"] == "unloaded"


def test_exported_at_the_top_level() -> None:
    assert argus_cortex.admin_evict is admin_evict
    assert argus_cortex.admin_wake is admin_wake
    assert {"admin_evict", "admin_wake"} <= set(argus_cortex.__all__)
