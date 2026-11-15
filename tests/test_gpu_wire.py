"""GPU broker wire models: leases, tenant manifests, and their committed schemas."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from argus_cortex.gpu_wire import (
    TARGET_RE,
    LeaseRequest,
    LeaseStatus,
    TenantManifest,
    lease_schema,
    load_tenant_manifest,
    target_model,
    target_service,
    tenant_schema,
)
from argus_cortex.wire import VersionError

ROOT = Path(__file__).resolve().parents[1]

DARKROOM = {
    "tenant": "argus-darkroom",
    "repo": "smk762/argus-darkroom",
    "kinds": [
        {"kind": "generate", "target": "comfyui", "vram_mb": 9000},
        {"kind": "expand", "target": "ollama:qwen3:8b", "vram_mb": 6000, "priority": "interactive"},
    ],
    "residents": [{"name": "singguard", "adapter": "ollama", "target": "ollama:singguard:latest", "pinned": True}],
}


@pytest.mark.parametrize(
    ("filename", "render"), [("gpu-lease-schema.json", lease_schema), ("gpu-tenant-schema.json", tenant_schema)]
)
def test_committed_schema_is_current(filename: str, render) -> None:  # noqa: ANN001
    assert (ROOT / filename).read_text(encoding="utf-8") == render(), f"{filename} is stale: run make schema"


@pytest.mark.parametrize(
    "target", ["comfyui", "in-process", "ollama:qwen3:8b", "ollama:singguard:latest", "argus-lens"]
)
def test_targets_accepted(target: str) -> None:
    assert LeaseRequest(tenant="t", target=target).target == target
    assert TARGET_RE.match(target)


@pytest.mark.parametrize("target", ["", "Ollama", "ollama:", "ollama: x", "-comfy", "comfy ui"])
def test_targets_refused(target: str) -> None:
    with pytest.raises(ValidationError):
        LeaseRequest(tenant="t", target=target)


def test_target_halves() -> None:
    assert target_service("ollama:qwen3:8b") == "ollama"
    assert target_model("ollama:qwen3:8b") == "qwen3:8b"
    assert target_service("comfyui") == "comfyui"
    assert target_model("comfyui") is None


def test_lease_request_defaults_leave_priority_to_the_manifest() -> None:
    req = LeaseRequest(tenant="argus-lens")
    assert req.lease_version == "1.0"
    assert req.priority is None and req.preemptible is None
    assert req.target == "in-process" and req.kind == "default"


def test_lease_times_must_carry_a_timezone() -> None:
    with pytest.raises(ValidationError):
        LeaseRequest(tenant="t", not_before=datetime(2026, 11, 16, 1, 0))  # noqa: DTZ001 - the point of the test


def test_lease_major_mismatch_is_refused() -> None:
    with pytest.raises(VersionError):
        LeaseStatus.model_validate({"lease_version": "2.0", "lease_id": "x", "state": "granted"})


def test_lease_status_ignores_fields_from_a_newer_minor() -> None:
    status = LeaseStatus.model_validate({"lease_version": "1.4", "lease_id": "x", "state": "queued", "eta_s": 3})
    assert status.state == "queued" and not status.terminal


def test_manifest_round_trip_and_kind_lookup() -> None:
    manifest = TenantManifest.model_validate(DARKROOM)
    assert manifest.kind_for("expand").target == "ollama:qwen3:8b"
    assert manifest.kind_for("generate").priority == "interactive"
    assert manifest.kind_for("nope") is None
    assert TenantManifest.model_validate(manifest.model_dump(mode="json")) == manifest


def test_manifest_refuses_typos_duplicates_and_incomplete_residents() -> None:
    with pytest.raises(ValidationError, match="vram"):
        TenantManifest.model_validate({**DARKROOM, "kinds": [{"kind": "g", "target": "comfyui", "vram": 1}]})
    with pytest.raises(ValidationError, match="duplicate"):
        TenantManifest.model_validate({**DARKROOM, "kinds": [DARKROOM["kinds"][0], DARKROOM["kinds"][0]]})
    with pytest.raises(ValidationError, match="container"):
        TenantManifest.model_validate({**DARKROOM, "residents": [{"name": "comfy", "adapter": "container"}]})
    with pytest.raises(VersionError):
        TenantManifest.model_validate({**DARKROOM, "tenant_version": "2.0"})


def test_load_manifest_from_yaml_and_json(tmp_path: Path) -> None:
    yaml = pytest.importorskip("yaml")
    as_yaml = tmp_path / "gpu-tenant.yaml"
    as_yaml.write_text(yaml.safe_dump(DARKROOM), encoding="utf-8")
    as_json = tmp_path / "gpu-tenant.json"
    as_json.write_text(json.dumps(DARKROOM), encoding="utf-8")
    assert load_tenant_manifest(as_yaml) == load_tenant_manifest(as_json) == TenantManifest.model_validate(DARKROOM)


def test_load_manifest_refuses_a_non_mapping(tmp_path: Path) -> None:
    path = tmp_path / "gpu-tenant.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_tenant_manifest(path)


def test_model_mb_splits_the_footprint() -> None:
    req = LeaseRequest(tenant="argus-darkroom", target="comfyui", vram_mb=9000, model_mb=6500)
    assert req.working_mb == 2500
    assert LeaseRequest(tenant="t", vram_mb=900).working_mb == 900  # unset: all working set
    kind = TenantManifest.model_validate(
        {"tenant": "t", "kinds": [{"kind": "g", "target": "comfyui", "vram_mb": 9000, "model_mb": 6500}]}
    ).kind_for("g")
    assert kind.working_mb == 2500


def test_model_mb_cannot_exceed_the_total() -> None:
    with pytest.raises(ValidationError, match="cannot exceed"):
        LeaseRequest(tenant="t", vram_mb=1000, model_mb=1001)
    with pytest.raises(ValidationError, match="cannot exceed"):
        TenantManifest.model_validate({"tenant": "t", "kinds": [{"kind": "g", "vram_mb": 1, "model_mb": 2}]})


VOLUPTAS = {
    "tenant": "voluptas",
    "node": "morpheus",
    "residents": [
        {
            "name": "comfyui",
            "adapter": "comfyui",
            "target": "comfyui",
            "container": "voluptas_comfyui",
            "base_url": "http://morpheus:8188",
            "host_ram_restart_mb": 12288,
        },
        {
            "name": "image-gen-diffusers",
            "adapter": "argus-http",
            "container": "voluptas_image_gen_diffusers",
            "base_url": "http://morpheus:8011",
            "unload_path": "/admin/unload",
            "warm_path": "/admin/wake",
            "admin_auth": True,
        },
        {
            "name": "miso",
            "adapter": "argus-http",
            "container": "voluptas_miso",
            "base_url": "http://morpheus:8034",
            "unload_path": "/admin/unload",
            "evict_path": "/admin/evict",
            "warm_path": "/admin/wake",
        },
    ],
}


def test_resident_contract_matches_the_merged_services() -> None:
    manifest = TenantManifest.model_validate(VOLUPTAS)
    comfy, diffusers, miso = manifest.residents
    assert comfy.host_ram_restart_mb == 12288 and comfy.container == "voluptas_comfyui"
    assert diffusers.health_path == "/health" and diffusers.admin_auth and diffusers.evict_path is None
    assert miso.evict_path == "/admin/evict" and not miso.admin_auth


@pytest.mark.parametrize("field", ["health_path", "unload_path", "evict_path", "warm_path"])
@pytest.mark.parametrize("path", ["admin/unload", "http://x/admin", "/admin unload", ""])
def test_resident_routes_are_absolute_paths(field: str, path: str) -> None:
    resident = {"name": "svc", "adapter": "argus-http", field: path}
    with pytest.raises(ValidationError):
        TenantManifest.model_validate({"tenant": "t", "residents": [resident]})


def test_resident_refuses_informational_fields() -> None:
    resident = {"name": "nudenet", "adapter": "argus-http", "gpu": False}  # manifests list GPU residents only
    with pytest.raises(ValidationError, match="gpu"):
        TenantManifest.model_validate({"tenant": "t", "residents": [resident]})
