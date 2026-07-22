"""Tests for :mod:`pptrepair.origin`.

Every fixture here is a real byte stream written to disk and run
through the actual scan -> census -> classify pipeline
(:func:`pptrepair.scan.diagnose_file`), matching
:mod:`test_classify_e2e`'s approach, so :func:`score_origin` is
exercised against the exact recorded metadata the CLI would see.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fixtures import (build_minimal_jpeg, build_minimal_pptx, find_eocd,
                      foreign_prefix, make_edited_version, truncate)

from pptrepair.census import from_central_directory, from_lfh_scan
from pptrepair.classify import Diagnosis
from pptrepair.origin import LINEAGE_SCORE_THRESHOLD, score_origin
from pptrepair.scan import diagnose_file


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the resulting path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _diagnose(path: Path) -> Diagnosis:
    """Run the real scan/census/classify pipeline over *path*.

    Fails the test immediately (rather than silently comparing None
    diagnoses) if the pipeline could not be run at all.
    """
    diagnosis, error = diagnose_file(path)
    assert error is None, error
    assert diagnosis is not None
    return diagnosis


def test_identical_copies_are_auto(tmp_path: Path) -> None:
    """Two byte-identical copies match completely -> tier "auto"."""
    data = build_minimal_pptx(num_slides=3, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    path_b = _write(tmp_path, "b.pptx", data)

    score = score_origin(_diagnose(path_a), _diagnose(path_b))

    assert score.size_match is True
    assert score.cd_pair is True
    assert score.triple_ratio == 1.0
    assert score.tier == "auto"


def test_foreign_head_corruption_still_matches_via_cd(tmp_path: Path) -> None:
    """A HEAD_FOREIGN_DATA-style corruption (size unchanged, central
    directory intact at the tail) is still recognised as the same file
    as an intact twin: the CD/CD comparison uses recorded metadata that
    ignores whether the entry's data actually verified."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    corrupted = foreign_prefix(data, 8192)
    path_a = _write(tmp_path, "corrupted.pptx", corrupted)
    path_b = _write(tmp_path, "clean.pptx", data)

    diag_a = _diagnose(path_a)
    diag_b = _diagnose(path_b)
    assert diag_a.cd_census is not None  # damage is confined to the head

    score = score_origin(diag_a, diag_b)

    assert score.size_match is True
    assert score.cd_pair is True
    assert score.tier == "auto"


def test_truncated_file_matches_via_lfh_scan(tmp_path: Path) -> None:
    """A file truncated right at the central directory (losing the CD
    and EOCD but keeping every local entry intact) falls back to an
    LFH-vs-CD comparison; enough content survives for a "lineage" call
    even though the sizes no longer match."""
    data = build_minimal_pptx(num_slides=3, media_bytes=50_000)
    cd_offset, _cd_size, _eocd_offset = find_eocd(data)
    truncated = truncate(data, cd_offset)
    path_a = _write(tmp_path, "truncated.pptx", truncated)
    path_b = _write(tmp_path, "clean.pptx", data)

    diag_a = _diagnose(path_a)
    diag_b = _diagnose(path_b)
    assert diag_a.cd_census is None  # the central directory was cut off

    score = score_origin(diag_a, diag_b)

    assert score.size_match is False
    assert score.cd_pair is False
    assert score.lineage_score >= LINEAGE_SCORE_THRESHOLD
    assert score.tier == "lineage"


def test_edited_version_with_shared_media_is_lineage(tmp_path: Path) -> None:
    """A different version of the same presentation -- one slide edited,
    everything else (including an added media part) unchanged -- has a
    mismatched size but a perfect media match, and is recognised as a
    lineage match rather than a copy."""
    base = build_minimal_pptx(num_slides=3, media_bytes=20_000)
    jpeg = build_minimal_jpeg(pad_to=9000)
    data_a = make_edited_version(base, add={"ppt/media/image1.jpeg": jpeg})

    new_slide = (
        b"<p:sld><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/><p:sp>"
        b"<p:txBody><a:p><a:r><a:t>Edited content for version B, padded "
        b"so the archive size clearly differs from A.</a:t></a:r>"
        b"</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    data_b = make_edited_version(
        data_a, replace={"ppt/slides/slide1.xml": new_slide})
    if len(data_b) == len(data_a):
        # Extremely unlikely coincidence; force a size mismatch anyway.
        new_slide += b"X" * 64
        data_b = make_edited_version(
            data_a, replace={"ppt/slides/slide1.xml": new_slide})
    assert len(data_b) != len(data_a)

    path_a = _write(tmp_path, "a.pptx", data_a)
    path_b = _write(tmp_path, "b.pptx", data_b)

    score = score_origin(_diagnose(path_a), _diagnose(path_b))

    assert score.size_match is False
    assert score.media_ratio == 1.0
    assert score.tier == "lineage"


def test_unrelated_files_are_rejected(tmp_path: Path) -> None:
    """Two files that share only the minimal-pptx template parts (theme,
    master, layout, ...) but differ in every slide and every media part
    are not mistaken for a shared origin. Some incidental overlap in
    triple_ratio/name_ratio from the shared template is expected and not
    asserted away; only the final tier and the weighted lineage score
    matter."""
    base = build_minimal_pptx(num_slides=3, media_bytes=20_000)
    jpeg_a = build_minimal_jpeg(pad_to=9000)
    data_a = make_edited_version(base, add={"ppt/media/image1.jpeg": jpeg_a})

    base_c = build_minimal_pptx(num_slides=3, media_bytes=20_000, seed=123)
    jpeg_c = build_minimal_jpeg(pad_to=5000)
    slide_replacements = {
        f"ppt/slides/slide{n}.xml":
            f"<p:sld><p:cSld>different content for C slide {n}"
            "</p:cSld></p:sld>".encode()
        for n in range(1, 4)
    }
    data_c = make_edited_version(
        base_c, add={"ppt/media/image1.jpeg": jpeg_c},
        replace=slide_replacements)

    path_a = _write(tmp_path, "a.pptx", data_a)
    path_c = _write(tmp_path, "c.pptx", data_c)

    score = score_origin(_diagnose(path_a), _diagnose(path_c))

    assert score.lineage_score < LINEAGE_SCORE_THRESHOLD
    assert score.tier == "rejected"


def test_empty_file_is_rejected_without_error(tmp_path: Path) -> None:
    """An empty file has no comparable entries at all; scoring it
    against a normal file degrades to "rejected" without raising."""
    data = build_minimal_pptx(num_slides=2, media_bytes=5000)
    path_a = _write(tmp_path, "empty.pptx", b"")
    path_b = _write(tmp_path, "normal.pptx", data)

    score = score_origin(_diagnose(path_a), _diagnose(path_b))

    assert score.tier == "rejected"
    assert score.triple_ratio == 0.0
    assert score.name_ratio == 0.0
    assert score.media_ratio == 0.0


def test_entry_result_records_crc_and_comp_size(tmp_path: Path) -> None:
    """:class:`~pptrepair.census.EntryResult` carries the recorded
    crc/comp_size the scoring above relies on: every central-directory
    entry of a normal file matches its :class:`zipfile.ZipInfo`
    counterpart, and every LFH-scanned entry surviving a truncation
    (right at the central directory) also has a non-None crc."""
    data = build_minimal_pptx(num_slides=3, media_bytes=20_000)
    path = _write(tmp_path, "normal.pptx", data)

    cd_census = from_central_directory(path)
    assert cd_census is not None
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        info_by_name = {info.filename: info for info in zf.infolist()}
    for entry in cd_census.entries:
        info = info_by_name[entry.name]
        assert entry.crc == info.CRC
        assert entry.comp_size == info.compress_size

    cd_offset, _cd_size, _eocd_offset = find_eocd(data)
    truncated_path = _write(tmp_path, "truncated.pptx",
                            truncate(data, cd_offset))
    lfh_census = from_lfh_scan(truncated_path)
    assert lfh_census.total() > 0
    for entry in lfh_census.ok_entries():
        assert entry.crc is not None
