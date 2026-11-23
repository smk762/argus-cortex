"""ServiceHealth and install_health: one /health shape, cheap to scrape, with nothing private in it."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.testclient import TestClient

from argus_cortex.gpu_wire import ResidentHealth
from argus_cortex.service import (
    BUILD_ENV,
    HEALTH_LABEL,
    METRICS_CONTENT_TYPE,
    PEER_ENV,
    PEER_LABEL,
    PORT_LABEL,
    SERVICE_LABEL,
    SERVICE_VERSION,
    DependencyCheck,
    ServiceHealth,
    build_info_collector,
    build_info_text,
    install_health,
    metrics_route,
    service_health_schema,
)
from argus_cortex.wire import VersionError

ROOT = Path(__file__).resolve().parents[1]
SECRET = "hunter2"


@pytest.fixture(autouse=True)
def _no_identity_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BUILD_ENV, raising=False)
    monkeypatch.delenv(PEER_ENV, raising=False)


def client(**kwargs) -> TestClient:  # noqa: ANN003
    app = Starlette()
    install_health(app, service="argus-test", version="1.2.3", **kwargs)
    return TestClient(app)


# ── the model ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_committed_schema_is_current() -> None:
    committed = (ROOT / "service-health-schema.json").read_text(encoding="utf-8")
    assert committed == service_health_schema(), "service-health-schema.json is stale: run make schema"


def test_the_label_names_are_pinned() -> None:
    """Compose files across the suite and argus-iris's agents carry these strings."""
    assert (PEER_LABEL, SERVICE_LABEL, HEALTH_LABEL, PORT_LABEL) == (
        "argus.peer",
        "argus.service",
        "argus.health",
        "argus.port",
    )


def test_model_defaults_and_refusals() -> None:
    now = datetime.now(UTC)
    health = ServiceHealth(service="argus-lens", version="0.14.2", status="ok", started_at=now)
    assert health.service_version == SERVICE_VERSION and health.ready and health.checks == {} and health.peer is None
    assert not ServiceHealth(service="argus-lens", version="1", status="starting", started_at=now).ready
    for bad in (
        {"service": "Argus Lens"},
        {"status": "down"},  # a process that answers is not down
        {"started_at": datetime(2026, 1, 1)},  # naive
        {"peer": "Nude_Net"},
        {"version": ""},
    ):
        with pytest.raises(ValidationError):
            ServiceHealth(**{"service": "argus-lens", "version": "1", "status": "ok", "started_at": now, **bad})
    with pytest.raises(VersionError):
        ServiceHealth(service="argus-lens", version="1", status="ok", started_at=now, service_version="2.0")


def test_one_body_is_both_a_service_and_a_resident_health() -> None:
    """The two models allow extra fields and share no field name, so a GPU resident serves one body."""
    assert not set(ServiceHealth.model_fields) & set(ResidentHealth.model_fields)
    body = {
        "service": "argus-echo",
        "version": "0.3.0",
        "status": "ok",
        "started_at": "2026-11-23T00:00:00Z",
        "model_state": "gpu",
        "busy": False,
        "idle_s": 4.0,
        "gpu": {"free_vram_mb": 1000},
    }
    assert ServiceHealth.model_validate(body).model_dump()["model_state"] == "gpu"
    assert ResidentHealth.model_validate(body).on_gpu


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        (f"GET http://user:{SECRET}@comfy:8188/ failed", "GET http://comfy:8188/ failed"),
        (f"postgresql://argus:{SECRET}@db/argus refused", "postgresql://db/argus refused"),
        (f"redis://:{SECRET}@cache", "redis://cache"),
        ("line one\n  line two", "line one line two"),
        ("x" * 500, "x" * 200),
        ("   ", None),
    ],
)
def test_a_check_detail_never_carries_url_credentials(detail: str, expected: str | None) -> None:
    check = DependencyCheck(ok=False, detail=detail)
    assert check.detail == expected and SECRET not in check.model_dump_json()


def test_a_reader_accepts_fields_from_a_later_minor() -> None:
    assert DependencyCheck.model_validate({"ok": True, "since": "new in 1.1"}).ok


# ── the route ────────────────────────────────────────────────────────────────────────────────────────────────────


def test_the_body_and_the_installed_version_default() -> None:
    app = FastAPI()
    install_health(app, service="argus-cortex", wire={"lease_version": "1.1"}, capabilities=["a", "b"], read_only=True)
    reply = TestClient(app).get("/health")
    body = reply.json()
    assert reply.status_code == 200 and reply.headers["cache-control"] == "no-store"
    health = ServiceHealth.model_validate(body)
    assert health.status == "ok" and health.version != "0.0.0+unknown" and health.read_only
    assert health.wire == {"lease_version": "1.1"} and health.capabilities == ("a", "b")
    assert body["service_version"] == SERVICE_VERSION and body["build"] is None and body["peer"] is None
    assert TestClient(app).head("/health").status_code == 200
    assert "/health" not in app.openapi()["paths"]


def test_an_unknown_package_reports_an_unknown_version_not_a_crash() -> None:
    app = Starlette()
    install_health(app, service="argus-not-installed")
    assert TestClient(app).get("/health").json()["version"] == "0.0.0+unknown"


def test_build_and_peer_come_from_this_process_never_the_suite_file(tmp_path: Path, monkeypatch) -> None:
    """They describe one container. A suite file names what the whole host shares, so it must not supply them."""
    suite = tmp_path / "suite.env"
    suite.write_text(f"{BUILD_ENV}=from-file\n{PEER_ENV}=from-file\n", encoding="utf-8")
    suite.chmod(0o600)
    monkeypatch.setenv("ARGUS_ENV_FILE", str(suite))
    assert client().get("/health").json()["build"] is None
    monkeypatch.setenv(BUILD_ENV, "abc1234")
    monkeypatch.setenv(PEER_ENV, "nudenet")
    body = client().get("/health").json()
    assert (body["build"], body["peer"]) == ("abc1234", "nudenet")
    assert client(build="tag", peer="ocr").get("/health").json()["peer"] == "ocr"


@pytest.mark.parametrize("bad", [{"service": "Not A Slug"}, {"peer": "Bad_Peer"}, {"informational": ["nope"]}])
def test_a_bad_identity_fails_at_startup(bad: dict) -> None:
    with pytest.raises(ValueError):
        install_health(Starlette(), **{"service": "argus-test", **bad})


def test_checks_decide_ok_or_degraded_and_both_answer_200() -> None:
    async def passing() -> DependencyCheck:
        return DependencyCheck(ok=True, detail="3 models")

    def sync_true() -> bool:
        return True

    state = {"ok": False}
    c = client(
        checks={"comfyui": passing, "store": sync_true, "optional": lambda: state["ok"], "none": lambda: None},
        informational=["optional"],
        check_ttl_s=0,
    )
    reply = c.get("/health")
    body = reply.json()
    assert reply.status_code == 200 and body["status"] == "ok"  # an informational failure changes nothing
    assert body["checks"]["optional"] == {
        "ok": False,
        "detail": None,
        "latency_ms": pytest.approx(0, abs=500),
        "required": False,
    }
    assert body["checks"]["comfyui"]["detail"] == "3 models" and body["checks"]["none"]["ok"]
    assert all(v["latency_ms"] >= 0 for v in body["checks"].values())

    c = client(checks={"store": lambda: False})
    reply = c.get("/health")
    assert reply.status_code == 200 and reply.json()["status"] == "degraded"


def test_a_check_that_raises_reports_its_type_and_never_its_message() -> None:
    def boom() -> bool:
        raise ConnectionError(f"http://admin:{SECRET}@10.0.0.5:8188/internal/path refused")

    reply = client(checks={"comfyui": boom}).get("/health")
    assert reply.json()["checks"]["comfyui"] == {
        "ok": False,
        "detail": "ConnectionError",
        "latency_ms": pytest.approx(0, abs=500),
        "required": True,
    }
    assert SECRET not in reply.text and "10.0.0.5" not in reply.text and reply.json()["status"] == "degraded"


def test_a_slow_check_times_out_instead_of_holding_the_route() -> None:
    async def slow() -> bool:
        await asyncio.sleep(30)
        return True

    body = client(checks={"slow": slow}, check_timeout_s=0.05).get("/health").json()
    assert body["checks"]["slow"]["detail"] == "timeout" and body["status"] == "degraded"


def test_a_blocking_sync_check_runs_off_the_event_loop() -> None:
    seen: list[str] = []

    def blocking() -> bool:
        seen.append(threading.current_thread().name)
        return True

    client(checks={"db": blocking}).get("/health")
    assert seen and seen[0] != threading.main_thread().name and "asyncio" in seen[0]


def test_checks_are_cached_so_a_scrape_does_not_hammer_dependencies() -> None:
    calls = {"n": 0}

    def counted() -> bool:
        calls["n"] += 1
        return True

    c = client(checks={"dep": counted}, check_ttl_s=60)
    for _ in range(5):
        assert c.get("/health").json()["checks"]["dep"]["ok"]
    assert calls["n"] == 1
    c = client(checks={"dep": counted}, check_ttl_s=0)
    c.get("/health"), c.get("/health")
    assert calls["n"] == 3


async def test_concurrent_requests_share_one_refresh_and_stale_is_served_meanwhile() -> None:
    from argus_cortex.service import _CachedCheck  # noqa: PLC0415

    calls = {"n": 0}
    gate = asyncio.Event()

    async def gated() -> bool:
        calls["n"] += 1
        await gate.wait()
        return True

    check = _CachedCheck("dep", gated, required=True, ttl_s=0, timeout_s=5)
    first = [asyncio.ensure_future(check.get()) for _ in range(5)]
    await asyncio.sleep(0.01)
    assert calls["n"] == 1  # five callers, one refresh
    first[0].cancel()  # one caller going away does not cancel it for the rest
    gate.set()
    assert all(r.ok for r in await asyncio.gather(*first[1:]))

    gate.clear()
    refreshing = asyncio.ensure_future(check.get())  # ttl 0: this starts a second refresh, and waits on it
    await asyncio.sleep(0.01)
    assert (await asyncio.wait_for(check.get(), 0.5)).ok and calls["n"] == 2  # others get the last result at once
    gate.set()
    await refreshing


def test_shallow_health_runs_no_checks() -> None:
    """For container healthchecks and one service's check on another: cheap, and two services cannot deadlock."""
    calls = {"n": 0}

    def counted() -> bool:
        calls["n"] += 1
        return False

    c = client(checks={"dep": counted})
    for flag in ("0", "false", "no", "off"):
        reply = c.get(f"/health?checks={flag}")
        assert reply.status_code == 200 and reply.json()["status"] == "ok" and reply.json()["checks"] == {}
    assert calls["n"] == 0
    assert c.get("/health?checks=1").json()["status"] == "degraded" and calls["n"] == 1


def test_starting_is_503_and_runs_no_checks(caplog: pytest.LogCaptureFixture) -> None:
    state = {"ready": False}
    calls = {"n": 0}

    def counted() -> bool:
        calls["n"] += 1
        return True

    c = client(readiness=lambda: None if state["ready"] else "starting", checks={"dep": counted})
    reply = c.get("/health")
    assert reply.status_code == 503 and reply.json()["status"] == "starting" and reply.json()["checks"] == {}
    assert calls["n"] == 0 and c.head("/health").status_code == 503
    state["ready"] = True
    assert c.get("/health").status_code == 200 and calls["n"] == 1

    def broken() -> None:
        raise RuntimeError(SECRET)

    with caplog.at_level(logging.WARNING):
        assert client(readiness=broken).get("/health").status_code == 503  # not knowing is not being ready
    assert "RuntimeError" in caplog.text and SECRET not in caplog.text


def test_capabilities_may_change_while_running() -> None:
    caps = ["tts"]
    c = client(capabilities=lambda: caps)
    assert c.get("/health").json()["capabilities"] == ["tts"]
    caps.append("voice-clone")
    assert c.get("/health").json()["capabilities"] == ["tts", "voice-clone"]


def test_extra_adds_resident_fields_and_never_replaces_the_contracts_own(caplog: pytest.LogCaptureFixture) -> None:
    async def resident() -> dict:
        return {"model_state": "cpu", "busy": True, "idle_s": None, "status": "healthy", "version": "9", "models": 2}

    with caplog.at_level(logging.WARNING):
        c = client(extra=resident)
        body = c.get("/health").json()
        c.get("/health")
    assert (body["status"], body["version"], body["models"]) == ("ok", "1.2.3", 2)
    assert ResidentHealth.model_validate(body).model_state == "cpu"
    assert caplog.text.count("may not set status, version") == 1  # said once, not on every scrape


@pytest.mark.parametrize("kind", ["raises", "not-json", "not-a-mapping"])
def test_a_broken_extra_never_takes_the_route_down(kind: str, caplog: pytest.LogCaptureFixture) -> None:
    def extra() -> object:
        if kind == "raises":
            raise RuntimeError(SECRET)
        return {"thing": object()} if kind == "not-json" else 7

    with caplog.at_level(logging.WARNING):
        reply = client(extra=extra).get("/health")
    assert reply.status_code == 200 and reply.json()["status"] == "ok" and "thing" not in reply.json()
    assert "left out" in caplog.text and SECRET not in caplog.text


# ── build info ───────────────────────────────────────────────────────────────────────────────────────────────────


def test_build_info_text(monkeypatch: pytest.MonkeyPatch) -> None:
    text = build_info_text("argus-lens", version="0.14.2")
    assert text.splitlines()[-1] == 'argus_build_info{service="argus-lens",version="0.14.2",service_version="1.0"} 1'
    assert "# TYPE argus_build_info gauge" in text and text.endswith("\n")
    monkeypatch.setenv(BUILD_ENV, 'sha"1\\2\n3')
    assert 'build="sha\\"1\\\\2\\n3"} 1' in build_info_text("argus-lens", version="1")
    assert 'build="tag"' in build_info_text("argus-lens", version="1", build="tag")


def test_metrics_route_serves_only_the_gauge() -> None:
    app = Starlette()
    metrics_route(app, service="argus-test", version="1.2.3")
    reply = TestClient(app).get("/metrics")
    assert reply.status_code == 200 and reply.headers["content-type"] == METRICS_CONTENT_TYPE
    assert reply.text == build_info_text("argus-test", version="1.2.3")


def test_build_info_collector_matches_the_text() -> None:
    from prometheus_client import CollectorRegistry, generate_latest  # noqa: PLC0415

    registry = CollectorRegistry()
    registry.register(build_info_collector("argus-test", version="1.2.3", build="abc"))
    out = generate_latest(registry).decode()
    sample = next(line for line in out.splitlines() if line.startswith("argus_build_info{"))
    for label in ('service="argus-test"', 'version="1.2.3"', 'service_version="1.0"', 'build="abc"'):
        assert label in sample  # the same labels as the text form; the library sorts them
    assert sample.endswith("} 1.0") and "# TYPE argus_build_info gauge" in out
