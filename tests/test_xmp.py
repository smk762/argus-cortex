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


@pytest.mark.parametrize("encoding", ["utf-16", "utf-16-le", "utf-16-be"])
def test_dtd_is_refused_in_any_encoding(encoding: str) -> None:
    """The byte scan can't see ``<!DOCTYPE`` in UTF-16; the parser's own DTD handler does."""
    evil = '<!DOCTYPE x [<!ENTITY a "aaaa">]><x:xmpmeta xmlns:x="adobe:ns:meta/">&a;</x:xmpmeta>'
    if encoding == "utf-16":
        evil = ('<?xml version="1.0" encoding="UTF-16"?>' + evil).encode(encoding)  # Python writes the BOM
    else:
        evil = ("\ufeff" + evil).encode(encoding)  # a BOM and no declaration: expat reads the byte order
    assert b"<!DOCTYPE" not in evil
    with pytest.raises(XmpError, match="DTD"):
        parse_xmp(evil)
    with pytest.raises(XmpError, match="DTD"):
        merge_xmp(evil, XmpData(keywords=["cat"]))


def test_a_utf16_sidecar_without_a_dtd_still_parses() -> None:
    assert parse_xmp(render_xmp(XmpData(keywords=["cat"])).encode("utf-16")).keywords == ["cat"]


def test_an_encoding_the_parser_cannot_read_is_an_xmp_error() -> None:
    doc = '\ufeff<?xml version="1.0" encoding="UTF-16-LE"?><x:xmpmeta xmlns:x="adobe:ns:meta/"/>'
    with pytest.raises(XmpError, match="encoding"):
        parse_xmp(doc.encode("utf-16-le"))


@pytest.mark.parametrize("bad", ["inf", "-Infinity", "1e400", "nan", "wide"])
def test_unusable_region_dimensions_are_ignored_not_raised(bad: str) -> None:
    doc = render_xmp(XmpData(keywords=["cat"], regions=XmpRegions(width=10, height=10, regions=[])))
    doc = doc.replace('stDim:w="10"', f'stDim:w="{bad}"')
    assert f'stDim:w="{bad}"' in doc
    data = parse_xmp(doc)
    assert data.regions is None
    assert data.keywords == ["cat"]


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


def test_region_structs_are_valid_rdf() -> None:
    """rdf:parseType="Resource" may not carry property attributes (strict parsers refuse it)."""
    import xml.etree.ElementTree as ET

    doc = render_xmp(XmpData(regions=XmpRegions(width=10, height=10, regions=[XmpRegion(x=0.5, y=0.5, w=0.1, h=0.1)])))
    parse_type = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}parseType"
    root = ET.fromstring(doc.split("?>", 1)[1].rsplit("<?xpacket", 1)[0])
    for el in root.iter():
        if parse_type in el.attrib:
            assert set(el.attrib) == {parse_type}, el.tag


def test_a_sidecar_cannot_rebind_the_suites_prefixes() -> None:
    import xml.etree.ElementTree as ET

    hostile = """<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" xmlns:junk="http://ns.adobe.com/xap/1.0/" xmlns:foo="urn:foo"
    junk:Label="Red" foo:Thing="1"/>
 </rdf:RDF></x:xmpmeta>"""
    before = dict(ET._namespace_map)
    merged = merge_xmp(hostile, XmpData(rating=2))
    assert 'xmp:Label="Red"' in merged and "junk:" not in merged
    assert 'foo:Thing="1"' in merged  # a foreign schema keeps its own prefix
    assert dict(ET._namespace_map) == before  # nothing leaked into the global registry
    assert "<xmp:Rating>" in render_xmp(XmpData(rating=1))


def test_concurrent_writers_do_not_drop_each_others_properties(tmp_path) -> None:
    import threading

    img = tmp_path / "photo.jpg"
    updates = [
        XmpData(keywords=["k"]),
        XmpData(description="d"),
        XmpData(hierarchical_keywords=["a|b"]),
        XmpData(rating=3),
        XmpData(label="Blue"),
        XmpData(persons=["Ada"]),
        XmpData(content_rating="safe"),
    ]
    for _ in range(5):
        sidecar_path(img).unlink(missing_ok=True)
        barrier = threading.Barrier(len(updates))

        def write(update: XmpData, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            write_xmp(img, update)

        threads = [threading.Thread(target=write, args=(u,)) for u in updates]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert read_xmp(img) == XmpData(
            keywords=["k"],
            description="d",
            hierarchical_keywords=["a|b"],
            rating=3,
            label="Blue",
            persons=["Ada"],
            content_rating="safe",
        )


def test_regions_nested_in_rdf_description_are_read() -> None:
    doc = """<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
   xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/"
   xmlns:stArea="http://ns.adobe.com/xmp/sType/Area#" xmlns:stDim="http://ns.adobe.com/xap/1.0/sType/Dimensions#">
  <rdf:Description rdf:about="">
   <mwg-rs:Regions><rdf:Description>
    <mwg-rs:AppliedToDimensions><rdf:Description stDim:w="4000" stDim:h="3000" stDim:unit="pixel"/>
    </mwg-rs:AppliedToDimensions>
    <mwg-rs:RegionList><rdf:Bag>
     <rdf:li><rdf:Description mwg-rs:Name="Ada" mwg-rs:Type="Face">
      <mwg-rs:Area><rdf:Description stArea:x="0.5" stArea:y="0.4" stArea:w="0.1" stArea:h="0.2"/></mwg-rs:Area>
     </rdf:Description></rdf:li>
     <rdf:li><rdf:Description mwg-rs:Name="no area"/></rdf:li>
    </rdf:Bag></mwg-rs:RegionList>
   </rdf:Description></mwg-rs:Regions>
  </rdf:Description>
 </rdf:RDF></x:xmpmeta>"""
    assert parse_xmp(doc).regions == XmpRegions(
        width=4000, height=3000, regions=[XmpRegion(x=0.5, y=0.4, w=0.1, h=0.2, name="Ada")]
    )
