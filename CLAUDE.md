# CLAUDE.md — argus-cortex

Guidance for AI agents working in this repo. Human-facing usage lives in [README.md](README.md); this file is the orientation an agent needs to change code safely.

## What this is

The **shared foundation library** of the Argus suite (quarry → curator → lens → forge → proof). It holds the code the stages *share* so it lives in one place instead of copy-pasted per repo: the target taxonomy, the wire-schema versioning discipline, the local/remote AI-backend contract, and the optional stateful-services layer.

Naming: the working directory is `argus-core`, but the GitHub repo, the PyPI package, and the import name are all **`argus-cortex`** (`src/argus_cortex/`). Don't "fix" the mismatch.

Because everything here is imported by the downstream stages, **a change ripples across the whole suite** — treat this repo as an API, not a leaf. Read [README.md](README.md) for the *why* and worked examples; it is the source of truth for behaviour.

## Layout

`src/argus_cortex/` (top level is pydantic-only; everything heavier is behind an extra and lazily imported):

- `taxonomy.py` — the "moat" types every stage inherits verbatim: `TargetProfile`, `TargetStyle`/`TargetCategory` literals. Keep it dependency-light; a change here retrains everyone's expectations.
- `wire.py` — the versioned wire-schema toolkit consumers build their contracts on: `make_versioned_base(field_name, current, majors)` (a Pydantic base that stamps + version-checks a field like `proof_version`/`manifest_version`), `check_version()` (the major-compatibility gate), `render_schema()`/`wire_schema()` (the canonical committed-schema string behind each consumer's `schema --check` CLI), and `VersionError`.
- `backends.py` — the AI-backend contract: `Backend`/`LocalBackend`/`RemoteBackend` (point at a hosted service via `from_host(host, port)` or `from_provider(RemoteProvider.…)`), plus `with_retries()` and `resolve_device()`. httpx is lazy, behind `[remote]`.
- `server.py` — the shared FastAPI/ASGI scaffolding other repos' micro-servers mount: `WriteGuard` (pure-ASGI method gate), the `constant_refuse()` (read-only/replay) and `cross_site_refuse()` (cross-origin write) predicates, `env_flag()`, and the `UNSAFE_METHODS`/`TRUTHY`/`FALSY` constants. Starlette is behind `[server]`.
- `store/` — the optional stateful-services layer, opt-in per `CORTEX_*` env var: `lineage.py` (Phase 1 Postgres DAG, `open_lineage_store()`), `vector.py` (Phase 2 Qdrant, `open_vector_store()`), `blob.py` (Phase 3 MinIO/S3, `open_blob_store()`), over `config.py`/`models.py`/`errors.py`. Each `open_*()` returns a `Null*` no-op when its URL is unset. Drivers are behind `[postgres]`/`[qdrant]`/`[s3]`.

There is **no CLI and no committed schema in this repo** — the `schema`/`schema --check` pattern is something `wire.py` *enables* for downstream repos, not something cortex runs on itself.

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
