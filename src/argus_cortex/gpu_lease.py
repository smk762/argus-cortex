"""The GPU lease contract every coordinator shares: :class:`GpuCoordinator`, the :class:`GpuLease` handle it yields,
:class:`GpuLeaseTimeout` and :class:`GpuLeaseRefused`, :class:`NullCoordinator` and :data:`DEFAULT_TIMEOUT_S`.

The coordinators themselves live in :mod:`argus_cortex.gpu` (local) and :mod:`argus_cortex.gpu_broker` (the broker
client); both import the contract from here, and :mod:`argus_cortex.gpu` keeps every name importable from it.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from argus_cortex.gpu_wire import GpuConstraints, PriorityClass

__all__ = [
    "DEFAULT_TIMEOUT_S",
    "GpuCoordinator",
    "GpuLease",
    "GpuLeaseRefused",
    "GpuLeaseTimeout",
    "NullCoordinator",
]

DEFAULT_TIMEOUT_S = 300.0


class GpuLeaseTimeout(TimeoutError):
    """No GPU slot within the timeout: the lock stayed held, or the card never had room.

    A ``TimeoutError``, so a caller can catch either, and tell "no GPU slot" from
    a failed job and back off.
    """


class GpuLeaseRefused(RuntimeError):
    """The broker refused the lease outright: it can never fit, or its deadline passed.

    Unlike :class:`GpuLeaseTimeout`, retrying the same request will not help.
    """


@dataclass
class GpuLease:
    """What a held lease tells its body. The local coordinators fill in only ``via``.

    * ``via``: ``local`` (none/lease coordinator), ``broker``, or ``fallback`` (the
      broker could not be reached and another coordinator granted it).
    * ``node``/``device``: where the broker placed the work.
    * ``base_url``: for a service target (``ollama:<model>``, ``comfyui``), the URL
      to call on that node. ``None`` means "use your configured URL".
    * :meth:`should_yield`: the broker asked for the GPU back; release at the next
      safe point (between tiles, seeds, batches). Nothing is killed if you don't.
    * :meth:`report_peak_vram`: tell the broker the peak you saw, so it learns the
      real footprint.
    * :meth:`report_progress`: how far the work has got, for the broker's dashboard.
      Sent with the next heartbeat; the broker shows it and decides nothing on it.
    """

    via: Literal["local", "broker", "fallback"] = "local"
    lease_id: str | None = None
    node: str | None = None
    device: int | None = None
    base_url: str | None = None
    peak_vram_mb: int | None = None
    progress: float | None = None
    stage: str | None = None
    _yield: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)

    def should_yield(self) -> bool:
        return self._yield.is_set()

    def report_peak_vram(self, mb: int) -> None:
        if mb >= 0:
            self.peak_vram_mb = max(self.peak_vram_mb or 0, int(mb))

    def report_progress(self, done: float, total: float | None = None, *, stage: str | None = None) -> None:
        """Record how far the work has got: *done* of *total* steps, or *done* as a fraction (0-1) without *total*.

        *stage* says what it is doing now (``"checkpoint 3/5"``), up to 200 characters. The broker sees the latest
        values at the next heartbeat, so report as often as is convenient. A *total* of zero or less (nothing counted
        yet) reports 0, not *done* read as a fraction.
        """
        fraction = done if total is None else (done / total if total > 0 else 0.0)
        self.progress = min(1.0, max(0.0, float(fraction)))
        if stage is not None:
            self.stage = stage[:200]


@runtime_checkable
class GpuCoordinator(Protocol):
    """Gates a block of GPU work behind a lease."""

    name: str

    def lease(
        self,
        *,
        caller: str,
        min_vram_mb: int,
        priority: PriorityClass | None = None,
        target: str | None = None,
        kind: str | None = None,
        preemptible: bool | None = None,
        constraints: GpuConstraints | None = None,
        model_mb: int | None = None,
    ) -> contextlib.AbstractContextManager[GpuLease]:
        """A context manager that holds a GPU slot for its body."""
        ...

    def alease(
        self,
        *,
        caller: str,
        min_vram_mb: int,
        priority: PriorityClass | None = None,
        target: str | None = None,
        kind: str | None = None,
        preemptible: bool | None = None,
        constraints: GpuConstraints | None = None,
        model_mb: int | None = None,
    ) -> contextlib.AbstractAsyncContextManager[GpuLease]:
        """:meth:`lease` for async code: waiting never blocks the event loop."""
        ...


class NullCoordinator:
    """No gating: every lease is granted at once. The default."""

    name = "none"

    @contextlib.contextmanager
    def lease(self, *, caller: str, min_vram_mb: int, **_: object) -> Iterator[GpuLease]:
        yield GpuLease()

    @contextlib.asynccontextmanager
    async def alease(self, *, caller: str, min_vram_mb: int, **_: object) -> AsyncIterator[GpuLease]:
        yield GpuLease()
