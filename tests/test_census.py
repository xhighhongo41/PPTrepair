"""Tests for :mod:`pptrepair.census`.

All fixtures are synthetic archives built in memory and written under
``tmp_path``; no real .pptx sample files are touched. The two census
strategies are exercised against the corruption patterns that motivate
them: head zero-fill, tail truncation, version mixing and streaming
(data-descriptor) archives.
"""

from __future__ import annotations

import random
import zipfile
from io import BytesIO
from pathlib import Path

from fixtures import (
    build_minimal_pptx,
    build_zip_with_data_descriptors,
    truncate,
    version_mix,
    zero_prefix,
)

from pptrepair import census

#: Tail parts that survive head corruption in build_minimal_pptx(),
#: written after the large media payload.
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


def _media_header_offset(data: bytes) -> int:
    """Return the local-header offset of the media part in *data*."""
    with zipfile.ZipFile(BytesIO(data)) as zf:
        return zf.getinfo("ppt/media/image1.png").header_offset


# --- healthy archive -------------------------------------------------------


def test_healthy_central_directory_all_ok(tmp_path: Path) -> None:
    """A well-formed archive reads cleanly through the central directory."""
    path = _write(tmp_path, "healthy.pptx", build_minimal_pptx())

    result = census.from_central_directory(path)

    assert result is not None
    assert result.method == "central_directory"
    assert result.ok_count() == result.total()
    assert result.has_pptx_core()


def test_healthy_lfh_scan_matches_central_directory(tmp_path: Path) -> None:
    """The LFH scan recovers exactly the same intact entries as the CD."""
    data = build_minimal_pptx()
    path = _write(tmp_path, "healthy.pptx", data)

    cd = census.from_central_directory(path)
    lfh = census.from_lfh_scan(path)

    assert cd is not None
    assert lfh.method == "lfh_scan"
    assert lfh.ok_count() == lfh.total()
    assert {e.name for e in lfh.entries} == {e.name for e in cd.entries}


# --- head zero-fill --------------------------------------------------------


def test_zero_prefix_central_directory_splits_head_and_tail(
    tmp_path: Path,
) -> None:
    """Zeroing the head breaks the entries it covers but spares the tail."""
    data = build_minimal_pptx()
    path = _write(tmp_path, "zp.pptx", zero_prefix(data, 262144))

    result = census.from_central_directory(path)

    assert result is not None
    by_name = {e.name: e for e in result.entries}
    # Head entries whose headers were zero-filled cannot be read.
    assert not by_name["[Content_Types].xml"].ok
    for slide in ("ppt/slides/slide1.xml", "ppt/slides/slide2.xml",
                  "ppt/slides/slide3.xml"):
        assert not by_name[slide].ok
    # The small tail parts survive intact.
    for name in _TAIL_NAMES:
        assert by_name[name].ok


def test_zero_prefix_lfh_scan_recovers_only_tail(tmp_path: Path) -> None:
    """The LFH scan only finds surviving tail entries, all intact."""
    data = build_minimal_pptx()
    path = _write(tmp_path, "zp.pptx", zero_prefix(data, 262144))

    result = census.from_lfh_scan(path)

    assert result.total() > 0
    assert result.ok_count() == result.total()
    assert {e.name for e in result.entries} <= _TAIL_NAMES


# --- tail truncation -------------------------------------------------------


def test_truncated_central_directory_is_none(tmp_path: Path) -> None:
    """Truncation destroys the EOCD, so the CD census cannot run."""
    data = build_minimal_pptx()
    cut = _media_header_offset(data) + 40 + 500_000
    path = _write(tmp_path, "tr.pptx", truncate(data, cut))

    assert census.from_central_directory(path) is None


def test_truncated_lfh_scan_flags_cut_entry(tmp_path: Path) -> None:
    """The LFH scan recovers head entries and flags the truncated media."""
    data = build_minimal_pptx()
    cut = _media_header_offset(data) + 40 + 500_000
    path = _write(tmp_path, "tr.pptx", truncate(data, cut))

    result = census.from_lfh_scan(path)

    by_name = {e.name: e for e in result.entries}
    assert by_name["[Content_Types].xml"].ok
    assert by_name["ppt/presentation.xml"].ok
    media = by_name["ppt/media/image1.png"]
    assert not media.ok
    assert media.error == "Truncated"


# --- version mixing --------------------------------------------------------


def test_version_mix_central_directory_mostly_fails(tmp_path: Path) -> None:
    """The new CD points at offsets that now hold old/zeroed data."""
    old = build_minimal_pptx(num_slides=2, media_bytes=200_000, seed=10)
    new = build_minimal_pptx(num_slides=5, media_bytes=1_500_000, seed=20)
    path = _write(tmp_path, "vm.pptx", version_mix(old, new))

    result = census.from_central_directory(path)

    assert result is not None
    assert result.ok_count() <= result.total() // 2


def test_version_mix_lfh_scan_recovers_old_layout(tmp_path: Path) -> None:
    """The LFH scan recovers the old head's entries at old offsets."""
    old = build_minimal_pptx(num_slides=2, media_bytes=200_000, seed=10)
    new = build_minimal_pptx(num_slides=5, media_bytes=1_500_000, seed=20)
    path = _write(tmp_path, "vm.pptx", version_mix(old, new))

    cd = census.from_central_directory(path)
    lfh = census.from_lfh_scan(path)

    assert cd is not None
    assert lfh.ok_count() >= 5
    cd_offsets = {e.header_offset for e in cd.entries}
    novel = [e for e in lfh.ok_entries() if e.header_offset not in cd_offsets]
    # The old layout's offsets barely overlap the new CD's offset set.
    assert len(novel) >= 5


# --- data descriptors ------------------------------------------------------


def test_data_descriptor_entries_all_recovered(tmp_path: Path) -> None:
    """Flag bit 3 (streaming) entries are inflated and CRC-checked."""
    entries = {
        "a.txt": b"alpha" * 1000,
        "b/c.txt": bytes(range(256)) * 20,
        "d.bin": b"\x00" * 5000,
        "nested/e.dat": b"lorem ipsum " * 300,
    }
    path = _write(
        tmp_path, "dd.zip", build_zip_with_data_descriptors(entries)
    )

    result = census.from_lfh_scan(path)

    assert result.total() == len(entries)
    assert result.ok_count() == len(entries)
    sizes = {e.name: e.file_size for e in result.entries}
    for name, payload in entries.items():
        assert sizes[name] == len(payload)


# --- bogus signature -------------------------------------------------------


def test_bogus_signature_does_not_crash(tmp_path: Path) -> None:
    """A stray PK\\x03\\x04 in random data yields no intact entries."""
    payload = b"PK\x03\x04" + random.Random(123).randbytes(100)
    path = _write(tmp_path, "bogus.bin", payload)

    result = census.from_lfh_scan(path)

    assert result.method == "lfh_scan"
    assert all(not e.ok for e in result.entries)


# --- empty file ------------------------------------------------------------


def test_empty_file(tmp_path: Path) -> None:
    """An empty file gives no CD census and an empty LFH census."""
    path = _write(tmp_path, "empty.bin", b"")

    assert census.from_central_directory(path) is None
    lfh = census.from_lfh_scan(path)
    assert lfh.method == "lfh_scan"
    assert lfh.total() == 0


# --- large archive (chunk-boundary crossing) -------------------------------


def test_large_media_crosses_chunks(tmp_path: Path) -> None:
    """An archive larger than one scan chunk is recovered in full."""
    data = build_minimal_pptx(media_bytes=9_000_000)
    path = _write(tmp_path, "big.pptx", data)

    cd = census.from_central_directory(path)
    lfh = census.from_lfh_scan(path)

    assert cd is not None
    assert lfh.ok_count() == lfh.total()
    assert {e.name for e in lfh.entries} == {e.name for e in cd.entries}
    media = next(e for e in lfh.entries if e.name == "ppt/media/image1.png")
    assert media.ok
    assert media.file_size == 9_000_000
