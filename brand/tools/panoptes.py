"""Argus brand assets: the marks and Panoptes, the Argus lettering, drawn on the icon grid.

Every glyph is a centre-line drawing in the icons' own unit space:
baseline y=18, x-height y=7, ascender y=2, descender y=24, stroke 2, round caps/joins.
Bowls are circles r 5.5 centred on y=12.5.

Everything is generated from brand/sheet.html (the identity sheet; its <symbol>s are the source of truth):

    python panoptes.py icons     -> mark/argus-mark{,-16px}-{light,dark,mono}.svg, packages/<pkg>/argus-<pkg>-*.svg
    python panoptes.py lockups   -> mark/argus-lockup-*.svg, packages/<pkg>/argus-<pkg>-lockup-*.svg
    python panoptes.py specimen  -> font/panoptes-specimen.svg
    python panoptes.py font      -> font/Panoptes-Regular.{ttf,woff2}   (needs: pip install skia-pathops fonttools brotli)
    python panoptes.py social    -> social/*.svg, 1280x640 GitHub social previews (gitignored; rasterise to PNG to upload)
    python panoptes.py all       -> everything except social
"""

import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent
STROKE = 2.0
GAP_STEM, GAP_ROUND, GAP_OPEN = 2.25, 2.0, 1.25  # side bearings by side shape (centre-line to edge of advance)


def circ(cx, cy, r):
    return f"M{cx - r} {cy}a{r} {r} 0 1 0 {2 * r} 0a{r} {r} 0 1 0 {-2 * r} 0Z"


B = circ(5.5, 12.5, 5.5)  # the standard bowl
# name: (centre-line width, left side, right side, path data, filled dots)
G = {
    "a": (11, "r", "s", B + "M11 7v11", []),
    "b": (11, "s", "r", "M0 2v16" + B, []),
    "c": (9.4, "r", "o", "M9.39 8.61A5.5 5.5 0 1 0 9.39 16.39", []),
    "d": (11, "r", "s", B + "M11 2v16", []),
    "e": (11, "r", 1.75, "M0 12.5h11A5.5 5.5 0 1 0 9.39 16.39", []),
    "f": (7.5, "o", 1.0, "M3 18V6a4 4 0 0 1 4-4h.5M0 7h7", []),
    "g": (11, "r", "s", B + "M11 7v12.5a4.5 4.5 0 0 1-4.5 4.5H4", []),
    "h": (11, "s", "s", "M0 2v16M0 12.5a5.5 5.5 0 0 1 11 0V18", []),
    "i": (0, "s", "s", "M0 7v11", [(0, 3.6, 1.4)]),
    "j": (0, "s", "s", "M0 7v13.5a3.5 3.5 0 0 1-3.5 3.5H-4", [(0, 3.6, 1.4)]),
    "k": (9.5, "s", "o", "M0 2v16M9.5 7 1 13.2M4.2 10.8 10 18", []),
    "l": (3, "s", 1.5, "M0 2v13a3 3 0 0 0 3 3", []),
    "m": (20, "s", "s", "M0 7v11M0 12a5 5 0 0 1 10 0v6M10 12a5 5 0 0 1 10 0v6", []),
    "n": (11, "s", "s", "M0 7v11M0 12.5a5.5 5.5 0 0 1 11 0V18", []),
    "o": (11, "r", "r", B, []),
    "p": (11, "s", "r", "M0 7v17" + B, []),
    "q": (11, "r", "s", B + "M11 7v17", []),
    "r": (5.5, "s", 2.0, "M0 18V7M0 12.5A5.5 5.5 0 0 1 5.5 7", []),
    "s": (
        8.6,
        "r",
        "r",
        "M8.4 8.4C7.4 7.3 6.2 7 4.4 7c-2.4 0-4 1.2-4 2.8 0 1.8 1.8 2.3 4 2.7 2.4.4 4.2 1 4.2 2.8 0 1.7-1.8 2.7-4.2 2.7-1.9 0-3.3-.4-4.4-1.6",
        [],
    ),
    "t": (7, "o", 1.5, "M3 3v12a3 3 0 0 0 3 3h1M0 7h7", []),
    "u": (11, "s", "s", "M0 7v5.5a5.5 5.5 0 0 0 11 0M11 7v11", []),
    "v": (10, "o", "o", "M0 7l5 11 5-11", []),
    "w": (16, "o", "o", "M0 7l4 11 4-9 4 9 4-11", []),
    "x": (9, "o", "o", "M0 7l9 11M9 7 0 18", []),
    "y": (10, "o", "o", "M0 7l5 11M10 7 2.27 24", []),
    "z": (9, 1.75, 1.75, "M0 7h9L0 18h9", []),
    "-": (6, 1.5, 1.5, "M0 12.5h6", []),
    " ": (2.5, 2.25, 2.25, "", []),
}
SIDE = {"s": GAP_STEM, "r": GAP_ROUND, "o": GAP_OPEN}


def side(v):
    return SIDE[v] if isinstance(v, str) else v


# The wordmark's own "a": the bowl is the suite mark's compound iris (pupil carries the accent colour).
IRIS_A = [(2.6 * math.cos(math.radians(60 * k)) + 5.5, 2.6 * math.sin(math.radians(60 * k)) + 12.5) for k in range(6)]


def layout(text):
    """Yield (glyph, x offset of its centre line) and return total advance."""
    x, out = 0.0, []
    for ch in text:
        w, left, r, _, _ = G[ch]
        x += side(left)
        out.append((ch, x))
        x += w + side(r)
    return out, x


def svg_group(text, ink="currentColor", accent="var(--pupil)", iris_a=False):
    """Stroke-drawn lettering as SVG elements; returns (markup, advance)."""
    items, adv = layout(text)
    parts = []
    for i, (ch, x) in enumerate(items):
        _, _, _, d, dots = G[ch]
        if d:
            parts.append(f'<path transform="translate({x:.2f} 0)" d="{d}"/>')
        for cx, cy, r in dots:
            parts.append(f'<circle cx="{cx + x:.2f}" cy="{cy}" r="{r}" fill="{ink}" stroke="none"/>')
        if iris_a and ch == "a" and i == 0:
            for cx, cy in IRIS_A:
                parts.append(f'<circle cx="{cx + x:.2f}" cy="{cy:.2f}" r="1.05" fill="{ink}" stroke="none"/>')
            parts.append(f'<circle cx="{5.5 + x:.2f}" cy="12.5" r="1.05" fill="{accent}" stroke="none"/>')
    g = f'<g fill="none" stroke="{ink}" stroke-width="{STROKE}" stroke-linecap="round" stroke-linejoin="round">{"".join(parts)}</g>'
    return g, adv


def specimen():
    ink, acc = "#1c1f22", "#b33a2a"
    lines = [
        "abcdefghijklm",
        "nopqrstuvwxyz",
        "quarry curator lens forge",
        "proof atlas cortex darkroom",
        "mint survey mask studio",
    ]
    rows, width = [], 0
    for i, t in enumerate(lines):
        g, adv = svg_group(t, ink, acc)
        rows.append(f'<g transform="translate(4 {i * 30})">{g}</g>')
        width = max(width, adv)
    h = len(lines) * 30
    (OUT / "font/panoptes-specimen.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -2 {width + 8:.1f} {h + 4}"><rect x="0" y="-2" width="100%" height="100%" fill="#fbfbf9"/>{"".join(rows)}</svg>\n'
    )


def build_font():
    import pathops
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.transformPen import TransformPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.svgLib.path import parse_path

    S = 40  # font units per grid unit: x-height 11 -> 440, ascender 640, descender -240
    upm = 1000
    names = {" ": "space", "-": "hyphen"}
    order = [".notdef"] + [names.get(c, c) for c in G]
    glyphs, metrics, cmap = {}, {}, {}
    for ch, (w, left, r, d, dots) in G.items():
        gname = names.get(ch, ch)
        lsb = side(left)
        path = pathops.Path()
        if d:
            src = pathops.Path()
            parse_path(d, pathops.PathPen(src))
            src.stroke(STROKE, pathops.LineCap.ROUND_CAP, pathops.LineJoin.ROUND_JOIN, 4)
            src.convertConicsToQuads(0.05)
            path = src
        for cx, cy, rad in dots:
            dot = pathops.Path()
            parse_path(circ(cx, cy, rad), pathops.PathPen(dot))
            path = pathops.op(path, dot, pathops.PathOp.UNION) if d else dot
        path.convertConicsToQuads(0.05)
        path.simplify()
        pen = TTGlyphPen(None)
        path.draw(TransformPen(Cu2QuPen(pen, 1.0, reverse_direction=False), (S, 0, 0, -S, lsb * S, 18 * S)))
        glyphs[gname] = pen.glyph()
        adv = round((lsb + w + side(r)) * S)
        metrics[gname] = (adv, 0)
        cmap[ord(ch)] = gname
    pen = TTGlyphPen(None)
    pen.moveTo((50, 0))
    pen.lineTo((50, 440))
    pen.lineTo((350, 440))
    pen.lineTo((350, 0))
    pen.closePath()
    glyphs[".notdef"] = pen.glyph()
    metrics[".notdef"] = (400, 0)

    fb = FontBuilder(upm, isTTF=True)
    fb.setupGlyphOrder(order)
    fb.setupCharacterMap(cmap)
    fb.setupGlyf(glyphs)
    for gname, (adv, _) in metrics.items():
        g = fb.font["glyf"][gname]
        g.recalcBounds(fb.font["glyf"])
        metrics[gname] = (adv, getattr(g, "xMin", 0))
    fb.setupHorizontalMetrics(metrics)
    fb.setupHorizontalHeader(ascent=760, descent=-280)
    fb.setupNameTable(
        {
            "familyName": "Panoptes",
            "styleName": "Regular",
            "uniqueFontIdentifier": "Panoptes-Regular-0.1",
            "fullName": "Panoptes Regular",
            "psName": "Panoptes-Regular",
            "version": "Version 0.100",
            "copyright": "Dragonhound Labs",
            "licenseDescription": "Draft. Lowercase a-z, hyphen, space.",
        }
    )
    fb.setupOS2(
        sTypoAscender=760,
        sTypoDescender=-280,
        sTypoLineGap=0,
        usWinAscent=800,
        usWinDescent=300,
        sxHeight=440,
        sCapHeight=640,
        achVendID="DHLB",
    )
    fb.setupPost()
    dst = OUT / "font"
    dst.mkdir(parents=True, exist_ok=True)
    fb.save(str(dst / "Panoptes-Regular.ttf"))
    fb.font.flavor = "woff2"
    fb.save(str(dst / "Panoptes-Regular.woff2"))


# ---- social previews: suite mark or package icon + lettering, 1280x640 ----
STAGE = {"data": "#6fb7cc", "train": "#e57a64", "apps": "#e8b54e", "base": "#8eacdc"}
PKG = {
    "quarry": "data",
    "curator": "data",
    "lens": "data",
    "forge": "train",
    "proof": "train",
    "atlas": "apps",
    "darkroom": "apps",
    "mint": "apps",
    "survey": "apps",
    "mask": "apps",
    "cortex": "base",
    "studio": "base",
}


def social():

    sym = {k: v[1] for k, v in symbols().items()}
    ink, bg = "#e6e8ea", "#121416"
    dst = OUT / "social"
    dst.mkdir(parents=True, exist_ok=True)
    for name, stage in [("suite", "train")] + list(PKG.items()):
        acc = STAGE[stage]
        icon = sym["m_vesica" if name == "suite" else name].replace("currentColor", ink).replace("var(--pupil)", acc)
        word, wadv = svg_group("argus", ink, STAGE["train"], iris_a=True)
        if name == "suite":
            body, total = word, wadv
        else:
            pkg, padv = svg_group(name, acc, acc)
            body = word + f'<g transform="translate({wadv + 6:.2f} 0)">{pkg}</g>'
            total = wadv + 6 + padv
        # icon 24u + 8u gap + lettering, centred; scale to fit 1040px wide
        span = 24 + 8 + total
        k = min(1040 / span, 18)
        x0 = (1280 - span * k) / 2
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="640" viewBox="0 0 1280 640">'
            f'<rect width="1280" height="640" fill="{bg}"/>'
            f'<g transform="translate({x0:.1f} {320 - 13 * k:.1f}) scale({k:.3f})">'
            f'<g transform="translate(0 1)">{icon}</g><g transform="translate(32 0)">{body}</g></g></svg>\n'
        )
        (dst / f"argus-{name}-social.svg").write_text(svg)


THEMES = {"light": ("#1c1f22", "light"), "dark": ("#e6e8ea", "dark"), "mono": ("currentColor", None)}
STAGE_LIGHT = {"data": "#1d5f74", "train": "#b33a2a", "apps": "#8f6210", "base": "#2a4a7a"}


def symbols():
    import re

    html = (OUT / "sheet.html").read_text()
    return {
        m.group(1): (m.group(2), m.group(3))
        for m in re.finditer(r'<symbol id="i-(\w+)" viewBox="([^"]+)">(.*?)</symbol>', html, re.S)
    }


def accent(stage, theme):
    return {"light": STAGE_LIGHT, "dark": STAGE}.get(theme, {}).get(stage, "currentColor")


def paint(body, ink, acc):
    return body.replace("currentColor", ink).replace("var(--pupil)", acc)


def write(path, viewbox, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{viewbox}">{body}</svg>\n')


def icons():
    sym = symbols()
    for theme, (ink, _) in THEMES.items():
        acc = accent("train", theme)
        write(OUT / f"mark/argus-mark-{theme}.svg", *_vb(sym["m_vesica"], ink, acc))
        write(OUT / f"mark/argus-mark-16px-{theme}.svg", *_vb(sym["px16"], ink, acc))
        for pkg, stage in PKG.items():
            write(OUT / f"packages/{pkg}/argus-{pkg}-{theme}.svg", *_vb(sym[pkg], ink, accent(stage, theme)))


def _vb(s, ink, acc):
    return s[0], paint(s[1], ink, acc)


def lockups():
    """Icon (24 units) + 8-unit gap + "argus" (iris a) [+ 6-unit gap + package name in its stage colour]."""
    sym = symbols()
    for theme, (ink, _) in THEMES.items():
        for pkg, stage in [(None, "train")] + list(PKG.items()):
            acc = accent(stage, theme)
            icon = paint(sym[pkg or "m_vesica"][1], ink, acc)
            word, wadv = svg_group("argus", ink, accent("train", theme), iris_a=True)
            body, total = word, wadv
            if pkg:
                name, nadv = svg_group(pkg, acc, acc)
                body += f'<g transform="translate({wadv + 6:.2f} 0)">{name}</g>'
                total = wadv + 6 + nadv
            svg = f'<g transform="translate(0 1)">{icon}</g><g transform="translate(32 0)">{body}</g>'
            dst = OUT / (f"packages/{pkg}/argus-{pkg}-lockup-{theme}.svg" if pkg else f"mark/argus-lockup-{theme}.svg")
            write(dst, f"0 0 {32 + total:.1f} 26", svg)


def all_():
    icons()
    lockups()
    specimen()
    build_font()


if __name__ == "__main__":
    {"icons": icons, "lockups": lockups, "specimen": specimen, "font": build_font, "social": social, "all": all_}[
        sys.argv[1]
    ]()
