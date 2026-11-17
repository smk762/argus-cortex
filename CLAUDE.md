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

## Must never happen

Each row names what enforces it. An **UNENFORCED** row is a known gap, linked to the issue that closes it: rely on
review until then, and close it with a check rather than more prose. A rule about code shape across the suite (a banned
import or API) belongs in argus-ci's `suite-rules/` catalogue rather than a test here.

| Never | Enforced by |
|---|---|
| Change `taxonomy.py`, `provenance.py`, `consent.py` or `gpu_wire.py` without `make schema` in the same commit | `test_taxonomy::test_committed_schema_matches_taxonomy`, `test_provenance::test_committed_schema_matches_provenance`, `test_consent::test_committed_schema_matches_consent`, `test_gpu_wire::test_committed_schema_is_current` |
| Ship a breaking change to a committed schema in anything but a major release (and, for a `*_version` schema, a new wire major) | `test_schema_compat::test_committed_schemas_break_only_in_a_major_release`: compares each `*-schema.json` with the last `v*` tag; the release is the newest CHANGELOG heading |
| Import NudeNet (AGPL-3.0): HTTP only | ruff `TID251` banned-api (`pyproject.toml`); `test_rules::test_cortex_never_imports_a_stage_or_nudenet` |
| Import a stage from cortex | `test_rules::test_cortex_never_imports_a_stage_or_nudenet` |
| `import argus_cortex` pulling in an optional extra (httpx, starlette, psycopg, qdrant, minio, Pillow, NVML, PyYAML) | `test_rules::test_top_level_import_loads_no_optional_extra` |
| Widen `DEFAULT_EXPOSED_LABELS` (every stage's NSFW numbers move with it) | `test_moderation::test_exposed_set_is_the_five_exposed_nudity_classes` |
| A policy text without its illegal-rule letters, or the reverse | `test_policies::test_every_policy_has_a_text_and_an_illegal_table` |
| Treat an unchecked moderation result as safe: a failed check raises `CheckFailed`, never returns a result | `test_moderation::test_nudenet_bad_responses_are_failures`, `::test_unreachable_server_is_a_failed_check`, `::test_nudenet_info_unreachable_is_a_failed_check` |
| `ModerationConfigError` echoing the URL (it can carry credentials) | `test_moderation::test_nudenet_url` |
| An XMP write dropping foreign properties, accepting a DTD (in any encoding), or overwriting an unparseable sidecar | `test_xmp::test_merge_preserves_foreign_properties_and_latest_wins`, `::test_dtd_is_refused`, `::test_dtd_is_refused_in_any_encoding`, `::test_malformed_sidecar_is_refused_not_clobbered` |
| Change the `argus` XMP namespace URI (sidecars on disk carry it) | `test_rules::test_argus_xmp_namespace_is_pinned` |
| A consent record holding the ID or the release document (it keeps who checked the ID and when, and `release_sha256`) | `test_consent::test_unknown_fields_are_refused_and_the_record_is_frozen`, `test_rules::test_an_age_check_cannot_hold_the_id` |
| `consent_valid` reading the clock, or raising on an unknown use | `test_rules::test_consent_valid_never_reads_the_clock`, `test_consent::test_an_unknown_use_is_refused_not_raised` |
| A store raising when its `CORTEX_*` URL is unset (it returns a `Null*` no-op) | `test_store::test_open_lineage_store_no_url_returns_null`, `test_vector::test_open_vector_store_no_url_returns_null`, `test_blob::test_open_blob_store_unconfigured_returns_null` |
| The broker client failing closed on a transport error, 5xx or missing route | `test_gpu_broker::test_unreachable_broker_falls_back`, `::test_broker_without_url_falls_back_with_a_warning` |
| `WriteGuard` buffering a stream (it stays pure ASGI, not `BaseHTTPMiddleware`) | `test_server::test_guard_leaves_streaming_responses_intact` |
| `env_flag` silently disabling a protection on a typo | `test_server::test_env_flag_unrecognised_warns_and_is_off` |
| `safe_join` returning the caller's spelling instead of the resolved path it checked | `test_fs::test_paths_under_the_root_are_returned_resolved` |
| A "skip pinning" option in `net`: an allow-listed host is still pinned | `test_net::test_an_allow_listed_host_is_still_pinned`, `::test_dns_rebinding_after_the_check_changes_nothing` |
| `net` forwarding `Authorization` or `Cookie` across a redirect to another origin, or inflating a body past `max_bytes` | `test_net::test_credentials_are_not_forwarded_to_another_origin`, `::test_a_compression_bomb_is_never_inflated_past_the_cap` |
| `bearer()` leaving a route open when its token is unset, unless the route passes `open_when_unset=True` | `test_auth::test_unset_token_closes_the_route`, `::test_open_when_unset_is_explicit_and_logged` |

## Facts the code doesn't make obvious

[README.md](README.md) is the source of truth for what each module does; read it before changing one.

- **Contracts owned elsewhere.** argus-mask owns the meaning of `provenance` and `consent` fields, and argus-iris serves
  `gpu_wire`: change them only in step with that repo. `gpu_wire` stays additive within a major. Replies ignore unknown
  fields, but manifests forbid them, so a new manifest field needs the schema regenerated and iris bumped.
- **GPU lease lock.** `LeaseCoordinator` takes a host-wide `flock`, then waits for NVML free VRAM, so tenants outside
  the lock (ComfyUI, Ollama) are counted. The lock file must be on a volume every GPU container mounts: a per-container
  `/tmp` coordinates nothing. A probe that can't run grants the lease.
- **Optional extras** are imported inside the function that needs them, raising "install `argus-cortex[extra]`" when
  missing. Don't hoist one to module level (the import test above catches it).
- **Blob identity** is the content's sha256 (`content_key()` == `source_asset.sha256`): storage, dedup and the lineage
  join share it.
- Register `WriteGuard` *before* `CORSMiddleware`.
- `net`'s tests fake DNS (`resolve=`) and the network (`transport=`). The Hypothesis tests in `test_fs.py`,
  `test_net.py` and `test_limits.py` are the hostile-input spec: keep them passing when you change a rule.
- **Codemods** (`src/argus_cortex/codemods/`, the `[codemods]` extra) are dev tools. Nothing in the library imports them
  (the import test above checks libcst stays out), and each one's output is reviewed per repo, never applied blind.
- **Versioning** comes from git tags (hatch-vcs). `src/argus_cortex/_version.py` is generated and gitignored; tag
  `vX.Y.Z` to release. No CLI; commands are in the `Makefile` (`make help`). Ruff is pinned to 0.15.16.

## Branches

`dev` is the default branch and the base for every PR. Cut feature branches from `origin/dev` and bring them up to date by merging `origin/dev`, never `main`. Never push work to `main` or open a PR against it, except the release PR from `dev`: `main` must never be ahead of `dev`, with one exception. A release fast-forwards `main` to a `dev` commit that passed CI (`git push origin origin/dev:main`), then tags it. If `main` is protected, the release is a reviewed PR from `dev` into `main`, and merging it leaves a merge commit on `main` alone. That merge commit is the one exception to the rule: it leaves `main` one commit ahead of `dev` with an identical tree. Leave it there and don't merge `main` back into `dev`. Anything else on `main` that `dev` lacks is a mistake (`git diff origin/dev origin/main` must be empty). Once `main` carries such a commit it can't be fast-forwarded, so later releases are PRs too.

## CI / release

CI runs via the shared [`argus-ci`](https://github.com/smk762/argus-ci) reusable workflow (ruff + pytest with the `dev` extra — which installs the real psycopg/qdrant/minio/starlette clients so each store's live driver path is exercised, not just fakes). Release publishes to PyPI (OIDC trusted publishing) on `v*` tags — **no container image, this is a library**. Scaffolded from [`argus-pkg-template`](https://github.com/smk762/argus-pkg-template); run `copier update` to pull tooling changes.
