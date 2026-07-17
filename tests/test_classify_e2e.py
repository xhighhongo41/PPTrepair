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

from fixtures import (append_foreign_tail, build_minimal_pptx, find_eocd,
                      foreign_prefix, zero_interior_entry, zero_prefix,
                      zero_range)

from pptrepair.census import from_central_directory, from_lfh_scan
from pptrepair.classify import Diagnosis, Verdict, classify
from pptrepair.scanner import scan_structure


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
