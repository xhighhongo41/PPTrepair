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
