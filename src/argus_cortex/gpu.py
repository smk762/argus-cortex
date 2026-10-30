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
* :func:`coordinator_from_env`: ``ARGUS_GPU_COORDINATOR`` (``none``/``lease``),
  ``ARGUS_GPU_LEASE_PATH``, ``ARGUS_GPU_LEASE_TIMEOUT_S``, ``ARGUS_GPU_DEVICE``.
  The names are host-wide, not per stage: every process must agree on them.
  One lock serialises every argus GPU job on the host; ``ARGUS_GPU_DEVICE``
  only selects which card NVML probes.

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

import contextlib
import logging
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from typing import Protocol, runtime_checkable

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 300.0
DEFAULT_POLL_S = 0.5
COORDINATORS = ("none", "lease")

_nvml_lock = threading.Lock()
_nvml_ready: bool | None = None  # None: not tried yet; False: NVML cannot run here


class GpuLeaseTimeout(TimeoutError):
    """No GPU slot within the timeout: the lock stayed held, or the card never had room.

    A ``TimeoutError``, so a caller can catch either, and tell "no GPU slot" from
    a failed job and back off.
    """


@runtime_checkable
class GpuCoordinator(Protocol):
    """Gates a block of GPU work behind a lease."""

    name: str

    def lease(self, *, caller: str, min_vram_mb: int) -> contextlib.AbstractContextManager[None]:
        """A context manager that holds a GPU slot for its body."""
        ...


class NullCoordinator:
    """No gating: every lease is granted at once. The default."""

    name = "none"

    @contextlib.contextmanager
    def lease(self, *, caller: str, min_vram_mb: int) -> Iterator[None]:
        yield


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

    @contextlib.contextmanager
    def lease(self, *, caller: str, min_vram_mb: int) -> Iterator[None]:
        try:
            import fcntl  # noqa: PLC0415 - POSIX only
        except ImportError as exc:  # pragma: no cover - non-POSIX host
            raise RuntimeError("the 'lease' GPU coordinator needs POSIX fcntl; use 'none'") from exc

        deadline = time.monotonic() + self.timeout_s
        directory = os.path.dirname(self.lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # 0o666: services in different containers may run as different users.
        # os.open's mode is masked by umask, so chmod the fd or the file is
        # 0644/0600 and the next UID cannot open it.
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o666)
        try:
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
                yield
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
) -> GpuCoordinator:
    """A coordinator by name, one of :data:`COORDINATORS` (empty is ``none``)."""
    key = (name or "none").strip().lower()
    if key == "none":
        return NullCoordinator()
    if key == "lease":
        return LeaseCoordinator(
            lock_path=lock_path,
            timeout_s=DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s,
            device=device,
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
    )
