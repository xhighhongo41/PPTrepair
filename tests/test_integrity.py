"""Tests for :mod:`pptrepair.integrity`.

All fixtures are minimal, synthetic ZIP archives assembled directly with
:mod:`zipfile` under ``tmp_path``; no real .pptx sample files are used
here, and ``tests/fixtures.py`` is intentionally left untouched.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pptrepair.integrity import (inspect_references, inspect_structure,
                                 inspect_timing)

_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_P14_NS = "http://schemas.microsoft.com/office/powerpoint/2010/main"


def _make_pptx(path: Path, parts: dict[str, bytes]) -> Path:
    """Write a ZIP archive containing *parts* (name -> raw bytes)."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in parts.items():
            zf.writestr(name, data)
    return path


def _rels_xml(entries: list[tuple[str, str, str]],
             external: list[tuple[str, str, str]] | None = None) -> bytes:
    """Build a ``.rels`` document from ``(id, type, target)`` entries.

    *external* entries additionally get ``TargetMode="External"``.
    """
    items = [
        f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>'
        for rid, rtype, target in entries
    ]
    for rid, rtype, target in external or []:
        items.append(
            f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}" '
            'TargetMode="External"/>')
    body = f'<Relationships xmlns="{_RELS_NS}">' + "".join(items) \
        + "</Relationships>"
    return body.encode("utf-8")


# --- (a) healthy package -----------------------------------------------


def test_healthy_package_has_no_dangling_refs(tmp_path: Path) -> None:
    """A well-formed package reports zero dangling references."""
    parts = {
        "[Content_Types].xml": b'<Types xmlns="urn:example:types"/>',
        "_rels/.rels": _rels_xml(
            [("rId1", "officeDocument", "ppt/presentation.xml")]),
        "ppt/presentation.xml": (
            f'<p:presentation xmlns:p="{_P_NS}" xmlns:r="{_R_NS}">'
            '<p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst>'
            '</p:presentation>'
        ).encode("utf-8"),
        "ppt/_rels/presentation.xml.rels": _rels_xml(
            [("rId1", "slide", "slides/slide1.xml")]),
        "ppt/slides/slide1.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:spTree><p:pic><p:blipFill>'
            '<a:blip r:embed="rId1"/>'
            '</p:blipFill></p:pic></p:spTree></p:cSld>'
            '</p:sld>'
        ).encode("utf-8"),
        "ppt/slides/_rels/slide1.xml.rels": _rels_xml(
            [("rId1", "image", "../media/image1.png")]),
        "ppt/media/image1.png": b"\x89PNG-fake-bytes",
    }
    path = _make_pptx(tmp_path / "healthy.pptx", parts)

    result = inspect_references(path)

    # [Content_Types].xml, ppt/presentation.xml, ppt/slides/slide1.xml.
    assert result.parts_scanned == 3
    assert result.dangling == []
    assert result.missing_rels_parts == []
    assert result.parse_errors == []


# --- (b) dangling a:blip r:embed ----------------------------------------


def test_dangling_blip_embed_detected(tmp_path: Path) -> None:
    """A blip whose r:embed target is undefined is reported precisely."""
    parts = {
        "ppt/slides/slide2.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:spTree><p:pic><p:blipFill>'
            '<a:blip r:embed="rId9"/>'
            '</p:blipFill></p:pic></p:spTree></p:cSld>'
            '</p:sld>'
        ).encode("utf-8"),
        "ppt/slides/_rels/slide2.xml.rels": _rels_xml(
            [("rId1", "image", "../media/image1.png")]),
    }
    path = _make_pptx(tmp_path / "dangling_blip.pptx", parts)

    result = inspect_references(path)

    assert len(result.dangling) == 1
    ref = result.dangling[0]
    assert ref.part == "ppt/slides/slide2.xml"
    assert ref.attribute == "embed"
    assert ref.rid == "rId9"
    assert ref.element == "blip"
    assert result.missing_rels_parts == []
    assert result.parse_errors == []


# --- (c) videoFile + p14:media dangling together ------------------------


def test_dangling_video_and_media_both_detected(tmp_path: Path) -> None:
    """A dangling videoFile link and its paired p14:media are both caught."""
    parts = {
        "ppt/slides/slide3.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}" '
            f'xmlns:p14="{_P14_NS}">'
            '<p:cSld><p:spTree><p:pic><p:nvPicPr><p:nvPr>'
            '<a:videoFile r:link="rId5"/>'
            '<p:extLst>'
            '<p:ext uri="{DAA4B4D4-6D71-4841-9C94-8C6D0BB5C96C}">'
            '<p14:media r:embed="rId6"/>'
            '</p:ext>'
            '</p:extLst>'
            '</p:nvPr></p:nvPicPr></p:pic></p:spTree></p:cSld>'
            '</p:sld>'
        ).encode("utf-8"),
        "ppt/slides/_rels/slide3.xml.rels": _rels_xml(
            [("rId1", "image", "../media/poster.png")]),
    }
    path = _make_pptx(tmp_path / "dangling_video.pptx", parts)

    result = inspect_references(path)

    # Document order: videoFile precedes p14:media.
    assert [(ref.attribute, ref.rid, ref.element) for ref in result.dangling] \
        == [("link", "rId5", "videoFile"), ("embed", "rId6", "media")]
    assert result.missing_rels_parts == []
    assert result.parse_errors == []


# --- (d) hlinkClick dangling / empty r:id ignored ------------------------


def test_hlink_dangling_and_empty_rid_ignored(tmp_path: Path) -> None:
    """A dangling hlinkClick is reported; an empty r:id anchor is not."""
    parts = {
        "ppt/slides/slide4.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:spTree>'
            '<p:sp><p:txBody><a:p><a:r><a:rPr>'
            '<a:hlinkClick r:id="rId7"/>'
            '</a:rPr></a:r></a:p></p:txBody></p:sp>'
            '<p:sp><p:txBody><a:p><a:r><a:rPr>'
            '<a:hlinkClick r:id=""/>'
            '</a:rPr></a:r></a:p></p:txBody></p:sp>'
            '</p:spTree></p:cSld></p:sld>'
        ).encode("utf-8"),
        "ppt/slides/_rels/slide4.xml.rels": _rels_xml([]),
    }
    path = _make_pptx(tmp_path / "dangling_hlink.pptx", parts)

    result = inspect_references(path)

    assert len(result.dangling) == 1
    ref = result.dangling[0]
    assert (ref.attribute, ref.rid, ref.element) == ("id", "rId7", "hlinkClick")
    assert result.missing_rels_parts == []


# --- (e) unknown attribute name still detected ---------------------------


def test_unknown_attribute_name_detected(tmp_path: Path) -> None:
    """Detection does not depend on a fixed allow-list of attribute names."""
    parts = {
        "ppt/slides/slide5.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:bg r:pict="rId8"/></p:cSld></p:sld>'
        ).encode("utf-8"),
        "ppt/slides/_rels/slide5.xml.rels": _rels_xml(
            [("rId1", "image", "../media/image1.png")]),
    }
    path = _make_pptx(tmp_path / "dangling_pict.pptx", parts)

    result = inspect_references(path)

    assert result.missing_rels_parts == []
    assert len(result.dangling) == 1
    ref = result.dangling[0]
    assert (ref.attribute, ref.rid, ref.element) == ("pict", "rId8", "bg")


# --- (f) External relationship id is not dangling ------------------------


def test_external_relationship_id_not_dangling(tmp_path: Path) -> None:
    """An attribute referencing an External relationship id is not dangling."""
    parts = {
        "ppt/slides/slide6.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:rPr>'
            '<a:hlinkClick r:id="rIdExt"/>'
            '</a:rPr></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>'
            '</p:sld>'
        ).encode("utf-8"),
        "ppt/slides/_rels/slide6.xml.rels": _rels_xml(
            [], external=[("rIdExt", "hyperlink", "https://example.com/")]),
    }
    path = _make_pptx(tmp_path / "external_ok.pptx", parts)

    result = inspect_references(path)

    assert result.dangling == []
    assert result.missing_rels_parts == []


# --- (g) missing .rels part flags every reference ------------------------


def test_missing_rels_part_flags_all_references(tmp_path: Path) -> None:
    """A part with r: references but no matching .rels part is flagged."""
    parts = {
        "ppt/slides/slide7.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:spTree><p:pic><p:blipFill>'
            '<a:blip r:embed="rId1"/>'
            '</p:blipFill></p:pic></p:spTree></p:cSld></p:sld>'
        ).encode("utf-8"),
        # Deliberately no ppt/slides/_rels/slide7.xml.rels part.
    }
    path = _make_pptx(tmp_path / "missing_rels.pptx", parts)

    result = inspect_references(path)

    assert result.missing_rels_parts == ["ppt/slides/slide7.xml"]
    assert len(result.dangling) == 1
    ref = result.dangling[0]
    assert (ref.part, ref.attribute, ref.rid, ref.element) == (
        "ppt/slides/slide7.xml", "embed", "rId1", "blip")


# --- (h) corrupt XML part is recorded, others still checked --------------


def test_corrupt_part_recorded_others_still_checked(tmp_path: Path) -> None:
    """A malformed XML part is reported without aborting other parts."""
    parts = {
        "ppt/slides/slide8.xml": b"<p:sld this is not well-formed xml",
        "ppt/slides/slide9.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:spTree><p:pic><p:blipFill>'
            '<a:blip r:embed="rId1"/>'
            '</p:blipFill></p:pic></p:spTree></p:cSld></p:sld>'
        ).encode("utf-8"),
        "ppt/slides/_rels/slide9.xml.rels": _rels_xml(
            [("rId1", "image", "../media/image1.png")]),
    }
    path = _make_pptx(tmp_path / "corrupt_part.pptx", parts)

    result = inspect_references(path)

    assert result.parse_errors == ["ppt/slides/slide8.xml"]
    assert result.dangling == []
    assert result.parts_scanned == 2


# --- corrupt .rels part is recorded, refs become dangling ----------------


def test_corrupt_rels_part_recorded_and_refs_dangling(tmp_path: Path) -> None:
    """An unparsable .rels is recorded; its part's refs become dangling."""
    parts = {
        "ppt/slides/slide10.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:spTree><p:pic><p:blipFill>'
            '<a:blip r:embed="rId1"/>'
            '</p:blipFill></p:pic></p:spTree></p:cSld></p:sld>'
        ).encode("utf-8"),
        "ppt/slides/_rels/slide10.xml.rels": b"<Relationships broken",
    }
    path = _make_pptx(tmp_path / "corrupt_rels.pptx", parts)

    result = inspect_references(path)

    assert result.parse_errors == ["ppt/slides/_rels/slide10.xml.rels"]
    assert result.missing_rels_parts == []
    assert len(result.dangling) == 1
    assert (result.dangling[0].rid, result.dangling[0].element) == (
        "rId1", "blip")


# --- bonus: BadZipFile is the caller's responsibility --------------------


def test_bad_zip_file_propagates(tmp_path: Path) -> None:
    """A non-ZIP file raises BadZipFile instead of being caught silently."""
    path = tmp_path / "not_a_zip.pptx"
    path.write_bytes(b"this is not a zip archive")

    with pytest.raises(zipfile.BadZipFile):
        inspect_references(path)


# --- inspect_timing: p:timing / shape-id integrity -----------------------


# --- (a) video shape with a:videoFile is healthy -------------------------


def test_timing_video_with_videofile_shape_is_healthy(tmp_path: Path) -> None:
    """A p:video targeting a shape that carries a:videoFile is healthy."""
    parts = {
        "ppt/slides/slide21.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:spTree><p:pic><p:nvPicPr>'
            '<p:cNvPr id="6" name="Video 1"/><p:cNvPicPr/>'
            '<p:nvPr><a:videoFile r:link="rId5"/></p:nvPr>'
            '</p:nvPicPr><p:blipFill/><p:spPr/></p:pic></p:spTree></p:cSld>'
            '<p:timing><p:tnLst><p:par><p:cTn id="1"><p:childTnLst>'
            '<p:seq><p:cTn id="2"/></p:seq>'
            '<p:video><p:cMediaNode vol="80000"><p:cTn id="7"/>'
            '<p:tgtEl><p:spTgt spid="6"/></p:tgtEl>'
            '</p:cMediaNode></p:video>'
            '</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>'
            '</p:sld>'
        ).encode("utf-8"),
    }
    path = _make_pptx(tmp_path / "timing_healthy.pptx", parts)

    result = inspect_timing(path)

    assert result.parts_scanned == 1
    assert result.dangling == []
    assert result.media_mismatch == []
    assert result.parse_errors == []


# --- (b) video shape missing a:videoFile is a media mismatch -------------


def test_timing_video_without_videofile_is_media_mismatch(
        tmp_path: Path) -> None:
    """A p:video targeting a shape lacking a:videoFile is a mismatch."""
    parts = {
        "ppt/slides/slide22.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}">'
            '<p:cSld><p:spTree><p:pic><p:nvPicPr>'
            '<p:cNvPr id="6" name="Video 1"/><p:cNvPicPr/><p:nvPr/>'
            '</p:nvPicPr><p:blipFill/><p:spPr/></p:pic></p:spTree></p:cSld>'
            '<p:timing><p:tnLst><p:par><p:cTn id="1"><p:childTnLst>'
            '<p:video><p:cMediaNode vol="80000"><p:cTn id="7"/>'
            '<p:tgtEl><p:spTgt spid="6"/></p:tgtEl>'
            '</p:cMediaNode></p:video>'
            '</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>'
            '</p:sld>'
        ).encode("utf-8"),
    }
    path = _make_pptx(tmp_path / "timing_media_mismatch.pptx", parts)

    result = inspect_timing(path)

    assert result.dangling == []
    assert len(result.media_mismatch) == 1
    mismatch = result.media_mismatch[0]
    assert (mismatch.kind, mismatch.spid) == ("video", "6")


# --- (c) spTgt targeting a non-existent shape is dangling, not mismatch --


def test_timing_sptgt_missing_shape_is_dangling_not_mismatch(
        tmp_path: Path) -> None:
    """A spTgt pointing at a non-existent shape id is dangling only."""
    parts = {
        "ppt/slides/slide23.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
            '<p:cSld><p:spTree><p:pic><p:nvPicPr>'
            '<p:cNvPr id="6" name="Video 1"/><p:cNvPicPr/>'
            '<p:nvPr><a:videoFile r:link="rId5"/></p:nvPr>'
            '</p:nvPicPr><p:blipFill/><p:spPr/></p:pic></p:spTree></p:cSld>'
            '<p:timing><p:tnLst><p:par><p:cTn id="1"><p:childTnLst>'
            '<p:video><p:cMediaNode vol="80000"><p:cTn id="7"/>'
            '<p:tgtEl><p:spTgt spid="99"/></p:tgtEl>'
            '</p:cMediaNode></p:video>'
            '</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>'
            '</p:sld>'
        ).encode("utf-8"),
    }
    path = _make_pptx(tmp_path / "timing_dangling_video_target.pptx", parts)

    result = inspect_timing(path)

    assert len(result.dangling) == 1
    ref = result.dangling[0]
    assert (ref.element, ref.spid) == ("spTgt", "99")
    assert result.media_mismatch == []


# --- (d) bldP targeting a non-existent shape is dangling -----------------


def test_timing_bldp_missing_shape_is_dangling(tmp_path: Path) -> None:
    """A p:bldP referencing a non-existent shape id is dangling."""
    parts = {
        "ppt/slides/slide24.xml": (
            f'<p:sld xmlns:p="{_P_NS}">'
            '<p:cSld><p:spTree/></p:cSld>'
            '<p:timing><p:tnLst><p:par><p:cTn id="1"/></p:par></p:tnLst>'
            '<p:bldLst><p:bldP spid="99" grpId="0"/></p:bldLst>'
            '</p:timing></p:sld>'
        ).encode("utf-8"),
    }
    path = _make_pptx(tmp_path / "timing_dangling_bldp.pptx", parts)

    result = inspect_timing(path)

    assert len(result.dangling) == 1
    ref = result.dangling[0]
    assert (ref.element, ref.spid) == ("bldP", "99")
    assert result.media_mismatch == []


# --- (e) empty spid is not reported ---------------------------------------


def test_timing_empty_spid_is_ignored(tmp_path: Path) -> None:
    """An empty spid attribute value is not reported as dangling."""
    parts = {
        "ppt/slides/slide25.xml": (
            f'<p:sld xmlns:p="{_P_NS}">'
            '<p:cSld><p:spTree/></p:cSld>'
            '<p:timing><p:tnLst><p:par><p:cTn id="1"><p:childTnLst>'
            '<p:seq><p:cTn id="2"><p:stCondLst><p:cond>'
            '<p:tgtEl><p:spTgt spid=""/></p:tgtEl>'
            '</p:cond></p:stCondLst></p:cTn></p:seq>'
            '</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>'
            '</p:sld>'
        ).encode("utf-8"),
    }
    path = _make_pptx(tmp_path / "timing_empty_spid.pptx", parts)

    result = inspect_timing(path)

    assert result.dangling == []
    assert result.media_mismatch == []


# --- (f) audio shape missing a:audioFile is a media mismatch -------------


def test_timing_audio_without_audiofile_is_media_mismatch(
        tmp_path: Path) -> None:
    """A p:audio targeting a shape lacking a:audioFile is a mismatch."""
    parts = {
        "ppt/slides/slide26.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}">'
            '<p:cSld><p:spTree><p:pic><p:nvPicPr>'
            '<p:cNvPr id="8" name="Audio 1"/><p:cNvPicPr/><p:nvPr/>'
            '</p:nvPicPr><p:blipFill/><p:spPr/></p:pic></p:spTree></p:cSld>'
            '<p:timing><p:tnLst><p:par><p:cTn id="1"><p:childTnLst>'
            '<p:audio><p:cMediaNode vol="80000"><p:cTn id="9"/>'
            '<p:tgtEl><p:spTgt spid="8"/></p:tgtEl>'
            '</p:cMediaNode></p:audio>'
            '</p:childTnLst></p:cTn></p:par></p:tnLst></p:timing>'
            '</p:sld>'
        ).encode("utf-8"),
    }
    path = _make_pptx(tmp_path / "timing_audio_mismatch.pptx", parts)

    result = inspect_timing(path)

    assert result.dangling == []
    assert len(result.media_mismatch) == 1
    mismatch = result.media_mismatch[0]
    assert (mismatch.kind, mismatch.spid) == ("audio", "8")


# --- (g) slide without p:timing has empty results -------------------------


def test_timing_absent_slide_has_empty_results(tmp_path: Path) -> None:
    """A slide without any p:timing tree reports empty result lists."""
    parts = {
        "ppt/slides/slide27.xml": (
            f'<p:sld xmlns:p="{_P_NS}" xmlns:a="{_A_NS}">'
            '<p:cSld><p:spTree><p:pic><p:nvPicPr>'
            '<p:cNvPr id="1" name="Picture 1"/><p:cNvPicPr/><p:nvPr/>'
            '</p:nvPicPr><p:blipFill/><p:spPr/></p:pic></p:spTree></p:cSld>'
            '</p:sld>'
        ).encode("utf-8"),
    }
    path = _make_pptx(tmp_path / "timing_absent.pptx", parts)

    result = inspect_timing(path)

    assert result.parts_scanned == 1
    assert result.dangling == []
    assert result.media_mismatch == []
    assert result.parse_errors == []


# --- (h) corrupt XML part is recorded, others still checked ---------------


def test_timing_corrupt_part_recorded_others_still_checked(
        tmp_path: Path) -> None:
    """A malformed XML part is reported without aborting other parts."""
    parts = {
        "ppt/slides/slide28.xml": b"<p:sld this is not well-formed xml",
        "ppt/slides/slide29.xml": (
            f'<p:sld xmlns:p="{_P_NS}">'
            '<p:cSld><p:spTree/></p:cSld></p:sld>'
        ).encode("utf-8"),
    }
    path = _make_pptx(tmp_path / "timing_corrupt_part.pptx", parts)

    result = inspect_timing(path)

    assert result.parse_errors == ["ppt/slides/slide28.xml"]
    assert result.dangling == []
    assert result.media_mismatch == []
    assert result.parts_scanned == 2


# --- inspect_structure: required relationship-type checks -----------------


def _rel_type(tail: str) -> str:
    """Build a full relationship ``Type`` URI ending in *tail*."""
    return f"{_R_NS}/{tail}"


# --- (a) a fully-related package has nothing missing ----------------------


def test_structure_complete_package_has_no_missing(tmp_path: Path) -> None:
    """A package satisfying every required relationship reports nothing
    missing, across all six part kinds :func:`inspect_structure` checks."""
    parts = {
        "ppt/presentation.xml": b"<p:presentation/>",
        "ppt/_rels/presentation.xml.rels": _rels_xml(
            [("rId1", _rel_type("slideMaster"),
              "slideMasters/slideMaster1.xml")]),
        "ppt/slides/slide1.xml": b"<p:sld/>",
        "ppt/slides/_rels/slide1.xml.rels": _rels_xml(
            [("rId1", _rel_type("slideLayout"),
              "../slideLayouts/slideLayout1.xml")]),
        "ppt/slideLayouts/slideLayout1.xml": b"<p:sldLayout/>",
        "ppt/slideLayouts/_rels/slideLayout1.xml.rels": _rels_xml(
            [("rId1", _rel_type("slideMaster"),
              "../slideMasters/slideMaster1.xml")]),
        "ppt/slideMasters/slideMaster1.xml": b"<p:sldMaster/>",
        "ppt/slideMasters/_rels/slideMaster1.xml.rels": _rels_xml(
            [("rId1", _rel_type("theme"), "../theme/theme1.xml")]),
        "ppt/notesMasters/notesMaster1.xml": b"<p:notesMaster/>",
        "ppt/notesMasters/_rels/notesMaster1.xml.rels": _rels_xml(
            [("rId1", _rel_type("theme"), "../theme/theme2.xml")]),
        "ppt/notesSlides/notesSlide1.xml": b"<p:notes/>",
        "ppt/notesSlides/_rels/notesSlide1.xml.rels": _rels_xml([
            ("rId1", _rel_type("slide"), "../slides/slide1.xml"),
            ("rId2", _rel_type("notesMaster"),
             "../notesMasters/notesMaster1.xml"),
        ]),
    }
    path = _make_pptx(tmp_path / "structure_complete.pptx", parts)

    result = inspect_structure(path)

    assert result.parts_checked == 6
    assert result.missing == []
    assert result.parse_errors == []


# --- (b) slide master missing its theme relationship -----------------------


def test_structure_slidemaster_missing_theme_is_reported(
        tmp_path: Path) -> None:
    """A slide master whose .rels lacks a theme relationship is flagged,
    the Type-only unindexed relationship inspect_references cannot see."""
    parts = {
        "ppt/slideMasters/slideMaster2.xml": b"<p:sldMaster/>",
        "ppt/slideMasters/_rels/slideMaster2.xml.rels": _rels_xml(
            [("rId1", _rel_type("slideLayout"),
              "../slideLayouts/slideLayout2.xml")]),
    }
    path = _make_pptx(tmp_path / "structure_missing_theme.pptx", parts)

    result = inspect_structure(path)

    assert result.parts_checked == 1
    assert len(result.missing) == 1
    missing = result.missing[0]
    assert (missing.part, missing.required_type) == (
        "ppt/slideMasters/slideMaster2.xml", "theme")
    assert result.parse_errors == []


# --- (c) the .rels part itself is absent -----------------------------------


def test_structure_missing_rels_part_flags_all_required_types(
        tmp_path: Path) -> None:
    """A target part with no matching .rels part flags every one of its
    required types (there being nothing to look any of them up in)."""
    parts = {
        "ppt/slides/slide2.xml": b"<p:sld/>",
        # Deliberately no ppt/slides/_rels/slide2.xml.rels part.
    }
    path = _make_pptx(tmp_path / "structure_missing_rels.pptx", parts)

    result = inspect_structure(path)

    assert result.parts_checked == 1
    assert len(result.missing) == 1
    missing = result.missing[0]
    assert (missing.part, missing.required_type) == (
        "ppt/slides/slide2.xml", "slideLayout")
    assert result.parse_errors == []


# --- (d) a notes slide missing only one of its two required types ----------


def test_structure_notes_slide_missing_one_of_two_required_types(
        tmp_path: Path) -> None:
    """A notes slide missing only its notesMaster relationship is flagged
    for that type alone; its (present) slide relationship is not."""
    parts = {
        "ppt/notesSlides/notesSlide2.xml": b"<p:notes/>",
        "ppt/notesSlides/_rels/notesSlide2.xml.rels": _rels_xml(
            [("rId1", _rel_type("slide"), "../slides/slide2.xml")]),
    }
    path = _make_pptx(tmp_path / "structure_notes_partial.pptx", parts)

    result = inspect_structure(path)

    assert result.parts_checked == 1
    assert len(result.missing) == 1
    missing = result.missing[0]
    assert (missing.part, missing.required_type) == (
        "ppt/notesSlides/notesSlide2.xml", "notesMaster")
    assert result.parse_errors == []


# --- (e) a corrupt .rels part is recorded and its types are missing --------


def test_structure_corrupt_rels_recorded_and_required_types_missing(
        tmp_path: Path) -> None:
    """An unparsable .rels part is recorded in parse_errors, and its
    part's required types are reported missing since no relationship
    could be read from it."""
    parts = {
        "ppt/slideMasters/slideMaster3.xml": b"<p:sldMaster/>",
        "ppt/slideMasters/_rels/slideMaster3.xml.rels": b"<Relationships broken",
    }
    path = _make_pptx(tmp_path / "structure_corrupt_rels.pptx", parts)

    result = inspect_structure(path)

    assert result.parse_errors == [
        "ppt/slideMasters/_rels/slideMaster3.xml.rels"]
    assert len(result.missing) == 1
    missing = result.missing[0]
    assert (missing.part, missing.required_type) == (
        "ppt/slideMasters/slideMaster3.xml", "theme")
