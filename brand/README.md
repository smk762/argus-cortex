# Argus brand

Marks, lettering and font for the Argus suite. They live in cortex because cortex already holds the suite's shared conventions. They aren't part of the Python package: `brand/` is excluded from both the wheel and the sdist.

**Concept:** Argus Panoptes had a hundred eyes. The suite mark holds many eyes and each package mark holds exactly one. That single eye is the only coloured part of a package mark, and its colour shows the package's stage in the pipeline.

| Stage | Packages | Light | Dark |
|---|---|---|---|
| Data | quarry, curator, lens | `#1d5f74` | `#6fb7cc` |
| Training | forge, proof | `#b33a2a` | `#e57a64` |
| Apps | atlas, darkroom, mint, survey, mask | `#8f6210` | `#e8b54e` |
| Foundation | cortex, studio | `#2a4a7a` | `#8eacdc` |

The suite mark's eye uses the Training red. Ink is `#1c1f22` on light backgrounds and `#e6e8ea` on dark.

## What's here

| Path | What |
|---|---|
| `mark/argus-mark-{light,dark,mono}.svg` | Suite mark: the **vesica iris**. A vesica-piscis eye with a compound iris of seven eyes in the Seed of Life layout. |
| `mark/argus-mark-16px-*.svg` | Pixel-drawn 16 px version. Use it wherever the mark is 16 px; the vector's seven eyes merge below 24 px. |
| `mark/argus-lockup-*.svg` | Suite mark + "argus" lettering. |
| `mark/favicon.ico`, `mark/argus-mark-{16…512}.png` | Favicon (16 px pixel-drawn, 32/48 vector) and PNGs for touch icons and manifests. |
| `packages/<pkg>/argus-<pkg>-{light,dark,mono}.svg` | Package mark. |
| `packages/<pkg>/argus-<pkg>-lockup-{light,dark,mono}.svg` | Package mark + "argus" + package name. Use it for README headers. |
| `mark/favicon/`, `packages/<pkg>/favicon/` | Favicon set for a web UI: `icon.svg` (follows light/dark), `favicon.ico` (16/32/48), `apple-icon.png`, `icon-192.png`, `icon-512.png` (mark on a dark tile). |
| `font/Panoptes-Regular.{ttf,woff2}` | **Panoptes**, the Argus lettering as a font. Draft 0.1: lowercase a–z, hyphen, space. |
| `sheet.html` | The identity sheet: every mark proofed at 16–128 px on light and dark, the construction rules, and the candidates that were considered. **Source of truth.** |
| `tools/panoptes.py` | Regenerates everything above from `sheet.html`. |

Demos and UIs use their package's mark: Lightbox uses lens, Cull uses curator, Realms uses atlas, Safelight uses darkroom, Assay uses mint, Sightline uses survey, and Studio uses the suite mark.

## README header

Paste this at the top of a package README, replacing `lens`:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/smk762/argus-cortex/dev/brand/packages/lens/argus-lens-lockup-dark.svg">
  <img alt="argus lens" src="https://raw.githubusercontent.com/smk762/argus-cortex/dev/brand/packages/lens/argus-lens-lockup-light.svg" height="52">
</picture>
```

## Favicons for the web UIs

Each UI uses its package's set. Studio uses the suite set. Every UI is a Next.js App Router app, so copy the set's
`favicon.ico`, `icon.svg` and `apple-icon.png` into the app directory, and Next adds the `<link>` tags itself:

| UI | Set | Copy into |
|---|---|---|
| argus-studio | `mark/favicon/` | `frontend/src/app/` (replaces the current `favicon.ico`) |
| argus-lightbox | `packages/lens/favicon/` | `web/src/app/` (and delete `web/public/favicon.ico`) |
| argus-cull | `packages/curator/favicon/` | `web/src/app/` |
| argus-safelight | `packages/darkroom/favicon/` | `web/src/app/` |
| argus-assay | `packages/mint/favicon/` | `web/src/app/` |
| argus-sightline | `packages/survey/favicon/` | `src/app/` |
| atlas-realms | `packages/atlas/favicon/` | its static root, with `<link rel="icon">` tags |

`icon-192.png` and `icon-512.png` are for a web app manifest, if a UI has one.

## Rules

- **Grid:** 24 × 24 units with 2 units of padding.
- **Stroke:** 2 units, round caps and joins.
- **Eye:** one filled circle, r 2.25, with at least 0.75 units of clear space around it.
- **Mono:** in one-colour use the eye takes the ink colour. The `-mono` files use `currentColor` throughout.
- **Minimum size:** 16 px, using the pixel-drawn mark at 16 px.
- **Don't** recolour the ink, add a second coloured eye to a package mark, or set the lettering in another font.

## Regenerating

The marks are the `<symbol>` elements in `sheet.html`. To change a mark, edit its symbol, then run:

```bash
pip install skia-pathops fonttools brotli   # font build only
python brand/tools/panoptes.py all          # icons, lockups, specimen, font
python brand/tools/panoptes.py favicons     # per-UI favicon sets (needs ImageMagick)
python brand/tools/panoptes.py social       # 1280x640 social previews into brand/social/ (gitignored)
for f in brand/social/*.svg; do magick "$f" "${f%.svg}.png"; done
```

Social previews have to be uploaded by hand under each repo's **Settings → Social preview**. GitHub has no API for it.

The letters are centre-line drawings in `tools/panoptes.py` (`G`), on the icons' grid: baseline 18, x-height 7, ascender 2, descender 24. The font build expands them to a 2-unit stroke with skia-pathops.
