# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/smk762/argus-cortex/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/smk762/argus-cortex/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/smk762/argus-cortex/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/smk762/argus-cortex/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/smk762/argus-cortex/releases/tag/v0.1.0
