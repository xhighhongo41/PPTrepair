"""Tests for :mod:`pptrepair.salvage`.

All fixtures are synthetic archives built in memory and written under
``tmp_path``; no real .pptx sample files are touched. The full diagnosis
pipeline (``scan_structure`` + ``from_central_directory`` +
``from_lfh_scan`` + ``classify``) drives the verdict-dependent source
selection, while the raw retrieval paths are exercised by streaming each
salvaged entry back out and comparing it byte-for-byte with the pristine
archive read through :mod:`zipfile`.
"""

from __future__ import annotations

import struct
import zipfile
from io import BytesIO
from pathlib import Path

import pytest
from fixtures import (
    build_minimal_pptx,
    build_zip_with_data_descriptors,
    find_eocd,
    truncate,
    version_mix,
    zero_prefix,
)

from pptrepair.census import (
    CensusResult,
    EntryResult,
    from_central_directory,
    from_lfh_scan,
)
from pptrepair.classify import Diagnosis, Verdict, classify
from pptrepair.salvage import (
    SalvageError,
    SalvageReader,
    SalvagedEntry,
    select_salvageable,
)
from pptrepair.scanner import scan_structure

#: Tail parts of build_minimal_pptx() that survive head corruption.
_TAIL_NAMES = frozenset(
    [
        "ppt/presProps.xml",
        "ppt/viewProps.xml",
        "ppt/tableStyles.xml",
        "docProps/core.xml",
        "docProps/app.xml",
    ]
)


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _diagnose(path: Path) -> Diagnosis:
    """Run the full diagnosis pipeline over *path*."""
    structure = scan_structure(path)
    cd = from_central_directory(path)
    lfh = from_lfh_scan(path)
    return classify(path, structure, cd, lfh)


def _original_contents(data: bytes) -> dict[str, bytes]:
    """Return ``{name: bytes}`` for every member of a pristine archive."""
    contents: dict[str, bytes] = {}
    with zipfile.ZipFile(BytesIO(data)) as zf:
        for name in zf.namelist():
            contents[name] = zf.read(name)
    return contents


def _read_all(reader: SalvageReader, salvaged: SalvagedEntry) -> bytes:
    """Concatenate every chunk yielded for *salvaged*."""
    return b"".join(reader.open(salvaged))


# --- source selection: tail truncation -------------------------------------


def test_select_tail_truncated_uses_lfh_only(tmp_path: Path) -> None:
    """Truncating before the central directory yields an LFH-only salvage."""
    data = build_minimal_pptx()
    cd_offset, _cd_size, _eocd_offset = find_eocd(data)
    path = _write(tmp_path, "tr.pptx", truncate(data, cd_offset))

    diagnosis = _diagnose(path)
    assert diagnosis.verdict == Verdict.TAIL_TRUNCATED

    entries, warnings = select_salvageable(diagnosis)

    assert entries
    assert warnings == []
    assert {e.source for e in entries} == {"lfh"}
    # Cutting exactly at the central directory keeps every local entry
    # intact, so the whole package is recoverable.
    names = {e.name for e in entries}
    assert "ppt/presentation.xml" in names
    assert "ppt/media/image1.png" in names


# --- source selection: head zero-fill --------------------------------------


def test_select_head_zero_fill_uses_cd_only(tmp_path: Path) -> None:
    """A zeroed head keeps the CD-indexed tail parts, all CD-sourced."""
    data = build_minimal_pptx()
    path = _write(tmp_path, "zp.pptx", zero_prefix(data, 262144))

    diagnosis = _diagnose(path)
    assert diagnosis.verdict == Verdict.HEAD_ZERO_FILL

    entries, warnings = select_salvageable(diagnosis)

    assert entries
    assert warnings == []
    assert {e.source for e in entries} == {"cd"}
    names = {e.name for e in entries}
    # The surviving tail parts are indexed by the intact central directory.
    assert _TAIL_NAMES <= names
    assert "ppt/presProps.xml" in names


# --- source selection: version mix -----------------------------------------


def test_select_version_mix_uses_lfh_without_new_only_names(
    tmp_path: Path,
) -> None:
    """VERSION_MIX salvages the old head via LFH, never new-CD-only names."""
    old = build_minimal_pptx(num_slides=2, media_bytes=200_000, seed=10)
    new = build_minimal_pptx(num_slides=5, media_bytes=1_500_000, seed=20)
    path = _write(tmp_path, "vm.pptx", version_mix(old, new))

    diagnosis = _diagnose(path)
    assert diagnosis.verdict == Verdict.VERSION_MIX

    entries, _warnings = select_salvageable(diagnosis)

    assert entries
    assert {e.source for e in entries} == {"lfh"}
    salvaged_names = {e.name for e in entries}

    old_names = set(_original_contents(old))
    new_names = set(_original_contents(new))
    new_only = new_names - old_names
    assert new_only  # the fixtures really do differ
    # Names that exist only in the new central directory (e.g. slide5)
    # must never leak into an LFH-sourced salvage of the old head.
    assert salvaged_names.isdisjoint(new_only)
    assert "ppt/slides/slide5.xml" in new_only


# --- round-trip: LFH-sourced entries ---------------------------------------


def test_roundtrip_lfh_sourced_entries(tmp_path: Path) -> None:
    """LFH-sourced entries stream back byte-identical to the pristine data."""
    data = build_minimal_pptx()
    cd_offset, _cd_size, _eocd_offset = find_eocd(data)
    path = _write(tmp_path, "tr.pptx", truncate(data, cd_offset))

    diagnosis = _diagnose(path)
    entries, _warnings = select_salvageable(diagnosis)
    originals = _original_contents(data)

    assert {e.source for e in entries} == {"lfh"}
    with SalvageReader(path) as reader:
        for salvaged in entries:
            recovered = _read_all(reader, salvaged)
            assert recovered == originals[salvaged.name], salvaged.name
    # A large, incompressible media part must be part of the check.
    assert any(e.name == "ppt/media/image1.png" for e in entries)


# --- round-trip: CD-sourced entries ----------------------------------------


def test_roundtrip_cd_sourced_entries(tmp_path: Path) -> None:
    """CD-sourced entries stream back byte-identical to the pristine data."""
    data = build_minimal_pptx()
    path = _write(tmp_path, "zp.pptx", zero_prefix(data, 262144))

    diagnosis = _diagnose(path)
    entries, _warnings = select_salvageable(diagnosis)
    originals = _original_contents(data)

    assert {e.source for e in entries} == {"cd"}
    with SalvageReader(path) as reader:
        for salvaged in entries:
            recovered = _read_all(reader, salvaged)
            assert recovered == originals[salvaged.name], salvaged.name


# --- data-descriptor entries -----------------------------------------------


def test_roundtrip_data_descriptor_entries(tmp_path: Path) -> None:
    """Flag-bit-3 (streaming) entries inflate and CRC-check on read-back."""
    payloads = {
        "a.txt": b"alpha" * 1000,
        "b/c.txt": bytes(range(256)) * 20,
        "nested/e.dat": b"lorem ipsum " * 300,
    }
    data = build_zip_with_data_descriptors(payloads)
    path = _write(tmp_path, "dd.zip", data)

    census = from_lfh_scan(path)
    assert census.ok_count() == len(payloads)

    with SalvageReader(path) as reader:
        for entry in census.ok_entries():
            salvaged = SalvagedEntry(entry.name, entry.category, "lfh", entry)
            recovered = _read_all(reader, salvaged)
            assert recovered == payloads[entry.name], entry.name


# --- CRC mismatch on a tampered file ---------------------------------------


def test_lfh_crc_mismatch_raises_after_full_consumption(
    tmp_path: Path,
) -> None:
    """A byte flipped in the data area makes read-back raise SalvageError."""
    # A single stored entry keeps the corruption deterministic: a flipped
    # data byte yields a CRC mismatch rather than a deflate error.
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("a.txt", b"hello world " * 100)
    data = buffer.getvalue()
    good_path = _write(tmp_path, "good.zip", data)

    census = from_lfh_scan(good_path)
    entry = next(e for e in census.entries if e.name == "a.txt")
    assert entry.ok
    salvaged = SalvagedEntry(entry.name, entry.category, "lfh", entry)

    # Locate the entry's data area from its local header and flip a byte.
    (_sig, _ver, _flags, _method, _mt, _md, _crc, _comp, _uncomp,
     name_len, extra_len) = struct.unpack(
        "<IHHHHHIIIHH", data[entry.header_offset:entry.header_offset + 30])
    data_start = entry.header_offset + 30 + name_len + extra_len
    corrupted = bytearray(data)
    corrupted[data_start + 5] ^= 0xFF
    bad_path = _write(tmp_path, "bad.zip", bytes(corrupted))

    with SalvageReader(bad_path) as reader:
        with pytest.raises(SalvageError):
            _read_all(reader, salvaged)


# --- duplicate-name resolution and warnings --------------------------------


def test_duplicate_resolution_prefers_cd_then_latest_offset() -> None:
    """OTHER_CORRUPT resolves duplicates CD-first, then by largest offset.

    The copy written last supersedes earlier ones (incremental saves
    append updated parts; a central directory would point at the last).
    """
    cd_entries = [
        EntryResult("dup.xml", "other", 100, 10, True),
        EntryResult("cd_only.xml", "other", 200, 10, True),
        EntryResult("bad.xml", "other", 300, 10, False, "BadCRC"),
    ]
    lfh_entries = [
        EntryResult("dup.xml", "other", 5000, 10, True),
        EntryResult("lfh_only.xml", "other", 6000, 10, True),
        EntryResult("lfh_only.xml", "other", 7000, 10, True),
    ]
    diagnosis = Diagnosis(
        path=Path("x.pptx"),
        verdict=Verdict.OTHER_CORRUPT,
        cd_census=CensusResult("central_directory", cd_entries),
        lfh_census=CensusResult("lfh_scan", lfh_entries),
    )

    entries, warnings = select_salvageable(diagnosis)

    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"dup.xml", "cd_only.xml", "lfh_only.xml"}
    # The unreadable CD entry is never selected.
    assert "bad.xml" not in by_name
    # CD wins the name clash; the later-offset LFH duplicate wins its clash.
    assert by_name["dup.xml"].source == "cd"
    assert by_name["dup.xml"].entry.header_offset == 100
    assert by_name["lfh_only.xml"].source == "lfh"
    assert by_name["lfh_only.xml"].entry.header_offset == 7000

    assert (
        "duplicate entry name 'dup.xml': kept offset 100, dropped offset 5000"
        in warnings
    )
    assert (
        "duplicate entry name 'lfh_only.xml': kept offset 7000, "
        "dropped offset 6000" in warnings
    )


def test_select_normal_and_not_a_zip_are_empty() -> None:
    """NORMAL and NOT_A_ZIP verdicts salvage nothing."""
    for verdict in (Verdict.NORMAL, Verdict.NOT_A_ZIP):
        diagnosis = Diagnosis(path=Path("x.pptx"), verdict=verdict)
        entries, warnings = select_salvageable(diagnosis)
        assert entries == []
        assert warnings == []


# --- datetime recovery -----------------------------------------------------


def test_datetime_of_cd_and_lfh(tmp_path: Path) -> None:
    """Both source paths decode a plausible six-field timestamp."""
    data = build_minimal_pptx()
    path = _write(tmp_path, "healthy.pptx", data)

    cd = from_central_directory(path)
    lfh = from_lfh_scan(path)
    assert cd is not None

    cd_entry = next(e for e in cd.entries if e.name == "[Content_Types].xml")
    lfh_entry = next(e for e in lfh.entries if e.name == "[Content_Types].xml")
    salvaged_cd = SalvagedEntry(cd_entry.name, cd_entry.category, "cd",
                                cd_entry)
    salvaged_lfh = SalvagedEntry(lfh_entry.name, lfh_entry.category, "lfh",
                                 lfh_entry)

    with SalvageReader(path) as reader:
        dt_cd = reader.datetime_of(salvaged_cd)
        dt_lfh = reader.datetime_of(salvaged_lfh)

    for dt in (dt_cd, dt_lfh):
        assert dt is not None
        assert len(dt) == 6
        assert dt[0] >= 1980
    # zipfile records the same DOS timestamp in both structures.
    assert dt_cd == dt_lfh
