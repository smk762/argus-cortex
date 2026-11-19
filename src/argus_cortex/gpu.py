"""GPU tenancy shared across the suite: one heavy argus job on a GPU at a time,
and only once the card has room for it.

Generalised from argus-lens's ``gpu`` coordinator so every stage that runs a
model on a GPU it shares (lens captioning, curator's faces, alongside ComfyUI
and an Ollama-backed SingGuard) takes the same lease.

* :class:`GpuCoordinator`: the protocol: ``lease(caller=, min_vram_mb=)`` is a
  context manager that holds a GPU slot for its body.
* :class:`NullCoordinator`: no gating, the default.
* :class:`LeaseCoordinator`: an advisory file lock (POSIX ``flock``) taken by
  every argus process on the host, then a wait until :func:`free_vram_mb` reports
  ``min_vram_mb`` free. The lock orders argus jobs among themselves; the VRAM wait
  is what accounts for tenants that never take the lock (ComfyUI, Ollama), since
  NVML reports free memory across every process on the card.
* :class:`~argus_cortex.gpu_broker.BrokerCoordinator`: asks the suite's GPU broker
  (argus-iris) over HTTP, which places the lease on a node, queues it by priority
  and evicts idle models to make room. Behind ``[remote]``; when the broker cannot
  be reached it falls back to another coordinator rather than failing.
* :func:`coordinator_from_env`: ``ARGUS_GPU_COORDINATOR`` (``none``/``lease``/``broker``),
  ``ARGUS_GPU_LEASE_PATH``, ``ARGUS_GPU_LEASE_TIMEOUT_S``, ``ARGUS_GPU_DEVICE``.
  The names are host-wide, not per stage: every process must agree on them.
  One lock serialises every argus GPU job on the host; ``ARGUS_GPU_DEVICE``
  only selects which card NVML probes. ``broker`` adds ``ARGUS_GPU_BROKER_URL``,
  ``ARGUS_GPU_BROKER_TOKEN``, ``ARGUS_GPU_TENANT``, ``ARGUS_GPU_TENANT_MANIFEST`` and
  ``ARGUS_GPU_BROKER_FALLBACK`` (``lease``, the default, or ``none``).

Every ``lease()`` yields a :class:`GpuLease`. Code that ignores it keeps working;
code that reads it learns where the broker placed the work (``node``,
``base_url``) and whether it should give the GPU back early (``should_yield()``).
The optional ``priority``/``target``/``kind``/``preemptible``/``constraints``/``model_mb``
arguments only mean something to the broker (``model_mb``: the part of
``min_vram_mb`` that is weights the service keeps loaded after the lease); the local coordinators ignore them.

The VRAM check is a snapshot, not a reservation: a tenant outside the lock can
still allocate between the check and the job. Each stage owns its footprint
estimates (``min_vram_mb``); a probe that cannot run (no driver, no NVML, a CPU
host) lets the lease through rather than blocking forever.

Containers have their own ``/tmp``: the lock only coordinates processes that see
the same file, so point ``ARGUS_GPU_LEASE_PATH`` at a volume every GPU service
mounts. ``flock`` works across containers on one host kernel. NVML is behind the
``[gpu]`` extra (``nvidia-ml-py``) and imported only when probed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import os
import stat
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from argus_cortex.gpu_wire import GpuConstraints, PriorityClass

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300.0
DEFAULT_POLL_S = 0.5
COORDINATORS = ("none", "lease", "broker")

_nvml_lock = threading.Lock()
_nvml_ready: bool | None = None  # None: not tried yet; False: NVML cannot run here


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


def _in_own_thread(
    fn: Callable[..., object], *args: object, on_orphan: Callable[[object], object] | None = None
) -> asyncio.Future:
    """Run blocking *fn* on a dedicated daemon thread and await it from the loop.

    Not ``asyncio.to_thread``: a lease can block for its whole timeout, and
    waiters parked on the loop's shared default executor would starve the very
    ``__exit__`` call the holder needs to release the lock (and every other
    ``to_thread`` user in the process).

    *on_orphan* gets a successful result nobody can receive (the future was
    cancelled, or the loop closed while *fn* ran), so a lease won too late is
    given back rather than left to garbage collection. It runs off the loop.
    """
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def _orphan(result: object) -> None:
        if on_orphan is None:
            return
        try:
            on_orphan(result)
        except Exception:  # noqa: BLE001 - nobody is left to raise to
            _log.warning("releasing an orphaned GPU lease failed", exc_info=True)

    def _settle(result: object, exc: BaseException | None) -> None:
        if fut.cancelled():
            if exc is None and on_orphan is not None:
                threading.Thread(target=_orphan, args=(result,), daemon=True, name="gpu-lease-orphan").start()
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(result)

    def _target() -> None:
        try:
            result, exc = fn(*args), None
        except BaseException as caught:  # noqa: BLE001 - handed to the awaiting task
            result, exc = None, caught
        try:
            loop.call_soon_threadsafe(_settle, result, exc)
        except RuntimeError:  # the loop closed while we waited: nobody will take the result
            if exc is None:
                _orphan(result)

    threading.Thread(target=_target, daemon=True, name="gpu-lease").start()
    return fut


@contextlib.asynccontextmanager
async def _sync_lease_in_thread(cm: contextlib.AbstractContextManager[GpuLease]) -> AsyncIterator[GpuLease]:
    """Hold a blocking lease from async code: enter and exit it on a dedicated thread.

    If the task is cancelled while the thread is still waiting for the lease, the
    thread is left to finish and the lease it gets is released at once, so a
    cancelled caller never strands the lock. That holds when the loop itself has
    closed by then too (``asyncio.run`` around a timed-out wait): the thread
    releases it.
    """
    entering = _in_own_thread(cm.__enter__, on_orphan=lambda _handle: cm.__exit__(None, None, None))
    try:
        handle = await asyncio.shield(entering)
    except asyncio.CancelledError:

        def _undo(fut: asyncio.Future[GpuLease]) -> None:
            if not fut.cancelled() and fut.exception() is None:
                threading.Thread(target=cm.__exit__, args=(None, None, None), daemon=True).start()

        entering.add_done_callback(_undo)
        raise
    try:
        yield handle
    except BaseException as exc:
        if not await _in_own_thread(cm.__exit__, type(exc), exc, exc.__traceback__):
            raise
    else:
        await _in_own_thread(cm.__exit__, None, None, None)


class NullCoordinator:
    """No gating: every lease is granted at once. The default."""

    name = "none"

    @contextlib.contextmanager
    def lease(self, *, caller: str, min_vram_mb: int, **_: object) -> Iterator[GpuLease]:
        yield GpuLease()

    @contextlib.asynccontextmanager
    async def alease(self, *, caller: str, min_vram_mb: int, **_: object) -> AsyncIterator[GpuLease]:
        yield GpuLease()


def free_vram_mb(device: int = 0) -> int | None:
    """Free VRAM on NVML device *device*, in MB, across every process on it.

    ``None`` when it cannot be read: no ``[gpu]`` extra, no driver, no such
    device. NVML numbers devices as ``nvidia-smi`` does, which inside a container
    means the GPUs the container was given.
    """
    global _nvml_ready
    try:
        import pynvml  # noqa: PLC0415 - optional, behind the [gpu] extra
    except ImportError:
        return None
    with _nvml_lock:
        if _nvml_ready is None:
            try:
                pynvml.nvmlInit()
                _nvml_ready = True
            except Exception:  # noqa: BLE001 - any NVML failure means "cannot probe"
                _nvml_ready = False
        if not _nvml_ready:
            return None
    try:
        info = pynvml.nvmlDeviceGetMemoryInfo(pynvml.nvmlDeviceGetHandleByIndex(device))
    except Exception:  # noqa: BLE001
        return None
    return max(0, int(info.free) // (1024 * 1024))


def default_lock_path() -> str:
    """``<tempdir>/argus-gpu.lock``: shared only by processes that share that directory."""
    return os.path.join(tempfile.gettempdir(), "argus-gpu.lock")


class LeaseCoordinator:
    """One argus GPU job at a time on this host, started only when the card has room.

    Takes an exclusive ``flock`` on *lock_path*, then (when *min_vram_mb* > 0)
    waits until *probe* reports that much free, polling every *poll_s*. The lock
    is held through the wait, so the next argus job cannot take the room first.
    Both waits share one *timeout_s* deadline; past it :class:`GpuLeaseTimeout`
    is raised and the lock released. A probe answering ``None`` grants the lease.
    """

    name = "lease"

    def __init__(
        self,
        lock_path: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_s: float = DEFAULT_POLL_S,
        device: int = 0,
        probe: Callable[[int], int | None] = free_vram_mb,
    ) -> None:
        if not math.isfinite(timeout_s):
            raise ValueError("timeout_s must be a finite number")
        self.lock_path = lock_path or default_lock_path()
        self.timeout_s = max(0.0, timeout_s)
        self.poll_s = poll_s
        self.device = device
        self.probe = probe

    def alease(self, *, caller: str, min_vram_mb: int, **_: object) -> contextlib.AbstractAsyncContextManager[GpuLease]:
        return _sync_lease_in_thread(self.lease(caller=caller, min_vram_mb=min_vram_mb))

    @contextlib.contextmanager
    def lease(self, *, caller: str, min_vram_mb: int, **_: object) -> Iterator[GpuLease]:
        try:
            import fcntl  # noqa: PLC0415 - POSIX only
        except ImportError as exc:  # pragma: no cover - non-POSIX host
            raise RuntimeError("the 'lease' GPU coordinator needs POSIX fcntl; use 'none'") from exc

        deadline = time.monotonic() + self.timeout_s
        directory = os.path.dirname(self.lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # The lock lives in a directory several UIDs can write to, so never
        # follow a symlink planted at the lock path (the fchmod below would make
        # its target world-writable), and only touch a regular file we own.
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        try:
            fd = os.open(self.lock_path, flags, 0o666)
        except OSError as exc:
            if os.path.islink(self.lock_path):
                raise RuntimeError(f"GPU lock path {self.lock_path} is a symlink; refusing to follow it") from exc
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise RuntimeError(f"GPU lock path {self.lock_path} is not a regular file")
            # 0o666: services in different containers may run as different users.
            # os.open's mode is masked by umask, so chmod the fd or the file is
            # 0644/0600 and the next UID cannot open it. Only the owner may chmod.
            if st.st_uid == os.geteuid():
                with contextlib.suppress(OSError):
                    os.fchmod(fd, 0o666)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GpuLeaseTimeout(
                            f"GPU lease not acquired within {self.timeout_s:g}s ({self.lock_path})"
                        ) from None
                    time.sleep(self.poll_s)
            try:
                self._wait_for_room(caller, min_vram_mb, deadline)
                _log.debug("gpu lease acquired by %s (min %d MB, %s)", caller, min_vram_mb, self.lock_path)
                yield GpuLease()
            finally:
                with contextlib.suppress(OSError):
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _wait_for_room(self, caller: str, min_vram_mb: int, deadline: float) -> None:
        if min_vram_mb <= 0:
            return
        while True:
            free = self.probe(self.device)
            if free is None or free >= min_vram_mb:
                return
            if time.monotonic() >= deadline:
                raise GpuLeaseTimeout(
                    f"GPU {self.device} had {free} MB free, {caller} needs {min_vram_mb} MB, after {self.timeout_s:g}s"
                )
            time.sleep(self.poll_s)


def build_coordinator(
    name: str,
    *,
    lock_path: str | None = None,
    timeout_s: float | None = None,
    device: int = 0,
    broker_url: str | None = None,
    broker_token: str | None = None,
    tenant: str | None = None,
    manifest_path: str | None = None,
    fallback: str = "lease",
) -> GpuCoordinator:
    """A coordinator by name, one of :data:`COORDINATORS` (empty is ``none``).

    ``broker`` needs *broker_url*; without one it logs a warning and returns the
    *fallback* coordinator (``lease`` or ``none``), which is also what the broker
    coordinator falls back to whenever the broker cannot be reached.
    """
    key = (name or "none").strip().lower()
    if key == "none":
        return NullCoordinator()
    if key == "lease":
        return LeaseCoordinator(
            lock_path=lock_path,
            timeout_s=DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s,
            device=device,
        )
    if key == "broker":
        fallback_key = (fallback or "lease").strip().lower()
        if fallback_key not in ("none", "lease"):
            _log.warning("GPU broker fallback %r is not 'lease' or 'none'; using 'lease'", fallback)
            fallback_key = "lease"
        local = build_coordinator(fallback_key, lock_path=lock_path, timeout_s=timeout_s, device=device)
        if not broker_url:
            _log.warning("ARGUS_GPU_COORDINATOR=broker but no broker URL is set; using the %r coordinator", local.name)
            return local
        from argus_cortex.gpu_broker import BrokerCoordinator  # noqa: PLC0415 - keeps the client out of the import
        from argus_cortex.gpu_wire import load_tenant_manifest  # noqa: PLC0415

        manifest = None
        if manifest_path:
            try:
                manifest = load_tenant_manifest(manifest_path)
            except Exception as exc:  # noqa: BLE001 - a bad manifest must not stop the service
                _log.warning("GPU tenant manifest %s not loaded: %s", manifest_path, exc)
        resolved_tenant = tenant or (manifest.tenant if manifest else None)
        if not resolved_tenant:
            _log.warning("no GPU tenant name (ARGUS_GPU_TENANT or a manifest); the broker will see 'unknown'")
            resolved_tenant = "unknown"
        return BrokerCoordinator(
            broker_url,
            tenant=resolved_tenant,
            token=broker_token,
            manifest=manifest,
            fallback=local,
            timeout_s=DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s,
        )
    raise ValueError(f"unknown GPU coordinator {name!r}; choose from: {', '.join(COORDINATORS)}")


def _env_number(name: str, kind: type[float] | type[int]) -> float | int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = kind(raw)
    except (ValueError, OverflowError):
        value = -1
    # inf/nan parse as float and never compare < 0, so the deadline never fires.
    if isinstance(value, float) and not math.isfinite(value):
        value = -1
    if value < 0:
        _log.warning("%s set to an unusable value %r; using the default", name, raw)
        return None
    return value


def coordinator_from_env() -> GpuCoordinator:
    """The coordinator the environment names; ``none`` when ``ARGUS_GPU_COORDINATOR`` is unset."""
    device = _env_number("ARGUS_GPU_DEVICE", int)
    return build_coordinator(
        os.environ.get("ARGUS_GPU_COORDINATOR", "none"),
        lock_path=os.environ.get("ARGUS_GPU_LEASE_PATH") or None,
        timeout_s=_env_number("ARGUS_GPU_LEASE_TIMEOUT_S", float),
        device=0 if device is None else int(device),
        broker_url=os.environ.get("ARGUS_GPU_BROKER_URL", "").strip() or None,
        broker_token=os.environ.get("ARGUS_GPU_BROKER_TOKEN", "").strip() or None,
        tenant=os.environ.get("ARGUS_GPU_TENANT", "").strip() or None,
        manifest_path=os.environ.get("ARGUS_GPU_TENANT_MANIFEST", "").strip() or None,
        fallback=os.environ.get("ARGUS_GPU_BROKER_FALLBACK", "lease"),
    )
