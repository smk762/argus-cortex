"""GPU coordination: the file lease, the VRAM wait, and configuration from the environment."""

from __future__ import annotations

import builtins
import os
import threading
from pathlib import Path

import pytest

from argus_cortex import gpu


@pytest.fixture(autouse=True)
def _reset_nvml_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu, "_nvml_ready", None)


def _lease(tmp_path: Path, probe=lambda device: None, timeout_s: float = 5.0) -> gpu.LeaseCoordinator:  # noqa: ANN001
    return gpu.LeaseCoordinator(
        lock_path=str(tmp_path / "locks" / "gpu.lock"), timeout_s=timeout_s, poll_s=0.01, probe=probe
    )


def test_null_coordinator_grants_at_once() -> None:
    coordinator = gpu.NullCoordinator()
    assert isinstance(coordinator, gpu.GpuCoordinator)
    with coordinator.lease(caller="t", min_vram_mb=10**9):
        pass


def test_lock_file_is_world_writable_despite_umask(tmp_path: Path) -> None:
    """os.open's mode is umask-masked; without fchmod the next container UID cannot open the lock."""
    path = tmp_path / "locks" / "gpu.lock"
    previous = os.umask(0o077)
    try:
        with gpu.LeaseCoordinator(lock_path=str(path), timeout_s=1, probe=lambda device: None).lease(
            caller="a", min_vram_mb=0
        ):
            mode = path.stat().st_mode & 0o777
    finally:
        os.umask(previous)
    assert mode == 0o666


def test_lease_released_when_the_body_raises(tmp_path: Path) -> None:
    with (
        pytest.raises(RuntimeError, match="boom"),
        _lease(tmp_path).lease(caller="a", min_vram_mb=0),
    ):
        raise RuntimeError("boom")
    with _lease(tmp_path, timeout_s=0.05).lease(caller="b", min_vram_mb=0):
        pass


def test_lease_is_exclusive_and_released(tmp_path: Path) -> None:
    """A second holder waits for the first; a holder that never lets go times the other out."""
    holder, waiter = _lease(tmp_path), _lease(tmp_path, timeout_s=0.1)
    assert isinstance(holder, gpu.GpuCoordinator)
    with (
        holder.lease(caller="a", min_vram_mb=0),
        pytest.raises(gpu.GpuLeaseTimeout, match="not acquired"),
        waiter.lease(caller="b", min_vram_mb=0),
    ):
        pass
    with waiter.lease(caller="b", min_vram_mb=0):  # free again once the first exits
        pass


def test_lease_waits_for_the_holder_to_finish(tmp_path: Path) -> None:
    order: list[str] = []
    entered, release = threading.Event(), threading.Event()

    def first() -> None:
        with _lease(tmp_path).lease(caller="a", min_vram_mb=0):
            entered.set()
            release.wait(5)
            order.append("a done")

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(5)
    threading.Timer(0.05, release.set).start()
    with _lease(tmp_path).lease(caller="b", min_vram_mb=0):
        order.append("b in")
    thread.join(5)
    assert order == ["a done", "b in"]


def test_lease_waits_for_free_vram_while_holding_the_lock(tmp_path: Path) -> None:
    readings = iter([1000, 2000, 9000])
    seen: list[int] = []

    def probe(device: int) -> int | None:
        seen.append(device)
        return next(readings)

    coordinator = _lease(tmp_path, probe=probe)
    # The lock is held during and after the wait: another argus job cannot take the room.
    with (
        coordinator.lease(caller="faces", min_vram_mb=4000),
        pytest.raises(gpu.GpuLeaseTimeout),
        _lease(tmp_path, timeout_s=0.05).lease(caller="lens", min_vram_mb=0),
    ):
        pass
    assert seen == [0, 0, 0]


def test_lease_times_out_when_the_card_never_has_room_and_lets_go(tmp_path: Path) -> None:
    coordinator = _lease(tmp_path, probe=lambda device: 500, timeout_s=0.05)
    with (
        pytest.raises(gpu.GpuLeaseTimeout, match="500 MB free, faces needs 4000 MB"),
        coordinator.lease(caller="faces", min_vram_mb=4000),
    ):
        pass
    with _lease(tmp_path, timeout_s=0.05).lease(caller="next", min_vram_mb=0):  # the lock was released
        pass


def test_lease_proceeds_when_vram_cannot_be_read_or_is_not_needed(tmp_path: Path) -> None:
    calls: list[int] = []
    with _lease(tmp_path, probe=lambda device: calls.append(device)).lease(caller="cpu host", min_vram_mb=4000):
        pass
    assert calls == [0]  # asked once, got None, went ahead
    with _lease(tmp_path, probe=lambda device: pytest.fail("probed")).lease(caller="tiny", min_vram_mb=0):
        pass


def test_free_vram_reads_bytes_for_the_asked_device(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    fake = types.ModuleType("pynvml")
    seen: list[int] = []

    class _Info:
        free = 3 * 1024 * 1024 * 1024

    def handle(index: int) -> str:
        seen.append(index)
        return f"dev{index}"

    def memory_info(dev: str) -> _Info:
        assert dev == "dev2"
        return _Info()

    fake.nvmlInit = lambda: None  # type: ignore[attr-defined]
    fake.nvmlDeviceGetHandleByIndex = handle  # type: ignore[attr-defined]
    fake.nvmlDeviceGetMemoryInfo = memory_info  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    assert gpu.free_vram_mb(2) == 3072
    assert seen == [2]


def test_free_vram_is_none_without_nvml(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def no_pynvml(name: str, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        if name == "pynvml":
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pynvml)
    assert gpu.free_vram_mb() is None


def test_free_vram_is_none_when_nvml_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """No driver: NVML init fails once and is not retried on every probe."""
    import sys
    import types

    attempts: list[int] = []
    fake = types.ModuleType("pynvml")

    def init() -> None:
        attempts.append(1)
        raise RuntimeError("NVML Shared Library Not Found")

    fake.nvmlInit = init  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pynvml", fake)
    monkeypatch.setattr(gpu, "_nvml_ready", None)
    assert gpu.free_vram_mb() is None and gpu.free_vram_mb() is None
    assert attempts == [1]


def test_build_coordinator() -> None:
    assert isinstance(gpu.build_coordinator(""), gpu.NullCoordinator)
    assert isinstance(gpu.build_coordinator(" NONE "), gpu.NullCoordinator)
    lease = gpu.build_coordinator("lease", lock_path="/x/gpu.lock", timeout_s=7, device=1)
    assert isinstance(lease, gpu.LeaseCoordinator)
    assert (lease.lock_path, lease.timeout_s, lease.device) == ("/x/gpu.lock", 7, 1)
    assert gpu.build_coordinator("lease").lock_path == gpu.default_lock_path()
    with pytest.raises(ValueError, match="none, lease"):
        gpu.build_coordinator("gothmog")
    with pytest.raises(ValueError, match="finite"):
        gpu.LeaseCoordinator(timeout_s=float("inf"))
    with pytest.raises(ValueError, match="finite"):
        gpu.LeaseCoordinator(timeout_s=float("nan"))


def test_coordinator_from_env(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    for name in ("ARGUS_GPU_COORDINATOR", "ARGUS_GPU_LEASE_PATH", "ARGUS_GPU_LEASE_TIMEOUT_S", "ARGUS_GPU_DEVICE"):
        monkeypatch.delenv(name, raising=False)
    assert isinstance(gpu.coordinator_from_env(), gpu.NullCoordinator)
    monkeypatch.setenv("ARGUS_GPU_COORDINATOR", "lease")
    monkeypatch.setenv("ARGUS_GPU_LEASE_PATH", "/shared/argus-gpu.lock")
    monkeypatch.setenv("ARGUS_GPU_LEASE_TIMEOUT_S", "30")
    monkeypatch.setenv("ARGUS_GPU_DEVICE", "1")
    lease = gpu.coordinator_from_env()
    assert (lease.lock_path, lease.timeout_s, lease.device) == ("/shared/argus-gpu.lock", 30.0, 1)
    monkeypatch.setenv("ARGUS_GPU_LEASE_TIMEOUT_S", "soon")
    monkeypatch.setenv("ARGUS_GPU_DEVICE", "-1")
    with caplog.at_level("WARNING"):
        lease = gpu.coordinator_from_env()
    assert (lease.timeout_s, lease.device) == (gpu.DEFAULT_TIMEOUT_S, 0)
    assert "ARGUS_GPU_LEASE_TIMEOUT_S" in caplog.text and "ARGUS_GPU_DEVICE" in caplog.text
    caplog.clear()
    monkeypatch.setenv("ARGUS_GPU_LEASE_TIMEOUT_S", "inf")
    with caplog.at_level("WARNING"):
        assert gpu.coordinator_from_env().timeout_s == gpu.DEFAULT_TIMEOUT_S
    assert "ARGUS_GPU_LEASE_TIMEOUT_S" in caplog.text
    caplog.clear()
    monkeypatch.setenv("ARGUS_GPU_LEASE_TIMEOUT_S", "nan")
    with caplog.at_level("WARNING"):
        assert gpu.coordinator_from_env().timeout_s == gpu.DEFAULT_TIMEOUT_S
    assert "ARGUS_GPU_LEASE_TIMEOUT_S" in caplog.text
