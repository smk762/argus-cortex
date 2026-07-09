# argus-cortex

Shared foundation for the Argus suite: taxonomy, versioned wire-schema tooling, and the local/remote AI backend contract.

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus) (quarry → curator → lens → forge → proof). This is the library the stages import so the code they *share* — the target taxonomy, the wire-schema versioning discipline, and the way they call local or hosted models — lives in one place instead of being copy-pasted per repo.

## What's here

- **`argus_cortex.taxonomy`** — `TargetProfile` / `TargetStyle` / `TargetCategory`, the "moat" types every stage inherits verbatim.
- **`argus_cortex.wire`** — the versioned wire-schema toolkit: `check_version()` (major-compatibility gate), `make_versioned_base()` (a Pydantic base that stamps + checks a version field like `proof_version` / `manifest_version`), and `wire_schema()` / `render_schema()` for the committed-schema `schema --check` CLI pattern.
- **`argus_cortex.backends`** — the AI-backend contract generalised from argus-lens: `Backend` / `LocalBackend` / `RemoteBackend` (point at a hosted service by `base_url`, `host`/`port`, or a known `RemoteProvider`), plus `with_retries()` and `resolve_device()`. httpx lives behind the `[remote]` extra.
- **`argus_cortex.store`** — the optional **stateful services layer** (the suite's persistent "memory"). External and opt-in — every store is configured by `CORTEX_*` env vars and no-ops when its URL is unset.
  - **Phase 1 — Postgres lineage store.** Persists the `source_asset → caption → human_edit → dataset_membership → training_run` DAG for LoRA reproducibility, edit capture, and the feedback loop. `open_lineage_store()` returns a no-op store when `CORTEX_PG_URL` is unset; psycopg lives behind the `[postgres]` extra.
  - **Phase 2 — Qdrant vector store.** Stores image + tag-set embeddings for retrieval-augmented few-shot and near-duplicate curation; put a `caption_id`/`asset_id` in a point's payload and a search hit joins straight back to the lineage. `open_vector_store()` no-ops when `CORTEX_QDRANT_URL` is unset; qdrant-client lives behind the `[qdrant]` extra. cortex stores vectors it's *handed* — computing embeddings is the caller's job.
  - **Phase 3 — MinIO/S3 blob store.** Owns image bytes when cortex can't rely on the filesystem/Immich (exported/selected training images). Blobs are content-addressed on sha256 (`content_key()` == `source_asset.sha256`), so storage, dedup, and lineage join share one identity. `open_blob_store()` no-ops when `CORTEX_S3_ENDPOINT`/`CORTEX_S3_BUCKET` are unset; the `minio` client lives behind the `[s3]` extra.

```python
from argus_cortex.wire import make_versioned_base, render_schema
from argus_cortex.backends import RemoteBackend, RemoteProvider

# per-package versioned base (readable field name kept local, logic shared)
_Versioned = make_versioned_base("proof_version", "1.0", ("1",))

# reach a hosted scorer by IP/port, or a known provider
scorer = RemoteBackend.from_host("192.168.1.20", 9000, api_key="…")
nim = RemoteBackend.from_provider(RemoteProvider.NVIDIA_NIM, api_key="…")
```

```python
from argus_cortex.store import open_lineage_store, SourceAsset, Caption, HumanEdit

# no-op if CORTEX_PG_URL is unset; PostgresLineageStore if it's set
store = open_lineage_store()          # reads CORTEX_* from the environment
store.ensure_schema()                 # idempotent bootstrap of the lineage tables

asset_id = store.record_asset(SourceAsset(uri="immich://…", sha256="…"))
# lens emits a CaptionResult; cortex ingests it (cortex never imports lens)
cap_id = store.record_caption(asset_id, Caption.from_caption_result(result, profile={...}))
store.record_edit(cap_id, HumanEdit(edited_caption="…", editor="alice"))

# the (model_caption, edited_caption) pairs the summariser feedback loop trains on
pairs = store.caption_edit_pairs()
```

```python
from argus_cortex.store import open_vector_store, IMAGE_COLLECTION

# no-op if CORTEX_QDRANT_URL is unset; QdrantVectorStore if it's set
vec = open_vector_store()
vec.ensure_collection(IMAGE_COLLECTION, dim=512)   # idempotent

# store an embedding you computed, tagged with its lineage id
vec.upsert(IMAGE_COLLECTION, point_id=cap_id, vector=embedding, payload={"caption_id": cap_id})

# retrieve similar past captions to steer the summariser (few-shot)
hits = vec.search(IMAGE_COLLECTION, vector=query_embedding, top_k=5)
similar_caption_ids = [h.payload["caption_id"] for h in hits]   # join back to Postgres
```

```python
from argus_cortex.store import open_blob_store

# no-op if CORTEX_S3_ENDPOINT / CORTEX_S3_BUCKET are unset; S3BlobStore if set
blobs = open_blob_store()
blobs.ensure_bucket()

# content-addressed: the returned key IS the sha256 (== source_asset.sha256)
sha = blobs.put_content_addressed(image_bytes, content_type="image/jpeg")
data = blobs.get(sha)                    # bytes, or None if absent
link = blobs.url(sha, expires=3600)      # presigned GET URL
```

## Install

```bash
uv pip install argus-cortex              # core (pydantic only)
uv pip install "argus-cortex[remote]"    # + httpx for RemoteBackend
uv pip install "argus-cortex[postgres]"  # + psycopg for the lineage store
uv pip install "argus-cortex[qdrant]"    # + qdrant-client for the vector store
uv pip install "argus-cortex[s3]"        # + minio for the blob store
```

The stateful services are configured via `CORTEX_*` env vars — see [`.env.example`](.env.example). Each is optional: leave a URL unset and that feature degrades to a no-op.

## Develop

```bash
make install   # venv + editable install with the "dev" extras
make test
make lint
```

## CI / Release

- **CI** runs via the shared [`argus-ci`](https://github.com/smk762/argus-ci) reusable workflow.
- **Release** publishes to PyPI (OIDC trusted publishing) on `v*` tags. No container image — this is a library.
- Versioning is derived from git tags via `hatch-vcs` — tag `vX.Y.Z` to cut a release.

This repo was scaffolded from [`argus-pkg-template`](https://github.com/smk762/argus-pkg-template).
Run `copier update` to pull template changes (CI, release, tooling).
