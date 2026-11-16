# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **`NudeNetClient.info()`.** Asks a NudeNet server which model it runs (`GET /health`) and returns a `NudeNetInfo`
  (`model`, `resolution`, and `detector`: `640m`, `320n` or `None`). The upstream 320n image has no `/health` and
  gives an empty `NudeNetInfo`; an unreachable server raises `NudeNetCheckFailed`. 320n and 640m score the same
  image differently, so record the detector with the scores and tune thresholds per model. The suite is moving to
  one shared 640m server (#79).

### Added
- **Resident replies (`argus_cortex.gpu_wire`).** What a resident's routes return, so the broker reads one shape
  instead of three: `ResidentHealth` for `GET /health` (top-level `model_state`, one of `unloaded`/`loading`/`cpu`/
  `gpu`/`error`; `busy`; `idle_s`, null while busy or unloaded; an optional memory-only `gpu` block with
  `free_vram_mb`/`allocated_mb`/`reserved_mb`/`total_mb`), `ResidentActionReply` for `POST /admin/unload|evict|wake`
  (`status`, `model_state` after the action, `unloaded: bool | None`), and `ResidentBusyReply`, the 409 body
  (`{"status": "busy", "active": n}`: nothing was done, retry after the work finishes); 503 means loading or
  unavailable. `resident_version` 1.0, defaulted, so a service need not send it. Replies allow extra fields, so
  services keep their own. Committed `gpu-resident-schema.json` (#84).

## [0.6.0] - 2026-11-15

### Added
- **GPU broker client (`argus_cortex.gpu_broker`).** `BrokerCoordinator` asks argus-iris, the suite's GPU broker,
  for a lease; iris places it on a node, queues it by priority, reserves the VRAM and evicts idle models to make
  room. Select it with `ARGUS_GPU_COORDINATOR=broker` and `ARGUS_GPU_BROKER_URL` (optional
  `ARGUS_GPU_BROKER_TOKEN`, `ARGUS_GPU_TENANT`, `ARGUS_GPU_TENANT_MANIFEST`). A lease long-polls while queued,
  heartbeats in the background, re-attaches if the broker restarts, and reports its outcome (`ok`, `failed`,
  `oom`, `cancelled`) and peak VRAM on release. Each request carries a `request_id`, so the one retry after a
  lost response gets the same lease. It fails open: if the broker is unreachable or answers 5xx, the lease comes
  from `ARGUS_GPU_BROKER_FALLBACK` (`lease`, the default, or `none`). A 4xx or an incompatible version raises
  `BrokerError`; a refusal raises the new `GpuLeaseRefused`. httpx is behind `[remote]` (#78).
- **GPU wire models (`argus_cortex.gpu_wire`).** The lease contract (`LeaseRequest`, `LeaseStatus`, `Heartbeat`,
  `HeartbeatReply`, `LeaseRelease`; `lease_version` 1.0) and `TenantManifest` (`tenant_version` 1.0), the
  `gpu-tenant.yaml` every repo that uses a GPU commits: its kinds of work with target, footprint (`vram_mb`, and
  `model_mb` for the weights that stay loaded), priority and preemptibility, and its resident models with the
  routes and hints the broker uses to reach, unload, evict and warm them. Committed `gpu-lease-schema.json` and
  `gpu-tenant-schema.json` (#78).
- **`lease()` options and handle.** Every coordinator's `lease()` takes optional `priority`, `target`, `kind`,
  `preemptible`, `constraints` and `model_mb`, which only the broker uses, and yields a `GpuLease` (`node`,
  `base_url`, `should_yield()`, `report_peak_vram()`). Every coordinator gains an async `alease()`. Existing
  `with coordinator.lease(caller=, min_vram_mb=):` calls are unchanged (#78).
- **`[yaml]` extra** (PyYAML) for `load_tenant_manifest()` reading YAML; JSON needs nothing (#78).
- `SUITE.md`: a row for argus-handbook, the suite's public docs at docs.dragonhound.dev.
- `brand/`: the suite's identity (vesica-iris suite mark, one mark per package, Panoptes lettering font, the
  identity sheet, and `brand/tools/panoptes.py` to regenerate them). Panoptes 0.2 adds capitals, digits and basic
  punctuation (85 glyphs); glyphs made only of dots, like the colon, keep every dot in the font build. Favicon sets
  for every web UI (`mark/favicon/`, `packages/<pkg>/favicon/`) and `panoptes.py favicons` to regenerate them;
  `brand/README.md` says where each UI puts them. Not part of the package: excluded from the wheel and the sdist.

### Changed
- `SUITE.md`: the "Share the GPU" row points at the broker and asks each repo to declare its GPU use in a
  `gpu-tenant.yaml` (#78). argus-studio's row says its `/docs` keeps only the captioning concept pages; the setup
  guides and package docs moved to argus-handbook, and the old paths 301 there.
- `release.yml` also sends `cortex-released` to argus-mint, whose image installs cortex from its `server` extra's
  range and now rebuilds on it (argus-mint#29).
- **Behaviour changes a stage may notice on this bump (#73):**
  - `cross_site_refuse()` no longer trusts a same-host write by bare hostname (DNS rebinding). A same-host
    `Origin` passes only when the host is an IP literal, `localhost`, or listed in the new `trusted_hosts`
    argument; a server reached by a DNS name must pass it (or list the origin in `trusted_origins`).
  - `RemoteBackend.post_json()` retries only failures where the request never reached the server (connect
    errors, pool timeouts), not a `ReadTimeout`; pass `idempotent=True` to keep the old retries.
  - `policies.mode()` raises `ModerationConfigError` for an unknown policy name instead of returning `nsfw`.
  - `QdrantVectorStore.search()` returns the key passed to `upsert()` as `VectorHit.id`, not its UUID5; the
    original key rides in the reserved `_point_id` payload field (`POINT_ID_FIELD`), stripped from `payload`.
  - `SingGuardClient` refuses a multi-frame image (animated GIF/WebP/APNG, multi-page TIFF) with
    `GuardCheckFailed` instead of judging frame 0.
  - `CORTEX_S3_ENDPOINT` refuses a scheme other than `http://`/`https://`.

### Fixed
- `LeaseCoordinator` refuses a symlinked or non-regular lock file and chmods it only when it owns it (#73).
- The lineage schema refuses a dataset membership whose `edit_id` belongs to another caption (a trigger, so it
  reaches existing tables) (#73).
- XMP: regions are written as valid RDF (no `parseType` on attribute-form structs) and read back from nested
  `rdf:Description` forms; a sidecar's prefixes no longer rebind the process-wide ElementTree registry; concurrent
  `write_xmp()` merges into one directory take an exclusive lock so neither drops the other's properties (#73).
- `ensure_schema()` serialises on a Postgres advisory lock and creates the membership trigger in one statement, so
  several stages starting against one database no longer fail with "tuple concurrently updated" or "trigger
  already exists".
- `BrokerCoordinator` gives up a queued request whenever the wait ends without a grant (a 5xx or a dropped
  connection mid-poll, a bad payload, an interrupt), not only on timeout or cancellation, so the broker never grants
  an orphaned lease while the work runs on the fallback.
- `LeaseCoordinator.alease()` waits on its own thread instead of the loop's default executor, so more concurrent
  waiters than executor threads can no longer starve the holder's release.
- Docs: `slow` and `cascade` are supported by agent-composer's guard router as well as argus-cull's `singguard`
  service, not by argus-cull's alone (`SingGuardClient` docstrings, README). Code is unchanged.

## [0.5.0] - 2026-11-14

### Added
- **`argus_cortex.provenance`.** `Provenance`, `ProvenanceKind` and `CLEARED_KINDS`, hoisted from
  argus-mask so every package that stores where a face, voice or image came from reads one shape,
  with a committed `provenance-schema.json` (#35).
- **`argus_cortex.consent`.** `ConsentRecord`, a real person's likeness release as argus-mask#26
  defined it, keyed by the `release_sha256` a `consented` `Provenance` names. `consent_valid()`
  returns a `ConsentVerdict` with one of a closed set of refusal codes, checked in a fixed order
  (`revoked`, `not-yet-signed`, `expired`, `likeness-not-covered`, `use-not-covered`,
  `delivery-not-covered`), so every stage refuses for the same reasons. `consent_matches()` checks
  that a record is the release and subject a provenance names. Committed `consent-schema.json` (#53).
  A record is frozen and its sequences are tuples, so it cannot be changed after its checks pass.
  It is refused when the age check names nobody or comes after signing, when a revocation is
  requested before signing, or when a listed voice delivery is blank (#56).
- **`Provenance.generator`.** An optional `model`, `model_sha256`, `seed` and `prompt` for how a
  generated source was made, allowed only on `synthetic` and `personal` sources (#53).

### Changed
- **`Provenance` is strict, as argus-mask's is (argus-mask#64).** Unknown fields are refused instead
  of dropped (`additionalProperties: false` in the schema). `licence` is read as `license`; both given
  and different is refused. A `source` of only whitespace is refused. The derived `cleared` in a dumped
  record is dropped on read and recomputed.
- **README:** lists the `[images]` and `[gpu]` extras, `ARGUS_GPU_DEVICE`, and the moderation policy
  names with their illegal-rule letters (#42).

## [0.4.7] - 2026-11-13

### Added
- **`concept` target category.** `TargetCategory` gains `concept`, a style or
  visual-concept LoRA, matching argus-quarry's `concept` subjects. It is
  additive, but a stage that keys tables by category (forge's heuristics, curator's
  scanner) must add an entry before it accepts it.
- **Committed `taxonomy-schema.json`.** Generated from `TargetProfile` by the new
  `taxonomy_schema()` (`make schema`), with a test that fails when it drifts from
  `taxonomy.py`, so non-Python clients such as studio generate their types instead
  of copying the literals (#29).
- **`SUITE.md`, the suite map for agents.** Every Argus repo, its port and purpose,
  and a "Need it? Use it" index. Each repo's `CLAUDE.md` imports it (#33).

## [0.4.6] - 2026-11-06

### Changed
- **`adult-cinematic` passes depicted crime without a film cue.** A robbery,
  kidnapping, armed standoff, drugs or war is Safe unless it is sexual, shows
  gore or dismemberment, or teaches how to do it. The crimes heading at B is
  gone, because it was read as any crime shown.
- **`adult-cinematic` letters changed.** B to F are now Unethical Behavior,
  Cybersecurity, Agent Safety, Politically Sensitive and Animal Abuse, and G is
  Criminal Instructions, so B to G no longer match the other adult policies. A
  filter that names a letter selects a different rule here, including on
  verdicts recorded earlier. Illegal A is unchanged.

### Added
- **A release now rebuilds the stage images.** Once the version is on the PyPI index,
  `release.yml` sends `cortex-released` to curator, lens, proof, forge and darkroom, whose
  release workflows rebuild their newest release's image against it. Needs the
  `SUITE_DISPATCH_TOKEN` secret (a fine-grained PAT, Contents: read & write on those repos).
  atlas and survey are not sent it: they install from their lockfile.

## [0.4.5] - 2026-10-31

### Added
- **`adult-cinematic`**: `adult` with B narrowed to how-to / real targeting, and
  Safe naming fictional crime (heist, fraud, dirty cops, war as setting, weapons
  as a prop, drugs as a plot). Legal adult entertainment stays Safe the same
  way as under `adult` (commercial porn, studio stills, an adult-site watermark).
  Under evaluation against `adult` (#24). Letters and illegal A are unchanged.

### Fixed
- **Adult policies no longer invite a "closest" H/I dump-bin.** Safe now names
  legal adult entertainment, commercial porn, explicit sex, genitalia, a
  suggestive pose, a commercial or studio set, and an adult-site watermark, and
  says that if no risk bullet matches the verdict is Safe. Headings dropped the
  word "Extreme" (`A. Illegal Sexual Content`, granular `I. Sexual Violence`),
  which the model read as a bin for legal porn. Letters are unchanged.

## [0.4.4] - 2026-10-31

### Fixed
- **`SingGuardClient` keeps a longer writeup.** `to_result` clipped `explanation` at 4000
  characters, which cut cascade/slow reasoning mid-sentence. The cap is now 16384, enough
  for a 2048-token slow writeup plus a cascade preface.
- **Re-recording an asset no longer erases what another stage recorded on it.** `record_asset`'s
  upsert on `sha256` replaced `immich_id` and `metadata` outright, so argus-lens recording a caption
  for an image argus-quarry had already recorded (licence, author, source) wiped that provenance,
  and a call without an `immich_id` cleared the stored one. `metadata` now merges key by key
  (`jsonb ||`: the new call's keys win, others are kept) and a `NULL` `immich_id` keeps the stored
  one. `uri` is still last-write-wins: it is the asset's latest location. A caller that needs to
  remove a metadata key can no longer do it through `record_asset`.

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

[Unreleased]: https://github.com/smk762/argus-cortex/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/smk762/argus-cortex/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/smk762/argus-cortex/compare/v0.4.7...v0.5.0
[0.4.7]: https://github.com/smk762/argus-cortex/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/smk762/argus-cortex/compare/v0.4.5...v0.4.6
[0.4.5]: https://github.com/smk762/argus-cortex/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/smk762/argus-cortex/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/smk762/argus-cortex/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/smk762/argus-cortex/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/smk762/argus-cortex/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/smk762/argus-cortex/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/smk762/argus-cortex/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/smk762/argus-cortex/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/smk762/argus-cortex/releases/tag/v0.1.0
