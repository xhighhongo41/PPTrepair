"""Tests for :mod:`pptrepair.archive`.

Every fixture here is a real byte stream: archives are built with the
standard library's own :mod:`zipfile`/:mod:`tarfile` writers (or, for
the encrypted-member case, by patching the raw bytes of a real zip),
and the ``.pptx``/``.pptm`` payloads inside them come from
:func:`fixtures.build_minimal_pptx`, matching the approach used
throughout this test suite.
"""

from __future__ import annotations

import io
import struct
import tarfile
import zipfile
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, zero_entry_data_tail

from pptrepair.archive import ArchiveMember, is_archive, list_members, materialize

#: (file name, tarfile open mode) for every non-zip format under test.
_TAR_FORMATS = {
    "archive.tar": "w",
    "archive.tar.gz": "w:gz",
    "archive.tar.bz2": "w:bz2",
    "archive.tar.xz": "w:xz",
}


def _write_zip(path: Path, entries: dict[str, bytes],
               dirs: tuple[str, ...] = ()) -> None:
    """Write *entries* (plus any *dirs* directory entries) to a new zip."""
    with zipfile.ZipFile(path, mode="w") as zf:
        for name in dirs:
            zf.writestr(name if name.endswith("/") else name + "/", b"")
        for name, data in entries.items():
            zf.writestr(name, data)


def _write_tar(path: Path, mode: str, entries: dict[str, bytes],
               dirs: tuple[str, ...] = ()) -> None:
    """Write *entries* (plus any *dirs* directory entries) to a new tar."""
    with tarfile.open(path, mode=mode) as tf:
        for name in dirs:
            info = tarfile.TarInfo(name.rstrip("/"))
            info.type = tarfile.DIRTYPE
            tf.addfile(info)
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _patch_encrypted_flag(data: bytes, member_name: str) -> bytes:
    """Flip the encrypted-entry flag bit for *member_name* in a raw zip.

    The standard library cannot itself write an encrypted zip, so this
    directly rewrites the general purpose bit flag field (bit 0) of
    both the local file header and the central directory record for
    *member_name*, leaving every other byte (including the CRC-32 and
    compressed payload) untouched.
    """
    buf = bytearray(data)
    name_bytes = member_name.encode("utf-8")

    # Local file header: "PK\x03\x04", flags at +6, name_len at +26,
    # file name starting at +30.
    idx = buf.find(b"PK\x03\x04")
    while idx != -1:
        name_len = struct.unpack_from("<H", buf, idx + 26)[0]
        if bytes(buf[idx + 30:idx + 30 + name_len]) == name_bytes:
            flags = struct.unpack_from("<H", buf, idx + 6)[0]
            struct.pack_into("<H", buf, idx + 6, flags | 0x1)
            break
        idx = buf.find(b"PK\x03\x04", idx + 4)

    # Central directory header: "PK\x01\x02", flags at +8, name_len at
    # +28, file name starting at +46.
    idx = buf.find(b"PK\x01\x02")
    while idx != -1:
        name_len = struct.unpack_from("<H", buf, idx + 28)[0]
        if bytes(buf[idx + 46:idx + 46 + name_len]) == name_bytes:
            flags = struct.unpack_from("<H", buf, idx + 8)[0]
            struct.pack_into("<H", buf, idx + 8, flags | 0x1)
            break
        idx = buf.find(b"PK\x01\x02", idx + 4)

    return bytes(buf)


# --- list_members: only .pptx/.pptm members are enumerated -----------------


@pytest.mark.parametrize("filename", ["archive.zip", *_TAR_FORMATS])
def test_list_members_only_pptx_pptm_across_formats(
        tmp_path: Path, filename: str) -> None:
    """Directories, unrelated extensions and a nested .zip member are
    never enumerated, in any of the five supported archive formats;
    only the real .pptx/.pptm members are."""
    pptx_data = build_minimal_pptx(num_slides=1, media_bytes=500, seed=0)
    pptm_data = build_minimal_pptx(num_slides=1, media_bytes=500, seed=1)
    entries = {
        "notes.txt": b"unrelated plain text",
        "nested/inner.zip": b"PK\x03\x04not a real nested archive body",
        "deck.pptx": pptx_data,
        "deck2.pptm": pptm_data,
    }
    path = tmp_path / filename
    if filename == "archive.zip":
        _write_zip(path, entries, dirs=("subdir",))
    else:
        _write_tar(path, _TAR_FORMATS[filename], entries, dirs=("subdir",))

    members, notes = list_members(path)

    assert notes == []
    assert {m.member_name for m in members} == {"deck.pptx", "deck2.pptm"}
    for member in members:
        assert member.archive_path == path


def test_list_members_skips_temp_file_without_note(tmp_path: Path) -> None:
    """An Office ``~$`` owner/lock temp member is skipped silently."""
    entries = {
        "~$deck.pptx": b"lock file placeholder",
        "deck.pptx": build_minimal_pptx(num_slides=1, media_bytes=500),
    }
    path = tmp_path / "archive.zip"
    _write_zip(path, entries)

    members, notes = list_members(path)

    assert notes == []
    assert {m.member_name for m in members} == {"deck.pptx"}


def test_list_members_skips_encrypted_member_with_note(
        tmp_path: Path) -> None:
    """An encrypted zip member is skipped with a note; other members
    in the same archive are unaffected."""
    plain_data = build_minimal_pptx(num_slides=1, media_bytes=500, seed=0)
    secret_data = build_minimal_pptx(num_slides=1, media_bytes=500, seed=1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as zf:
        zf.writestr("plain.pptx", plain_data)
        zf.writestr("secret.pptx", secret_data)
    patched = _patch_encrypted_flag(buffer.getvalue(), "secret.pptx")
    path = tmp_path / "archive.zip"
    path.write_bytes(patched)

    members, notes = list_members(path)

    assert {m.member_name for m in members} == {"plain.pptx"}
    assert len(notes) == 1
    assert "secret.pptx" in notes[0]


def test_list_members_broken_archive_returns_empty_with_note(
        tmp_path: Path) -> None:
    """An archive that cannot be opened at all never raises; it yields
    an empty member list plus a single note."""
    path = tmp_path / "broken.zip"
    path.write_bytes(b"\x00" * 64)

    members, notes = list_members(path)

    assert members == []
    assert len(notes) == 1


def test_list_members_truncated_tar_stream_never_raises(
        tmp_path: Path) -> None:
    """A compressed tar cut off in the middle of its stream surfaces
    during enumeration as a raw decompression error (EOFError /
    zlib.error, not tarfile.TarError): the open itself succeeds because
    the gzip header and first tar block are intact, and the failure
    only hits when the iterator seeks past the first member's data.
    list_members must degrade to ([], [note]) instead of raising."""
    pptx_data = build_minimal_pptx(num_slides=1, media_bytes=40_000)
    path = tmp_path / "backup.tar.gz"
    _write_tar(path, "w:gz", {"deck.pptx": pptx_data})
    raw = path.read_bytes()
    path.write_bytes(raw[:len(raw) // 2])

    members, notes = list_members(path)

    assert members == []
    assert len(notes) == 1


# --- materialize -------------------------------------------------------------


def test_materialize_matches_content_and_flattens_directories(
        tmp_path: Path) -> None:
    """Extracted content is byte-identical to the source, and the
    destination file sits directly under dest_dir even though the
    member name carries directory structure."""
    pptx_data = build_minimal_pptx(num_slides=2, media_bytes=1000)
    path = tmp_path / "archive.zip"
    _write_zip(path, {"folder/sub/deck.pptx": pptx_data})

    members, notes = list_members(path)
    assert notes == []
    assert len(members) == 1

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    extracted, extract_notes = materialize(path, members, dest_dir)

    assert extract_notes == []
    dest_path = extracted[members[0]]
    assert dest_path.parent == dest_dir
    assert dest_path.read_bytes() == pptx_data


def test_materialize_same_basename_different_dirs_are_kept_separate(
        tmp_path: Path) -> None:
    """Two members that share a basename but live in different
    directories inside the archive extract to two distinct files."""
    data_a = build_minimal_pptx(num_slides=1, media_bytes=300, seed=0)
    data_b = build_minimal_pptx(num_slides=1, media_bytes=300, seed=1)
    path = tmp_path / "archive.zip"
    _write_zip(path, {"2023/deck.pptx": data_a, "2024/deck.pptx": data_b})

    members, notes = list_members(path)
    assert notes == []
    assert len(members) == 2

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    extracted, extract_notes = materialize(path, members, dest_dir)

    assert extract_notes == []
    dest_paths = [extracted[m] for m in members]
    assert len(set(dest_paths)) == 2  # never overwrote one another
    assert {p.read_bytes() for p in dest_paths} == {data_a, data_b}


def test_materialize_excludes_corrupted_member_but_keeps_others(
        tmp_path: Path) -> None:
    """A member whose data was damaged is left out of the result (with
    a note); a healthy sibling member still extracts successfully."""
    good_data = build_minimal_pptx(num_slides=1, media_bytes=20_000, seed=0)
    bad_data = build_minimal_pptx(num_slides=1, media_bytes=20_000, seed=1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w",
                         compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("good.pptx", good_data)
        zf.writestr("bad.pptx", bad_data)
    archive_bytes = zero_entry_data_tail(
        buffer.getvalue(), "bad.pptx", keep_fraction=0.3)
    path = tmp_path / "archive.zip"
    path.write_bytes(archive_bytes)

    members, notes = list_members(path)
    assert notes == []
    assert {m.member_name for m in members} == {"good.pptx", "bad.pptx"}
    good_member = next(m for m in members if m.member_name == "good.pptx")
    bad_member = next(m for m in members if m.member_name == "bad.pptx")

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    extracted, extract_notes = materialize(path, members, dest_dir)

    assert len(extract_notes) == 1
    assert good_member in extracted
    assert bad_member not in extracted
    assert extracted[good_member].read_bytes() == good_data


# --- is_archive / ArchiveMember.display -------------------------------------


def test_is_archive_matches_case_insensitively(tmp_path: Path) -> None:
    """Recognised suffixes match regardless of case."""
    assert is_archive(Path("backup.ZIP")) is True
    assert is_archive(Path("backup.Tar.GZ")) is True
    assert is_archive(Path("backup.TBZ2")) is True


def test_is_archive_rejects_unrelated_suffixes() -> None:
    """Suffixes outside ARCHIVE_SUFFIXES are never considered archives."""
    assert is_archive(Path("deck.pptx")) is False
    assert is_archive(Path("backup.rar")) is False


def test_archive_member_display_format() -> None:
    """display() joins the archive path and member name with '::'."""
    member = ArchiveMember(
        archive_path=Path("/backups/onedrive.zip"),
        member_name="folder/deck.pptx", size=1234)

    assert member.display() == "/backups/onedrive.zip::folder/deck.pptx"
