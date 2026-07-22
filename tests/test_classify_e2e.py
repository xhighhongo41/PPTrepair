"""End-to-end tests for :mod:`pptrepair.classify`.

Unlike :mod:`test_classify`, which builds :class:`ZipStructure` /
:class:`CensusResult` dataclasses by hand, every fixture here is a real
byte stream run through the actual scan -> census -> classify pipeline
(:func:`pptrepair.scanner.scan_structure`,
:func:`pptrepair.census.from_central_directory`,
:func:`pptrepair.census.from_lfh_scan`). This exercises the decision
procedure against the exact byte-level geometry the CLI would see, in
particular for the v1.1.1 verdicts that depend on real EOCD offsets,
comment lengths and local-file-header positions.
"""

from __future__ import annotations

from pathlib import Path

from fixtures import (append_foreign_tail, build_foreign_zip,
                      build_minimal_pptx, find_eocd, foreign_prefix,
                      header_offset, lfh_offsets, overlay_foreign_zip_head,
                      zero_interior_entry, zero_prefix, zero_range)

from pptrepair.census import from_central_directory, from_lfh_scan
from pptrepair.classify import Diagnosis, Verdict, classify
from pptrepair.scanner import scan_structure

#: A handful of foreign, CRC-valid entries whose names imitate the Intel
#: driver-package ZIP fragments found overwriting a real corrupted .pptx.
_FOREIGN_ENTRIES = {
    "DTT/drivers/x64/DptfPolicyCritical.dll": b"critical policy driver " * 40,
    "DTT/drivers/x64/DptfPolicyPassive.dll": b"passive policy driver " * 40,
    "DTT/drivers/x64/DptfPolicyLpm.dll": b"lpm policy driver blob " * 40,
}


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the resulting path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _diagnose(path: Path) -> Diagnosis:
    """Run the scan -> census -> classify pipeline over *path*."""
    structure = scan_structure(path)
    cd_census = from_central_directory(path)
    lfh_census = from_lfh_scan(path)
    return classify(path, structure, cd_census, lfh_census)


def test_empty_file_is_empty_file(tmp_path: Path) -> None:
    """A zero-byte file classifies as EMPTY_FILE through the real pipeline."""
    path = _write(tmp_path, "empty.pptx", b"")

    diag = _diagnose(path)

    assert diag.verdict == Verdict.EMPTY_FILE


def test_all_zero_no_signature_is_full_zero_fill(tmp_path: Path) -> None:
    """A large all-zero file with no ZIP signature -> FULL_ZERO_FILL."""
    path = _write(tmp_path, "zerofilled.pptx", b"\x00" * (256 * 1024))

    diag = _diagnose(path)

    assert diag.verdict == Verdict.FULL_ZERO_FILL


def test_zeroed_up_to_eocd_is_full_zero_fill(tmp_path: Path) -> None:
    """Zeroing everything before the EOCD (keeping only its 22 bytes)
    destroys the central directory but leaves the EOCD parseable, taking
    the "EOCD present, CD unreadable, almost entirely zero" path (4a)."""
    data = build_minimal_pptx(num_slides=3, media_bytes=100_000)
    assert len(data) >= 2200  # required for zero_ratio to reach ALL_ZERO_RATIO
    _cd_offset, _cd_size, eocd_offset = find_eocd(data)
    zeroed = zero_range(data, 0, eocd_offset)
    path = _write(tmp_path, "zeroed_to_eocd.pptx", zeroed)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.FULL_ZERO_FILL
    assert diag.cd_census is None


def test_foreign_tail_is_tail_foreign_data(tmp_path: Path) -> None:
    """A complete archive followed by >TAIL_JUNK_MIN foreign bytes
    -> TAIL_FOREIGN_DATA."""
    data = build_minimal_pptx(num_slides=2, media_bytes=50_000)
    path = _write(tmp_path, "tail.pptx", append_foreign_tail(data, 131072))

    diag = _diagnose(path)

    assert diag.verdict == Verdict.TAIL_FOREIGN_DATA


def test_zeroed_interior_entry_is_interior_damage(tmp_path: Path) -> None:
    """One entry destroyed deep inside an otherwise-intact archive
    -> INTERIOR_DAMAGE (head and central directory both survive)."""
    data = build_minimal_pptx(num_slides=3, media_bytes=50_000)
    path = _write(tmp_path, "interior.pptx", zero_interior_entry(data))

    diag = _diagnose(path)

    assert diag.verdict == Verdict.INTERIOR_DAMAGE


def test_short_leading_zero_run_is_head_zero_fill(tmp_path: Path) -> None:
    """An 8192-byte leading zero run (below the old 65536 threshold, at
    or above the relaxed HEAD_ZERO_MIN_LENGTH of 4096) -> HEAD_ZERO_FILL."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    path = _write(tmp_path, "short_head_zero.pptx", zero_prefix(data, 8192))

    diag = _diagnose(path)

    assert diag.verdict == Verdict.HEAD_ZERO_FILL


def test_foreign_head_with_zero_start_is_head_foreign_data(
    tmp_path: Path,
) -> None:
    """Foreign head data whose first 16 bytes happen to read as zeros is
    still recognised as HEAD_FOREIGN_DATA (the ``"zeros"`` head-kind
    allowance)."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    corrupted = foreign_prefix(data, 8192)
    corrupted = b"\x00" * 16 + corrupted[16:]
    path = _write(tmp_path, "zeros_then_foreign.pptx", corrupted)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.HEAD_FOREIGN_DATA


def test_scattered_damage_stays_other_corrupt(tmp_path: Path) -> None:
    """Foreign head data plus a second, independent entry destroyed
    further into the archive must NOT be absorbed by HEAD_FOREIGN_DATA's
    "confined to the head" guard, and stays OTHER_CORRUPT."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    corrupted = foreign_prefix(data, 8192)
    scattered = zero_interior_entry(corrupted, skip=1)
    path = _write(tmp_path, "scattered.pptx", scattered)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.OTHER_CORRUPT


def test_embedded_foreign_zip_head_is_head_foreign_data(
    tmp_path: Path,
) -> None:
    """A leading region overwritten by a *real* foreign ZIP (whose own
    CRC-valid local headers live deep inside the overwrite) is still
    recognised as HEAD_FOREIGN_DATA: rule 6c's boundary is taken only
    from CD-corroborated entries, so the foreign fragments cannot pull it
    earlier. Their names are surfaced as evidence."""
    data = build_minimal_pptx(num_slides=3, media_bytes=100_000)
    boundary = header_offset(data, "ppt/presProps.xml")
    foreign_zip = build_foreign_zip(_FOREIGN_ENTRIES)
    corrupted = overlay_foreign_zip_head(data, boundary, foreign_zip)
    path = _write(tmp_path, "foreign_head.pptx", corrupted)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.HEAD_FOREIGN_DATA
    joined = "\n".join(diag.evidence)
    assert "foreign archive fragments found by scanning" in joined
    assert "DptfPolicyCritical.dll" in joined


def test_random_foreign_prefix_stays_head_foreign_data(
    tmp_path: Path,
) -> None:
    """Regression: the classic random foreign-prefix corruption still
    classifies as HEAD_FOREIGN_DATA (no CD-unknown CRC-valid fragments,
    so no foreign-fragment evidence line)."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    boundary = header_offset(data, "ppt/presProps.xml")
    corrupted = foreign_prefix(data, boundary)
    path = _write(tmp_path, "foreign_prefix.pptx", corrupted)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.HEAD_FOREIGN_DATA
    assert "foreign archive fragments" not in "\n".join(diag.evidence)


def test_embedded_foreign_zip_plus_interior_kill_is_foreign_zip_overwrite(
    tmp_path: Path,
) -> None:
    """A foreign-ZIP overwrite that is NOT confined to the head (a
    surviving tail entry is also destroyed, defeating rule 6c's
    head-confinement guard) falls through to FOREIGN_ZIP_OVERWRITE."""
    data = build_minimal_pptx(num_slides=3, media_bytes=100_000)
    boundary = header_offset(data, "ppt/presProps.xml")
    foreign_zip = build_foreign_zip(_FOREIGN_ENTRIES)
    corrupted = overlay_foreign_zip_head(data, boundary, foreign_zip)
    # Destroy the second surviving tail entry so damage spreads past the
    # head; its offset then sits at or after the first CD-matched entry.
    tail = [off for off in lfh_offsets(corrupted) if off >= boundary]
    assert len(tail) >= 3
    scattered = zero_range(corrupted, tail[1], tail[2])
    path = _write(tmp_path, "foreign_scattered.pptx", scattered)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.FOREIGN_ZIP_OVERWRITE
    assert "unrelated ZIP archive" in "\n".join(diag.evidence)


def test_whole_body_overwrite_is_scattered_overwrite(tmp_path: Path) -> None:
    """Overwriting the entire archive body (every local entry, keeping
    only the central directory and EOCD) on an archive with >= 20 indexed
    entries -> SCATTERED_OVERWRITE."""
    data = build_minimal_pptx(num_slides=25, media_bytes=4096)
    cd_offset, _cd_size, _eocd_offset = find_eocd(data)
    corrupted = foreign_prefix(data, cd_offset, seed=5)
    path = _write(tmp_path, "scattered_body.pptx", corrupted)

    diag = _diagnose(path)

    assert diag.verdict == Verdict.SCATTERED_OVERWRITE
    assert diag.cd_census is not None
    assert diag.cd_census.total() >= 20
