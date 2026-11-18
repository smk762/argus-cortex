"""Wire models for the suite's GPU broker (argus-iris): leases, tenant manifests and resident replies.

Three contracts, each versioned with :mod:`argus_cortex.wire` and committed as a JSON Schema so non-Python clients can
generate types from them:

* **Lease** (``lease_version``, ``gpu-lease-schema.json``): what a caller and the broker exchange over HTTP.
  :class:`LeaseRequest` asks for VRAM, :class:`LeaseStatus` answers (queued, granted, refused...), :class:`Heartbeat`
  and :class:`HeartbeatReply` keep a granted lease alive and carry the broker's request to yield, and
  :class:`LeaseRelease` gives it back with what was observed.
* **Tenant manifest** (``tenant_version``, ``gpu-tenant-schema.json``): the ``gpu-tenant.yaml`` every repo that uses a
  GPU commits, saying what work it does, on which target, at what priority and VRAM, and which models it keeps
  resident. A service registers it with the broker on startup.
* **Resident replies** (``resident_version``, ``gpu-resident-schema.json``): what a resident's routes return to the
  broker's argus-http adapter. :class:`ResidentHealth` is the ``GET /health`` body, :class:`ResidentActionReply`
  answers ``POST /admin/unload|evict|wake``, and :class:`ResidentBusyReply` is the 409 body for "busy, retry later".

The HTTP routes are the broker's (argus-iris); the client is :class:`argus_cortex.gpu_broker.BrokerCoordinator`.
Times on the wire are timezone-aware and compared in UTC. Pydantic only: no httpx, no YAML parser at import time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from argus_cortex.wire import make_versioned_base, render_schema

LEASE_VERSION = "1.1"
LEASE_MAJORS = ("1",)
TENANT_VERSION = "1.0"
TENANT_MAJORS = ("1",)
RESIDENT_VERSION = "1.0"
RESIDENT_MAJORS = ("1",)

LEASE_SCHEMA_TITLE = "Argus GPU lease"
TENANT_SCHEMA_TITLE = "Argus GPU tenant manifest"
RESIDENT_SCHEMA_TITLE = "Argus GPU resident"

PriorityClass = Literal["realtime", "interactive", "batch", "background"]
"""Highest first. ``realtime`` is never asked to yield; ``batch`` and ``background`` may be, cooperatively."""
PRIORITY_CLASSES: tuple[str, ...] = get_args(PriorityClass)

LeaseState = Literal["queued", "granted", "refused", "expired", "released"]
"""``queued`` waits; ``granted`` may run; the other three are terminal."""
LEASE_STATES: tuple[str, ...] = get_args(LeaseState)
TERMINAL_STATES: frozenset[str] = frozenset({"refused", "expired", "released"})

ReleaseOutcome = Literal["ok", "failed", "oom", "cancelled"]
"""How the work under a lease ended. ``oom`` feeds the broker's footprint learning; ``cancelled`` gives up a queued lease."""
RELEASE_OUTCOMES: tuple[str, ...] = get_args(ReleaseOutcome)

AdapterKind = Literal["comfyui", "ollama", "argus-http", "immich", "container"]
"""How the broker reads and evicts a resident model (see argus-iris DESIGN.md, "Adapters")."""
ADAPTER_KINDS: tuple[str, ...] = get_args(AdapterKind)

ResidentState = Literal["unloaded", "loading", "cpu", "gpu", "error"]
"""Where a resident's model is: not loaded; loading; warm in host RAM (evicted, or a CPU-only stage); on the GPU; or
failed to load."""
RESIDENT_STATES: tuple[str, ...] = get_args(ResidentState)

IN_PROCESS = "in-process"
TARGET_PATTERN = r"^[a-z][a-z0-9-]*(:\S+)?$"
"""``<service>`` or ``<service>:<model>``: ``comfyui``, ``ollama:qwen3:8b``, ``in-process``."""
TARGET_RE = re.compile(TARGET_PATTERN)
_SLUG_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"

Target = Annotated[str, Field(pattern=TARGET_PATTERN, max_length=200)]
Slug = Annotated[str, Field(pattern=_SLUG_PATTERN, max_length=100)]
Megabytes = Annotated[int, Field(ge=0)]
RoutePath = Annotated[str, Field(pattern=r"^/\S*$", max_length=200)]


def target_service(target: str) -> str:
    """The service half of a target: ``'ollama:qwen3:8b' -> 'ollama'``."""
    return target.split(":", 1)[0]


def target_model(target: str) -> str | None:
    """The model half of a target, or ``None``: ``'ollama:qwen3:8b' -> 'qwen3:8b'``."""
    _, sep, model = target.partition(":")
    return model if sep else None


def _check_model_mb(model: Any) -> Any:
    model_mb, vram_mb = getattr(model, "model_mb", None), getattr(model, "vram_mb", 0)
    if model_mb is not None and model_mb > vram_mb:
        raise ValueError(f"model_mb ({model_mb}) cannot exceed vram_mb ({vram_mb}), the whole footprint")
    return model


class GpuConstraints(BaseModel):
    """Where a lease may be placed, beyond fitting its VRAM. Unset fields do not constrain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: Slug | None = Field(None, description="Only this node (e.g. a process pinned to its host's GPU).")
    min_cc: float | None = Field(None, ge=0, description="Minimum CUDA compute capability, e.g. 7.0 (no Pascal).")
    bf16: bool | None = Field(None, description="True: only a GPU with native bf16.")


# --- lease -------------------------------------------------------------------------------------------------------

LeaseVersioned = make_versioned_base(
    "lease_version", LEASE_VERSION, LEASE_MAJORS, label="GPU lease", class_name="LeaseVersioned"
)


class LeaseRequest(LeaseVersioned):
    """``POST /leases``: ask for *vram_mb* on a node that serves *target*.

    ``priority`` and ``preemptible`` left unset take the tenant manifest's default for *kind*. ``resume`` re-attaches
    a lease whose work is already running (the broker restarted and lost it): the broker accounts for it at once
    instead of queueing it.
    """

    tenant: Slug
    caller: str = Field("", max_length=200, description="Free-form detail for logs and the dashboard.")
    kind: Slug = "default"
    target: Target = IN_PROCESS
    vram_mb: Megabytes = Field(0, description="The whole footprint: model weights plus the working set.")
    model_mb: Megabytes | None = Field(
        None,
        description="The part of vram_mb that is model weights the service keeps loaded after the lease (a ComfyUI "
        "checkpoint, an Ollama model). The rest, vram_mb - model_mb, is the working set held only while the job runs. "
        "Unset: the broker treats all of vram_mb as working set.",
    )
    priority: PriorityClass | None = None
    preemptible: bool | None = None
    constraints: GpuConstraints = GpuConstraints()
    request_id: str | None = Field(
        None,
        max_length=100,
        description="Client-generated id that makes a retried POST idempotent: the broker answers a repeated "
        "(tenant, request_id) with the lease it already made.",
    )
    ttl_s: float = Field(60.0, gt=0, le=3600, description="The lease expires this long after the last heartbeat.")
    wait_s: float = Field(0.0, ge=0, le=300, description="How long the broker may hold this request before answering.")
    not_before: AwareDatetime | None = None
    deadline: AwareDatetime | None = Field(None, description="Refuse rather than grant after this time.")
    resume: str | None = Field(None, max_length=100, description="The id of a lease whose work is already running.")

    @model_validator(mode="after")
    def _model_within_total(self) -> LeaseRequest:
        return _check_model_mb(self)

    @property
    def working_mb(self) -> int:
        """The working set: ``vram_mb - model_mb``, or all of ``vram_mb`` when ``model_mb`` is unset."""
        return self.vram_mb - (self.model_mb or 0)


class LeaseStatus(LeaseVersioned):
    """The broker's answer to a request or a status poll (``GET /leases/{id}``)."""

    lease_id: str = Field(max_length=100)
    state: LeaseState
    node: Slug | None = None
    device: int | None = Field(None, ge=0, description="The NVML index of the GPU on *node*.")
    base_url: str | None = Field(None, description="For a service target: the URL to call on the granted node.")
    vram_mb: Megabytes = 0
    ttl_s: float = Field(60.0, gt=0)
    position: int | None = Field(None, ge=0, description="While queued: how many leases are ahead.")
    reason: str | None = Field(None, description="Why it was refused or expired, or what it is waiting for.")
    expires_at: AwareDatetime | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES


class Heartbeat(LeaseVersioned):
    """``POST /leases/{id}/heartbeat``: keep a granted lease alive."""

    peak_vram_mb: Megabytes | None = Field(None, description="Peak VRAM the caller saw since the last heartbeat.")
    progress: float | None = Field(
        None,
        ge=0,
        le=1,
        description="How much of the work is done, 0 to 1, if the caller can tell. Shown, not acted on.",
    )
    stage: str | None = Field(None, max_length=200, description="What the work is doing now, e.g. 'checkpoint 3/5'.")


class HeartbeatReply(LeaseVersioned):
    """The broker's answer to a heartbeat. ``yield_requested``: release at the next safe point."""

    state: LeaseState
    yield_requested: bool = False
    ttl_s: float = Field(60.0, gt=0)
    reason: str | None = None


class LeaseRelease(LeaseVersioned):
    """``POST /leases/{id}/release``: give a lease back (or give up a queued one, with ``cancelled``)."""

    outcome: ReleaseOutcome = "ok"
    peak_vram_mb: Megabytes | None = None
    detail: str | None = Field(None, max_length=2000)


LEASE_MODELS: tuple[type[BaseModel], ...] = (LeaseRequest, LeaseStatus, Heartbeat, HeartbeatReply, LeaseRelease)


def lease_schema() -> str:
    """The canonical JSON Schema for the lease models, as committed in ``gpu-lease-schema.json``.

    ``tests/test_gpu_wire.py`` fails if the committed file drifts; regenerate it with ``make schema``.
    """
    return render_schema(LEASE_MODELS, title=LEASE_SCHEMA_TITLE)


# --- tenant manifest ---------------------------------------------------------------------------------------------

TenantVersioned = make_versioned_base(
    "tenant_version", TENANT_VERSION, TENANT_MAJORS, label="GPU tenant manifest", class_name="TenantVersioned"
)


class TenantKind(BaseModel):
    """One kind of GPU work a tenant does, and its defaults when a lease does not say."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Slug
    target: Target = IN_PROCESS
    vram_mb: Megabytes = Field(
        description="Declared footprint, weights plus working set. The broker uses its learned p95 when larger."
    )
    model_mb: Megabytes | None = Field(None, description="The part of vram_mb that stays loaded after a lease.")
    priority: PriorityClass = "interactive"
    preemptible: bool = False
    constraints: GpuConstraints = GpuConstraints()
    description: str = ""

    @model_validator(mode="after")
    def _model_within_total(self) -> TenantKind:
        return _check_model_mb(self)

    @property
    def working_mb(self) -> int:
        """The working set: ``vram_mb - model_mb``, or all of ``vram_mb`` when ``model_mb`` is unset."""
        return self.vram_mb - (self.model_mb or 0)


class TenantResident(BaseModel):
    """A model a service keeps loaded between leases, how the broker reaches it, and hints for keeping or evicting it.

    A manifest lists GPU residents only. The comfyui and ollama adapters use their services' own routes (``/free``,
    ``keep_alive``); the ``*_path`` fields are for the argus-http adapter, joined to ``base_url``, and called with
    POST. The verbs are distinct: **unload** frees VRAM and host RAM; **evict** moves the model from GPU to CPU,
    for services with a CPU stage (TTS, embedders), freeing VRAM but keeping it warm in RAM; **warm** loads it
    back. ``admin_auth`` says only whether those routes need ``Authorization: Bearer $ARGUS_ADMIN_TOKEN``; the token
    lives in the broker's config, never in a committed manifest. The operator's policy file on the broker overrides
    the hints.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Slug
    adapter: AdapterKind
    target: Target = IN_PROCESS
    vram_mb: Megabytes | None = None
    pinned: bool = Field(False, description="Hint: never evict (e.g. SingGuard's model, a2f during a broadcast).")
    idle_kill_s: float | None = Field(None, ge=0, description="Hint: evict after this long without a lease.")
    base_url: str | None = Field(
        None,
        description="Where the adapter reaches the service, if not the tenant's own: an http(s) URL with a host and "
        "no credentials, query or fragment. A broker sends its admin token only to hosts its operator configured, never "
        "on the strength of this field alone.",
    )
    container: str | None = Field(
        None,
        max_length=200,
        description="The compose container name. The broker's node agent matches GPU processes to this resident by "
        "it, for every adapter, and starts it to warm a stopped service (e.g. ComfyUI).",
    )
    health_path: RoutePath = Field(
        "/health", description="argus-http: status route, answering a ResidentHealth (model state, busy, idle)."
    )
    unload_path: RoutePath | None = Field(None, description="argus-http: free VRAM and host RAM, e.g. /admin/unload.")
    evict_path: RoutePath | None = Field(
        None, description="argus-http: move the model from GPU to CPU, keeping it warm."
    )
    warm_path: RoutePath | None = Field(None, description="argus-http: load the model back, e.g. /admin/wake.")
    admin_auth: bool = Field(False, description="The admin routes need Authorization: Bearer $ARGUS_ADMIN_TOKEN.")
    host_ram_restart_mb: Megabytes | None = Field(
        None,
        description="Restart the container when it is idle and its cgroup anonymous memory exceeds this "
        "(ComfyUI keeps offloaded weights in host RAM; voluptas#16).",
    )

    @field_validator("base_url")
    @classmethod
    def _http_base_url(cls, url: str | None) -> str | None:
        """An http(s) URL to a host, nothing more: the adapter joins ``*_path`` to it and may send the admin token.

        Whether that host may receive the token is the broker's call (an operator allow-list); this only rules out
        what is never a service URL (``file:``, a bare ``host:port``) and what would carry or mangle a credential.
        """
        if url is None:
            return None
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - raises ValueError on a malformed port
        except ValueError as exc:
            raise ValueError(f"base_url is not a URL: {exc}") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("base_url must be an http:// or https:// URL with a host")
        if parts.username is not None or parts.password is not None:
            raise ValueError("base_url must not carry credentials; the broker holds the admin token")
        if parts.query or parts.fragment or "?" in url or "#" in url:
            raise ValueError("base_url must not have a query or fragment: route paths are joined to it")
        return url


class TenantManifest(TenantVersioned):
    """A repo's ``gpu-tenant.yaml``: the GPU work it does and the models it keeps resident."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant: Slug
    description: str = ""
    repo: str | None = Field(None, description="e.g. smk762/argus-darkroom")
    node: Slug | None = Field(None, description="The node this tenant's own process runs on, if pinned to one.")
    kinds: tuple[TenantKind, ...] = ()
    residents: tuple[TenantResident, ...] = ()

    @field_validator("kinds", "residents")
    @classmethod
    def _unique_names(cls, items: tuple[TenantKind, ...] | tuple[TenantResident, ...]) -> tuple:
        names = [getattr(item, "kind", None) or getattr(item, "name", None) for item in items]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate names: {', '.join(dupes)}")
        return items

    @model_validator(mode="after")
    def _resident_needs_its_adapter_fields(self) -> TenantManifest:
        for r in self.residents:
            if r.adapter == "container" and not r.container:
                raise ValueError(f"resident {r.name!r}: the container adapter needs 'container'")
        return self

    def kind_for(self, kind: str) -> TenantKind | None:
        """The declared kind named *kind*, or ``None``."""
        return next((k for k in self.kinds if k.kind == kind), None)


def tenant_schema() -> str:
    """The canonical JSON Schema for :class:`TenantManifest`, as committed in ``gpu-tenant-schema.json``.

    Non-Python repos validate their ``gpu-tenant.yaml`` against that file. ``tests/test_gpu_wire.py`` fails if it
    drifts; regenerate it with ``make schema``.
    """
    return render_schema([TenantManifest], title=TENANT_SCHEMA_TITLE)


def load_tenant_manifest(path: str | Path) -> TenantManifest:
    """Read and validate a ``gpu-tenant.yaml`` (or ``.json``).

    YAML needs PyYAML (the ``[yaml]`` extra), imported only here; JSON needs nothing.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        data = json.loads(text)
    else:
        try:
            import yaml  # noqa: PLC0415 - optional, behind the [yaml] extra
        except ImportError as exc:
            raise RuntimeError(f"reading {p.name} needs PyYAML: install argus-cortex[yaml], or use JSON") from exc
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{p}: a tenant manifest must be a mapping")
    return TenantManifest.model_validate(data)


# --- resident replies --------------------------------------------------------------------------------------------

ResidentVersioned = make_versioned_base(
    "resident_version", RESIDENT_VERSION, RESIDENT_MAJORS, label="GPU resident reply", class_name="ResidentVersioned"
)

MemoryMb = Annotated[float, Field(ge=0)]


class ResidentGpu(BaseModel):
    """The ``gpu`` block of :class:`ResidentHealth`: memory only. A field the service cannot measure is left out.

    The model's state lives at the top level, not here. ``extra="allow"``, so a field a service still sends during a
    migration (argus-lens's ``gpu.loaded``) is kept rather than refused.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    free_vram_mb: MemoryMb | None = Field(None, description="Free VRAM on the device, across every process.")
    allocated_mb: MemoryMb | None = Field(None, description="VRAM this process's tensors occupy.")
    reserved_mb: MemoryMb | None = Field(None, description="VRAM this process's allocator holds, allocated or cached.")
    total_mb: MemoryMb | None = Field(None, description="The device's total VRAM.")


class ResidentHealth(ResidentVersioned):
    """A resident's ``GET /health`` body, which the broker's argus-http adapter reads.

    ``extra="allow"``: a service keeps its own fields (``status``, ``version``, per-model detail) alongside these.
    A service that has not moved to this shape fails validation on ``model_state`` and ``busy``, which is the point:
    the broker reads one shape. ``resident_version`` defaults to 1.0, so a service need not send it.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    model_state: ResidentState
    busy: bool = Field(description="Work is running now. An unload sent while busy is answered 409 or waits.")
    idle_s: float | None = Field(
        None,
        ge=0,
        description="Seconds since the last work finished. Null while busy or unloaded, and when the service does "
        "not track it.",
    )
    gpu: ResidentGpu | None = Field(None, description="Memory figures; omitted when the service has no CUDA device.")

    @property
    def on_gpu(self) -> bool:
        """The model holds VRAM now (``model_state == 'gpu'``)."""
        return self.model_state == "gpu"


class ResidentActionReply(ResidentVersioned):
    """The 200 body of ``POST /admin/unload``, ``/admin/evict`` and ``/admin/wake``.

    ``model_state`` is the state after the action. ``unloaded`` says whether the call freed anything: false when the
    model was already unloaded (or already on the CPU, for evict), null on wake. A service with several models lists
    them under a field of its own, never in ``unloaded``. While loading or unavailable a route answers 503; while busy,
    409 with a :class:`ResidentBusyReply`.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    status: str = Field("ok", max_length=200)
    model_state: ResidentState
    unloaded: bool | None = Field(None, description="True when the call freed memory; null when it does not apply.")


class ResidentBusyReply(ResidentVersioned):
    """The 409 body an admin route returns while work is running: nothing was done; retry after the work finishes.

    A service may instead wait for the work and then act; it never interrupts it.
    """

    model_config = ConfigDict(extra="allow", frozen=True)

    status: Literal["busy"] = "busy"
    active: int = Field(ge=0, description="How many requests are running.")


RESIDENT_MODELS: tuple[type[BaseModel], ...] = (ResidentHealth, ResidentActionReply, ResidentBusyReply)


def resident_schema() -> str:
    """The canonical JSON Schema for the resident replies, as committed in ``gpu-resident-schema.json``.

    ``tests/test_gpu_wire.py`` fails if the committed file drifts; regenerate it with ``make schema``.
    """
    return render_schema(RESIDENT_MODELS, title=RESIDENT_SCHEMA_TITLE)


__all__ = [
    "ADAPTER_KINDS",
    "IN_PROCESS",
    "LEASE_MAJORS",
    "LEASE_MODELS",
    "LEASE_SCHEMA_TITLE",
    "LEASE_STATES",
    "LEASE_VERSION",
    "PRIORITY_CLASSES",
    "RELEASE_OUTCOMES",
    "RESIDENT_MAJORS",
    "RESIDENT_MODELS",
    "RESIDENT_SCHEMA_TITLE",
    "RESIDENT_STATES",
    "RESIDENT_VERSION",
    "TARGET_PATTERN",
    "TARGET_RE",
    "TENANT_MAJORS",
    "TENANT_SCHEMA_TITLE",
    "TENANT_VERSION",
    "TERMINAL_STATES",
    "AdapterKind",
    "GpuConstraints",
    "Heartbeat",
    "HeartbeatReply",
    "LeaseRelease",
    "LeaseRequest",
    "LeaseState",
    "LeaseStatus",
    "PriorityClass",
    "ReleaseOutcome",
    "ResidentActionReply",
    "ResidentBusyReply",
    "ResidentGpu",
    "ResidentHealth",
    "ResidentState",
    "TenantKind",
    "TenantManifest",
    "TenantResident",
    "lease_schema",
    "load_tenant_manifest",
    "resident_schema",
    "target_model",
    "target_service",
    "tenant_schema",
]
