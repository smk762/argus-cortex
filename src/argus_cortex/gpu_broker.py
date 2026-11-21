"""The client side of the suite's GPU broker (argus-iris): :class:`BrokerCoordinator`.

A :class:`~argus_cortex.gpu.GpuCoordinator` like the others, so a stage switches to the broker by environment alone
(``ARGUS_GPU_COORDINATOR=broker``, see :func:`~argus_cortex.gpu.coordinator_from_env`). A lease:

1. registers the tenant manifest (``POST /tenants``), if one was given: before the first lease, then again every
   ``REREGISTER_S`` and after the broker was unreachable or forgot a lease, since the broker keeps tenants in memory;
2. asks for the lease (``POST /leases``, with a fresh ``request_id`` so the one retry after a lost response is
   idempotent) and, while it is queued, long-polls ``GET /leases/{id}?wait_s=`` until it is
   granted, refused, or the coordinator's ``timeout_s`` runs out (then it cancels the queued request and raises
   :class:`~argus_cortex.gpu.GpuLeaseTimeout`);
3. while the body runs, heartbeats every third of the lease's TTL in the background (``POST /leases/{id}/heartbeat``),
   setting :meth:`~argus_cortex.gpu.GpuLease.should_yield` when the broker asks for the GPU back, and re-attaching
   with ``resume`` if the broker restarted and forgot the lease;
4. releases it (``POST /leases/{id}/release``) with the outcome (``ok``, ``failed``, ``oom``, ``cancelled``) and the
   peak VRAM the body reported.

**Fails open.** If the broker cannot be reached (connection error, timeout, a 5xx, no such route), the lease is taken
from the *fallback* coordinator instead and its handle says ``via="fallback"``; ``base_url`` is then ``None``, so a
caller uses its own configured URL. A 4xx other than 404 is a bug on one side (a bad request, an incompatible
``lease_version``) and raises :class:`BrokerError` rather than being hidden. A refusal raises
:class:`~argus_cortex.gpu.GpuLeaseRefused`. A lost heartbeat never stops the body: the work is already running.

httpx is imported only when a lease is taken (``argus-cortex[remote]``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any

from argus_cortex.gpu import (
    DEFAULT_TIMEOUT_S,
    GpuCoordinator,
    GpuLease,
    GpuLeaseRefused,
    GpuLeaseTimeout,
    NullCoordinator,
)
from argus_cortex.gpu_wire import (
    GpuConstraints,
    Heartbeat,
    HeartbeatReply,
    LeaseRelease,
    LeaseRequest,
    LeaseStatus,
    PriorityClass,
    ReleaseOutcome,
    TenantManifest,
)
from argus_cortex.wire import VersionError

if TYPE_CHECKING:
    from pydantic import BaseModel

_log = logging.getLogger(__name__)

DEFAULT_POLL_WAIT_S = 20.0
DEFAULT_MIN_POLL_S = 0.5
DEFAULT_REQUEST_TIMEOUT_S = 10.0
DEFAULT_TTL_S = 60.0
# The broker keeps tenants in memory, so the manifest is posted again this often (and at once after the broker was
# unreachable or forgot a lease): a restarted broker would otherwise never learn it from a long-lived client.
REREGISTER_S = 300.0


class BrokerError(RuntimeError):
    """The broker answered, but with something this client cannot use (a 4xx, or a malformed payload)."""


class _Unreachable(Exception):
    """The broker cannot be reached or is broken: fall back."""


def _httpx() -> Any:
    try:
        import httpx  # noqa: PLC0415 - optional, behind the [remote] extra
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError("the 'broker' GPU coordinator needs: pip install argus-cortex[remote]") from exc
    return httpx


def _outcome(exc: BaseException) -> tuple[ReleaseOutcome, str]:
    """How the body ended, from the exception it raised."""
    detail = f"{type(exc).__name__}: {exc}"[:2000]
    if not isinstance(exc, Exception):  # CancelledError, KeyboardInterrupt, GeneratorExit
        return "cancelled", detail
    if type(exc).__name__ == "OutOfMemoryError" or "out of memory" in str(exc).lower():
        return "oom", detail
    return "failed", detail


def _gave_up(exc: BaseException) -> str:
    """The release detail for a queued lease abandoned because of *exc*."""
    if isinstance(exc, GpuLeaseTimeout):
        return "timed out waiting for a grant"
    if isinstance(exc, asyncio.CancelledError):
        return "caller cancelled"
    return f"gave up while queued: {type(exc).__name__}: {exc}"[:2000]


def _heartbeat_body(handle: GpuLease) -> dict[str, Any]:
    """The heartbeat for *handle*. Progress is display only: a value the wire refuses (``handle.progress = 50``)
    is dropped rather than failing the beat, which would let the broker expire a lease whose work is running."""
    try:
        beat = Heartbeat(peak_vram_mb=handle.peak_vram_mb, progress=handle.progress, stage=handle.stage)
    except ValueError:  # pydantic's ValidationError
        _log.debug(
            "GPU lease %s: progress %r / stage %r not sendable; beating without them",
            handle.lease_id,
            handle.progress,
            handle.stage,
        )
        beat = Heartbeat(peak_vram_mb=handle.peak_vram_mb)
    return beat.model_dump(mode="json")


class BrokerCoordinator:
    """Leases from the GPU broker at *url*, falling back to *fallback* when it cannot be reached.

    *tenant* names the calling service (a slug, e.g. ``argus-darkroom``); *manifest*, if given, is registered with
    the broker before the first lease, again every :data:`REREGISTER_S`, and again after the broker was unreachable
    or forgot a lease (a restart). *timeout_s* bounds the whole wait for a grant. *transport* and
    *async_transport* inject httpx transports (tests).
    """

    name = "broker"

    def __init__(
        self,
        url: str,
        *,
        tenant: str,
        token: str | None = None,
        manifest: TenantManifest | None = None,
        fallback: GpuCoordinator | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        ttl_s: float = DEFAULT_TTL_S,
        poll_wait_s: float = DEFAULT_POLL_WAIT_S,
        min_poll_s: float = DEFAULT_MIN_POLL_S,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        transport: Any = None,
        async_transport: Any = None,
    ) -> None:
        LeaseRequest(tenant=tenant, ttl_s=ttl_s)  # fail fast on a bad tenant slug or TTL, not at the first lease
        self.url = url.rstrip("/")
        self.tenant = tenant
        self.manifest = manifest
        self.fallback: GpuCoordinator = fallback or NullCoordinator()
        self.timeout_s = max(0.0, timeout_s)
        self.ttl_s = ttl_s
        self.poll_wait_s = poll_wait_s
        self.min_poll_s = min_poll_s
        self.request_timeout_s = request_timeout_s
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._transport = transport
        self._async_transport = async_transport
        self._registered_at: float | None = None  # time.monotonic() of the last POST /tenants; None: post it next
        self._register_lock = threading.Lock()
        self._unreachable_warned = False

    # --- shared pieces -------------------------------------------------------------------------------------------

    def _request(
        self,
        *,
        caller: str,
        min_vram_mb: int,
        priority: PriorityClass | None,
        target: str | None,
        kind: str | None,
        preemptible: bool | None,
        constraints: GpuConstraints | None,
        model_mb: int | None,
    ) -> LeaseRequest:
        vram_mb = max(0, int(min_vram_mb))
        return LeaseRequest(
            tenant=self.tenant,
            caller=caller[:200],
            kind=kind or "default",
            target=target or "in-process",
            vram_mb=vram_mb,
            model_mb=None if model_mb is None else min(max(0, int(model_mb)), vram_mb),
            request_id=uuid.uuid4().hex,  # one per lease() call, so a retried POST is answered with the same lease
            priority=priority,
            preemptible=preemptible,
            constraints=constraints or GpuConstraints(),
            ttl_s=self.ttl_s,
        )

    def _client_kwargs(self) -> dict[str, Any]:
        # trust_env=False: the broker is on the LAN, and a desktop's HTTP(S)_PROXY must not swallow it.
        return {"base_url": self.url, "headers": self._headers, "trust_env": False}

    def _timeout(self, wait_s: float = 0.0) -> float:
        return wait_s + self.request_timeout_s

    def _remaining(self, deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    def _poll_wait(self, deadline: float) -> float:
        return min(self.poll_wait_s, self._remaining(deadline))

    def _pause_after(self, started: float, deadline: float) -> float:
        """How long to sleep after a poll that returned early, so a broker that ignores ``wait_s`` isn't hammered."""
        return max(0.0, min(self.min_poll_s - (time.monotonic() - started), self._remaining(deadline)))

    def _parse(self, response: Any, model: type[BaseModel]) -> Any:
        """The payload as *model*; ``None`` on 404. A 5xx is unreachable, any other 4xx a :class:`BrokerError`."""
        code = response.status_code
        if code >= 500:
            raise _Unreachable(f"broker answered {code}")
        if code == 404:
            return None
        if code >= 400:
            raise BrokerError(f"broker refused the request ({code}): {response.text[:300]}")
        try:
            return model.model_validate(response.json())
        except (ValueError, VersionError) as exc:  # bad JSON, a failed validation, an incompatible lease_version
            raise BrokerError(f"broker sent an unreadable {model.__name__}: {exc}") from exc

    def _transport_errors(self) -> tuple[type[BaseException], ...]:
        return (_httpx().TransportError,)

    def _granted_or_raise(self, status: LeaseStatus) -> LeaseStatus:
        if status.state == "granted":
            return status
        raise GpuLeaseRefused(
            f"GPU broker {status.state} lease {status.lease_id}: {status.reason or 'no reason given'}"
        )

    def _timed_out(self, req: LeaseRequest, status: LeaseStatus) -> GpuLeaseTimeout:
        where = f", {status.position} ahead" if status.position is not None else ""
        why = f" ({status.reason})" if status.reason else ""
        return GpuLeaseTimeout(
            f"GPU broker did not grant {req.tenant}/{req.kind} ({req.vram_mb} MB on {req.target}) "
            f"within {self.timeout_s:g}s{where}{why}"
        )

    def _handle(self, status: LeaseStatus) -> GpuLease:
        return GpuLease(
            via="broker", lease_id=status.lease_id, node=status.node, device=status.device, base_url=status.base_url
        )

    def _needs_register(self) -> bool:
        if self.manifest is None:
            return False
        at = self._registered_at
        return at is None or time.monotonic() - at >= REREGISTER_S

    def _forget_registration(self) -> None:
        """Post the manifest again before the next lease: the broker may have restarted and lost it."""
        self._registered_at = None

    def _note_registered(self, status_code: int) -> None:
        """Record a POST /tenants answer. A 5xx is unreachable; a 4xx is logged and retried only after the interval."""
        if status_code >= 500:
            raise _Unreachable(f"broker answered {status_code} registering the tenant")
        if status_code >= 400 and self.manifest is not None:
            _log.warning("GPU broker refused tenant manifest %s (%d)", self.manifest.tenant, status_code)
        self._registered_at = time.monotonic()

    def _note_unreachable(self, exc: BaseException) -> None:
        self._forget_registration()
        if not self._unreachable_warned:
            _log.warning("GPU broker %s unreachable (%s); using the %r coordinator", self.url, exc, self.fallback.name)
            self._unreachable_warned = True
        else:
            _log.debug("GPU broker %s still unreachable (%s)", self.url, exc)

    def _note_reachable(self) -> None:
        if self._unreachable_warned:
            _log.info("GPU broker %s reachable again", self.url)
            self._unreachable_warned = False

    def _after_beat(self, reply: HeartbeatReply | None, handle: GpuLease, lost: list[bool]) -> float:
        """Apply a heartbeat reply to the handle; return the next interval."""
        if reply is None:
            return self.ttl_s / 3
        if reply.yield_requested and not handle.should_yield():
            _log.info("GPU broker asks lease %s to yield: %s", handle.lease_id, reply.reason or "higher priority work")
            handle._yield.set()
        if reply.state != "granted" and not lost[0]:
            _log.warning(
                "GPU broker reports lease %s %s (%s); the work continues", handle.lease_id, reply.state, reply.reason
            )
            lost[0] = True
        return reply.ttl_s / 3

    # --- sync ----------------------------------------------------------------------------------------------------

    @contextlib.contextmanager
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
    ) -> Iterator[GpuLease]:
        options = {
            "priority": priority,
            "target": target,
            "kind": kind,
            "preemptible": preemptible,
            "model_mb": model_mb,
        }
        req = self._request(caller=caller, min_vram_mb=min_vram_mb, constraints=constraints, **options)
        httpx = _httpx()
        with httpx.Client(transport=self._transport, timeout=self.request_timeout_s, **self._client_kwargs()) as client:
            status: LeaseStatus | None
            try:
                self._register(client)
                status = self._acquire(client, req)
                self._note_reachable()
            except _Unreachable as exc:
                self._note_unreachable(exc)
                status = None
            if status is None:
                with self.fallback.lease(
                    caller=caller, min_vram_mb=min_vram_mb, constraints=constraints, **options
                ) as fallback_handle:
                    fallback_handle.via = "fallback"
                    yield fallback_handle
                return

            handle = self._handle(status)
            stop = threading.Event()
            beat = threading.Thread(
                target=self._heartbeat_loop, args=(req, handle, status.ttl_s, stop), daemon=True, name="gpu-heartbeat"
            )
            beat.start()
            outcome: ReleaseOutcome = "ok"
            detail: str | None = None
            try:
                yield handle
            except BaseException as exc:
                outcome, detail = _outcome(exc)
                raise
            finally:
                stop.set()
                beat.join(timeout=self.request_timeout_s)
                self._release(client, handle.lease_id, outcome, handle.peak_vram_mb, detail)

    def _send(self, call: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return call(*args, **kwargs)
        except self._transport_errors() as exc:
            raise _Unreachable(str(exc) or type(exc).__name__) from exc

    def _register(self, client: Any) -> None:
        if not self._needs_register():
            return
        with self._register_lock:
            if not self._needs_register() or self.manifest is None:
                return
            response = self._send(client.post, "/tenants", json=self.manifest.model_dump(mode="json"))
            self._note_registered(response.status_code)

    def _post_lease(self, client: Any, req: LeaseRequest) -> LeaseStatus:
        response = self._send(
            client.post, "/leases", json=req.model_dump(mode="json"), timeout=self._timeout(req.wait_s)
        )
        status = self._parse(response, LeaseStatus)
        if status is None:
            raise _Unreachable("no /leases route at the broker URL")
        return status

    def _first_post(self, client: Any, req: LeaseRequest) -> LeaseStatus:
        """The first POST, retried once if its response was lost to a timeout: ``request_id`` makes that safe."""
        try:
            return self._post_lease(client, req)
        except _Unreachable as exc:
            if not isinstance(exc.__cause__, _httpx().TimeoutException):
                raise
            _log.debug("GPU broker POST /leases timed out; retrying request %s once", req.request_id)
            return self._post_lease(client, req)

    def _acquire(self, client: Any, req: LeaseRequest) -> LeaseStatus:
        deadline = time.monotonic() + self.timeout_s
        status = self._first_post(client, req.model_copy(update={"wait_s": self._poll_wait(deadline)}))
        try:
            while status.state == "queued":
                if self._remaining(deadline) <= 0:
                    raise self._timed_out(req, status)
                wait = self._poll_wait(deadline)
                started = time.monotonic()
                response = self._send(
                    client.get, f"/leases/{status.lease_id}", params={"wait_s": wait}, timeout=self._timeout(wait)
                )
                polled = self._parse(response, LeaseStatus)
                if polled is not None and polled.state == "queued":
                    time.sleep(self._pause_after(started, deadline))
                if polled is None:  # the broker restarted and lost the queued request: register and ask again
                    self._forget_registration()
                    self._register(client)
                    polled = self._post_lease(client, req.model_copy(update={"wait_s": self._poll_wait(deadline)}))
                status = polled
        except BaseException as exc:
            # Whatever ended the wait (timeout, a 5xx mid-poll that sends us to the fallback, a bad payload,
            # Ctrl-C), don't leave the request queued: the broker would grant it later and hold VRAM nobody uses.
            if status.state == "queued":
                self._release(client, status.lease_id, "cancelled", None, _gave_up(exc))
            raise
        return self._granted_or_raise(status)

    def _beat(self, client: Any, req: LeaseRequest, handle: GpuLease) -> HeartbeatReply | None:
        body = _heartbeat_body(handle)
        reply = self._parse(self._send(client.post, f"/leases/{handle.lease_id}/heartbeat", json=body), HeartbeatReply)
        if reply is None:  # the broker forgot the lease (restart): register and re-attach, the work is already running
            self._forget_registration()
            self._register(client)
            status = self._post_lease(client, req.model_copy(update={"resume": handle.lease_id, "wait_s": 0.0}))
            _log.info(
                "GPU broker forgot lease %s; re-attached as %s (%s)", handle.lease_id, status.lease_id, status.state
            )
            handle.lease_id = status.lease_id
        return reply

    def _heartbeat_loop(self, req: LeaseRequest, handle: GpuLease, ttl_s: float, stop: threading.Event) -> None:
        httpx = _httpx()
        lost = [False]
        interval = ttl_s / 3
        with httpx.Client(transport=self._transport, timeout=self.request_timeout_s, **self._client_kwargs()) as client:
            while not stop.wait(interval):
                try:
                    interval = self._after_beat(self._beat(client, req, handle), handle, lost)
                except (_Unreachable, BrokerError) as exc:
                    _log.debug("GPU broker heartbeat for %s failed: %s", handle.lease_id, exc)
                except Exception:  # noqa: BLE001 - one odd reply must not end the heartbeat for the rest of the lease
                    _log.warning("GPU broker heartbeat for %s failed", handle.lease_id, exc_info=True)

    def _release(
        self, client: Any, lease_id: str | None, outcome: ReleaseOutcome, peak_vram_mb: int | None, detail: str | None
    ) -> None:
        if not lease_id:
            return
        body = LeaseRelease(outcome=outcome, peak_vram_mb=peak_vram_mb, detail=detail).model_dump(mode="json")
        try:
            response = self._send(client.post, f"/leases/{lease_id}/release", json=body)
            if response.status_code >= 400 and response.status_code != 404:
                _log.warning(
                    "GPU broker refused to release %s (%d); its TTL will expire it", lease_id, response.status_code
                )
        except _Unreachable as exc:
            _log.warning("GPU broker unreachable releasing %s (%s); its TTL will expire it", lease_id, exc)

    # --- async ---------------------------------------------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def alease(
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
    ) -> AsyncIterator[GpuLease]:
        options = {
            "priority": priority,
            "target": target,
            "kind": kind,
            "preemptible": preemptible,
            "model_mb": model_mb,
        }
        req = self._request(caller=caller, min_vram_mb=min_vram_mb, constraints=constraints, **options)
        httpx = _httpx()
        async with httpx.AsyncClient(
            transport=self._async_transport, timeout=self.request_timeout_s, **self._client_kwargs()
        ) as client:
            status: LeaseStatus | None
            try:
                await self._aregister(client)
                status = await self._aacquire(client, req)
                self._note_reachable()
            except _Unreachable as exc:
                self._note_unreachable(exc)
                status = None
            if status is None:
                async with self.fallback.alease(
                    caller=caller, min_vram_mb=min_vram_mb, constraints=constraints, **options
                ) as fallback_handle:
                    fallback_handle.via = "fallback"
                    yield fallback_handle
                return

            handle = self._handle(status)
            beat = asyncio.create_task(self._aheartbeat_loop(client, req, handle, status.ttl_s), name="gpu-heartbeat")
            outcome: ReleaseOutcome = "ok"
            detail: str | None = None
            try:
                yield handle
            except BaseException as exc:
                outcome, detail = _outcome(exc)
                raise
            finally:
                beat.cancel()
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        await beat
                except Exception:  # noqa: BLE001 - a dead heartbeat must not skip the release or replace the result
                    _log.warning("GPU broker heartbeat for %s ended with an error", handle.lease_id, exc_info=True)
                finally:
                    await self._arelease(client, handle.lease_id, outcome, handle.peak_vram_mb, detail)

    async def _asend(self, call: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return await call(*args, **kwargs)
        except self._transport_errors() as exc:
            raise _Unreachable(str(exc) or type(exc).__name__) from exc

    async def _aregister(self, client: Any) -> None:
        if not self._needs_register() or self.manifest is None:
            return
        response = await self._asend(client.post, "/tenants", json=self.manifest.model_dump(mode="json"))
        self._note_registered(response.status_code)

    async def _apost_lease(self, client: Any, req: LeaseRequest) -> LeaseStatus:
        response = await self._asend(
            client.post, "/leases", json=req.model_dump(mode="json"), timeout=self._timeout(req.wait_s)
        )
        status = self._parse(response, LeaseStatus)
        if status is None:
            raise _Unreachable("no /leases route at the broker URL")
        return status

    async def _afirst_post(self, client: Any, req: LeaseRequest) -> LeaseStatus:
        try:
            return await self._apost_lease(client, req)
        except _Unreachable as exc:
            if not isinstance(exc.__cause__, _httpx().TimeoutException):
                raise
            _log.debug("GPU broker POST /leases timed out; retrying request %s once", req.request_id)
            return await self._apost_lease(client, req)

    async def _aacquire(self, client: Any, req: LeaseRequest) -> LeaseStatus:
        deadline = time.monotonic() + self.timeout_s
        status = await self._afirst_post(client, req.model_copy(update={"wait_s": self._poll_wait(deadline)}))
        try:
            while status.state == "queued":
                if self._remaining(deadline) <= 0:
                    raise self._timed_out(req, status)
                wait = self._poll_wait(deadline)
                started = time.monotonic()
                response = await self._asend(
                    client.get, f"/leases/{status.lease_id}", params={"wait_s": wait}, timeout=self._timeout(wait)
                )
                polled = self._parse(response, LeaseStatus)
                if polled is not None and polled.state == "queued":
                    await asyncio.sleep(self._pause_after(started, deadline))
                if polled is None:  # see _acquire
                    self._forget_registration()
                    await self._aregister(client)
                    polled = await self._apost_lease(
                        client, req.model_copy(update={"wait_s": self._poll_wait(deadline)})
                    )
                status = polled
        except BaseException as exc:  # see _acquire: never leave the request in the queue
            if status.state == "queued":
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._arelease(client, status.lease_id, "cancelled", None, _gave_up(exc)))
            raise
        return self._granted_or_raise(status)

    async def _abeat(self, client: Any, req: LeaseRequest, handle: GpuLease) -> HeartbeatReply | None:
        body = _heartbeat_body(handle)
        response = await self._asend(
            client.post, f"/leases/{handle.lease_id}/heartbeat", json=body, timeout=self.request_timeout_s
        )
        reply = self._parse(response, HeartbeatReply)
        if reply is None:  # see _beat
            self._forget_registration()
            await self._aregister(client)
            status = await self._apost_lease(client, req.model_copy(update={"resume": handle.lease_id, "wait_s": 0.0}))
            _log.info(
                "GPU broker forgot lease %s; re-attached as %s (%s)", handle.lease_id, status.lease_id, status.state
            )
            handle.lease_id = status.lease_id
        return reply

    async def _aheartbeat_loop(self, client: Any, req: LeaseRequest, handle: GpuLease, ttl_s: float) -> None:
        lost = [False]
        interval = ttl_s / 3
        while True:
            await asyncio.sleep(interval)
            try:
                interval = self._after_beat(await self._abeat(client, req, handle), handle, lost)
            except (_Unreachable, BrokerError) as exc:
                _log.debug("GPU broker heartbeat for %s failed: %s", handle.lease_id, exc)
            except Exception:  # noqa: BLE001 - one odd reply must not end the heartbeat for the rest of the lease
                _log.warning("GPU broker heartbeat for %s failed", handle.lease_id, exc_info=True)

    async def _arelease(
        self, client: Any, lease_id: str | None, outcome: ReleaseOutcome, peak_vram_mb: int | None, detail: str | None
    ) -> None:
        if not lease_id:
            return
        body = LeaseRelease(outcome=outcome, peak_vram_mb=peak_vram_mb, detail=detail).model_dump(mode="json")
        try:
            response = await self._asend(
                client.post, f"/leases/{lease_id}/release", json=body, timeout=self.request_timeout_s
            )
            if response.status_code >= 400 and response.status_code != 404:
                _log.warning(
                    "GPU broker refused to release %s (%d); its TTL will expire it", lease_id, response.status_code
                )
        except _Unreachable as exc:
            _log.warning("GPU broker unreachable releasing %s (%s); its TTL will expire it", lease_id, exc)


__all__ = ["BrokerCoordinator", "BrokerError"]
