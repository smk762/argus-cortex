from __future__ import annotations

import stat

import pytest
from pydantic import ValidationError

from argus_cortex.xmp import (
    XmpData,
    XmpError,
    XmpRegion,
    XmpRegions,
    merge_xmp,
    parse_xmp,
    read_xmp,
    render_xmp,
    sidecar_path,
    write_xmp,
)

# A Lightroom-style sidecar: attribute-form simple properties, a foreign schema
# (crs = Camera Raw develop settings), GPS, and two rdf:Description blocks.
LIGHTROOM_SIDECAR = """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 7.0">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:exif="http://ns.adobe.com/exif/1.0/"
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
    xmp:Rating="4"
    exif:GPSLatitude="51,30.0N"
    crs:Exposure2012="+0.35">
   <dc:subject xmlns:dc="http://purl.org/dc/elements/1.1/">
    <rdf:Bag><rdf:li>holiday</rdf:li></rdf:Bag>
   </dc:subject>
  </rdf:Description>
  <rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Label="Red"/>
 </rdf:RDF>
</x:xmpmeta>
"""


def test_render_round_trips_every_field() -> None:
    data = XmpData(
        keywords=["mountain", "lake & sky"],
        description="a <scenic> view",
        hierarchical_keywords=["places|alps"],
        rating=3,
        label="Green",
        persons=["Ada"],
        regions=XmpRegions(width=4000, height=3000, regions=[XmpRegion(x=0.5, y=0.4, w=0.1, h=0.2, name="Ada")]),
        content_rating="safe",
    )
    doc = render_xmp(data)
    assert doc.startswith("<?xpacket begin=")
    assert doc.rstrip().endswith('<?xpacket end="w"?>')
    assert "lake &amp; sky" in doc and "&lt;scenic&gt;" in doc
    assert "mwg-rs:Regions" in doc and "argus:ContentRating" in doc
    assert parse_xmp(doc) == data


def test_illegal_xml_chars_are_stripped() -> None:
    doc = render_xmp(XmpData(keywords=["cat\x00\x07dog"], description="line\x0bbreak"))
    assert parse_xmp(doc) == XmpData(keywords=["catdog"], description="linebreak")


def test_merge_preserves_foreign_properties_and_latest_wins() -> None:
    merged = merge_xmp(LIGHTROOM_SIDECAR, XmpData(keywords=["cat"], description="a cat", rating=1))
    # Foreign schemas and untouched properties survive, with their prefixes.
    assert 'crs:Exposure2012="+0.35"' in merged
    assert 'exif:GPSLatitude="51,30.0N"' in merged
    data = parse_xmp(merged)
    assert data.label == "Red"  # untouched, still in the second Description
    # Conflicting keys: the latest write replaces the old value wholesale.
    assert data.keywords == ["cat"]
    assert data.rating == 1  # was an attribute on the first Description
    assert merged.count("<xmp:Rating>") == 1 and 'xmp:Rating="' not in merged
    assert data.description == "a cat"


def test_none_leaves_property_and_empty_removes_it() -> None:
    base = render_xmp(XmpData(keywords=["a"], description="d", persons=["Ada"]))
    merged = parse_xmp(merge_xmp(base, XmpData(keywords=[], content_rating="explicit")))
    assert merged.keywords is None  # [] removed it
    assert merged.description == "d"  # None left it alone
    assert merged.persons == ["Ada"]
    assert merged.content_rating == "explicit"


def test_successive_writers_merge(tmp_path) -> None:
    """Lens then curator writing the same sidecar: neither clobbers the other."""
    img = tmp_path / "photo.jpg"
    write_xmp(img, XmpData(keywords=["beach"], description="a beach"))
    write_xmp(img, XmpData(persons=["Ada"], content_rating="safe"))
    write_xmp(img, XmpData(description="a sunny beach"))
    data = read_xmp(img)
    assert data == XmpData(keywords=["beach"], description="a sunny beach", persons=["Ada"], content_rating="safe")


def test_replace_discards_existing(tmp_path) -> None:
    img = tmp_path / "photo.jpg"
    sidecar_path(img).write_text(LIGHTROOM_SIDECAR, encoding="utf-8")
    write_xmp(img, XmpData(keywords=["cat"]), replace=True)
    assert read_xmp(img) == XmpData(keywords=["cat"])
    assert "crs:" not in sidecar_path(img).read_text(encoding="utf-8")


def test_malformed_sidecar_is_refused_not_clobbered(tmp_path) -> None:
    img = tmp_path / "photo.jpg"
    sidecar = sidecar_path(img)
    sidecar.write_text("<x:xmpmeta not xml", encoding="utf-8")
    with pytest.raises(XmpError):
        write_xmp(img, XmpData(keywords=["cat"]))
    assert sidecar.read_text(encoding="utf-8") == "<x:xmpmeta not xml"
    assert list(tmp_path.iterdir()) == [sidecar]  # no temp file left behind


def test_dtd_is_refused() -> None:
    evil = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x:xmpmeta xmlns:x="adobe:ns:meta/"/>'
    with pytest.raises(XmpError, match="DTD"):
        parse_xmp(evil)


def test_non_xmp_root_is_refused() -> None:
    with pytest.raises(XmpError, match="not an XMP document"):
        parse_xmp("<html/>")


def test_bare_rdf_root_is_wrapped() -> None:
    bare = (
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="2"/></rdf:RDF>'
    )
    merged = merge_xmp(bare, XmpData(label="Blue"))
    assert "x:xmpmeta" in merged
    assert parse_xmp(merged) == XmpData(rating=2, label="Blue")


def test_write_keeps_existing_file_mode(tmp_path) -> None:
    img = tmp_path / "photo.jpg"
    first = write_xmp(img, XmpData(keywords=["a"]))
    assert stat.S_IMODE(first.stat().st_mode) == 0o644
    first.chmod(0o664)
    write_xmp(img, XmpData(keywords=["b"]))
    assert stat.S_IMODE(first.stat().st_mode) == 0o664


def test_sidecar_path_and_missing_read(tmp_path) -> None:
    assert str(sidecar_path("/pics/photo.jpg")) == "/pics/photo.jpg.xmp"
    assert read_xmp(tmp_path / "none.jpg") is None


def test_validation() -> None:
    with pytest.raises(ValidationError):
        XmpData(rating=6)
    with pytest.raises(ValidationError):
        XmpRegion(x=1.5, y=0, w=0, h=0)
