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
from fixtures import (build_minimal_jpeg, build_minimal_pptx, find_eocd,
                      make_corrupted_copies, make_edited_version)

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


def _aligned_boundaries(start: int, end: int) -> list[int]:
    """Return the 64 KiB-aligned offsets strictly inside ``[start, end)``.

    Mirrors :func:`pptrepair.merge._crossover_boundaries` so the tests can
    target the exact segments the crossover splice splits an entry into.
    """
    align = 65536
    first = (start // align + 1) * align
    return list(range(first, end, align))


def _seg_chunk(split_points: list[int], index: int) -> tuple:
    """Return a ``zero_range`` op zeroing 4000 bytes inside segment *index*.

    The zeroed span sits near the middle of the segment, safely past any
    local file header and inside the compressed payload, so it corrupts
    that one 64 KiB-aligned segment while leaving its neighbours intact.
    """
    lo = split_points[index]
    hi = split_points[index + 1]
    mid = (lo + hi) // 2
    return ("zero_range", mid - 2000, mid + 2000)


def _read_member(data: bytes, name: str) -> bytes:
    """Return member *name*'s decompressed bytes from archive *data*."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name)


def _lineage_versions(media_bytes: int = 60_000, *, add_jpeg: bool = True,
                      seed: int = 0) -> tuple[bytes, bytes]:
    """Return an original archive and a lineage version of it.

    The original is a minimal deck (optionally with an extra stored JPEG
    media part); the version replaces ``slide1`` with a longer body so the
    two differ in size while every media part stays byte-identical -- the
    shape :func:`pptrepair.origin.score_origin` recognises as a
    ``lineage`` donor rather than a same-save copy.
    """
    base = build_minimal_pptx(num_slides=3, media_bytes=media_bytes, seed=seed)
    if add_jpeg:
        original = make_edited_version(
            base,
            add={"ppt/media/image1.jpeg": build_minimal_jpeg(pad_to=9000)})
    else:
        original = base
    new_slide = (
        b"<p:sld><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/><p:sp>"
        b"<p:txBody><a:p><a:r><a:t>Edited slide body for the lineage "
        b"version, padded so the archive size clearly differs.</a:t>"
        b"</a:r></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    version = make_edited_version(
        original, replace={"ppt/slides/slide1.xml": new_slide})
    if len(version) == len(original):
        version = make_edited_version(
            original, replace={"ppt/slides/slide1.xml": new_slide + b"X" * 64})
    assert len(version) != len(original)
    return original, version


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


def test_three_copies_each_sole_survivor_restore_full(
        tmp_path: Path) -> None:
    """Three copies, each the sole survivor of one entry, still merge full.

    ``merge_restore`` takes any number of sources, not just a pair. Here
    every damaged entry survives in exactly *one* of three copies (media
    only in B, slide1 only in C, slide2 only in A), so a byte-identical
    reconstruction is only possible when the splice draws on all three --
    which the per-entry provenances then prove it did.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    media = _entry_interval(data, "ppt/media/image1.png")
    slide1 = _entry_interval(data, "ppt/slides/slide1.xml")
    slide2 = _entry_interval(data, "ppt/slides/slide2.xml")
    copy_a, copy_b, copy_c = make_corrupted_copies(data, [
        [("zero_range", *media), ("zero_range", *slide1)],
        [("zero_range", *slide1), ("zero_range", *slide2)],
        [("zero_range", *media), ("zero_range", *slide2)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    path_c = _write(tmp_path, "c.pptx", copy_c)

    outcome = merge_restore([path_a, path_b, path_c],
                            output=tmp_path / "out.pptx")

    assert outcome.guarantee == "full"
    assert outcome.output_path.read_bytes() == data
    assert _provenance(outcome, "ppt/media/image1.png").source == path_b
    assert _provenance(outcome, "ppt/slides/slide1.xml").source == path_c
    assert _provenance(outcome, "ppt/slides/slide2.xml").source == path_a


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


def test_crossover_restores_full(tmp_path: Path) -> None:
    """A single 64 KiB copy switch inside one entry restores the file whole.

    The large media entry spans several 64 KiB boundaries. Copy A breaks
    the segment before the first boundary while copy B breaks the segment
    after the last one, so no single copy carries the media whole; the
    crossover splice takes the head from B and the tail from A across the
    first boundary, reproducing the original file byte-for-byte with a
    two-segment provenance.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=280_000)
    start, end = _entry_interval(data, "ppt/media/image1.png")
    boundaries = _aligned_boundaries(start, end)
    assert len(boundaries) >= 1
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", boundaries[0] - 4000, boundaries[0])],
        [("zero_range", boundaries[-1], min(boundaries[-1] + 4000, end))],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)

    outcome = merge_restore([path_a, path_b], output=tmp_path / "out.pptx")

    prov = _provenance(outcome, "ppt/media/image1.png")
    assert prov.method == "crossover"
    assert prov.sources is not None
    assert len(prov.sources) == 2
    assert outcome.guarantee == "full"
    assert outcome.output_path is not None
    assert outcome.output_path.read_bytes() == data


def test_crossover_two_switch_shortfall_is_missing(tmp_path: Path) -> None:
    """Alternating damage needing three switches exceeds the crossover limit.

    Copy A breaks the even media segments (S0, S2) and copy B the odd ones
    (S1, S3), so each segment survives in exactly one copy and recovery
    would need the sequence B, A, B, A -- three copy switches, one more
    than the crossover's two-switch cap. The entry therefore stays
    missing (no attempt-cap note, the search space is exhausted) and the
    rebuild fallback yields a partial output.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=280_000)
    start, end = _entry_interval(data, "ppt/media/image1.png")
    boundaries = _aligned_boundaries(start, end)
    assert len(boundaries) >= 3
    split_points = [start, *boundaries, end]
    copy_a, copy_b = make_corrupted_copies(data, [
        [_seg_chunk(split_points, 0), _seg_chunk(split_points, 2)],
        [_seg_chunk(split_points, 1), _seg_chunk(split_points, 3)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)

    outcome = merge_restore([path_a, path_b], output=tmp_path / "out.pptx")

    prov = _provenance(outcome, "ppt/media/image1.png")
    assert prov.method == "missing"
    assert prov.sources is None
    assert outcome.guarantee == "partial"
    assert outcome.output_path is not None
    assert outcome.output_path.exists()
    assert not any("attempt cap" in note for note in outcome.notes)


def test_crossover_attempt_cap_noted(tmp_path: Path, monkeypatch) -> None:
    """Hitting the crossover attempt cap is recorded as a note.

    The material is the recoverable case of
    :func:`test_crossover_restores_full`, but the per-entry cap is lowered
    to a single combination so the search is cut off before it reaches the
    winning one. The entry then falls through to missing and the run
    records the attempt-cap note.
    """
    monkeypatch.setattr(merge_module, "MAX_CROSSOVER_ATTEMPTS", 1)
    data = build_minimal_pptx(num_slides=3, media_bytes=280_000)
    start, end = _entry_interval(data, "ppt/media/image1.png")
    boundaries = _aligned_boundaries(start, end)
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", boundaries[0] - 4000, boundaries[0])],
        [("zero_range", boundaries[-1], min(boundaries[-1] + 4000, end))],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)

    outcome = merge_restore([path_a, path_b], output=tmp_path / "out.pptx")

    assert any("hit the attempt cap" in note for note in outcome.notes)


def test_crossover_skipped_without_boundary(tmp_path: Path) -> None:
    """A small entry with no interior 64 KiB boundary is never crossed over.

    Both copies break a small slide entry that fits inside one 64 KiB
    block, so its range holds no absolute alignment boundary to switch at.
    The crossover is not attempted, the entry is reported missing (with no
    attempt-cap note), and the rebuild fallback handles the rest.
    """
    data = build_minimal_pptx(num_slides=3, media_bytes=60_000)
    start, end = _entry_interval(data, "ppt/slides/slide2.xml")
    assert not _aligned_boundaries(start, end)
    ds, de = _entry_data_span(data, "ppt/slides/slide2.xml")
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", ds, de)],
        [("zero_range", ds + 4, de)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)

    outcome = merge_restore([path_a, path_b], output=tmp_path / "out.pptx")

    prov = _provenance(outcome, "ppt/slides/slide2.xml")
    assert prov.method == "missing"
    assert not any("attempt cap" in note for note in outcome.notes)


def test_lineage_donor_supplies_missing_entry(tmp_path: Path) -> None:
    """An entry broken in every copy is rescued from a lineage donor.

    Two identical copies both destroy the shared media part, so the
    splice cannot recover it; a lineage donor (a different saved version
    that never edited that part) carries it with the reference CD's
    recorded CRC-32, so it is adopted as an oracle-verified ``donor``
    entry and the rebuilt output holds it byte-for-byte.
    """
    original, donor = _lineage_versions()
    name = "ppt/media/image1.jpeg"
    start, end = _entry_interval(original, name)
    copy_a, copy_b = make_corrupted_copies(original, [
        [("zero_range", start, end)],
        [("zero_range", start, end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    path_donor = _write(tmp_path, "donor.pptx", donor)

    outcome = merge_restore(
        [path_a, path_b, path_donor], output=tmp_path / "out.pptx",
        allow_lineage=True)

    prov = _provenance(outcome, name)
    assert prov.method == "donor"
    assert prov.source == path_donor
    assert prov.sources is None
    assert outcome.guarantee == "partial"
    assert outcome.output_path is not None
    assert _read_member(outcome.output_path.read_bytes(), name) == \
        _read_member(original, name)


def test_lineage_donor_edited_entry_adopted_unverified(
        tmp_path: Path) -> None:
    """A donor's edited entry is adopted unverified, never as verified donor.

    The entry broken in every copy is the very slide the donor edited, so
    the donor's copy carries a different CRC-32 than the reference central
    directory recorded. The oracle-checked first pass therefore refuses it
    as a ``"donor"``; the second pass adopts it on the donor's own CRC-32
    alone as ``"donor_unverified"``, which caps the run at ``"hybrid"``.
    The rebuilt output then holds the donor's edited version of the entry,
    explicitly marked as an unverified adoption.
    """
    original, donor = _lineage_versions()
    name = "ppt/slides/slide1.xml"  # the slide the donor edited
    start, end = _entry_interval(original, name)
    copy_a, copy_b = make_corrupted_copies(original, [
        [("zero_range", start, end)],
        [("zero_range", start, end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    path_donor = _write(tmp_path, "donor.pptx", donor)

    outcome = merge_restore(
        [path_a, path_b, path_donor], output=tmp_path / "out.pptx",
        allow_lineage=True)

    prov = _provenance(outcome, name)
    assert prov.method == "donor_unverified"
    assert prov.source == path_donor
    assert not any(other.method == "donor" for other in outcome.provenances)
    assert outcome.guarantee == "hybrid"
    assert outcome.output_path is not None
    out_bytes = outcome.output_path.read_bytes()
    assert _read_member(out_bytes, name) == _read_member(donor, name)


def test_degraded_lineage_donor_is_hybrid(tmp_path: Path) -> None:
    """Degraded mode adopts donor entries unverified, capped at hybrid.

    Both copies are truncated inside the media part, so the central
    directory and every tail part are lost and no reference oracle
    survives. A lineage donor supplies the truncated media and the missing
    tail parts on its own CRC-32 alone (``donor_unverified``), which
    downgrades the run to ``hybrid`` while still producing a self-checking
    archive.
    """
    original, donor = _lineage_versions(media_bytes=60_000, add_jpeg=False)
    media_start, media_end = _entry_interval(original, "ppt/media/image1.png")
    cut = (media_start + media_end) // 2
    copy_a, copy_b = make_corrupted_copies(original, [
        [("truncate", cut)],
        [("truncate", cut)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    path_donor = _write(tmp_path, "donor.pptx", donor)

    outcome = merge_restore(
        [path_a, path_b, path_donor], output=tmp_path / "out.pptx",
        allow_lineage=True)

    assert any(prov.method == "donor_unverified"
               for prov in outcome.provenances)
    assert outcome.guarantee == "hybrid"
    assert outcome.output_path is not None
    with zipfile.ZipFile(outcome.output_path) as archive:
        assert archive.testzip() is None


def test_lineage_donor_gated_off(tmp_path: Path) -> None:
    """Without allow_lineage a lineage source is noted, never used.

    The same material as :func:`test_lineage_donor_supplies_missing_entry`
    but with the flag left off: the broken media stays missing, no donor
    provenance appears, and the run records the lineage source as unused.
    """
    original, donor = _lineage_versions()
    name = "ppt/media/image1.jpeg"
    start, end = _entry_interval(original, name)
    copy_a, copy_b = make_corrupted_copies(original, [
        [("zero_range", start, end)],
        [("zero_range", start, end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    path_donor = _write(tmp_path, "donor.pptx", donor)

    outcome = merge_restore(
        [path_a, path_b, path_donor], output=tmp_path / "out.pptx")

    assert _provenance(outcome, name).method == "missing"
    assert not any(
        prov.method in ("donor", "donor_unverified")
        for prov in outcome.provenances)
    assert any("pass allow_lineage to include it" in note
               for note in outcome.notes)


def test_lineage_donor_unused_on_full_restore(tmp_path: Path) -> None:
    """A full splice restore never touches an available lineage donor.

    Complementary copies (one breaks the media, the other slide1) restore
    the original byte-for-byte, so no entry is ever missing; the lineage
    donor supplied alongside them is left entirely unused and the
    guarantee stays ``full``.
    """
    original, donor = _lineage_versions(media_bytes=200_000, add_jpeg=False)
    media_start, media_end = _entry_interval(original, "ppt/media/image1.png")
    slide_start, slide_end = _entry_interval(original, "ppt/slides/slide1.xml")
    copy_a, copy_b = make_corrupted_copies(original, [
        [("zero_range", media_start, media_end)],
        [("zero_range", slide_start, slide_end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    path_donor = _write(tmp_path, "donor.pptx", donor)

    outcome = merge_restore(
        [path_a, path_b, path_donor], output=tmp_path / "out.pptx",
        allow_lineage=True)

    assert outcome.guarantee == "full"
    assert outcome.output_path is not None
    assert outcome.output_path.read_bytes() == original
    assert not any(
        prov.method in ("donor", "donor_unverified")
        for prov in outcome.provenances)


def test_plumbing_xml_donor_unverified_is_hybrid(tmp_path: Path) -> None:
    """Broken plumbing XML with a readable CD is rescued unverified (hybrid).

    Both copies keep their central directory (the oracle survives) but
    zero out ``ppt/presentation.xml``. A lineage donor carries that part,
    yet with a different CRC-32 -- the real-world case where every
    plumbing XML part (presentation/content-types/rels) changes between
    saved versions. The oracle-checked first pass therefore cannot adopt
    it; the second pass adopts it on the donor's own CRC-32 alone
    (``donor_unverified``), which caps the run at ``hybrid`` while still
    producing a self-checking archive that carries the required parts.

    The donor's ``ppt/presentation.xml`` is given a distinct CRC-32 here
    by inserting a harmless XML comment, since the shared
    :func:`_lineage_versions` helper edits only ``slide1`` and would
    otherwise leave every plumbing part byte-identical to the original
    (an oracle match, adopted as a verified ``donor``).
    """
    original, base_donor = _lineage_versions()
    pres_name = "ppt/presentation.xml"
    edited_pres = _read_member(base_donor, pres_name).replace(
        b"</p:presentation>", b"<!-- lineage version --></p:presentation>")
    assert edited_pres != _read_member(base_donor, pres_name)
    donor = make_edited_version(base_donor, replace={pres_name: edited_pres})

    start, end = _entry_interval(original, pres_name)
    copy_a, copy_b = make_corrupted_copies(original, [
        [("zero_range", start, end)],
        [("zero_range", start, end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    path_donor = _write(tmp_path, "donor.pptx", donor)

    outcome = merge_restore(
        [path_a, path_b, path_donor], output=tmp_path / "out.pptx",
        allow_lineage=True)

    prov = _provenance(outcome, pres_name)
    assert prov.method == "donor_unverified"
    assert prov.source == path_donor
    assert outcome.guarantee == "hybrid"
    assert outcome.output_path is not None
    with zipfile.ZipFile(outcome.output_path) as archive:
        assert archive.testzip() is None
        names = set(archive.namelist())
    assert {pres_name, "[Content_Types].xml", "_rels/.rels"} <= names
    assert any(n.startswith("ppt/slides/slide") for n in names)


def test_entry_absent_from_donor_stays_missing(tmp_path: Path) -> None:
    """An entry no copy and no donor carries is reported missing, not adopted.

    The entry broken in every copy (slide3) is one the lineage donor
    dropped entirely, so neither rescue pass can supply it: it stays
    ``missing`` while the rebuild fallback prunes its dangling references
    and still emits a self-checking archive from the parts that survived.
    """
    original, base_donor = _lineage_versions()
    name = "ppt/slides/slide3.xml"
    donor = make_edited_version(base_donor, remove=[name])
    with zipfile.ZipFile(io.BytesIO(donor)) as archive:
        assert name not in set(archive.namelist())

    start, end = _entry_interval(original, name)
    copy_a, copy_b = make_corrupted_copies(original, [
        [("zero_range", start, end)],
        [("zero_range", start, end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    path_donor = _write(tmp_path, "donor.pptx", donor)

    outcome = merge_restore(
        [path_a, path_b, path_donor], output=tmp_path / "out.pptx",
        allow_lineage=True)

    assert _provenance(outcome, name).method == "missing"
    assert outcome.guarantee in ("partial", "hybrid")
    assert outcome.output_path is not None
    with zipfile.ZipFile(outcome.output_path) as archive:
        assert archive.testzip() is None
