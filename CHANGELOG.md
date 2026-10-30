# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.3] - 2026-10-30

### Added
- **`argus_cortex.gpu`**: GPU tenancy shared across the stages, generalised
  from argus-lens's `gpu` coordinator.
  - `GpuCoordinator`: the protocol. `lease(caller=, min_vram_mb=)` is a
    context manager that holds a GPU slot for its body. `GpuLeaseTimeout` is a
    `TimeoutError`.
  - `NullCoordinator`: no gating, and the default.
  - `LeaseCoordinator`: an exclusive `flock` that every argus process on the
    host takes, then, while still holding it, a wait until `free_vram_mb()`
    reports `min_vram_mb`. Unlike lens's lease, `min_vram_mb` is honoured. NVML
    counts every process on the card, so ComfyUI and Ollama are accounted for
    without taking part. One deadline covers both waits. A probe that cannot
    run (no driver, no `[gpu]` extra, a CPU host) lets the lease through.
  - `free_vram_mb(device)`: NVML free memory in MB, or `None`.
  - `build_coordinator()` / `coordinator_from_env()`: `ARGUS_GPU_COORDINATOR`
    (`none` / `lease`), `ARGUS_GPU_LEASE_PATH`, `ARGUS_GPU_LEASE_TIMEOUT_S`
    (300 s) and `ARGUS_GPU_DEVICE`. These are the names lens already reads. An
    unusable number logs a warning and falls back to the default.
  - The default lock is `<tempdir>/argus-gpu.lock`. Containers have their own
    `/tmp`, so a shared lock needs `ARGUS_GPU_LEASE_PATH` on a volume every
    GPU service mounts. The lock file is `chmod 0o666` after create so a
    restrictive umask does not block other container UIDs from opening it.
- `[gpu]` extra: `nvidia-ml-py`, imported only when probed.

## [0.4.2] - 2026-10-29

### Added
- `SingGuardClient.check_text(text, role="user")`: a text-only check, with no
  image, for judging a prompt before it is run (or, with `role="assistant"`, a
  model's reply). It sends `input_role` and keeps the client's policy and
  thinking mode. It needs a server that takes a check without `image_base64`:
  agent-composer's guard router does, while argus-cull's `singguard` service
  answers HTTP 422, raised as `GuardCheckFailed`. `INPUT_ROLES` lists the roles.
- **`argus_cortex.moderation.policies`**: the named SingGuard policies the
  stages share, moved from argus-curator's `guard.py` with their texts
  unchanged. `POLICIES` / `PolicyName` (`default`, `adult`, `adult-granular`,
  `adult-cautious`) and `policy_text(name)` (`None` for SingGuard's own rules).
  - `illegal_rules(name)` / `is_illegal(name, rule)`: the rule letters whose
    firing means illegal content, per policy. `adult-granular` is A, H and I;
    `adult` and `adult-cautious` are A. `default` has none, because its A
    covers consensual adult nudity too. B (crime) and G (animal abuse) are
    never illegal.
  - `mode(name)`: `sfw` for `default` and any `sfw-*` name (the prefix is
    reserved for SFW policies added later), `nsfw` for every other policy.
  - An unknown name raises `ModerationConfigError`.

## [0.4.1] - 2026-10-27

### Added
- `SingGuardClient(thinking="slow")` and `thinking="cascade"`: the argus-cull
  `singguard` service's modes. `slow` reasons with no fast first-line verdict,
  so the reasoning is not anchored to a verdict made before it; `cascade` runs
  `fast` on every image and `slow` only on the ones fast flags, and slow's
  verdict stands. `THINKING_MODES` lists them. A SingGuard server without a
  mode answers HTTP 400, raised as `GuardCheckFailed` per check.
- `GuardResult.thinking`: the mode the server says it judged in (`None` from a
  server that does not say). The argus-cull service can be forced server-side
  to a mode other than the one asked for, and this records which it used.

## [0.4.0] - 2026-10-26

### Added
- **`argus_cortex.moderation`** — content-moderation clients shared by the
  stages, hoisted from argus-curator. Both talk HTTP to a server the deployer
  runs; nothing imports a detector.
  - `NudeNetClient(base_url, timeout)` — a NudeNet server's `/infer`, returning
    `NsfwResult` (detections, highest score per class, exposed score) and
    raising `NudeNetCheckFailed` per image. `NudeNetLabel` / `NUDENET_LABELS`
    are NudeNet's 18 classes; `DEFAULT_EXPOSED_LABELS` (the five exposed
    buttocks/breast/genitalia/anus classes) and `exposed_score()` are the
    suite's one definition of nudity.
  - `SingGuardClient(base_url, timeout, thinking, policy, downscale)` — a
    SingGuard server's `/api/guard/check`, returning `GuardResult` (verdict,
    the rules that fired, explanation, model, policy name) and raising
    `GuardCheckFailed`. `rule_matches()` / `rule_letter()` match rules by
    letter or full name; `policy_name()` names a custom policy by a sha256
    prefix.
  - Proxies in the environment are ignored: images go only to the configured
    server. Config errors (`ModerationConfigError`) never echo the URL. A
    `user:password@` in the URL is sent as HTTP Basic auth.
- `[images]` extra (Pillow), for `SingGuardClient`'s downscaling of images
  over 1536 px / 4 MB; imported only when it downscales. Downscaling is on by
  default, so without the extra `SingGuardClient(...)` raises
  `ModerationConfigError` at construction unless given `downscale=False`.

### Changed
- `release.yml` now creates the **GitHub Release** for each `v*` tag once the
  version is on PyPI, with that version's `CHANGELOG.md` section as notes and
  the built sdist/wheel attached (a missing section fails the job; a re-run
  edits the Release). Tags with `-`, `rc`, `a<n>` or `b<n>` are pre-releases.

## [0.3.0] - 2026-10-25

### Added
- **`argus_cortex.xmp`** — the suite's one XMP sidecar implementation, hoisted
  from argus-lens so lens (keywords/caption) and argus-curator (face regions,
  content rating) share a writer. `write_xmp()` / `read_xmp()` /
  `merge_xmp()` / `parse_xmp()` / `render_xmp()` / `sidecar_path()` over an
  `XmpData` model, all exported from the top-level package.
  - Namespaces: `dc` (subject, description), `xmp` (Rating, Label), `lr`
    (hierarchicalSubject), `Iptc4xmpExt` (PersonInImage), `mwg-rs` regions
    (`XmpRegions` / `XmpRegion`, MWG centre-based normalised coordinates), and a
    new `argus` namespace for `ContentRating`. The `argus` namespace URI
    (`https://github.com/smk762/argus-cortex/ns/xmp/1.0/`) is now a wire
    contract.
  - **Writes merge**: each property set in `XmpData` replaces that property
    (latest write wins, element or attribute form, across every
    `rdf:Description`); everything else in the sidecar — Lightroom develop
    settings, GPS, another stage's fields, foreign namespace prefixes — is
    preserved. `None` leaves a property untouched, an empty value removes it;
    `replace=True` discards the existing sidecar.
  - Writes are atomic (temp file + rename) and keep the existing file mode
    (`0644` for a new sidecar). An unparseable existing sidecar raises
    `XmpError` and is never clobbered; a DTD / entity declaration is refused.
  - Stdlib `xml.etree` + pydantic only, so the top-level import stays
    pydantic-only.
- `CLAUDE.md` agent guide.

### Security
- **eMudhra/emSign CA incident.** The extras that make outbound TLS
  connections (`[remote]` httpx, `[qdrant]`, `[s3]`) now require
  `certifi>=2026.10.19`, which no longer trusts the compromised emSign
  intermediates. See https://discuss.python.org/t/109576.

## [0.2.0] - 2026-07-21

### Added
- **Optional stateful-services layer** (`argus_cortex.store`), each store opt-in
  via `CORTEX_*` env vars and a `Null*` no-op when unset:
  - Phase 1 — Postgres lineage store (`source_asset → caption → human_edit →
    dataset_membership → training_run`), backed by a psycopg connection pool
    with an atomic `transaction()` boundary (`[postgres]` extra).
  - Phase 2 — Qdrant vector store for image / tag-set embeddings
    (`[qdrant]` extra).
  - Phase 3 — MinIO/S3 content-addressed blob store, keyed on sha256
    (`content_key()` == `source_asset.sha256`) (`[s3]` extra).
- **`argus_cortex.server`** — shared pure-ASGI `WriteGuard`, the
  `constant_refuse()` / `cross_site_refuse()` predicates, `env_flag()` and the
  `UNSAFE_METHODS` / `TRUTHY` / `FALSY` constants (`[server]` extra).

### Fixed
- Release: a TestPyPI failure no longer gates the real PyPI release.

## [0.1.0] - 2026-07-08

### Added
- Initial release (renamed from `argus-core`; that PyPI name was taken):
  - `argus_cortex.taxonomy` — `TargetProfile`, `TargetStyle`, `TargetCategory`.
  - `argus_cortex.wire` — versioned wire-schema toolkit (`make_versioned_base`,
    `check_version` with an injectable error type, `render_schema` /
    `wire_schema`, `VersionError`).
  - `argus_cortex.backends` — the local/remote AI-backend contract
    (`LocalBackend`, `RemoteBackend`, `RemoteProvider`, `with_retries`
    retrying only transient errors, `resolve_device`).

[Unreleased]: https://github.com/smk762/argus-cortex/compare/v0.4.3...HEAD
[0.4.3]: https://github.com/smk762/argus-cortex/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/smk762/argus-cortex/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/smk762/argus-cortex/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/smk762/argus-cortex/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/smk762/argus-cortex/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/smk762/argus-cortex/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/smk762/argus-cortex/releases/tag/v0.1.0
