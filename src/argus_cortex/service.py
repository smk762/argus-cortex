"""What an Argus service says about itself: one ``GET /health`` shape for the whole suite.

Every service answered ``/health`` its own way, so nothing could list the suite with each service's version and
state. :class:`ServiceHealth` is the one body: who it is, its version, the wire contracts it speaks, whether it is
ready, and how its dependencies are doing. ``install_health`` serves it::

    from argus_cortex.service import install_health

    install_health(
        app,
        service="argus-lens",
        wire={"caption_version": CAPTION_VERSION},
        checks={"lineage_store": check_store},      # async or sync; returns a DependencyCheck or bool, or raises
        readiness=lambda: None if model_loaded() else "starting",
        extra=resident_fields,                       # a GPU resident adds model_state, busy, idle_s, gpu here
    )

* **Status.** ``ok``: every required check passes. ``degraded``: the service answers, but a required check failed.
  ``starting``: not ready yet. There is no ``down``: a process that answers is not down.
* **HTTP.** ``200`` for ``ok`` and ``degraded``, ``503`` for ``starting``, so a container healthcheck can use the
  route and a failed dependency does not restart the service.
* **``GET /health?checks=0``** skips the dependency checks. Use it for container healthchecks and for one service's
  check on another, so a probe every few seconds stays cheap and two services that check each other cannot wait on
  each other.
* **Checks are cached** for ``check_ttl_s`` and bounded by ``check_timeout_s``. While one is being refreshed, other
  requests get its last result.
* **It composes with ``ResidentHealth``.** Both models allow extra fields and share no field name, so one body is
  both. A field from ``extra`` never replaces one of the contract's own.
* **Capabilities, two forms.** A list of names, or a map of every capability the service knows to whether it is on
  (``service_version`` 1.1), so a service whose clients already read such a map keeps it. A reader asks
  ``ServiceHealth.capability_names`` and gets the names that are on either way.
* **Nothing private in the body.** The route is unauthenticated and scraped. A check that raises reports the
  exception's type only, and ``user:password@`` in a detail is removed.

Where a service is reachable is not its business: a process does not know its host or published port. The compose
labels below let a discovery layer (argus-iris's agents) find it.

The models and the gauge text need pydantic only. ``install_health`` and ``metrics_route`` need Starlette
(``[server]``); ``build_info_collector`` needs ``prometheus-client`` (``[metrics]``).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

from argus_cortex.gpu_wire import RoutePath, Slug
from argus_cortex.peer_names import PEER_NAME_PATTERN
from argus_cortex.wire import make_versioned_base, render_schema

__all__ = [
    "BUILD_ENV",
    "HEALTH_LABEL",
    "PEER_ENV",
    "PEER_LABEL",
    "PORT_LABEL",
    "SERVICE_LABEL",
    "SERVICE_VERSION",
    "DIRECTORY_VERSION",
    "DependencyCheck",
    "DirectoryEntry",
    "EntryHealth",
    "PeerLookup",
    "ServiceHealth",
    "ServiceStatus",
    "build_info_collector",
    "build_info_text",
    "capability_names",
    "install_health",
    "metrics_route",
    "service_directory_schema",
    "service_health_schema",
]

_log = logging.getLogger(__name__)

SERVICE_VERSION = "1.1"
SERVICE_MAJORS = ("1",)
SERVICE_SCHEMA_TITLE = "Argus service health"

# Compose labels a discovery layer reads. Set them in the service's own compose file.
PEER_LABEL = "argus.peer"
"""Required: the name callers ask for (``nudenet``, ``lens``, ``xtts``), as ``argus_cortex.peers`` spells it."""
SERVICE_LABEL = "argus.service"
"""Optional: the package (``argus-retina``), :attr:`ServiceHealth.service`. One package can run several peers."""
HEALTH_LABEL = "argus.health"
"""Optional: the health route, when it is not ``/health``."""
PORT_LABEL = "argus.port"
"""Optional: the container port to probe, when the container publishes several."""

BUILD_ENV = "ARGUS_BUILD"
"""The image's build id (a git sha or tag), baked in at image build time: ``ARG GIT_SHA`` / ``ENV ARGUS_BUILD``."""
PEER_ENV = "ARGUS_PEER"
"""The container's ``argus.peer`` label, repeated in its environment, since a process cannot read its own labels."""

DEFAULT_CHECK_TTL_S = 10.0
DEFAULT_CHECK_TIMEOUT_S = 2.0
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

ServiceStatus = Literal["ok", "degraded", "starting"]
SERVICE_STATUSES: tuple[str, ...] = get_args(ServiceStatus)
PeerName = Annotated[str, Field(pattern=PEER_NAME_PATTERN, max_length=100)]

_USERINFO = re.compile(r"(?<=://)[^/\s@]*@")
_DETAIL_MAX = 200


class DependencyCheck(BaseModel):
    """How one thing a service depends on is doing. Unknown fields are ignored, so a reader accepts a later 1.x."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    ok: bool
    detail: str | None = Field(
        None,
        max_length=_DETAIL_MAX,
        description="Short and human-readable. Never a token; credentials in a URL are removed, and it is cut to "
        f"{_DETAIL_MAX} characters.",
    )
    latency_ms: float | None = Field(None, ge=0)
    required: bool = Field(
        True, description="A failed required check makes the service degraded. Others are reported and nothing more."
    )

    @field_validator("detail", mode="before")
    @classmethod
    def _scrub(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        value = _USERINFO.sub("", " ".join(value.split()))
        return value[:_DETAIL_MAX] or None


ServiceVersioned = make_versioned_base(
    "service_version", SERVICE_VERSION, SERVICE_MAJORS, label="service health", class_name="ServiceVersioned"
)


class ServiceHealth(ServiceVersioned):
    """A service's ``GET /health`` body.

    ``extra="allow"``: a service keeps its own fields beside these, and a GPU resident's body is also a
    ``gpu_wire.ResidentHealth``. Never in it: tokens, credentials, internal URLs, hostnames or file paths.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    service: Slug = Field(description="The package name (argus-lens), the same on every host.")
    version: str = Field(min_length=1, max_length=100, description="The installed package version.")
    status: ServiceStatus
    started_at: AwareDatetime
    wire: dict[str, str] = Field(
        default_factory=dict, description='Wire contracts it speaks and their versions: {"caption_version": "2.3"}.'
    )
    capabilities: tuple[str, ...] | dict[str, StrictBool] = Field(
        (),
        description=(
            "What it can do now: a list of names (tts, voice-clone), or every capability it knows with whether it is "
            'on ({"identity_check": false}). Since 1.1; a 1.0 body has the list.'
        ),
    )
    checks: dict[str, DependencyCheck] = Field(default_factory=dict)
    read_only: bool = False
    build: str | None = Field(None, max_length=200, description="The image's build id (a git sha or tag), if known.")
    peer: PeerName | None = Field(None, description="The container's argus.peer label, if it was told it.")

    @property
    def ready(self) -> bool:
        """It is serving (``ok`` or ``degraded``), which is when the route answers 200."""
        return self.status != "starting"

    @property
    def capability_names(self) -> tuple[str, ...]:
        """The capabilities that are on, whichever form the body used: the list as given, or the map's true names
        in order. Read this, not ``capabilities``, to ask "can it do X"."""
        return capability_names(self.capabilities)


def capability_names(capabilities: Sequence[str] | Mapping[str, bool]) -> tuple[str, ...]:
    """The names that are on: a list as given, or the sorted names a map marks true."""
    if isinstance(capabilities, Mapping):
        return tuple(sorted(name for name, on in capabilities.items() if on))
    return tuple(capabilities)


def service_health_schema() -> str:
    """The committed ``service-health-schema.json`` (``make schema`` regenerates it; a test fails when it is stale)."""
    return render_schema([ServiceHealth], title=SERVICE_SCHEMA_TITLE)


# ── the directory: where a peer is ───────────────────────────────────────────────────────────────────────────────

DIRECTORY_VERSION = "1.0"
DIRECTORY_MAJORS = ("1",)
DIRECTORY_SCHEMA_TITLE = "Argus service directory"

DirectoryStatus = Literal["ok", "degraded", "starting", "down"]


class EntryHealth(BaseModel):
    """What the directory last read from an entry's health route.

    In full when the body was a :class:`ServiceHealth` (``contract``), and as up or ``down`` from the HTTP status
    otherwise. ``down`` exists here and not in :class:`ServiceHealth`: it is what a reader says of a service that
    did not answer.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    checked_at: AwareDatetime
    status: DirectoryStatus
    http_status: int | None = Field(None, description="Null: no answer.")
    contract: bool = Field(False, description="The body was a ServiceHealth.")
    service: str | None = None
    version: str | None = None
    wire: dict[str, str] = Field(default_factory=dict)
    capabilities: tuple[str, ...] = Field((), description="The names that are on (ServiceHealth.capability_names).")
    checks: dict[str, DependencyCheck] = Field(default_factory=dict)
    error: str | None = Field(None, max_length=_DETAIL_MAX)


class DirectoryEntry(BaseModel):
    """One place a peer runs: a container labelled ``argus.peer`` on a node, or a peer the directory's config names.

    ``url`` is where callers reach it: for a labelled container, the node agent's host and the container's
    published port, never a host anyone supplied. Null means not reachable, and ``reason`` says why.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    peer: PeerName
    origin: str = Field(description="How the directory knows it: label (a container) or config.")
    service: str | None = Field(None, description="The argus.service label, else what its health says.")
    node: str | None = None
    container: str | None = None
    running: bool = True
    url: str | None = None
    reason: str | None = None
    health_path: RoutePath = "/health"
    health: EntryHealth | None = None


DirectoryVersioned = make_versioned_base(
    "directory_version", DIRECTORY_VERSION, DIRECTORY_MAJORS, label="service directory", class_name="DirectoryVersioned"
)


class PeerLookup(DirectoryVersioned):
    """``GET /directory/{peer}`` on argus-iris: where callers reach *peer*, best first.

    Unknown fields are ignored and ``directory_version`` defaults to 1.0, so a reader accepts a later 1.x and a
    directory that does not send the version yet.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    peer: PeerName
    urls: tuple[DirectoryEntry, ...] = ()


DIRECTORY_MODELS: tuple[type[BaseModel], ...] = (PeerLookup, DirectoryEntry)


def service_directory_schema() -> str:
    """The committed ``service-directory-schema.json`` (``make schema``; a test fails when it is stale)."""
    return render_schema(DIRECTORY_MODELS, title=DIRECTORY_SCHEMA_TITLE)


# ── identity and the build-info gauge ────────────────────────────────────────────────────────────────────────────


def _installed_version(service: str) -> str:
    from importlib.metadata import PackageNotFoundError, version  # noqa: PLC0415

    try:
        return version(service)
    except PackageNotFoundError:
        return "0.0.0+unknown"


def _own(name: str) -> str | None:
    # Read from this process's environment only, never the suite file: it describes one container, not the suite.
    return os.environ.get(name, "").strip() or None


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _build_labels(service: str, version: str | None, build: str | None) -> dict[str, str]:
    labels = {
        "service": service,
        "version": version or _installed_version(service),
        "service_version": SERVICE_VERSION,
    }
    build = build if build is not None else _own(BUILD_ENV)
    if build:
        labels["build"] = build
    return labels


def build_info_text(service: str, *, version: str | None = None, build: str | None = None) -> str:
    """The ``argus_build_info`` gauge in Prometheus text format, for a service with no metrics library.

    ``argus_build_info{service="argus-lens",version="0.14.2",service_version="1.0"} 1``, with a ``build`` label when
    the build id is known.
    """
    labels = ",".join(f'{k}="{_label(v)}"' for k, v in _build_labels(service, version, build).items())
    return (
        "# HELP argus_build_info The running Argus service, its version and its health contract version.\n"
        "# TYPE argus_build_info gauge\n"
        f"argus_build_info{{{labels}}} 1\n"
    )


def build_info_collector(service: str, *, version: str | None = None, build: str | None = None) -> Any:
    """The same gauge as a ``prometheus_client`` collector, for a service that already serves ``/metrics``::

    prometheus_client.REGISTRY.register(build_info_collector("argus-lens"))
    """
    try:
        from prometheus_client.core import GaugeMetricFamily  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError("build_info_collector requires prometheus-client: pip install argus-cortex[metrics]") from exc
    labels = _build_labels(service, version, build)

    class _BuildInfo:
        def describe(self) -> list[Any]:
            return list(self.collect())

        def collect(self) -> list[Any]:
            family = GaugeMetricFamily(
                "argus_build_info",
                "The running Argus service, its version and its health contract version.",
                labels=list(labels),
            )
            family.add_metric(list(labels.values()), 1)
            return [family]

    return _BuildInfo()


def metrics_route(
    app: Any, *, service: str, version: str | None = None, build: str | None = None, path: str = "/metrics"
) -> None:
    """Serve only the ``argus_build_info`` gauge at *path*, for a service with no ``/metrics`` route of its own."""
    Response = _starlette().Response
    body = build_info_text(service, version=version, build=build)

    async def metrics(_request: Any) -> Any:
        return Response(body, media_type=METRICS_CONTENT_TYPE)

    app.add_route(path, metrics, methods=["GET"], include_in_schema=False)


# ── the health route ─────────────────────────────────────────────────────────────────────────────────────────────

CheckResult = DependencyCheck | bool | None
Check = Callable[[], CheckResult | Awaitable[CheckResult]]


def _starlette() -> Any:
    try:
        import starlette.responses as responses  # noqa: PLC0415
    except ImportError as exc:
        raise ImportError("this route requires Starlette: pip install argus-cortex[server]") from exc
    return responses


async def _call(fn: Callable[[], Any], *, thread: bool = False) -> Any:
    """Call *fn* whether it is sync or async. With *thread*, a sync one runs in a worker thread, so a check that
    does blocking I/O cannot stall the event loop."""
    if inspect.iscoroutinefunction(fn):
        return await fn()
    result = await asyncio.to_thread(fn) if thread else fn()
    return await result if inspect.isawaitable(result) else result


class _CachedCheck:
    """One dependency check: cached for a while, bounded in time, and refreshed by one request at a time."""

    def __init__(self, name: str, fn: Check, *, required: bool, ttl_s: float, timeout_s: float) -> None:
        self.name, self.fn, self.required, self.ttl_s, self.timeout_s = name, fn, required, ttl_s, timeout_s
        self._result: DependencyCheck | None = None
        self._expires = 0.0
        self._refresh: asyncio.Task[DependencyCheck] | None = None

    async def _run(self) -> DependencyCheck:
        started = time.perf_counter()
        try:
            raw = await asyncio.wait_for(_call(self.fn, thread=True), self.timeout_s)
        except TimeoutError:
            result = DependencyCheck(ok=False, detail="timeout")
        except Exception as exc:  # noqa: BLE001 - a failing dependency is a result, not an error in this route
            # The type only: an exception's message can carry a URL, a host or a path.
            result = DependencyCheck(ok=False, detail=type(exc).__name__)
        else:
            if isinstance(raw, DependencyCheck):
                result = raw
            elif raw is None or raw is True:
                result = DependencyCheck(ok=True)
            elif raw is False:
                result = DependencyCheck(ok=False)
            else:
                result = DependencyCheck(ok=False, detail="check returned an unexpected value")
        updates: dict[str, Any] = {"required": self.required}
        if result.latency_ms is None:
            updates["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        result = result.model_copy(update=updates)
        self._result, self._expires = result, time.monotonic() + self.ttl_s
        return result

    async def get(self) -> DependencyCheck:
        if self._result is not None and time.monotonic() < self._expires:
            return self._result
        if self._refresh is None or self._refresh.done():
            self._refresh = asyncio.ensure_future(self._run())
        elif self._result is not None:
            return self._result  # someone is refreshing it: the last result will do
        # Shielded: one caller going away must not cancel the refresh the others are waiting for.
        return await asyncio.shield(self._refresh)


Capabilities = Sequence[str] | Mapping[str, bool]


def _capabilities(value: Capabilities) -> tuple[str, ...] | dict[str, bool]:
    return dict(value) if isinstance(value, Mapping) else tuple(value)


def install_health(
    app: Any,
    *,
    service: str,
    version: str | None = None,
    wire: Mapping[str, str] | None = None,
    capabilities: Capabilities | Callable[[], Capabilities] | None = None,
    checks: Mapping[str, Check] | None = None,
    informational: Collection[str] = (),
    read_only: bool = False,
    readiness: Callable[[], Any] | None = None,
    extra: Callable[[], Any] | None = None,
    build: str | None = None,
    peer: str | None = None,
    path: str = "/health",
    check_ttl_s: float = DEFAULT_CHECK_TTL_S,
    check_timeout_s: float = DEFAULT_CHECK_TIMEOUT_S,
) -> None:
    """Serve :class:`ServiceHealth` at ``GET`` *path* on a Starlette or FastAPI *app*.

    Install it before any route of the app's own on the same path: the first one registered answers.

    * *version*: defaults to the installed version of the package named *service*.
    * *checks*: name → a callable, sync or async, that returns a :class:`DependencyCheck`, a bool (``None`` is ok)
      or raises. A sync one runs in a worker thread. Names in *informational* are reported but never make the
      service ``degraded``.
    * *capabilities*: a list of names, or a map of every capability to whether it is on (for a service whose
      clients already read such a map), or a callable returning either for a service whose capabilities change
      while it runs. If the callable raises, or returns a value that is not valid, the last good value is reported
      (none at first) and a warning is logged once.
    * *readiness*: returns ``"starting"`` while the service is not ready (models loading), else ``None``. While
      starting the route answers ``503`` and runs no checks. It and *extra* are called on every request, on the
      event loop, so they must only read state.
    * *extra*: returns a mapping merged into the body, for the service's own fields and a resident's
      ``ResidentHealth`` fields. One of the contract's own fields is never replaced by it.
    * *build* and *peer*: default to ``ARGUS_BUILD`` and ``ARGUS_PEER`` in this process's environment.

    Raises ``ValueError`` here, at startup, if the identity is not valid, never on a request.
    """
    JSONResponse = _starlette().JSONResponse
    started_at = datetime.now(UTC).replace(microsecond=0)
    unknown = sorted(set(informational) - set(checks or {}))
    if unknown:
        raise ValueError(f"informational names no check: {', '.join(unknown)}")
    cached = [
        _CachedCheck(name, fn, required=name not in informational, ttl_s=check_ttl_s, timeout_s=check_timeout_s)
        for name, fn in (checks or {}).items()
    ]
    fixed: dict[str, Any] = {
        "service_version": SERVICE_VERSION,
        "service": service,
        "version": version or _installed_version(service),
        "started_at": started_at,
        "wire": dict(wire or {}),
        "read_only": read_only,
        "build": build if build is not None else _own(BUILD_ENV),
        "peer": peer if peer is not None else _own(PEER_ENV),
    }
    static_capabilities = () if capabilities is None or callable(capabilities) else _capabilities(capabilities)
    try:  # fail at startup on a bad name, not on every request
        ServiceHealth(**fixed, status="starting", capabilities=static_capabilities)
    except ValidationError as exc:
        fields = ", ".join(sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]}))
        raise ValueError(f"install_health: invalid {fields}") from None
    warned: set[str] = set()

    def warn_once(key: str, message: str, *args: object) -> None:
        if key not in warned:
            warned.add(key)
            _log.warning(message, *args)

    last_capabilities: Any = static_capabilities

    def current_capabilities() -> tuple[str, ...] | dict[str, bool]:
        nonlocal last_capabilities
        if callable(capabilities):
            try:
                value = _capabilities(capabilities())
                ServiceHealth(**fixed, status="starting", capabilities=value)
                last_capabilities = value
            except Exception as exc:  # noqa: BLE001 - like extra: it must not take the route down
                warn_once(
                    "capabilities",
                    "%s health: capabilities failed or is not valid (%s); reporting the last good value",
                    service,
                    type(exc).__name__,
                )
        return last_capabilities

    async def body(*, run_checks: bool) -> tuple[dict[str, Any], bool]:
        status: ServiceStatus = "ok"
        if readiness is not None:
            try:
                state = await _call(readiness)
            except Exception as exc:  # noqa: BLE001 - not knowing whether it is ready is not being ready
                warn_once(
                    "readiness", "%s health: readiness raised %s; reporting starting", service, type(exc).__name__
                )
                state = "starting"
            if state is not None:
                status = "starting"
        results: dict[str, DependencyCheck] = {}
        if run_checks and status != "starting":
            for check, result in zip(cached, await asyncio.gather(*(c.get() for c in cached)), strict=True):
                results[check.name] = result
            if any(not r.ok and r.required for r in results.values()):
                status = "degraded"
        own = {
            **fixed,
            "status": status,
            "checks": results,
            "capabilities": current_capabilities(),
        }
        extras: dict[str, Any] = {}
        if extra is not None:
            try:
                extras = dict(await _call(extra) or {})
            except Exception as exc:  # noqa: BLE001 - the service's own fields must not take the route down
                warn_once("extra", "%s health: extra raised %s; its fields are left out", service, type(exc).__name__)
            taken = sorted(set(extras) & set(own))
            if taken:
                warn_once("taken", "%s health: extra may not set %s; ignored", service, ", ".join(taken))
        if extras:
            try:
                return ServiceHealth(**{**extras, **own}).model_dump(mode="json"), status != "starting"
            except (ValidationError, ValueError, TypeError):  # not valid, or a value JSON cannot carry
                warn_once("invalid", "%s health: extra's fields are not valid JSON values; they are left out", service)
        return ServiceHealth(**own).model_dump(mode="json"), status != "starting"

    async def health(request: Any) -> Any:
        shallow = request.query_params.get("checks", "").strip().lower() in ("0", "false", "no", "off")
        report, ready = await body(run_checks=not shallow)
        return JSONResponse(report, status_code=200 if ready else 503, headers={"Cache-Control": "no-store"})

    app.add_route(path, health, methods=["GET"], include_in_schema=False)
