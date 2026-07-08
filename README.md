# argus-cortex

Shared foundation for the Argus suite: taxonomy, versioned wire-schema tooling, and the local/remote AI backend contract.

Part of the [Argus suite](https://github.com/smk762?tab=repositories&q=argus) (quarry → curator → lens → forge → proof). This is the library the stages import so the code they *share* — the target taxonomy, the wire-schema versioning discipline, and the way they call local or hosted models — lives in one place instead of being copy-pasted per repo.

## What's here

- **`argus_cortex.taxonomy`** — `TargetProfile` / `TargetStyle` / `TargetCategory`, the "moat" types every stage inherits verbatim.
- **`argus_cortex.wire`** — the versioned wire-schema toolkit: `check_version()` (major-compatibility gate), `make_versioned_base()` (a Pydantic base that stamps + checks a version field like `proof_version` / `manifest_version`), and `wire_schema()` / `render_schema()` for the committed-schema `schema --check` CLI pattern.
- **`argus_cortex.backends`** — the AI-backend contract generalised from argus-lens: `Backend` / `LocalBackend` / `RemoteBackend` (point at a hosted service by `base_url`, `host`/`port`, or a known `RemoteProvider`), plus `with_retries()` and `resolve_device()`. httpx lives behind the `[remote]` extra.

```python
from argus_cortex.wire import make_versioned_base, render_schema
from argus_cortex.backends import RemoteBackend, RemoteProvider

# per-package versioned base (readable field name kept local, logic shared)
_Versioned = make_versioned_base("proof_version", "1.0", ("1",))

# reach a hosted scorer by IP/port, or a known provider
scorer = RemoteBackend.from_host("192.168.1.20", 9000, api_key="…")
nim = RemoteBackend.from_provider(RemoteProvider.NVIDIA_NIM, api_key="…")
```

## Install

```bash
uv pip install argus-cortex            # core (pydantic only)
uv pip install "argus-cortex[remote]"  # + httpx for RemoteBackend
```

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
