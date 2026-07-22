"""Tests for :mod:`pptrepair.merge` (lineage S byte-splice restoration).

Every fixture is a real byte stream written to disk and merged through
the actual diagnose -> score -> splice pipeline, matching the approach of
:mod:`test_origin`, so the splice is exercised against exactly the
recorded metadata the CLI would see. Corrupted copies are produced with
:func:`fixtures.make_corrupted_copies` from a synthetic minimal .pptx.
"""

from __future__ import annotations

import io
import struct
import zipfile
from pathlib import Path

import pytest

import fixtures
from fixtures import build_minimal_pptx, find_eocd, make_corrupted_copies

from pptrepair import merge as merge_module
from pptrepair.merge import merge_restore
from pptrepair.origin import OriginScore
from pptrepair.repair import OutputExistsError

_LFH_STRUCT = "<IHHHHHIIIHH"


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the resulting path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _cd_offsets(data: bytes) -> list[int]:
    """Return every entry's local-header offset, ordered, from the CD."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return sorted(info.header_offset for info in archive.infolist())


def _entry_interval(data: bytes, name: str) -> tuple[int, int]:
    """Return the ``[offset, next_offset)`` byte range of member *name*.

    The end is the next entry's header offset, or the central-directory
    start when *name* is the last entry, matching the ranges the splice
    reconstructs.
    """
    offsets = _cd_offsets(data)
    offset = fixtures.header_offset(data, name)
    cd_offset, _size, _eocd = find_eocd(data)
    index = offsets.index(offset)
    end = offsets[index + 1] if index + 1 < len(offsets) else cd_offset
    return offset, end


def _entry_data_span(data: bytes, name: str) -> tuple[int, int]:
    """Return the ``[data_start, data_end)`` compressed-payload range.

    Unlike :func:`_entry_interval` this keeps the local file header
    intact, so the entry still appears in a header scan but fails its CRC.
    """
    offset = fixtures.header_offset(data, name)
    fields = struct.unpack(_LFH_STRUCT, data[offset:offset + 30])
    comp_size = fields[7]
    name_len = fields[9]
    extra_len = fields[10]
    data_start = offset + 30 + name_len + extra_len
    return data_start, data_start + comp_size


def _provenance(outcome, name: str):
    """Return the :class:`EntryProvenance` for *name*, or None."""
    for prov in outcome.provenances:
        if prov.name == name:
            return prov
    return None


def test_complementary_copies_restore_full(tmp_path: Path) -> None:
    """Two copies broken in non-overlapping ranges restore byte-identically.

    Copy A destroys the media entry's range; copy B destroys slide1's
    range. Each missing range survives in the other copy, so the splice
    reproduces the original file exactly (``guarantee="full"``) and each
    entry's provenance points at the copy that still carried it.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    media_start, media_end = _entry_interval(data, "ppt/media/image1.png")
    slide_start, slide_end = _entry_interval(data, "ppt/slides/slide1.xml")
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", media_start, media_end)],
        [("zero_range", slide_start, slide_end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)

    outcome = merge_restore([path_a, path_b], output=tmp_path / "out.pptx")

    assert outcome.guarantee == "full"
    assert outcome.output_path is not None
    assert outcome.output_path.read_bytes() == data
    assert _provenance(outcome, "ppt/media/image1.png").source == path_b
    assert _provenance(outcome, "ppt/slides/slide1.xml").source == path_a


def test_same_length_truncated_pair_degrades_to_partial(
        tmp_path: Path) -> None:
    """Two equally truncated copies (both CD lost) degrade to partial.

    Each copy zeroes a different slide's data but keeps its header, so
    both copies still pass same-origin scoring; with no central directory
    the degraded union recovers every entry and rebuilds a partial output.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=60_000)
    cd_offset, _size, _eocd = find_eocd(data)
    s1_start, s1_end = _entry_data_span(data, "ppt/slides/slide1.xml")
    s2_start, s2_end = _entry_data_span(data, "ppt/slides/slide2.xml")
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", s1_start, s1_end), ("truncate", cd_offset)],
        [("zero_range", s2_start, s2_end), ("truncate", cd_offset)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)

    outcome = merge_restore([path_a, path_b], output=tmp_path / "out.pptx")

    assert outcome.guarantee == "partial"
    assert outcome.output_path is not None
    with zipfile.ZipFile(outcome.output_path) as archive:
        assert archive.testzip() is None


def test_entry_broken_in_both_copies_is_missing(tmp_path: Path) -> None:
    """An entry broken in every copy is reported missing and rebuilt.

    Both copies damage slide3's data (differently, so they are not
    identical), leaving it recoverable from neither; the splice marks it
    missing and delegates to the rebuild fallback for a partial output.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=60_000)
    start, end = _entry_data_span(data, "ppt/slides/slide3.xml")
    mid_a = start + (end - start) * 6 // 10
    mid_b = start + (end - start) * 3 // 10
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", mid_a, end)],
        [("zero_range", mid_b, end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)

    outcome = merge_restore([path_a, path_b], output=tmp_path / "out.pptx")

    assert _provenance(outcome, "ppt/slides/slide3.xml").method == "missing"
    assert outcome.guarantee == "partial"
    assert outcome.output_path is not None
    assert outcome.output_path.exists()


def test_single_source_raises(tmp_path: Path) -> None:
    """A single source is a usage error (nothing to merge against)."""
    data = build_minimal_pptx(num_slides=2, media_bytes=10_000)
    path = _write(tmp_path, "only.pptx", data)

    with pytest.raises(ValueError):
        merge_restore([path])


def test_unrelated_file_is_excluded(tmp_path: Path) -> None:
    """A differently sized unrelated file is excluded, and merge continues.

    The target is head-corrupted, an unrelated .pptx of a different size
    is mixed in, and an intact twin follows; the unrelated file is noted
    as excluded while the twin restores the target.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    (corrupted,) = make_corrupted_copies(data, [[("foreign_prefix", 8192)]])
    unrelated = build_minimal_pptx(num_slides=6, media_bytes=400_000, seed=99)

    path_a = _write(tmp_path, "a.pptx", corrupted)
    path_unrelated = _write(tmp_path, "unrelated.pptx", unrelated)
    path_b = _write(tmp_path, "b.pptx", data)

    outcome = merge_restore(
        [path_a, path_unrelated, path_b], output=tmp_path / "out.pptx")

    assert any(path_unrelated.name in note for note in outcome.notes)
    assert outcome.guarantee == "full"
    assert outcome.output_path is not None
    assert outcome.output_path.read_bytes() == data


def test_identical_copies_are_reported(tmp_path: Path) -> None:
    """Two byte-identical copies are flagged as offering no merge gain."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    path_b = _write(tmp_path, "b.pptx", data)

    outcome = merge_restore([path_a, path_b], output=tmp_path / "out.pptx")

    assert any("identical copies" in note for note in outcome.notes)


def test_intact_twin_restores_full(tmp_path: Path) -> None:
    """A head-corrupted file plus an intact twin restore byte-identically."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    (corrupted,) = make_corrupted_copies(data, [[("foreign_prefix", 8192)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    path_b = _write(tmp_path, "b.pptx", data)

    outcome = merge_restore([path_a, path_b], output=tmp_path / "out.pptx")

    assert outcome.guarantee == "full"
    assert outcome.output_path is not None
    assert outcome.output_path.read_bytes() == data


def test_candidate_gate(tmp_path: Path, monkeypatch) -> None:
    """A candidate-tier source is used only when allow_candidate is set.

    Scoring is stubbed to a ``candidate`` tier so the gate can be tested
    deterministically. Without the flag the twin is noted as unused;
    with it, the intact twin fully restores the head-corrupted target.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    (corrupted,) = make_corrupted_copies(data, [[("foreign_prefix", 8192)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    path_b = _write(tmp_path, "b.pptx", data)

    def fake_score(diag_a, diag_b):
        return OriginScore(
            size_match=True, cd_pair=True, triple_ratio=0.5, name_ratio=0.8,
            media_ratio=0.0, lineage_score=0.0, tier="candidate", evidence=[])

    monkeypatch.setattr(merge_module, "score_origin", fake_score)

    without = merge_restore([path_a, path_b], output=tmp_path / "n1.pptx")
    assert any("candidate" in note and "not used" in note
               for note in without.notes)

    with_flag = merge_restore(
        [path_a, path_b], output=tmp_path / "n2.pptx", allow_candidate=True)
    assert with_flag.guarantee == "full"
    assert with_flag.output_path is not None
    assert with_flag.output_path.read_bytes() == data


def test_output_collision(tmp_path: Path) -> None:
    """An existing output is refused unless --force is given."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    path_b = _write(tmp_path, "b.pptx", data)
    out_path = tmp_path / "taken.pptx"
    out_path.write_bytes(b"existing")

    with pytest.raises(OutputExistsError):
        merge_restore([path_a, path_b], output=out_path)

    outcome = merge_restore([path_a, path_b], output=out_path, force=True)
    assert outcome.output_path == out_path
    assert out_path.exists()


def test_failed_leaves_no_file(tmp_path: Path) -> None:
    """When nothing usable survives, the output path stays absent.

    Both copies destroy ``ppt/presentation.xml`` and lose the central
    directory, so the degraded rebuild cannot proceed: the outcome is
    ``failed`` with no output path and no file on disk.
    """
    data = build_minimal_pptx(num_slides=2, media_bytes=30_000)
    cd_offset, _size, _eocd = find_eocd(data)
    pres_start, pres_end = _entry_data_span(data, "ppt/presentation.xml")
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", pres_start, pres_end), ("truncate", cd_offset)],
        [("zero_range", pres_start, pres_end), ("truncate", cd_offset)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    out_path = tmp_path / "out.pptx"

    outcome = merge_restore([path_a, path_b], output=out_path)

    assert outcome.guarantee == "failed"
    assert outcome.output_path is None
    assert not out_path.exists()
