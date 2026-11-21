"""BrokerCoordinator against a fake broker: grant, queue, refuse, timeout, fall back, heartbeat, release."""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from argus_cortex import gpu, gpu_broker
from argus_cortex.gpu import GpuLease, GpuLeaseRefused, GpuLeaseTimeout, LeaseCoordinator, NullCoordinator
from argus_cortex.gpu_broker import BrokerCoordinator, BrokerError
from argus_cortex.gpu_wire import TenantManifest


@dataclass
class FakeBroker:
    """A scripted broker. ``polls_before_grant`` GETs answer ``queued`` before the lease is granted."""

    polls_before_grant: int = 0
    refuse: str | None = None
    down: bool = False
    status_code: int | None = None
    ttl_s: float = 60.0
    yield_after_beats: int | None = None
    forget_on_beat: bool = False
    node: str = "dragon"
    base_url: str | None = "http://dragon:11434"
    requests: list[dict] = field(default_factory=list)
    tenants: list[dict] = field(default_factory=list)
    beats: list[dict] = field(default_factory=list)
    releases: dict[str, dict] = field(default_factory=dict)
    _ids: Any = field(default_factory=lambda: itertools.count(1))
    _polls: dict[str, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _status(self, lease_id: str, state: str, **extra: Any) -> dict:
        body = {"lease_version": "1.0", "lease_id": lease_id, "state": state, "ttl_s": self.ttl_s, **extra}
        if state == "granted":
            body.update(node=self.node, device=0, base_url=self.base_url, vram_mb=self.requests[-1]["vram_mb"])
        return body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            return self._handle(request)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.status_code is not None:
            return httpx.Response(self.status_code, text="nope")
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if request.method == "POST" and path == "/tenants":
            self.tenants.append(body)
            return httpx.Response(200, json={"ok": True})
        if request.method == "POST" and path == "/leases":
            self.requests.append(body)
            lease_id = body.get("resume") or f"L{next(self._ids)}"
            if self.refuse:
                return httpx.Response(200, json=self._status(lease_id, "refused", reason=self.refuse))
            if body.get("resume") or self.polls_before_grant == 0:
                return httpx.Response(200, json=self._status(lease_id, "granted"))
            self._polls[lease_id] = self.polls_before_grant
            return httpx.Response(200, json=self._status(lease_id, "queued", position=1, reason="waiting for VRAM"))
        lease_id = path.split("/")[2]
        if request.method == "GET":
            if lease_id not in self._polls:
                return httpx.Response(404)
            self._polls[lease_id] -= 1
            if self._polls[lease_id] <= 0:
                return httpx.Response(200, json=self._status(lease_id, "granted"))
            return httpx.Response(200, json=self._status(lease_id, "queued", position=1, reason="waiting for VRAM"))
        if path.endswith("/heartbeat"):
            self.beats.append(body)
            if self.forget_on_beat:
                self.forget_on_beat = False
                return httpx.Response(404)
            wants_yield = self.yield_after_beats is not None and len(self.beats) >= self.yield_after_beats
            reply = {"lease_version": "1.0", "state": "granted", "ttl_s": self.ttl_s, "yield_requested": wants_yield}
            return httpx.Response(200, json=reply)
        if path.endswith("/release"):
            self.releases[lease_id] = body
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)


MANIFEST = TenantManifest.model_validate(
    {"tenant": "argus-darkroom", "kinds": [{"kind": "expand", "target": "ollama:qwen3:8b", "vram_mb": 6000}]}
)


def _coordinator(broker: FakeBroker, **kwargs: Any) -> BrokerCoordinator:
    transport = httpx.MockTransport(broker)
    options = {
        "tenant": "argus-darkroom",
        "timeout_s": 5.0,
        "poll_wait_s": 0.01,
        "min_poll_s": 0.005,
        "fallback": NullCoordinator(),
    }
    options.update(kwargs)
    return BrokerCoordinator("http://iris:8120/", transport=transport, async_transport=transport, **options)


def test_is_a_gpu_coordinator() -> None:
    assert isinstance(_coordinator(FakeBroker()), gpu.GpuCoordinator)


def test_bad_tenant_slug_fails_at_construction() -> None:
    with pytest.raises(ValueError):
        BrokerCoordinator("http://iris:8120", tenant="Argus Darkroom")


def test_granted_lease_reports_placement_and_releases_ok() -> None:
    broker = FakeBroker()
    coordinator = _coordinator(broker, manifest=MANIFEST)
    with coordinator.lease(caller="expand", min_vram_mb=6000, target="ollama:qwen3:8b", kind="expand") as handle:
        assert (handle.via, handle.node, handle.base_url) == ("broker", "dragon", "http://dragon:11434")
        handle.report_peak_vram(5400)
    request = broker.requests[0]
    assert request["tenant"] == "argus-darkroom" and request["target"] == "ollama:qwen3:8b"
    assert request["vram_mb"] == 6000 and request["kind"] == "expand" and request["priority"] is None
    assert broker.releases[handle.lease_id] == {
        "lease_version": "1.1",
        "outcome": "ok",
        "peak_vram_mb": 5400,
        "detail": None,
    }


def test_request_carries_model_mb_and_a_fresh_request_id() -> None:
    broker = FakeBroker()
    coordinator = _coordinator(broker)
    for _ in range(2):
        with coordinator.lease(caller="gen", min_vram_mb=9000, model_mb=6500, target="comfyui"):
            pass
    first, second = broker.requests
    assert (first["vram_mb"], first["model_mb"]) == (9000, 6500)
    assert first["request_id"] and second["request_id"] and first["request_id"] != second["request_id"]


def test_model_mb_is_clamped_to_the_total() -> None:
    broker = FakeBroker()
    with _coordinator(broker).lease(caller="gen", min_vram_mb=1000, model_mb=5000):
        pass
    assert broker.requests[0]["model_mb"] == 1000


def test_lost_post_response_is_retried_once_with_the_same_request_id() -> None:
    broker = FakeBroker()
    original = broker._handle
    timed_out = []

    def lose_first_response(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/leases" and not timed_out:
            timed_out.append(True)
            raise httpx.ReadTimeout("response lost", request=request)  # the broker made the lease; we never heard
        return response

    broker._handle = lose_first_response  # type: ignore[method-assign]
    with _coordinator(broker).lease(caller="x", min_vram_mb=1) as handle:
        assert handle.via == "broker"
    assert len(broker.requests) == 2
    assert broker.requests[0]["request_id"] == broker.requests[1]["request_id"]


def test_refused_connection_is_not_retried() -> None:
    broker = FakeBroker(down=True)
    calls = []
    original = broker.__call__

    def counting(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return original(request)

    coordinator = BrokerCoordinator(
        "http://iris:8120", tenant="t", transport=httpx.MockTransport(counting), fallback=NullCoordinator()
    )
    with coordinator.lease(caller="x", min_vram_mb=1) as handle:
        assert handle.via == "fallback"
    assert calls == ["/leases"]


async def test_async_lost_post_response_is_retried_once() -> None:
    broker = FakeBroker()
    original = broker._handle
    timed_out = []

    def lose_first_response(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/leases" and not timed_out:
            timed_out.append(True)
            raise httpx.ReadTimeout("response lost", request=request)
        return response

    broker._handle = lose_first_response  # type: ignore[method-assign]
    async with _coordinator(broker).alease(caller="x", min_vram_mb=1) as handle:
        assert handle.via == "broker"
    assert broker.requests[0]["request_id"] == broker.requests[1]["request_id"]


def test_manifest_registered_once() -> None:
    broker = FakeBroker()
    coordinator = _coordinator(broker, manifest=MANIFEST)
    for _ in range(2):
        with coordinator.lease(caller="x", min_vram_mb=0):
            pass
    assert [t["tenant"] for t in broker.tenants] == ["argus-darkroom"]


def test_manifest_not_registered_on_every_lease_within_the_interval() -> None:
    broker = FakeBroker()
    coordinator = _coordinator(broker, manifest=MANIFEST)
    for _ in range(20):  # a caller taking a lease per request
        with coordinator.lease(caller="x", min_vram_mb=0):
            pass
    assert len(broker.tenants) == 1


def _age_registration(coordinator: BrokerCoordinator) -> None:
    assert coordinator._registered_at is not None
    coordinator._registered_at -= gpu_broker.REREGISTER_S + 1


def test_manifest_registered_again_after_the_interval() -> None:
    broker = FakeBroker()
    coordinator = _coordinator(broker, manifest=MANIFEST)
    with coordinator.lease(caller="x", min_vram_mb=0):
        pass
    _age_registration(coordinator)
    for _ in range(2):
        with coordinator.lease(caller="x", min_vram_mb=0):
            pass
    assert len(broker.tenants) == 2


async def test_async_manifest_registered_again_after_the_interval() -> None:
    broker = FakeBroker()
    coordinator = _coordinator(broker, manifest=MANIFEST)
    async with coordinator.alease(caller="x", min_vram_mb=0):
        pass
    async with coordinator.alease(caller="x", min_vram_mb=0):
        pass
    assert len(broker.tenants) == 1
    _age_registration(coordinator)
    async with coordinator.alease(caller="x", min_vram_mb=0):
        pass
    assert len(broker.tenants) == 2


def test_manifest_registered_again_after_the_broker_was_unreachable() -> None:
    broker = FakeBroker()
    coordinator = _coordinator(broker, manifest=MANIFEST)
    with coordinator.lease(caller="x", min_vram_mb=0):
        pass
    broker.down = True  # the broker restarts...
    with coordinator.lease(caller="x", min_vram_mb=0) as handle:
        assert handle.via == "fallback"
    broker.down = False  # ...and comes back with no tenants
    for _ in range(2):
        with coordinator.lease(caller="x", min_vram_mb=0) as handle:
            assert handle.via == "broker"
    assert len(broker.tenants) == 2


async def test_async_manifest_registered_again_after_the_broker_was_unreachable() -> None:
    broker = FakeBroker()
    coordinator = _coordinator(broker, manifest=MANIFEST)
    async with coordinator.alease(caller="x", min_vram_mb=0):
        pass
    broker.status_code = 503
    async with coordinator.alease(caller="x", min_vram_mb=0) as handle:
        assert handle.via == "fallback"
    broker.status_code = None
    async with coordinator.alease(caller="x", min_vram_mb=0) as handle:
        assert handle.via == "broker"
    assert len(broker.tenants) == 2


def test_manifest_registered_again_when_a_heartbeat_finds_the_lease_forgotten() -> None:
    broker = FakeBroker(ttl_s=0.06)
    coordinator = _coordinator(broker, manifest=MANIFEST, ttl_s=0.06)
    with coordinator.lease(caller="x", min_vram_mb=1):
        assert _wait_for(lambda: len(broker.beats) >= 1)
        broker.forget_on_beat = True  # the broker restarted while the lease was held
        assert _wait_for(lambda: len(broker.requests) == 2)
    assert len(broker.tenants) == 2
    assert broker.requests[1]["resume"] == "L1"
    with coordinator.lease(caller="x", min_vram_mb=1):
        pass
    assert len(broker.tenants) == 2  # the beat re-registered; the next lease within the interval doesn't


async def test_async_manifest_registered_again_when_a_heartbeat_finds_the_lease_forgotten() -> None:
    broker = FakeBroker(ttl_s=0.06, forget_on_beat=True)
    coordinator = _coordinator(broker, manifest=MANIFEST, ttl_s=0.06)
    async with coordinator.alease(caller="x", min_vram_mb=1):
        for _ in range(200):
            if len(broker.requests) == 2:
                break
            await asyncio.sleep(0.01)
    assert broker.requests[1]["resume"] == "L1"
    assert len(broker.tenants) == 2


def test_refused_manifest_is_retried_after_the_interval_not_on_every_lease(caplog: pytest.LogCaptureFixture) -> None:
    broker = FakeBroker()
    original = broker._handle

    def refuse_tenants(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/tenants":
            broker.tenants.append({})
            return httpx.Response(422, text="bad manifest")
        return original(request)

    broker._handle = refuse_tenants  # type: ignore[method-assign]
    coordinator = _coordinator(broker, manifest=MANIFEST)
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            with coordinator.lease(caller="x", min_vram_mb=0) as handle:
                assert handle.via == "broker"
    assert len(broker.tenants) == 1 and "refused tenant manifest" in caplog.text
    _age_registration(coordinator)
    with coordinator.lease(caller="x", min_vram_mb=0):
        pass
    assert len(broker.tenants) == 2


def test_queued_lease_is_polled_until_granted() -> None:
    broker = FakeBroker(polls_before_grant=3)
    with _coordinator(broker).lease(caller="x", min_vram_mb=100) as handle:
        assert handle.via == "broker"
    assert len(broker.requests) == 1


def test_a_broker_that_ignores_wait_is_not_hammered() -> None:
    broker = FakeBroker(polls_before_grant=10**6)
    with (
        pytest.raises(GpuLeaseTimeout),
        _coordinator(broker, timeout_s=0.2, min_poll_s=0.05).lease(caller="x", min_vram_mb=1),
    ):
        pass
    assert broker._polls["L1"] > 10**6 - 8  # about 4 polls in 0.2 s, not thousands


def test_queued_lease_lost_by_a_restarted_broker_is_requested_again() -> None:
    broker = FakeBroker(polls_before_grant=2)
    coordinator = _coordinator(broker)
    original = broker._handle

    def forget_first_poll(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and len(broker.requests) == 1:
            broker._polls.clear()
            broker.polls_before_grant = 0
        return original(request)

    broker._handle = forget_first_poll  # type: ignore[method-assign]
    with coordinator.lease(caller="x", min_vram_mb=100) as handle:
        assert handle.via == "broker"
    assert len(broker.requests) == 2


def test_timeout_while_queued_cancels_the_request() -> None:
    broker = FakeBroker(polls_before_grant=10**6)
    with (
        pytest.raises(GpuLeaseTimeout, match="1 ahead"),
        _coordinator(broker, timeout_s=0.05).lease(caller="x", min_vram_mb=100),
    ):
        pass
    assert broker.releases["L1"]["outcome"] == "cancelled"


class _FailPolls(FakeBroker):
    """Queues the lease, then answers every status poll with a 503."""

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(503, text="busy")
        return super()._handle(request)


def test_broker_failing_mid_poll_gives_up_the_queued_request_before_falling_back() -> None:
    broker = _FailPolls(polls_before_grant=10**6)
    with _coordinator(broker).lease(caller="x", min_vram_mb=100) as handle:
        assert handle.via == "fallback"
    assert broker.releases["L1"]["outcome"] == "cancelled"


async def test_async_broker_failing_mid_poll_gives_up_the_queued_request() -> None:
    broker = _FailPolls(polls_before_grant=10**6)
    async with _coordinator(broker).alease(caller="x", min_vram_mb=100) as handle:
        assert handle.via == "fallback"
    assert broker.releases["L1"]["outcome"] == "cancelled"


def test_refusal_raises_and_is_not_a_timeout() -> None:
    broker = FakeBroker(refuse="needs 30000 MB; the largest node has 24576")
    with (
        pytest.raises(GpuLeaseRefused, match="largest node"),
        _coordinator(broker).lease(caller="x", min_vram_mb=30000),
    ):
        pass
    assert not issubclass(GpuLeaseRefused, GpuLeaseTimeout)


@pytest.mark.parametrize("broker", [FakeBroker(down=True), FakeBroker(status_code=503)], ids=["down", "5xx"])
def test_unreachable_broker_falls_back(broker: FakeBroker, caplog: pytest.LogCaptureFixture) -> None:
    coordinator = _coordinator(broker)
    with caplog.at_level(logging.WARNING), coordinator.lease(caller="x", min_vram_mb=100, target="comfyui") as handle:
        assert handle.via == "fallback" and handle.base_url is None
    assert "unreachable" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING), coordinator.lease(caller="x", min_vram_mb=100):
        pass
    assert "unreachable" not in caplog.text  # warned once, not on every lease


def test_falls_back_to_the_file_lease(tmp_path: Path) -> None:
    fallback = LeaseCoordinator(lock_path=str(tmp_path / "gpu.lock"), timeout_s=1, probe=lambda device: None)
    with _coordinator(FakeBroker(down=True), fallback=fallback).lease(caller="x", min_vram_mb=1) as handle:
        assert handle.via == "fallback"
        assert (tmp_path / "gpu.lock").exists()


def test_client_error_is_raised_not_hidden() -> None:
    with (
        pytest.raises(BrokerError, match="400"),
        _coordinator(FakeBroker(status_code=400)).lease(caller="x", min_vram_mb=1),
    ):
        pass


def test_incompatible_major_is_a_broker_error() -> None:
    broker = FakeBroker()
    original = broker._handle

    def from_the_future(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.url.path == "/leases":
            return httpx.Response(200, json={**response.json(), "lease_version": "2.0"})
        return response

    broker._handle = from_the_future  # type: ignore[method-assign]
    with pytest.raises(BrokerError, match="not supported"), _coordinator(broker).lease(caller="x", min_vram_mb=1):
        pass


@pytest.mark.parametrize(
    ("exc", "outcome"),
    [
        (RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB"), "oom"),
        (ValueError("bad prompt"), "failed"),
        (KeyboardInterrupt(), "cancelled"),
    ],
)
def test_release_outcome_follows_the_body(exc: BaseException, outcome: str) -> None:
    broker = FakeBroker()
    with pytest.raises(type(exc)), _coordinator(broker).lease(caller="x", min_vram_mb=1) as handle:
        raise exc
    assert broker.releases[handle.lease_id]["outcome"] == outcome


def _wait_for(predicate: Any, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def test_heartbeat_carries_peak_and_sets_should_yield() -> None:
    broker = FakeBroker(ttl_s=0.06, yield_after_beats=2)
    with _coordinator(broker, ttl_s=0.06).lease(caller="atlas tile", min_vram_mb=1, preemptible=True) as handle:
        handle.report_peak_vram(700)
        assert _wait_for(handle.should_yield)
    assert broker.beats[0]["peak_vram_mb"] == 700


def test_heartbeat_carries_progress_and_stage() -> None:
    broker = FakeBroker(ttl_s=0.06)
    with _coordinator(broker, ttl_s=0.06).lease(caller="proof", min_vram_mb=1) as handle:
        handle.report_progress(3, 5, stage="checkpoint 3/5")
        assert _wait_for(lambda: any(b.get("progress") for b in broker.beats))
    beat = next(b for b in broker.beats if b.get("progress"))
    assert beat["progress"] == pytest.approx(0.6) and beat["stage"] == "checkpoint 3/5"


def test_report_progress_takes_a_fraction_and_clamps() -> None:
    handle = gpu.GpuLease()
    handle.report_progress(0.25)
    assert handle.progress == 0.25 and handle.stage is None
    handle.report_progress(7, 5, stage="x" * 300)
    assert handle.progress == 1.0 and handle.stage == "x" * 200


def test_report_progress_with_no_total_yet_is_zero() -> None:
    handle = gpu.GpuLease()
    handle.report_progress(3, 0)
    assert handle.progress == 0.0


def test_progress_the_wire_refuses_does_not_stop_the_heartbeat() -> None:
    """``handle.progress = 50`` (a percentage) fails validation: the beat still goes, without it."""
    broker = FakeBroker(ttl_s=0.06)
    with _coordinator(broker, ttl_s=0.06).lease(caller="x", min_vram_mb=1) as handle:
        handle.progress = 50.0
        handle.report_peak_vram(700)
        assert _wait_for(lambda: len(broker.beats) >= 1)
    assert broker.beats[0]["peak_vram_mb"] == 700 and broker.beats[0]["progress"] is None


def test_heartbeat_reattaches_a_forgotten_lease() -> None:
    broker = FakeBroker(ttl_s=0.06, forget_on_beat=True)
    with _coordinator(broker, ttl_s=0.06).lease(caller="x", min_vram_mb=1) as handle:
        assert _wait_for(lambda: len(broker.requests) == 2)
        assert _wait_for(lambda: len(broker.beats) >= 2)
    assert broker.requests[1]["resume"] == "L1"
    assert handle.lease_id == "L1" and "L1" in broker.releases


@dataclass
class SlowReattach(FakeBroker):
    """Forgets the lease on the first heartbeat, then answers the re-attach with a *new* id, late."""

    forget_on_beat: bool = True
    ttl_s: float = 0.06
    reattaching: threading.Event = field(default_factory=threading.Event)
    answer: threading.Event = field(default_factory=threading.Event)

    def is_reattach(self, request: httpx.Request) -> bool:
        return request.url.path == "/leases" and bool(json.loads(request.content).get("resume"))

    def reattached(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=self._status("L2", "granted"))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.is_reattach(request):
            self.reattaching.set()
            self.answer.wait(5)
            return self.reattached(request)
        return super().__call__(request)


def test_a_reattach_that_lands_after_the_release_is_released_too() -> None:
    broker = SlowReattach()
    coordinator = _coordinator(broker, ttl_s=0.06, request_timeout_s=0.05)
    with coordinator.lease(caller="x", min_vram_mb=1):
        assert broker.reattaching.wait(5)
    # The body is over and the release did not wait for the re-attach: it released the id it knew.
    assert set(broker.releases) == {"L1"}
    broker.answer.set()  # now the broker grants the re-attached lease, to a job that has finished
    assert _wait_for(lambda: "L2" in broker.releases)  # the heartbeat releases it, not the TTL
    assert broker.releases["L2"]["outcome"] == "ok"


async def test_async_reattach_cut_short_by_the_end_of_the_lease_is_released() -> None:
    broker = SlowReattach()

    async def handler(request: httpx.Request) -> httpx.Response:
        if broker.is_reattach(request):
            broker.reattaching.set()
            await asyncio.sleep(0.05)  # the lease ends, and the heartbeat is cancelled, in here
            return broker.reattached(request)
        return FakeBroker.__call__(broker, request)

    coordinator = BrokerCoordinator(
        "http://iris:8120/", tenant="argus-darkroom", ttl_s=0.06, async_transport=httpx.MockTransport(handler)
    )
    async with coordinator.alease(caller="x", min_vram_mb=1) as handle:
        while not broker.reattaching.is_set():
            await asyncio.sleep(0.005)
    assert handle.lease_id == "L2"  # the broker made it, so the client learned it
    assert "L2" in broker.releases and broker.releases["L2"]["outcome"] == "ok"


def test_heartbeat_failure_does_not_stop_the_body() -> None:
    broker = FakeBroker(ttl_s=0.06)
    with _coordinator(broker, ttl_s=0.06).lease(caller="x", min_vram_mb=1) as handle:
        broker.down = True
        time.sleep(0.1)
        broker.down = False
    assert handle.lease_id in broker.releases


# --- async ---------------------------------------------------------------------------------------------------------


async def test_async_granted_and_released() -> None:
    broker = FakeBroker(polls_before_grant=2)
    async with _coordinator(broker, manifest=MANIFEST).alease(caller="x", min_vram_mb=6000, target="comfyui") as handle:
        assert handle.via == "broker" and handle.node == "dragon"
    assert broker.releases[handle.lease_id]["outcome"] == "ok"
    assert broker.tenants


async def test_async_falls_back_when_down() -> None:
    async with _coordinator(FakeBroker(down=True)).alease(caller="x", min_vram_mb=1) as handle:
        assert handle.via == "fallback"


async def test_async_cancel_while_queued_gives_the_place_up() -> None:
    broker = FakeBroker(polls_before_grant=10**6)
    coordinator = _coordinator(broker, timeout_s=30)

    async def take() -> None:
        async with coordinator.alease(caller="x", min_vram_mb=1):
            pass

    task = asyncio.create_task(take())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.releases["L1"]["outcome"] == "cancelled"


async def test_async_heartbeat_sets_should_yield() -> None:
    broker = FakeBroker(ttl_s=0.06, yield_after_beats=1)
    async with _coordinator(broker, ttl_s=0.06).alease(caller="x", min_vram_mb=1) as handle:
        for _ in range(200):
            if handle.should_yield():
                break
            await asyncio.sleep(0.005)
        assert handle.should_yield()


async def test_async_heartbeat_error_neither_skips_the_release_nor_replaces_the_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error the heartbeat doesn't expect (not a transport error) is logged; beats go on and the lease is released."""
    broker = FakeBroker(ttl_s=0.03)
    coordinator = _coordinator(broker, ttl_s=0.03)
    beats: list[int] = []

    async def garbled(*_: Any) -> None:
        beats.append(1)
        raise httpx.DecodingError("garbled heartbeat reply")

    monkeypatch.setattr(coordinator, "_abeat", garbled)
    async with coordinator.alease(caller="x", min_vram_mb=1) as handle:
        await asyncio.sleep(0.1)
    assert len(beats) >= 2  # the first error did not end the heartbeat
    assert broker.releases[handle.lease_id]["outcome"] == "ok"


def test_sync_heartbeat_error_does_not_end_the_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    broker = FakeBroker(ttl_s=0.03)
    coordinator = _coordinator(broker, ttl_s=0.03)
    beats: list[int] = []

    def garbled(*_: Any) -> None:
        beats.append(1)
        raise httpx.DecodingError("garbled heartbeat reply")

    monkeypatch.setattr(coordinator, "_beat", garbled)
    with coordinator.lease(caller="x", min_vram_mb=1) as handle:
        time.sleep(0.1)
    assert len(beats) >= 2
    assert broker.releases[handle.lease_id]["outcome"] == "ok"


async def test_async_oom_outcome() -> None:
    broker = FakeBroker()
    with pytest.raises(RuntimeError):
        async with _coordinator(broker).alease(caller="x", min_vram_mb=1) as handle:
            raise RuntimeError("CUDA out of memory")
    assert broker.releases[handle.lease_id]["outcome"] == "oom"


# --- local coordinators keep working, and now yield a handle ----------------------------------------------------------


async def test_local_coordinators_alease(tmp_path: Path) -> None:
    async with NullCoordinator().alease(caller="x", min_vram_mb=1, priority="batch") as handle:
        assert handle == GpuLease()
    lease = LeaseCoordinator(lock_path=str(tmp_path / "gpu.lock"), timeout_s=1, poll_s=0.01, probe=lambda device: None)
    async with lease.alease(caller="x", min_vram_mb=1, target="comfyui") as handle:
        assert handle.via == "local"
    with lease.lease(caller="y", min_vram_mb=0):  # the async lease let go of the lock
        pass


async def test_cancelled_async_file_lease_does_not_strand_the_lock(tmp_path: Path) -> None:
    lock = str(tmp_path / "gpu.lock")
    holder = LeaseCoordinator(lock_path=lock, timeout_s=5, poll_s=0.01, probe=lambda device: None)
    waiter = LeaseCoordinator(lock_path=lock, timeout_s=5, poll_s=0.01, probe=lambda device: None)
    with holder.lease(caller="holder", min_vram_mb=0):

        async def wait() -> None:
            async with waiter.alease(caller="waiter", min_vram_mb=0):
                pass

        task = asyncio.create_task(wait())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # The waiter's thread takes the lock once the holder lets go, then releases it at once.
    await asyncio.sleep(0.2)
    with LeaseCoordinator(lock_path=lock, timeout_s=1, poll_s=0.01, probe=lambda device: None).lease(
        caller="after", min_vram_mb=0
    ):
        pass


async def test_async_file_lease_waiters_do_not_starve_the_holders_release(tmp_path: Path) -> None:
    """More waiters than the default executor has threads must not block the holder's release."""
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
    lock = str(tmp_path / "gpu.lock")

    def coordinator() -> LeaseCoordinator:
        return LeaseCoordinator(lock_path=lock, timeout_s=5, poll_s=0.01, probe=lambda device: None)

    async def job() -> None:
        async with coordinator().alease(caller="job", min_vram_mb=0):
            await asyncio.sleep(0.01)

    await asyncio.wait_for(asyncio.gather(*(job() for _ in range(6))), timeout=4)


# --- configuration -------------------------------------------------------------------------------------------------


def test_broker_without_url_falls_back_with_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        coordinator = gpu.build_coordinator("broker")
    assert isinstance(coordinator, LeaseCoordinator)
    assert "no broker URL" in caplog.text
    assert isinstance(gpu.build_coordinator("broker", fallback="none"), NullCoordinator)


def test_coordinator_from_env_builds_the_broker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    manifest = tmp_path / "gpu-tenant.json"
    manifest.write_text(MANIFEST.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("ARGUS_GPU_COORDINATOR", "broker")
    monkeypatch.setenv("ARGUS_GPU_BROKER_URL", "http://iris:8120")
    monkeypatch.setenv("ARGUS_GPU_BROKER_TOKEN", "s3cret")
    monkeypatch.setenv("ARGUS_GPU_TENANT_MANIFEST", str(manifest))
    monkeypatch.setenv("ARGUS_GPU_BROKER_FALLBACK", "none")
    monkeypatch.delenv("ARGUS_GPU_TENANT", raising=False)
    coordinator = gpu.coordinator_from_env()
    assert isinstance(coordinator, BrokerCoordinator)
    assert coordinator.tenant == "argus-darkroom"  # from the manifest
    assert coordinator.manifest == MANIFEST
    assert isinstance(coordinator.fallback, NullCoordinator)
    assert coordinator._headers == {"Authorization": "Bearer s3cret"}
    monkeypatch.setenv("ARGUS_GPU_TENANT", "argus-mint")
    assert gpu.coordinator_from_env().tenant == "argus-mint"


def test_unreadable_manifest_does_not_stop_the_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    bad = tmp_path / "gpu-tenant.json"
    bad.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        coordinator = gpu.build_coordinator(
            "broker", broker_url="http://iris:8120", tenant="argus-lens", manifest_path=str(bad)
        )
    assert isinstance(coordinator, BrokerCoordinator) and coordinator.manifest is None
    assert "not loaded" in caplog.text
