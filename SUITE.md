# The Argus suite: map for agents

The canonical list of every Argus repo: what each one is for, and which one already does the thing you are about to build.
Every repo's `CLAUDE.md` imports this file. **Before designing a feature, check the "Need it? Use it" index.** If a
sibling already does it, call that sibling (HTTP, or its library) instead of rebuilding it.

**Keep it current. Every release of any Argus repo checks its row here.** If a release adds, removes or renames a
capability, endpoint, port, UI pairing or dependency, open a PR to `argus-cortex` that updates this file in the same
release. A new repo adds a row and the import block (see the end of this file) before its first release.

## Repos

GitHub: `smk762/<repo>`. Ports are each service's default.

| Repo | Kind | Port | What it is for |
|---|---|---|---|
| **argus-cortex** | library | n/a | Shared foundation: taxonomy, wire-schema versioning, AI-backend contract, server guards, GPU lease, XMP, moderation clients, optional lineage/vector/blob stores. |
| **argus-quarry** | service | 8102 | Acquire public-domain / CC0 images with provenance and licence into a category-sorted raw pool. Acquisition only. |
| **argus-curator** | service | 8101 | Decide which images belong in a LoRA training set (quality, near-dup, faces, NSFW, diversity) and export them with a `manifest.jsonl`. |
| **argus-lens** | service | 8100 (8080 standalone) | Structured, category-bucketed captions, `.txt` sidecars and XMP keywords. |
| **argus-forge** | service | 8103 | Turn a curated, captioned export into runnable kohya_ss / OneTrainer / diffusers configs. |
| **argus-proof** | service | 8104 | Evaluate a trained LoRA against its dataset: sample grid, scores, pass/fail. |
| **argus-atlas** | service | 8105 | Bake georeferenced fantasy-map overlays from real terrain (GDAL controls plus a cartography LoRA), to GeoTIFF / PMTiles. |
| **argus-darkroom** | service | 8106 | Guarded image generation: checks the prompt, generates via ComfyUI, checks each output, and keeps a record of every attempt. |
| **argus-survey** | service | 8107 | Point-cloud deliverables for land surveyors: QA, ground, DTM/DSM, contours, volumes, change, ASPRS accuracy. |
| **argus-mint** | service | 8108 | Provenance tokens for game assets: renders reviewed templates through darkroom, pins to IPFS, mints ERC-721 cards and soulbound deeds, anchors Merkle roots (Base). |
| **argus-mask** | service | 8109 | A talking face: audio in, facial-animation frames out, rendered in a browser on any LAN screen. |
| **argus-studio** | UI + compose | 3000 | The suite web UI, and the compose stack that runs the services from GHCR images. No backend code. |
| **argus-safelight** | UI | 3097 | Demo UI for darkroom: prompt, verdicts, generation over SSE, history. |
| **argus-cull** | UI | 3000 | Demo UI for curator: which images make the set and why, plus the paths to act on. |
| **argus-lightbox** | UI (static) | 3000 | Demo gallery navigated by lens captions; bakes from cortex stores, then runs with no server. |
| **argus-sightline** | UI | 3107 | Workspace UI for survey. |
| **argus-assay** | UI | 3108 | Review UI for mint: the moderator and owner approve every card, template and frame. |
| **argus-halo** | infra | n/a | Terraform for the public demo (`argus.dragonhound.dev`): read-only replay services plus a private data host. |
| **argus-ci** | infra | n/a | Reusable GitHub Actions workflows (uv, ruff, pytest, build, release). |
| **argus-pkg-template** | infra | n/a | Copier template for new Argus Python packages; `copier update` pulls tooling changes. |

**How they fit.** The LoRA pipeline is `quarry → curator → lens → forge → (trainer) → proof → atlas`; curator, lens,
forge and proof carry the same `TargetProfile` from cortex. Darkroom is the suite's way to generate an image: mint
renders through it, and anything new that needs a picture (mask backgrounds, game art) should too. Proof's sample grids
and atlas's bakes predate it and still drive their own diffusion backend. Survey stands alone; its GeoTIFFs can feed
atlas. Every Python service except quarry depends on cortex. UIs are thin clients: the service decides and the UI only
shows.

**API and UI pairs.** darkroom ↔ safelight · curator ↔ cull · lens ↔ lightbox · survey ↔ sightline · mint ↔ assay.
Studio has a page for lens, curator, quarry, forge, proof, atlas and darkroom, and links out to sightline.

## Need it? Use it

| Need | Use | Don't |
|---|---|---|
| Generate an image (a plate, a background, art, a sample) | **darkroom** `POST /generate/stream` (SSE), `GET /image/{id}`. Its record id, model and seed are your provenance. Example client: mint's `DarkroomClient`. | Call ComfyUI directly or generate without checks. |
| Check an image for nudity, or text/an image against a policy | `argus_cortex.moderation` (`NudeNetClient`, `SingGuardClient`, `policies`) | Write your own nudity definition or policy text. |
| Share the GPU with other services | `argus_cortex.gpu` (`coordinator_from_env()`, `lease()`) | Assume the GPU is free. |
| Call a local or hosted model | `argus_cortex.backends` (`LocalBackend`, `RemoteBackend`, `with_retries`) | Hand-roll an HTTP client per model. |
| Version a wire payload, commit its schema | `argus_cortex.wire` (`make_versioned_base`, `render_schema`) | Ship an unversioned contract. |
| Guard a FastAPI server's writes, or run read-only | `argus_cortex.server` (`WriteGuard`, `env_flag`) | Use `BaseHTTPMiddleware`, which buffers streams. |
| Read or write XMP sidecars | `argus_cortex.xmp` | Overwrite another stage's properties. |
| Record lineage, embeddings or blobs | `argus_cortex.store` (`open_lineage_store` / `open_vector_store` / `open_blob_store`) | Add a database to a single stage. |
| The training target (style, category) | `argus_cortex.taxonomy`; non-Python clients use `taxonomy-schema.json` | Copy the literals. |
| Acquire source photos with licence and provenance | **quarry** | Scrape inside another stage. |
| Score, dedupe or select images; faces; content rating | **curator** | Re-score in a later stage. |
| Caption or keyword images | **lens** | Write ad-hoc captions. |
| Training configs, or evaluating a LoRA | **forge**, **proof** | |
| Terrain, georeferencing, map tiles | **atlas** (stylised), **survey** (survey-grade) | |
| A face that speaks | **mask** | |
| On-chain provenance, IPFS | **mint** | |
| Run the whole stack locally | **studio** `compose.yaml` | |
| A new Python package, or CI | **argus-pkg-template**, **argus-ci** | Copy another repo's tooling by hand. |

## Shared rules

- Shared code lives in cortex: import it, don't copy it. A cortex change ripples into every stage on its next bump.
- Provenance first. Anything the suite serves or trains on carries its source and licence; unlicensed input is refused.
- Optional heavy dependencies are lazy imports behind extras. A missing service config fails open to a no-op, never a crash.
- Talk across repos over HTTP or a versioned wire schema, never by reading another repo's files.

## Local checkouts

The maintainer's siblings live under `~/`, so the paths are `../argus-<name>`, with two exceptions:
- cortex is `../argus-core`;
- studio is `../argus-vision-demo`.

Directories named `argus-<repo>-<topic>` are worktrees of `<repo>`, not separate repos.

## The import block (every repo's CLAUDE.md)

```markdown
## Argus suite
<!-- argus-suite: keep this block; argus-ci checks for it -->
This repo is part of the Argus suite. The suite map below lists every sibling and what it owns; check it before
building something a sibling may already do. **On every release, update this repo's row and capabilities in
argus-cortex's SUITE.md** (a PR to argus-cortex in the same release).

@../argus-core/SUITE.md

Not checked out? Read https://github.com/smk762/argus-cortex/blob/dev/SUITE.md
```
