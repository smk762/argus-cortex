"""Wire models for the suite's GPU broker (argus-iris): leases and tenant manifests.

Two contracts, each versioned with :mod:`argus_cortex.wire` and committed as a JSON Schema so non-Python clients can
generate types from them:

* **Lease** (``lease_version``, ``gpu-lease-schema.json``): what a caller and the broker exchange over HTTP.
  :class:`LeaseRequest` asks for VRAM, :class:`LeaseStatus` answers (queued, granted, refused...), :class:`Heartbeat`
  and :class:`HeartbeatReply` keep a granted lease alive and carry the broker's request to yield, and
  :class:`LeaseRelease` gives it back with what was observed.
* **Tenant manifest** (``tenant_version``, ``gpu-tenant-schema.json``): the ``gpu-tenant.yaml`` every repo that uses a
  GPU commits, saying what work it does, on which target, at what priority and VRAM, and which models it keeps
  resident. A service registers it with the broker on startup.

The HTTP routes are the broker's (argus-iris); the client is :class:`argus_cortex.gpu_broker.BrokerCoordinator`.
Times on the wire are timezone-aware and compared in UTC. Pydantic only: no httpx, no YAML parser at import time.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Annotated, Literal, get_args

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from argus_cortex.wire import make_versioned_base, render_schema

LEASE_VERSION = "1.0"
LEASE_MAJORS = ("1",)
TENANT_VERSION = "1.0"
TENANT_MAJORS = ("1",)

LEASE_SCHEMA_TITLE = "Argus GPU lease"
TENANT_SCHEMA_TITLE = "Argus GPU tenant manifest"

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

IN_PROCESS = "in-process"
TARGET_PATTERN = r"^[a-z][a-z0-9-]*(:\S+)?$"
"""``<service>`` or ``<service>:<model>``: ``comfyui``, ``ollama:qwen3:8b``, ``in-process``."""
TARGET_RE = re.compile(TARGET_PATTERN)
_SLUG_PATTERN = r"^[a-z0-9][a-z0-9._-]*$"

Target = Annotated[str, Field(pattern=TARGET_PATTERN, max_length=200)]
Slug = Annotated[str, Field(pattern=_SLUG_PATTERN, max_length=100)]
Megabytes = Annotated[int, Field(ge=0)]


def target_service(target: str) -> str:
    """The service half of a target: ``'ollama:qwen3:8b' -> 'ollama'``."""
    return target.split(":", 1)[0]


def target_model(target: str) -> str | None:
    """The model half of a target, or ``None``: ``'ollama:qwen3:8b' -> 'qwen3:8b'``."""
    _, sep, model = target.partition(":")
    return model if sep else None


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
    vram_mb: Megabytes = 0
    priority: PriorityClass | None = None
    preemptible: bool | None = None
    constraints: GpuConstraints = GpuConstraints()
    ttl_s: float = Field(60.0, gt=0, le=3600, description="The lease expires this long after the last heartbeat.")
    wait_s: float = Field(0.0, ge=0, le=300, description="How long the broker may hold this request before answering.")
    not_before: AwareDatetime | None = None
    deadline: AwareDatetime | None = Field(None, description="Refuse rather than grant after this time.")
    resume: str | None = Field(None, max_length=100, description="The id of a lease whose work is already running.")


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
    vram_mb: Megabytes = Field(description="Declared footprint. The broker uses its learned p95 when that is larger.")
    priority: PriorityClass = "interactive"
    preemptible: bool = False
    constraints: GpuConstraints = GpuConstraints()
    description: str = ""


class TenantResident(BaseModel):
    """A model the tenant keeps loaded between leases, and the hints for keeping or evicting it.

    The operator's policy file on the broker overrides these hints.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Slug
    adapter: AdapterKind
    target: Target = IN_PROCESS
    vram_mb: Megabytes | None = None
    pinned: bool = Field(False, description="Hint: never evict (e.g. SingGuard's model, a2f during a broadcast).")
    idle_kill_s: float | None = Field(None, ge=0, description="Hint: evict after this long without a lease.")
    base_url: str | None = Field(None, description="Where the adapter reaches the service, if not the tenant's own.")
    unload_path: str | None = Field(None, description="argus-http adapter: the unload route, e.g. /admin/unload.")
    container: str | None = Field(None, description="container adapter: the compose container name.")


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
    "TenantKind",
    "TenantManifest",
    "TenantResident",
    "lease_schema",
    "load_tenant_manifest",
    "target_model",
    "target_service",
    "tenant_schema",
]
