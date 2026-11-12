# CLAUDE.md — argus-cortex

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

## What this is

The **shared foundation library** of the Argus suite. It holds the code the stages *share* so it lives in one place instead of copy-pasted per repo: the target taxonomy, the wire-schema versioning discipline, the local/remote AI-backend contract, and the optional stateful-services layer.

Naming: the working directory is `argus-core`, but the GitHub repo, the PyPI package, and the import name are all **`argus-cortex`** (`src/argus_cortex/`). Don't "fix" the mismatch.

Because everything here is imported by the downstream stages, **a change ripples across the whole suite** — treat this repo as an API, not a leaf. Read [README.md](README.md) for the *why* and worked examples; it is the source of truth for behaviour.

## Argus suite
<!-- argus-suite: keep this block; argus-ci checks for it -->
Cortex owns the suite map, [SUITE.md](SUITE.md), which every repo's CLAUDE.md imports. **Keep it accurate:** a cortex
release that adds or changes a shared module updates its "Need it? Use it" row, and PRs from other repos that update
their own rows land here. Keep it short, because it is loaded into every session in every repo.

@SUITE.md

## Layout

`src/argus_cortex/` (top level is pydantic-only; everything heavier is behind an extra and lazily imported):

- `taxonomy.py` — the "moat" types every stage inherits verbatim: `TargetProfile`, `TargetStyle`/`TargetCategory` literals, and `taxonomy_schema()` (the source of the committed `taxonomy-schema.json`). Keep it dependency-light; a change here retrains everyone's expectations.
- `provenance.py` — `Provenance`/`ProvenanceKind`/`CLEARED_KINDS` for face, voice and image sources, hoisted from argus-mask (#19/#57): `personal` (never cleared) vs cleared `stock`/`synthetic`/`consented` (licence required; `consented` needs `subject` + `release_sha256`); `curated` reserved. **argus-mask owns the meaning of each field — change it only in step with mask.** `provenance_schema()` renders the committed `provenance-schema.json` (`make schema`; drift-tested).
- `wire.py` — the versioned wire-schema toolkit consumers build their contracts on: `make_versioned_base(field_name, current, majors)` (a Pydantic base that stamps + version-checks a field like `proof_version`/`manifest_version`), `check_version()` (the major-compatibility gate), `render_schema()`/`wire_schema()` (the canonical committed-schema string behind each consumer's `schema --check` CLI), and `VersionError`.
- `backends.py` — the AI-backend contract: `Backend`/`LocalBackend`/`RemoteBackend` (point at a hosted service via `from_host(host, port)` or `from_provider(RemoteProvider.…)`), plus `with_retries()` and `resolve_device()`. httpx is lazy, behind `[remote]`.
- `server.py` — the shared FastAPI/ASGI scaffolding other repos' micro-servers mount: `WriteGuard` (pure-ASGI method gate), the `constant_refuse()` (read-only/replay) and `cross_site_refuse()` (cross-origin write) predicates, `env_flag()`, and the `UNSAFE_METHODS`/`TRUTHY`/`FALSY` constants. Starlette is behind `[server]`.
- `gpu.py` — GPU tenancy (from argus-lens): the `GpuCoordinator` protocol (`lease(caller=, min_vram_mb=)`), `NullCoordinator`, `LeaseCoordinator` (host-wide `flock` **then** a wait for NVML free VRAM, so tenants outside the lock like ComfyUI and Ollama are accounted for), `free_vram_mb()`, `coordinator_from_env()` (`ARGUS_GPU_*` — host-wide names on purpose: every process must agree). NVML lazily, behind `[gpu]`; a probe that cannot run grants the lease. **The lock file must be on a volume every GPU container mounts** — per-container `/tmp` coordinates nothing.
- `xmp.py` — the suite's XMP sidecar read/merge/write (`write_xmp`/`read_xmp`/`merge_xmp`/`parse_xmp`, `XmpData`/`XmpRegions`, `XmpError`), used by lens (keywords/caption) and curator (faces, content rating). Stdlib `xml.etree` only. **Writes merge — latest wins per property, foreign properties preserved; keep that.** `None` = leave untouched, empty = remove. A DTD is refused and an unparseable sidecar raises rather than being overwritten. The `argus` namespace URI is a wire contract — don't change it.
- `moderation/` — content-moderation clients over HTTP: `nudenet.py` (`NudeNetClient`, `NsfwResult`, `DEFAULT_EXPOSED_LABELS` — **the suite's one definition of nudity; don't widen it casually**, every stage's NSFW numbers move with it), `singguard.py` (`SingGuardClient` incl. text-only `check_text()`, `GuardResult`, `rule_matches`), `policies.py` (the named policy texts, the per-policy illegal-rule table and `mode()`: `default`/`sfw-*` are SFW — **a policy's text and its illegal letters change together**), `_http.py` (shared POST, proxy-free opener, `CheckFailed`/`ModerationConfigError`). Stdlib + pydantic; Pillow lazily, behind `[images]`. Never import NudeNet (AGPL) — HTTP only. URLs are passed in; stages own their env config.
- `store/` — the optional stateful-services layer, opt-in per `CORTEX_*` env var: `lineage.py` (Phase 1 Postgres DAG, `open_lineage_store()`), `vector.py` (Phase 2 Qdrant, `open_vector_store()`), `blob.py` (Phase 3 MinIO/S3, `open_blob_store()`), over `config.py`/`models.py`/`errors.py`. Each `open_*()` returns a `Null*` no-op when its URL is unset. Drivers are behind `[postgres]`/`[qdrant]`/`[s3]`.

There is **no CLI in this repo**. The one committed schema is `taxonomy-schema.json` (from `taxonomy_schema()`; `make schema` regenerates it and `tests/test_taxonomy.py` fails when it is stale). Studio generates its taxonomy types from it, so a `taxonomy.py` change needs `make schema` in the same commit.

## Commands

```bash
make install   # uv venv + editable install with [dev]
make test      # uv run --no-sync pytest --tb=short -q
make lint      # ruff check + format --check (pinned ruff 0.15.16)
make format    # ruff format + check --fix
```

Run one test: `uv run --no-sync pytest tests/test_wire.py::test_name -q`.

## Conventions & gotchas

- **This is shared code — changes ripple.** A `taxonomy.py` edit or a `wire.py`/`backends.py` semantics change lands in every stage on their next bump. Prefer additive, backward-compatible changes; if you break the wire contract, that is a **major** version (`check_version` is what refuses the old major downstream).
- **Keep the top-level import pydantic-only.** `import argus_cortex` must not pull in starlette/httpx/psycopg/qdrant/minio — those are optional extras, imported lazily inside the module that needs them (see the `try/except ImportError` → "install `argus-cortex[extra]`" pattern). Don't hoist an optional import to module top level.
- **Stores fail *open* to a no-op, not an error.** `open_*()` returns a `Null*` store when the `CORTEX_*` URL is unset, so a stage with no DB configured still runs. Preserve that; blob identity is content-addressed on sha256 (`content_key()` == `source_asset.sha256`) — storage, dedup, and lineage join share one identity.
- **`WriteGuard` is pure ASGI on purpose** (not `BaseHTTPMiddleware`), so it never buffers the SSE/NDJSON streams downstream servers return. Register it *before* `CORSMiddleware`. `env_flag()` warns on a mistyped protection flag rather than silently disabling it — keep that fail-safe behaviour.
- **Versioning is git-tag-derived** (`hatch-vcs`). Never hand-edit a version; `src/argus_cortex/_version.py` is generated (gitignored). Tag `vX.Y.Z` to release.
- Pydantic v2 everywhere; async tests via `asyncio_mode = auto`. Ruff pinned **0.15.16**, line-length 120, target py311.

## CI / release

CI runs via the shared [`argus-ci`](https://github.com/smk762/argus-ci) reusable workflow (ruff + pytest with the `dev` extra — which installs the real psycopg/qdrant/minio/starlette clients so each store's live driver path is exercised, not just fakes). Release publishes to PyPI (OIDC trusted publishing) on `v*` tags — **no container image, this is a library**. Scaffolded from [`argus-pkg-template`](https://github.com/smk762/argus-pkg-template); run `copier update` to pull tooling changes.
