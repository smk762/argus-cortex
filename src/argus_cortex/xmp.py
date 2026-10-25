"""XMP sidecar read / merge / write — the suite's one XMP implementation.

XMP sidecars (``<image>.<ext>.xmp``) are the zero-coupling interop surface:
Lightroom, digiKam and Immich all ingest them, so metadata any stage writes
here shows up in the user's own photo tools. Hoisted from
``argus_lens.connectors.xmp`` so lens (captions/keywords) and curator (face
regions, content rating) share one writer instead of each growing their own.

Writes **merge**: the existing sidecar is parsed, each property being written
replaces that property wholesale (the latest write wins on a conflicting key),
and every other property — ratings, GPS, develop settings, another stage's
fields — is preserved untouched. A field left as ``None`` in :class:`XmpData`
is not touched; an empty list/string removes the property.

Stdlib + pydantic only (``xml.etree``), so it is safe for the top-level import.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

NS: dict[str, str] = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "mwg-rs": "http://www.metadataworkinggroup.com/schemas/regions/",
    "stArea": "http://ns.adobe.com/xmp/sType/Area#",
    "stDim": "http://ns.adobe.com/xap/1.0/sType/Dimensions#",
    # Suite-specific properties with no standard home (e.g. a content rating —
    # deliberately not xmp:Rating, which is the user's star rating).
    "argus": "https://github.com/smk762/argus-cortex/ns/xmp/1.0/",
}

for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)

_XPACKET_BEGIN = '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
_XPACKET_END = '\n<?xpacket end="w"?>\n'

# Characters that are illegal in XML 1.0 even when escaped. Stripped so a value
# can never produce a sidecar Lightroom/digiKam/Immich would refuse to parse.
_ILLEGAL_XML_CHARS = re.compile("[^\u0009\u000a\u000d -퟿-�\U00010000-\U0010ffff]")
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


class XmpError(ValueError):
    """An existing sidecar could not be parsed; it is left untouched rather than clobbered."""


class XmpRegion(BaseModel):
    """One MWG region (``mwg-rs:RegionList`` item), e.g. a detected face.

    Coordinates are normalised to ``[0, 1]`` and, per the MWG spec, ``x``/``y``
    are the region's **centre**, not its top-left corner.
    """

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    w: float = Field(ge=0, le=1)
    h: float = Field(ge=0, le=1)
    name: str | None = None
    type: Literal["Face", "Pet", "Focus", "BarCode"] = "Face"


class XmpRegions(BaseModel):
    """The ``mwg-rs:Regions`` struct: regions plus the image dimensions they apply to."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    regions: list[XmpRegion] = Field(default_factory=list)


class XmpData(BaseModel):
    """The XMP properties the suite reads and writes.

    ``None`` means "leave this property alone" on a merge; an empty list or
    string removes it.
    """

    keywords: list[str] | None = None  # dc:subject
    description: str | None = None  # dc:description (x-default)
    hierarchical_keywords: list[str] | None = None  # lr:hierarchicalSubject, "a|b|c"
    rating: int | None = Field(default=None, ge=-1, le=5)  # xmp:Rating (user stars; -1 = rejected)
    label: str | None = None  # xmp:Label (colour label)
    persons: list[str] | None = None  # Iptc4xmpExt:PersonInImage
    regions: XmpRegions | None = None  # mwg-rs:Regions
    content_rating: str | None = None  # argus:ContentRating (e.g. safe / suggestive / explicit)


def _q(qname: str) -> str:
    """``"dc:subject"`` → ``"{http://purl.org/dc/elements/1.1/}subject"``."""
    prefix, local = qname.split(":", 1)
    return f"{{{NS[prefix]}}}{local}"


def _text(value: str) -> str:
    return _ILLEGAL_XML_CHARS.sub("", value)


def _num(value: float) -> str:
    return f"{value:.6g}"


def _container(tag: str, kind: str, items: list[str]) -> ET.Element:
    prop = ET.Element(_q(tag))
    container = ET.SubElement(prop, _q(f"rdf:{kind}"))
    for item in items:
        ET.SubElement(container, _q("rdf:li")).text = _text(item)
    return prop


def _lang_alt(tag: str, value: str) -> ET.Element:
    prop = ET.Element(_q(tag))
    li = ET.SubElement(ET.SubElement(prop, _q("rdf:Alt")), _q("rdf:li"), {_XML_LANG: "x-default"})
    li.text = _text(value)
    return prop


def _simple(tag: str, value: str) -> ET.Element:
    prop = ET.Element(_q(tag))
    prop.text = _text(value)
    return prop


def _regions(value: XmpRegions) -> ET.Element:
    prop = ET.Element(_q("mwg-rs:Regions"), {_q("rdf:parseType"): "Resource"})
    ET.SubElement(
        prop,
        _q("mwg-rs:AppliedToDimensions"),
        {
            _q("rdf:parseType"): "Resource",
            _q("stDim:w"): str(value.width),
            _q("stDim:h"): str(value.height),
            _q("stDim:unit"): "pixel",
        },
    )
    bag = ET.SubElement(ET.SubElement(prop, _q("mwg-rs:RegionList")), _q("rdf:Bag"))
    for region in value.regions:
        li = ET.SubElement(bag, _q("rdf:li"), {_q("rdf:parseType"): "Resource"})
        if region.name:
            ET.SubElement(li, _q("mwg-rs:Name")).text = _text(region.name)
        ET.SubElement(li, _q("mwg-rs:Type")).text = region.type
        ET.SubElement(
            li,
            _q("mwg-rs:Area"),
            {
                _q("rdf:parseType"): "Resource",
                _q("stArea:x"): _num(region.x),
                _q("stArea:y"): _num(region.y),
                _q("stArea:w"): _num(region.w),
                _q("stArea:h"): _num(region.h),
                _q("stArea:unit"): "normalized",
            },
        )
    return prop


def _properties(data: XmpData) -> list[tuple[str, ET.Element | None]]:
    """``(qname, element)`` for every field that is set; ``element`` is None to remove it."""
    out: list[tuple[str, ET.Element | None]] = []

    def add(qname: str, value: object, build) -> None:
        if value is None:
            return
        out.append((qname, build(value) if value != [] and value != "" else None))

    add("dc:subject", data.keywords, lambda v: _container("dc:subject", "Bag", v))
    add("dc:description", data.description, lambda v: _lang_alt("dc:description", v))
    add("lr:hierarchicalSubject", data.hierarchical_keywords, lambda v: _container("lr:hierarchicalSubject", "Bag", v))
    add("xmp:Rating", data.rating, lambda v: _simple("xmp:Rating", str(v)))
    add("xmp:Label", data.label, lambda v: _simple("xmp:Label", v))
    add("Iptc4xmpExt:PersonInImage", data.persons, lambda v: _container("Iptc4xmpExt:PersonInImage", "Bag", v))
    add("mwg-rs:Regions", data.regions, _regions)
    add("argus:ContentRating", data.content_rating, lambda v: _simple("argus:ContentRating", v))
    return out


def _parse(raw: bytes) -> ET.Element:
    """Parse a sidecar into its ``x:xmpmeta`` root (wrapping a bare ``rdf:RDF``)."""
    # XMP never carries a DTD; refusing one outright rules out entity-expansion tricks.
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise XmpError("XMP sidecar contains a DTD/entity declaration; refusing to parse it")
    for prefix, uri in _declared_namespaces(raw):
        # Keep a foreign schema's own prefix on re-serialisation (cosmetic, but
        # Lightroom-written sidecars stay recognisable). Ours always win.
        if prefix and prefix not in NS:
            with contextlib.suppress(ValueError):
                ET.register_namespace(prefix, uri)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise XmpError(f"malformed XMP sidecar: {exc}") from exc
    if root.tag == _q("x:xmpmeta"):
        return root
    if root.tag == _q("rdf:RDF"):
        meta = ET.Element(_q("x:xmpmeta"))
        meta.append(root)
        return meta
    raise XmpError(f"not an XMP document (root element {root.tag!r})")


def _declared_namespaces(raw: bytes) -> list[tuple[str, str]]:
    try:
        return [ns for _, ns in ET.iterparse(io.BytesIO(raw), events=("start-ns",))]
    except ET.ParseError:
        return []  # surfaced properly by ET.fromstring in _parse


def _fresh() -> ET.Element:
    meta = ET.Element(_q("x:xmpmeta"))
    rdf = ET.SubElement(meta, _q("rdf:RDF"))
    ET.SubElement(rdf, _q("rdf:Description"), {_q("rdf:about"): ""})
    return meta


def _descriptions(meta: ET.Element) -> list[ET.Element]:
    rdf = meta.find(_q("rdf:RDF"))
    if rdf is None:
        rdf = ET.SubElement(meta, _q("rdf:RDF"))
    descs = rdf.findall(_q("rdf:Description"))
    if not descs:
        descs = [ET.SubElement(rdf, _q("rdf:Description"), {_q("rdf:about"): ""})]
    return descs


def merge_xmp(existing: bytes | str | None, data: XmpData) -> str:
    """Merge *data* into an existing XMP document and return the new document.

    Each property set in *data* replaces that property wherever it appears
    (element or attribute form, in any ``rdf:Description``); everything else is
    kept. ``existing=None`` renders a fresh document.

    Raises:
        XmpError: if *existing* is not a parseable XMP document.
    """
    if isinstance(existing, str):
        existing = existing.encode("utf-8")
    meta = _parse(existing) if existing else _fresh()
    descs = _descriptions(meta)
    for qname, element in _properties(data):
        tag = _q(qname)
        for desc in descs:
            desc.attrib.pop(tag, None)
            for child in desc.findall(tag):
                desc.remove(child)
        if element is not None:
            descs[0].append(element)
    ET.indent(meta, space=" ")
    return _XPACKET_BEGIN + ET.tostring(meta, encoding="unicode") + _XPACKET_END


def render_xmp(data: XmpData) -> str:
    """Render a fresh XMP document holding only *data* (pure; useful for tests)."""
    return merge_xmp(None, data)


def _prop_values(desc: ET.Element, qname: str) -> list[str] | None:
    tag = _q(qname)
    if tag in desc.attrib:
        return [desc.attrib[tag]]
    prop = desc.find(tag)
    if prop is None:
        return None
    items = prop.findall(f"./*/{_q('rdf:li')}")
    if items:
        return [li.text or "" for li in items]
    return [prop.text or ""]


def _read_regions(desc: ET.Element) -> XmpRegions | None:
    prop = desc.find(_q("mwg-rs:Regions"))
    if prop is None:
        return None

    def field(el: ET.Element | None, qname: str) -> str | None:
        if el is None:
            return None
        if _q(qname) in el.attrib:
            return el.attrib[_q(qname)]
        child = el.find(_q(qname))
        return child.text if child is not None else None

    dims = prop.find(_q("mwg-rs:AppliedToDimensions"))
    regions: list[XmpRegion] = []
    for li in prop.findall(f"./{_q('mwg-rs:RegionList')}/*/{_q('rdf:li')}"):
        area = li.find(_q("mwg-rs:Area"))
        try:
            regions.append(
                XmpRegion(
                    x=float(field(area, "stArea:x") or 0),
                    y=float(field(area, "stArea:y") or 0),
                    w=float(field(area, "stArea:w") or 0),
                    h=float(field(area, "stArea:h") or 0),
                    name=field(li, "mwg-rs:Name"),
                    type=field(li, "mwg-rs:Type") or "Face",
                )
            )
        except ValueError:
            continue  # a foreign region we can't represent; it is still preserved on disk
    try:
        return XmpRegions(
            width=int(float(field(dims, "stDim:w") or 0)),
            height=int(float(field(dims, "stDim:h") or 0)),
            regions=regions,
        )
    except ValueError:
        return None


def parse_xmp(raw: bytes | str) -> XmpData:
    """Read the suite's known properties out of an XMP document.

    Raises:
        XmpError: if *raw* is not a parseable XMP document.
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    descs = _descriptions(_parse(raw))
    found: dict[str, object] = {}

    def first(qname: str) -> list[str] | None:
        for desc in descs:
            values = _prop_values(desc, qname)
            if values is not None:
                return values
        return None

    for field_name, qname in (
        ("keywords", "dc:subject"),
        ("hierarchical_keywords", "lr:hierarchicalSubject"),
        ("persons", "Iptc4xmpExt:PersonInImage"),
    ):
        if (values := first(qname)) is not None:
            found[field_name] = values
    for field_name, qname in (
        ("description", "dc:description"),
        ("label", "xmp:Label"),
        ("content_rating", "argus:ContentRating"),
    ):
        if (values := first(qname)) is not None:
            found[field_name] = values[0] if values else ""
    if (values := first("xmp:Rating")) and values[0].strip().lstrip("-").isdigit():
        found["rating"] = int(values[0])
    for desc in descs:
        if (regions := _read_regions(desc)) is not None:
            found["regions"] = regions
            break
    return XmpData.model_validate(found)


def sidecar_path(image_path: str | os.PathLike[str]) -> Path:
    """Return the sidecar path for an image: ``photo.jpg`` → ``photo.jpg.xmp``."""
    path = Path(image_path)
    return path.with_suffix(path.suffix + ".xmp")


def read_xmp(image_path: str | os.PathLike[str]) -> XmpData | None:
    """Read the sidecar next to *image_path*, or ``None`` if there is none.

    Raises:
        XmpError: if the sidecar exists but cannot be parsed.
    """
    sidecar = sidecar_path(image_path)
    if not sidecar.exists():
        return None
    return parse_xmp(sidecar.read_bytes())


def write_xmp(image_path: str | os.PathLike[str], data: XmpData, *, replace: bool = False) -> Path:
    """Merge *data* into the sidecar next to *image_path* and return its path.

    By default the existing sidecar is merged (latest write wins per property,
    everything else preserved). ``replace=True`` discards the existing sidecar
    and writes a document holding only *data*. The write is atomic (temp file +
    rename), so a crash never leaves a half-written sidecar.

    Raises:
        XmpError: if merging and the existing sidecar cannot be parsed — it is
            left untouched rather than clobbered.
    """
    sidecar = sidecar_path(image_path)
    exists = sidecar.exists()
    existing = sidecar.read_bytes() if exists and not replace else None
    doc = merge_xmp(existing, data)
    # mkstemp creates 0600; keep the old sidecar's mode (or a world-readable
    # default) so other tools — e.g. an Immich container user — can still read it.
    mode = sidecar.stat().st_mode & 0o777 if exists else 0o644
    fd, tmp = tempfile.mkstemp(dir=sidecar.parent, prefix=f".{sidecar.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(doc)
        os.chmod(tmp, mode)
        os.replace(tmp, sidecar)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return sidecar
