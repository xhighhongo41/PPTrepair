"""Tests for :mod:`pptrepair.rebuild`.

Exercises package reconstruction end to end with an in-memory fake
:class:`~pptrepair.salvage.SalvageReader`. Fixtures are derived from
:func:`fixtures.build_minimal_pptx` (unzipped into a name -> bytes
mapping) so tests can add, drop or rewrite individual parts without
depending on the real salvage implementation. Each rebuilt archive is
re-diagnosed through the ``check`` pipeline (scanner -> census ->
classify) to prove it is a well-formed, intact package again.
"""

from __future__ import annotations

import io
import posixpath
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Iterator

import pytest
from fixtures import build_minimal_pptx

from pptrepair.census import (EntryResult, categorize, from_central_directory,
                              from_lfh_scan)
from pptrepair.classify import Verdict, classify
from pptrepair.rebuild import RebuildError, rebuild_package
from pptrepair.salvage import SalvagedEntry
from pptrepair.scanner import scan_structure

#: Small media payload so fixtures stay fast to build and scan.
_MEDIA_BYTES = 8192

#: Fixed ZIP timestamp handed back by the fake reader (seconds even so it
#: round-trips through the two-second DOS-time granularity).
_FIXED_DATE_TIME = (2020, 1, 2, 3, 4, 6)

#: Chunk size for the fake reader, small enough to exercise chunking.
_CHUNK = 4096

_PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P14_NS = "http://schemas.microsoft.com/office/powerpoint/2010/main"

#: Relationship type URIs used when building dangling-reference fixtures.
_LAYOUT_REL = f"{_R_NS}/slideLayout"
_IMAGE_REL = f"{_R_NS}/image"
_VIDEO_REL = f"{_R_NS}/video"
_MEDIA_REL = f"{_R_NS}/media"
_HLINK_REL = f"{_R_NS}/hyperlink"
_CUSTOM_REL = f"{_R_NS}/customXml"


class FakeReader:
    """In-memory stand-in for :class:`SalvageReader`.

    Provides the ``open`` / ``datetime_of`` surface that
    :func:`rebuild_package` relies on, backed by a name -> bytes mapping.
    """

    def __init__(self, payloads: dict[str, bytes],
                 timestamp: tuple[int, ...] | None = _FIXED_DATE_TIME) -> None:
        """Store the payloads and the timestamp reported for every entry."""
        self._payloads = payloads
        self._timestamp = timestamp

    def open(self, salvaged: SalvagedEntry) -> Iterator[bytes]:
        """Yield the stored payload of *salvaged* in fixed-size chunks."""
        data = self._payloads[salvaged.name]
        for start in range(0, len(data) or 1, _CHUNK):
            yield data[start:start + _CHUNK]

    def datetime_of(self, salvaged: SalvagedEntry) -> tuple[int, ...] | None:
        """Return the fixed timestamp configured for this reader."""
        return self._timestamp


def _unzip(archive: bytes) -> dict[str, bytes]:
    """Return a name -> decompressed-bytes mapping for *archive*."""
    payloads: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(archive)) as zf:
        for name in zf.namelist():
            payloads[name] = zf.read(name)
    return payloads


def _entries(payloads: dict[str, bytes]) -> list[SalvagedEntry]:
    """Wrap every payload name in a minimal :class:`SalvagedEntry`."""
    entries: list[SalvagedEntry] = []
    for name, data in payloads.items():
        info = EntryResult(name=name, category=categorize(name),
                           header_offset=0, file_size=len(data), ok=True)
        entries.append(SalvagedEntry(name=name, category=info.category,
                                     source="cd", entry=info))
    return entries


def _verdict(path: Path) -> Verdict:
    """Run the check pipeline over *path* and return its verdict."""
    structure = scan_structure(path)
    cd_census = from_central_directory(path)
    lfh_census = from_lfh_scan(path)
    return classify(path, structure, cd_census, lfh_census).verdict


def _member(path: Path, name: str) -> bytes:
    """Return the decompressed bytes of member *name* inside *path*."""
    with zipfile.ZipFile(path) as zf:
        return zf.read(name)


def _is_plumbing(name: str) -> bool:
    """Return True for parts the rebuild is allowed to re-serialise."""
    return (name == "[Content_Types].xml"
            or name == "ppt/presentation.xml"
            or name.endswith(".rels"))


def _presentation_xml(masters: list[tuple[str, str]],
                      slides: list[tuple[str, str]]) -> bytes:
    """Build a presentation part from ``(id, r:id)`` list entries."""
    master_items = "".join(
        f'<p:sldMasterId id="{sid}" r:id="{rid}"/>' for sid, rid in masters)
    slide_items = "".join(
        f'<p:sldId id="{sid}" r:id="{rid}"/>' for sid, rid in slides)
    body = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<p:presentation xmlns:p="{_PML_NS}" xmlns:r="{_R_NS}">'
        f'<p:sldMasterIdLst>{master_items}</p:sldMasterIdLst>'
        f'<p:sldIdLst>{slide_items}</p:sldIdLst>'
        '</p:presentation>'
    )
    return body.encode("utf-8")


def test_full_roundtrip_preserves_content(tmp_path: Path) -> None:
    """A complete salvage set rebuilds into an intact, faithful package."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=3, media_bytes=_MEDIA_BYTES))
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    assert output.exists()
    with zipfile.ZipFile(output) as zf:
        assert zf.testzip() is None
    assert _verdict(output) == Verdict.NORMAL

    # Untouched (non-plumbing) parts stay byte-identical.
    for name, data in payloads.items():
        if _is_plumbing(name):
            continue
        assert _member(output, name) == data

    # Streamed entries keep the original timestamp.
    with zipfile.ZipFile(output) as zf:
        assert zf.getinfo("ppt/media/image1.png").date_time \
            == _FIXED_DATE_TIME

    assert not result.pruned_relationships
    assert not result.pruned_slide_ids
    assert set(payloads) <= set(result.written_entries)


def test_missing_slide_prunes_references(tmp_path: Path) -> None:
    """Dropping a slide prunes its relationship, list id and Override."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=3, media_bytes=_MEDIA_BYTES))
    for gone in ("ppt/slides/slide2.xml",
                 "ppt/slides/_rels/slide2.xml.rels",
                 "ppt/media/image1.png"):
        del payloads[gone]
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    presentation = ET.fromstring(_member(output, "ppt/presentation.xml"))
    slide_ids = presentation.findall(
        f"{{{_PML_NS}}}sldIdLst/{{{_PML_NS}}}sldId")
    assert len(slide_ids) == 2

    rels = ET.fromstring(_member(output, "ppt/_rels/presentation.xml.rels"))
    assert "slides/slide2.xml" not in {rel.get("Target") for rel in rels}

    content_types = _member(output, "[Content_Types].xml").decode("utf-8")
    assert "/ppt/slides/slide2.xml" not in content_types

    assert any("slide2" in item for item in result.pruned_relationships)
    assert result.pruned_slide_ids
    assert _verdict(output) == Verdict.NORMAL


def test_content_types_synthesised_when_lost(tmp_path: Path) -> None:
    """A lost ``[Content_Types].xml`` is rebuilt with full coverage."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES))
    del payloads["[Content_Types].xml"]
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    assert "[Content_Types].xml" in result.synthesized_parts
    types = ET.fromstring(_member(output, "[Content_Types].xml"))
    default_exts = {d.get("Extension").lower()
                    for d in types.findall(f"{{{_CT_NS}}}Default")}
    assert {"rels", "xml", "png"} <= default_exts

    overrides = {o.get("PartName"): o.get("ContentType")
                 for o in types.findall(f"{{{_CT_NS}}}Override")}
    assert overrides.get("/ppt/presentation.xml", "") \
        .endswith("presentation.main+xml")
    assert overrides.get("/ppt/slides/slide1.xml", "").endswith("slide+xml")
    assert "/ppt/slideMasters/slideMaster1.xml" in overrides
    assert "/ppt/slideLayouts/slideLayout1.xml" in overrides
    assert "/ppt/theme/theme1.xml" in overrides
    assert "/docProps/core.xml" in overrides
    assert "/docProps/app.xml" in overrides
    assert _verdict(output) == Verdict.NORMAL


def test_root_rels_synthesised_when_lost(tmp_path: Path) -> None:
    """A lost ``_rels/.rels`` is rebuilt from the surviving core parts."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES))
    del payloads["_rels/.rels"]
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    assert "_rels/.rels" in result.synthesized_parts
    rels = ET.fromstring(_member(output, "_rels/.rels"))
    targets = {rel.get("Target"): rel.get("Type") for rel in rels}
    assert "ppt/presentation.xml" in targets
    assert targets["ppt/presentation.xml"].endswith("/officeDocument")
    assert "docProps/core.xml" in targets
    assert "docProps/app.xml" in targets
    assert _verdict(output) == Verdict.NORMAL


def test_missing_presentation_raises(tmp_path: Path) -> None:
    """Rebuilding without ``ppt/presentation.xml`` raises RebuildError."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=1, media_bytes=_MEDIA_BYTES))
    del payloads["ppt/presentation.xml"]
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    with pytest.raises(RebuildError):
        rebuild_package(reader, _entries(payloads), output)


def test_external_relationship_is_kept(tmp_path: Path) -> None:
    """External relationships survive pruning; dangling internal ones do not."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES))
    payloads["ppt/_rels/presentation.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<Relationships xmlns="{_REL_NS}">'
        f'<Relationship Id="rId1" Type="{_R_NS}/slideMaster" '
        'Target="slideMasters/slideMaster1.xml"/>'
        f'<Relationship Id="rId2" Type="{_R_NS}/slide" '
        'Target="slides/slide1.xml"/>'
        f'<Relationship Id="rId3" Type="{_R_NS}/slide" '
        'Target="slides/slide2.xml"/>'
        f'<Relationship Id="rId4" Type="{_R_NS}/theme" '
        'Target="theme/theme1.xml"/>'
        f'<Relationship Id="rId9" Type="{_R_NS}/hyperlink" '
        'Target="https://example.com/" TargetMode="External"/>'
        f'<Relationship Id="rId8" Type="{_R_NS}/image" '
        'Target="../media/missing.png"/>'
        '</Relationships>'
    ).encode("utf-8")
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    rels = ET.fromstring(_member(output, "ppt/_rels/presentation.xml.rels"))
    by_id = {rel.get("Id"): rel for rel in rels}
    assert "rId9" in by_id
    assert by_id["rId9"].get("Target") == "https://example.com/"
    assert "rId8" not in by_id
    assert any("rId8" in item for item in result.pruned_relationships)
    assert not any("rId9" in item for item in result.pruned_relationships)
    assert _verdict(output) == Verdict.NORMAL


def test_namespace_declaration_preserved(tmp_path: Path) -> None:
    """An unused-but-declared namespace (p14) survives re-serialisation."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES))
    mc_ns = "http://schemas.openxmlformats.org/markup-compatibility/2006"
    p14_ns = "http://schemas.microsoft.com/office/powerpoint/2010/main"
    payloads["ppt/presentation.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<p:presentation xmlns:p="{_PML_NS}" xmlns:r="{_R_NS}" '
        f'xmlns:mc="{mc_ns}" xmlns:p14="{p14_ns}" mc:Ignorable="p14">'
        '<p:sldMasterIdLst>'
        '<p:sldMasterId id="2147483648" r:id="rId1"/>'
        '</p:sldMasterIdLst>'
        '<p:sldIdLst>'
        '<p:sldId id="256" r:id="rId2"/>'
        '<p:sldId id="257" r:id="rId3"/>'
        '</p:sldIdLst>'
        '</p:presentation>'
    ).encode("utf-8")
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    rebuild_package(reader, _entries(payloads), output)

    text = _member(output, "ppt/presentation.xml").decode("utf-8")
    assert f'xmlns:p14="{p14_ns}"' in text
    assert p14_ns in text
    assert _verdict(output) == Verdict.NORMAL


def test_all_slides_lost_produces_warning(tmp_path: Path) -> None:
    """Losing every slide still rebuilds, warning about the empty list."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=3, media_bytes=_MEDIA_BYTES))
    # Rewrite the presentation so its slide ids point only at the slide
    # relationships (rId2..rId4), leaving the master (rId1) intact.
    payloads["ppt/presentation.xml"] = _presentation_xml(
        masters=[("2147483648", "rId1")],
        slides=[("256", "rId2"), ("257", "rId3"), ("258", "rId4")])
    for name in list(payloads):
        if name.startswith("ppt/slides/"):
            del payloads[name]
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    assert output.exists()
    assert any("sldIdLst" in warning for warning in result.warnings)
    presentation = ET.fromstring(_member(output, "ppt/presentation.xml"))
    assert presentation.findall(
        f"{{{_PML_NS}}}sldIdLst/{{{_PML_NS}}}sldId") == []


def test_missing_timestamp_uses_default_date(tmp_path: Path) -> None:
    """A reader without timestamps falls back to the 1980 minimum date."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=1, media_bytes=_MEDIA_BYTES))
    reader = FakeReader(payloads, timestamp=None)
    output = tmp_path / "rebuilt.pptx"

    rebuild_package(reader, _entries(payloads), output)

    with zipfile.ZipFile(output) as zf:
        assert zf.getinfo("ppt/media/image1.png").date_time \
            == (1980, 1, 1, 0, 0, 0)
    assert _verdict(output) == Verdict.NORMAL


# --- Dangling reference cleanup (v1.1.2) -----------------------------------


def _rels(relationships: list[tuple[str, str, str]]) -> bytes:
    """Build a ``.rels`` part from ``(id, type, target)`` triples."""
    items = "".join(
        f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>'
        for rid, rtype, target in relationships)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<Relationships xmlns="{_REL_NS}">{items}</Relationships>'
    ).encode("utf-8")


def _slide_with(shapes: str) -> bytes:
    """Wrap *shapes* markup in a minimal ``p:sld`` with all namespaces."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<p:sld xmlns:p="{_PML_NS}" xmlns:a="{_A_NS}" '
        f'xmlns:r="{_R_NS}" xmlns:p14="{_P14_NS}">'
        '<p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>'
        f'{shapes}'
        '</p:spTree></p:cSld></p:sld>'
    ).encode("utf-8")


def _pic(rid: str, cid: int = 4, name: str = "Img") -> str:
    """A ``p:pic`` whose poster/still image comes from *rid*."""
    return (
        f'<p:pic><p:nvPicPr><p:cNvPr id="{cid}" name="{name}"/>'
        '<p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
        f'<p:blipFill><a:blip r:embed="{rid}"/><a:stretch/></p:blipFill>'
        '<p:spPr/></p:pic>')


def _group_pic(rid: str) -> str:
    """A ``p:grpSp`` wrapping a single picture referencing *rid*."""
    return (
        '<p:grpSp><p:nvGrpSpPr><p:cNvPr id="6" name="Group"/>'
        '<p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
        + _pic(rid, cid=7, name="Img2") + '</p:grpSp>')


def _video_pic(poster_rid: str, link_rid: str, media_rid: str) -> str:
    """A video ``p:pic`` with a surviving poster and dangling media refs."""
    return (
        '<p:pic><p:nvPicPr><p:cNvPr id="5" name="Media"/><p:cNvPicPr/>'
        '<p:nvPr>'
        f'<a:videoFile r:link="{link_rid}"/>'
        '<p:extLst><p:ext uri="urn:media">'
        f'<p14:media r:embed="{media_rid}"/>'
        '</p:ext></p:extLst>'
        '</p:nvPr></p:nvPicPr>'
        f'<p:blipFill><a:blip r:embed="{poster_rid}"/>'
        '<a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        '<p:spPr/></p:pic>')


def _fill_shape(rid: str) -> str:
    """A shape whose fill is an ``a:blipFill`` referencing *rid*."""
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="8" name="Filled"/><p:cNvSpPr/>'
        '<p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:blipFill><a:blip r:embed="{rid}"/><a:stretch/>'
        '</a:blipFill></p:spPr>'
        '<p:txBody><a:p><a:r><a:t>fill shape</a:t></a:r></a:p></p:txBody>'
        '</p:sp>')


def _hlink_shape(rid: str) -> str:
    """A text run carrying an ``a:hlinkClick`` referencing *rid*."""
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="10" name="Linked"/><p:cNvSpPr/>'
        '<p:nvPr/></p:nvSpPr><p:spPr/>'
        '<p:txBody><a:p><a:r>'
        f'<a:rPr><a:hlinkClick r:id="{rid}"/></a:rPr>'
        '<a:t>linked text</a:t>'
        '</a:r></a:p></p:txBody></p:sp>')


def _unknown_shape(rid: str) -> str:
    """A carrier no removal rule covers (fallback path)."""
    return f'<p:contentPart r:pict="{rid}"/>'


def _diagram_frame(dm: str, lo: str, qs: str, cs: str) -> str:
    """A ``p:graphicFrame`` holding a diagram with four relationship ids."""
    dgm_ns = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
    return (
        '<p:graphicFrame><p:nvGraphicFramePr>'
        '<p:cNvPr id="12" name="Diagram"/><p:cNvGraphicFramePr/><p:nvPr/>'
        '</p:nvGraphicFramePr><p:xfrm/>'
        f'<a:graphic><a:graphicData uri="{dgm_ns}">'
        f'<dgm:relIds xmlns:dgm="{dgm_ns}" '
        f'r:dm="{dm}" r:lo="{lo}" r:qs="{qs}" r:cs="{cs}"/>'
        '</a:graphicData></a:graphic></p:graphicFrame>')


_TEXT_SHAPE = (
    '<p:sp><p:nvSpPr><p:cNvPr id="11" name="Text"/><p:cNvSpPr/><p:nvPr/>'
    '</p:nvSpPr><p:spPr/>'
    '<p:txBody><a:p><a:r><a:t>keep me</a:t></a:r></a:p></p:txBody></p:sp>')


def _slide1_payloads(shapes: str, relationships: list[tuple[str, str, str]],
                     num_slides: int = 2,
                     extra_media: tuple[tuple[str, bytes], ...] = ()
                     ) -> dict[str, bytes]:
    """Return a salvage set whose slide1 carries *shapes* and *relationships*."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=num_slides, media_bytes=_MEDIA_BYTES))
    payloads["ppt/slides/slide1.xml"] = _slide_with(shapes)
    payloads["ppt/slides/_rels/slide1.xml.rels"] = _rels(relationships)
    for media_name, media_bytes in extra_media:
        payloads[media_name] = media_bytes
    return payloads


def _slide_texts(slide: ET.Element) -> list[str]:
    """Return the text of every ``a:t`` run in *slide*."""
    return [node.text for node in slide.findall(f".//{{{_A_NS}}}t")]


def _no_dangling(path: Path) -> list[tuple[str, str, str]]:
    """Return every dangling relationship reference in the archive *path*.

    Cross-checks each XML part's relationships-namespace attribute values
    against the ids defined by its companion ``.rels`` part. Kept local to
    this test module (not imported from ``pptrepair.integrity``, developed
    in a parallel effort) so it never depends on that module existing.
    """
    with zipfile.ZipFile(path) as zf:
        payloads = {name: zf.read(name) for name in zf.namelist()}
    r_prefix = "{" + _R_NS + "}"
    dangling: list[tuple[str, str, str]] = []
    for name, data in payloads.items():
        if not name.endswith(".xml") or name.endswith(".rels"):
            continue
        if name == "[Content_Types].xml":
            continue
        rels_name = posixpath.join(
            posixpath.dirname(name), "_rels",
            posixpath.basename(name) + ".rels")
        defined: set[str] = set()
        rels_bytes = payloads.get(rels_name)
        if rels_bytes is not None:
            for rel in ET.fromstring(rels_bytes):
                rid = rel.get("Id")
                if rid:
                    defined.add(rid)
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            continue
        for elem in root.iter():
            for key, value in elem.attrib.items():
                if not key.startswith(r_prefix):
                    continue
                if value and value not in defined:
                    dangling.append((name, key, value))
    return dangling


def test_cleanup_keeps_video_pic_as_still_image(tmp_path: Path) -> None:
    """Rules 1-2: a video picture keeps its poster as a static image."""
    payloads = _slide1_payloads(
        _video_pic("rId2", "rId3", "rId4"),
        [("rId1", _LAYOUT_REL, "../slideLayouts/slideLayout1.xml"),
         ("rId2", _IMAGE_REL, "../media/image2.png"),
         ("rId3", _VIDEO_REL, "../media/video1.mp4"),
         ("rId4", _MEDIA_REL, "../media/media1.mp4")],
        extra_media=(("ppt/media/image2.png", b"\x89PNG poster data"),))
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    slide = ET.fromstring(_member(output, "ppt/slides/slide1.xml"))
    assert len(slide.findall(f".//{{{_PML_NS}}}pic")) == 1
    assert slide.find(f".//{{{_A_NS}}}videoFile") is None
    assert slide.find(f".//{{{_P14_NS}}}media") is None
    assert slide.find(f".//{{{_PML_NS}}}extLst") is None
    blips = slide.findall(f".//{{{_A_NS}}}blip")
    assert len(blips) == 1
    assert blips[0].get(f"{{{_R_NS}}}embed") == "rId2"
    assert "ppt/slides/slide1.xml" in result.cleaned_parts
    assert "ppt/slides/slide1.xml: videoFile (rId3)" in result.removed_elements
    assert "ppt/slides/slide1.xml: ext (rId4)" in result.removed_elements
    assert _no_dangling(output) == []


def test_cleanup_removes_image_pic_including_grouped(tmp_path: Path) -> None:
    """Rule 3: a picture whose image is gone is removed, group and all."""
    payloads = _slide1_payloads(
        _pic("rId2") + _group_pic("rId3") + _TEXT_SHAPE,
        [("rId1", _LAYOUT_REL, "../slideLayouts/slideLayout1.xml"),
         ("rId2", _IMAGE_REL, "../media/gone1.png"),
         ("rId3", _IMAGE_REL, "../media/gone2.png")])
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    slide = ET.fromstring(_member(output, "ppt/slides/slide1.xml"))
    assert slide.findall(f".//{{{_PML_NS}}}pic") == []
    assert slide.find(f".//{{{_PML_NS}}}grpSp") is not None
    assert "keep me" in _slide_texts(slide)
    assert "ppt/slides/slide1.xml: pic (rId2)" in result.removed_elements
    assert "ppt/slides/slide1.xml: pic (rId3)" in result.removed_elements
    assert _no_dangling(output) == []


def test_cleanup_strips_fill_blip_attribute_only(tmp_path: Path) -> None:
    """Rule 4: a shape's fill blip keeps its element, losing the attribute."""
    payloads = _slide1_payloads(
        _fill_shape("rId2"),
        [("rId1", _LAYOUT_REL, "../slideLayouts/slideLayout1.xml"),
         ("rId2", _IMAGE_REL, "../media/gone.png")])
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    slide = ET.fromstring(_member(output, "ppt/slides/slide1.xml"))
    blips = slide.findall(f".//{{{_A_NS}}}blip")
    assert len(blips) == 1
    assert blips[0].get(f"{{{_R_NS}}}embed") is None
    assert slide.find(f".//{{{_PML_NS}}}sp") is not None
    assert "fill shape" in _slide_texts(slide)
    assert ("ppt/slides/slide1.xml: @embed on blip (rId2)"
            in result.removed_elements)
    assert _no_dangling(output) == []


def test_cleanup_removes_hyperlink_keeps_text(tmp_path: Path) -> None:
    """Rule 5: a dangling hyperlink is removed but its text survives."""
    payloads = _slide1_payloads(
        _hlink_shape("rId2"),
        [("rId1", _LAYOUT_REL, "../slideLayouts/slideLayout1.xml"),
         ("rId2", _HLINK_REL, "../slides/slide99.xml")])
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    slide = ET.fromstring(_member(output, "ppt/slides/slide1.xml"))
    assert slide.find(f".//{{{_A_NS}}}hlinkClick") is None
    assert "linked text" in _slide_texts(slide)
    assert ("ppt/slides/slide1.xml: hlinkClick (rId2)"
            in result.removed_elements)
    assert _no_dangling(output) == []


def test_cleanup_unknown_reference_falls_back_with_warning(
        tmp_path: Path) -> None:
    """Rule 8: an uncovered reference loses its attribute and warns."""
    payloads = _slide1_payloads(
        _TEXT_SHAPE + _unknown_shape("rId2"),
        [("rId1", _LAYOUT_REL, "../slideLayouts/slideLayout1.xml"),
         ("rId2", _CUSTOM_REL, "../customXml/item1.xml")])
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    slide = ET.fromstring(_member(output, "ppt/slides/slide1.xml"))
    content_part = slide.find(f".//{{{_PML_NS}}}contentPart")
    assert content_part is not None
    assert content_part.get(f"{{{_R_NS}}}pict") is None
    assert "keep me" in _slide_texts(slide)
    assert ("ppt/slides/slide1.xml: @pict on contentPart (rId2)"
            in result.removed_elements)
    assert any("not covered by a cleanup rule" in warning
               for warning in result.warnings)
    assert _no_dangling(output) == []


def test_cleanup_removes_graphic_frame_with_aggregated_ids(
        tmp_path: Path) -> None:
    """Rule 6: a diagram's frame is removed and every id reported together."""
    payloads = _slide1_payloads(
        _diagram_frame("rId2", "rId3", "rId4", "rId5") + _TEXT_SHAPE,
        [("rId1", _LAYOUT_REL, "../slideLayouts/slideLayout1.xml")])
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    slide = ET.fromstring(_member(output, "ppt/slides/slide1.xml"))
    assert slide.find(f".//{{{_PML_NS}}}graphicFrame") is None
    assert "keep me" in _slide_texts(slide)
    assert ("ppt/slides/slide1.xml: graphicFrame (rId2, rId3, rId4, rId5)"
            in result.removed_elements)
    assert _no_dangling(output) == []


def test_cleanup_removes_custom_show_slide_reference(tmp_path: Path) -> None:
    """Rule 7: a dangling custom-show slide reference is dropped in-place."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=1, media_bytes=_MEDIA_BYTES))
    payloads["ppt/presentation.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
        f'<p:presentation xmlns:p="{_PML_NS}" xmlns:r="{_R_NS}">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/>'
        '</p:sldMasterIdLst>'
        '<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
        '<p:custShowLst><p:custShow name="Show" id="0"><p:sldLst>'
        '<p:sld r:id="rId2"/>'
        '<p:sld r:id="rId4"/>'
        '</p:sldLst></p:custShow></p:custShowLst>'
        '</p:presentation>'
    ).encode("utf-8")
    payloads["ppt/_rels/presentation.xml.rels"] = _rels([
        ("rId1", f"{_R_NS}/slideMaster", "slideMasters/slideMaster1.xml"),
        ("rId2", f"{_R_NS}/slide", "slides/slide1.xml"),
        ("rId3", f"{_R_NS}/theme", "theme/theme1.xml")])
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    presentation = ET.fromstring(_member(output, "ppt/presentation.xml"))
    show_ids = [node.get(f"{{{_R_NS}}}id") for node in presentation.findall(
        f".//{{{_PML_NS}}}custShow/{{{_PML_NS}}}sldLst/{{{_PML_NS}}}sld")]
    assert show_ids == ["rId2"]
    assert "ppt/presentation.xml" in result.cleaned_parts
    assert "ppt/presentation.xml: sld (rId4)" in result.removed_elements
    assert _no_dangling(output) == []


def test_cleanup_leaves_untouched_parts_byte_identical(
        tmp_path: Path) -> None:
    """Parts without a dangling reference are streamed byte-identical."""
    payloads = _slide1_payloads(
        _pic("rId2"),
        [("rId1", _LAYOUT_REL, "../slideLayouts/slideLayout1.xml"),
         ("rId2", _IMAGE_REL, "../media/gone.png")])
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    assert "ppt/slides/slide1.xml" in result.cleaned_parts
    assert "ppt/slides/slide2.xml" not in result.cleaned_parts
    # The clean sibling slide and the media part are untouched.
    assert _member(output, "ppt/slides/slide2.xml") \
        == payloads["ppt/slides/slide2.xml"]
    assert _member(output, "ppt/media/image1.png") \
        == payloads["ppt/media/image1.png"]
    assert _no_dangling(output) == []


def test_cleanup_missing_rels_part_treats_all_as_dangling(
        tmp_path: Path) -> None:
    """A vanished ``.rels`` part makes every reference dangling, with a warning."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES))
    payloads["ppt/slides/slide1.xml"] = _slide_with(_pic("rId2"))
    del payloads["ppt/slides/_rels/slide1.xml.rels"]
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    slide = ET.fromstring(_member(output, "ppt/slides/slide1.xml"))
    assert slide.findall(f".//{{{_PML_NS}}}pic") == []
    assert "ppt/slides/slide1.xml" in result.cleaned_parts
    assert any("relationships part" in warning and "missing" in warning
               for warning in result.warnings)
    assert _no_dangling(output) == []


def test_cleanup_unparsable_part_is_left_unchanged(tmp_path: Path) -> None:
    """An XML part that fails to parse is streamed as-is, with a warning."""
    payloads = _unzip(
        build_minimal_pptx(num_slides=1, media_bytes=_MEDIA_BYTES))
    broken = b'<p:sld xmlns:p="urn:x"><p:cSld><unclosed r:embed="rId9">'
    payloads["ppt/slides/slide1.xml"] = broken
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    assert _member(output, "ppt/slides/slide1.xml") == broken
    assert "ppt/slides/slide1.xml" not in result.cleaned_parts
    assert any("could not be parsed" in warning
               for warning in result.warnings)


def test_cleanup_output_has_no_dangling_references(tmp_path: Path) -> None:
    """A slide mixing every carrier kind rebuilds free of dangling refs."""
    shapes = (_video_pic("rId2", "rId3", "rId4") + _pic("rId5")
              + _group_pic("rId9") + _fill_shape("rId6")
              + _hlink_shape("rId7") + _TEXT_SHAPE + _unknown_shape("rId8"))
    payloads = _slide1_payloads(
        shapes,
        [("rId1", _LAYOUT_REL, "../slideLayouts/slideLayout1.xml"),
         ("rId2", _IMAGE_REL, "../media/image2.png"),
         ("rId3", _VIDEO_REL, "../media/video1.mp4"),
         ("rId4", _MEDIA_REL, "../media/media1.mp4"),
         ("rId5", _IMAGE_REL, "../media/gone5.png"),
         ("rId6", _IMAGE_REL, "../media/gone6.png"),
         ("rId7", _HLINK_REL, "../slides/slide99.xml"),
         ("rId8", _CUSTOM_REL, "../customXml/item1.xml"),
         ("rId9", _IMAGE_REL, "../media/gone9.png")],
        extra_media=(("ppt/media/image2.png", b"\x89PNG poster data"),))
    reader = FakeReader(payloads)
    output = tmp_path / "rebuilt.pptx"

    result = rebuild_package(reader, _entries(payloads), output)

    assert _no_dangling(output) == []
    assert _verdict(output) == Verdict.NORMAL
    assert result.cleaned_parts == ["ppt/slides/slide1.xml"]
    # The surviving poster keeps exactly one picture; the two broken
    # pictures (bare and grouped) are gone.
    slide = ET.fromstring(_member(output, "ppt/slides/slide1.xml"))
    assert len(slide.findall(f".//{{{_PML_NS}}}pic")) == 1
