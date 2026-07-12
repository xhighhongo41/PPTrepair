"""Tests for :mod:`pptrepair.scanner`.

All fixtures are synthetic binaries written directly under ``tmp_path``;
no real .pptx sample files are used here.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from pptrepair import scanner
from pptrepair.scanner import CD_SIG, LFH_SIG, scan_structure


def _make_zip(path: Path, entries: list[tuple[str, bytes]]) -> Path:
    """Write a small real ZIP archive with the given entries and return its path."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return path


# --- zero-run detection ----------------------------------------------------


def test_zero_run_boundary_precision(tmp_path: Path) -> None:
    """A single zero run is refined to its exact byte boundaries."""
    data = b"\xff" * 4000 + b"\x00" * 10000 + b"\xff" * 2000
    path = tmp_path / "a.bin"
    path.write_bytes(data)

    result = scan_structure(path)

    assert len(result.zero_runs) == 1
    assert result.zero_runs[0].start == 4000
    assert result.zero_runs[0].end == 14000


def test_zero_run_multiple_regions(tmp_path: Path) -> None:
    """Several disjoint zero runs are each reported with exact boundaries.

    Each zero region here is long enough (9000 bytes) to guarantee it
    fully contains at least one BLOCK_SIZE-aligned block, so the coarse
    detection pass is guaranteed to flag it before refinement runs.
    """
    data = (
        b"\xff" * 100
        + b"\x00" * 9000
        + b"\xff" * 300
        + b"\x00" * 9000
        + b"\xff" * 50
    )
    path = tmp_path / "b.bin"
    path.write_bytes(data)

    result = scan_structure(path)

    starts_ends = [(run.start, run.end) for run in result.zero_runs]
    assert starts_ends == [(100, 9100), (9400, 18400)]


def test_zero_run_ending_at_eof(tmp_path: Path) -> None:
    """A zero run that reaches the end of the file is not read past EOF."""
    data = b"\xff" * 100 + b"\x00" * 8000
    path = tmp_path / "c.bin"
    path.write_bytes(data)

    result = scan_structure(path)

    assert len(result.zero_runs) == 1
    assert result.zero_runs[0].start == 100
    assert result.zero_runs[0].end == len(data)


def test_all_zero_file(tmp_path: Path) -> None:
    """A file made entirely of zero bytes has zero_ratio() == 1.0."""
    path = tmp_path / "d.bin"
    path.write_bytes(b"\x00" * 20000)

    result = scan_structure(path)

    assert len(result.zero_runs) == 1
    assert result.zero_runs[0].start == 0
    assert result.zero_runs[0].end == 20000
    assert result.zero_ratio() == 1.0


# --- empty file --------------------------------------------------------


def test_empty_file(tmp_path: Path) -> None:
    """An empty file yields empty/None facts throughout."""
    path = tmp_path / "empty.bin"
    path.write_bytes(b"")

    result = scan_structure(path)

    assert result.size == 0
    assert result.head_kind == "other"
    assert result.zero_runs == []
    assert result.eocd is None


# --- signature detection across chunk boundaries ------------------------


def test_signature_across_chunk_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signatures straddling or starting exactly at a chunk boundary are
    found exactly once, with no double-counting from the carried-over
    tail bytes."""
    size = 20000
    buf = bytearray(b"\xff" * size)

    lfh_offsets_expected = [0, 100, 8190, 16384, size - 4]
    cd_offsets_expected = [50, 8100, 12288, 19000]
    for off in lfh_offsets_expected:
        buf[off:off + 4] = LFH_SIG
    for off in cd_offsets_expected:
        buf[off:off + 4] = CD_SIG

    path = tmp_path / "boundary.bin"
    path.write_bytes(bytes(buf))

    monkeypatch.setattr(scanner, "CHUNK_SIZE", 8192)
    result = scan_structure(path)

    assert result.lfh_offsets == lfh_offsets_expected
    assert result.cd_sig_count == len(cd_offsets_expected)


# --- EOCD parsing --------------------------------------------------------


def test_eocd_parsing_valid_zip(tmp_path: Path) -> None:
    """A well-formed small ZIP produces a consistent EocdInfo."""
    entries = [("a.txt", b"hello"), ("b.txt", b"world" * 10), ("c/d.txt", b"x" * 100)]
    path = _make_zip(tmp_path / "sample.zip", entries)

    result = scan_structure(path)

    assert result.head_kind == "zip"
    assert result.eocd is not None
    assert result.eocd.total_entries == len(entries)
    assert result.eocd.is_consistent
    assert result.eocd.cd_offset + result.eocd.cd_size == result.eocd.offset


def test_eocd_none_when_tail_truncated(tmp_path: Path) -> None:
    """Stripping the last few bytes leaves an EOCD signature but no full
    22-byte record, so eocd is None while lfh_offsets survive."""
    entries = [("a.txt", b"hello"), ("b.txt", b"world" * 10)]
    original = _make_zip(tmp_path / "sample.zip", entries)
    data = original.read_bytes()

    truncated = tmp_path / "truncated.zip"
    truncated.write_bytes(data[:-10])

    result = scan_structure(truncated)

    assert result.eocd is None
    assert len(result.lfh_offsets) == len(entries)


def test_eocd_none_when_central_directory_removed(tmp_path: Path) -> None:
    """Removing the whole central directory (and EOCD) leaves no eocd but
    keeps the local file header offsets."""
    entries = [("a.txt", b"hello"), ("b.txt", b"world" * 10)]
    original = _make_zip(tmp_path / "sample.zip", entries)
    intact = scan_structure(original)
    assert intact.eocd is not None

    data = original.read_bytes()
    without_cd = tmp_path / "without_cd.zip"
    without_cd.write_bytes(data[: intact.eocd.cd_offset])

    result = scan_structure(without_cd)

    assert result.eocd is None
    assert len(result.lfh_offsets) == len(entries)


# --- head_kind classification -------------------------------------------


def test_head_kind_zeros(tmp_path: Path) -> None:
    """A file starting with four zero bytes is classified as ``"zeros"``."""
    path = tmp_path / "zeros.bin"
    path.write_bytes(b"\x00" * 100)

    result = scan_structure(path)

    assert result.head_kind == "zeros"


def test_head_kind_other(tmp_path: Path) -> None:
    """A file starting with unrelated bytes is classified as ``"other"``."""
    path = tmp_path / "other.bin"
    path.write_bytes(b"hello world")

    result = scan_structure(path)

    assert result.head_kind == "other"


# --- chunk-size independence ---------------------------------------------


def test_chunk_size_independent_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scan results do not depend on CHUNK_SIZE."""
    size = 100_000
    buf = bytearray(b"\xa5" * size)
    buf[20000:50000] = bytes(30000)  # a zero run spanning several 8192-byte chunks
    for off in (10, 8188, 16383, 60000, 90000, size - 4):
        buf[off:off + 4] = LFH_SIG
    for off in (100, 8000, 55000, 95000):
        buf[off:off + 4] = CD_SIG

    path = tmp_path / "probe.bin"
    path.write_bytes(bytes(buf))

    default_result = scan_structure(path)

    monkeypatch.setattr(scanner, "CHUNK_SIZE", 8192)
    patched_result = scan_structure(path)

    assert patched_result.zero_runs == default_result.zero_runs
    assert patched_result.lfh_offsets == default_result.lfh_offsets
    assert patched_result.cd_sig_count == default_result.cd_sig_count
    assert patched_result.eocd == default_result.eocd
    assert patched_result.head_kind == default_result.head_kind
