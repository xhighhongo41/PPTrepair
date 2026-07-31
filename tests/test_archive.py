"""Tests for :mod:`pptrepair.archive`.

Every fixture here is a real byte stream: archives are built with the
standard library's own :mod:`zipfile`/:mod:`tarfile` writers (or, for
the encrypted-member case, by patching the raw bytes of a real zip),
and the ``.pptx``/``.pptm`` payloads inside them come from
:func:`fixtures.build_minimal_pptx`, matching the approach used
throughout this test suite.
"""

from __future__ import annotations

import gzip
import io
import os
import struct
import tarfile
import zipfile
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, zero_entry_data_tail

import pptrepair.archive
from pptrepair.archive import (
    ArchiveMember,
    is_archive,
    is_hidden_member,
    iter_materialized_members,
    list_members,
    materialize,
)

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


class _CloseFails:
    """Wraps *inner*, delegating every attribute but failing close().

    Stands in for a handle whose close() raises an environmental OSError
    (observed: a stale SMB handle going bad only at close time) even
    though every read through it already succeeded -- see
    :func:`pptrepair.archive._close_noting`.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def close(self) -> None:
        self._inner.close()
        raise OSError(22, "Invalid argument")


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


# --- _open_with_retry: retry ladder for transient archive-open errors -------


def test_open_with_retry_transient_oserror_recovers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient OSError on each of the first two attempts is retried,
    sleeping the full delay ladder before the third attempt succeeds."""
    sentinel = object()
    attempts = 0

    def _open_call() -> object:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError(22, "Invalid argument")
        return sentinel

    sleeps: list[float] = []
    monkeypatch.setattr(pptrepair.archive.time, "sleep", sleeps.append)

    result = pptrepair.archive._open_with_retry(_open_call)

    assert result is sentinel
    assert sleeps == [2.0, 5.0]


def test_open_with_retry_exhausted_raises_last_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """An OSError on every attempt propagates from the final one, after
    sleeping through the delay ladder exactly once each."""
    def _always_fails() -> None:
        raise OSError(22, "Invalid argument")

    sleeps: list[float] = []
    monkeypatch.setattr(pptrepair.archive.time, "sleep", sleeps.append)

    with pytest.raises(OSError):
        pptrepair.archive._open_with_retry(_always_fails)

    assert sleeps == [2.0, 5.0]


@pytest.mark.parametrize("exc", [FileNotFoundError("missing"),
                                 gzip.BadGzipFile("bad")])
def test_open_with_retry_deterministic_errors_raise_immediately(
        exc: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic failure (missing file, bad gzip signature, ...) is
    re-raised on the first attempt, without ever sleeping: waiting
    cannot fix it."""
    sleeps: list[float] = []
    monkeypatch.setattr(pptrepair.archive.time, "sleep", sleeps.append)

    def _raise() -> None:
        raise exc

    with pytest.raises(type(exc)):
        pptrepair.archive._open_with_retry(_raise)

    assert sleeps == []


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


def test_materialize_close_failure_keeps_extracted_members(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A close() failure once every member has already been extracted is
    degraded to a note; the extracted files themselves are kept intact
    and byte-correct."""
    data_a = build_minimal_pptx(num_slides=1, media_bytes=2000, seed=0)
    data_b = build_minimal_pptx(num_slides=1, media_bytes=2000, seed=1)
    path = tmp_path / "archive.tar.gz"
    _write_tar(path, "w:gz", {"deck1.pptx": data_a, "deck2.pptx": data_b})
    members, notes = list_members(path)
    assert notes == []

    real_open = tarfile.open
    monkeypatch.setattr(
        pptrepair.archive.tarfile, "open",
        lambda *a, **k: _CloseFails(real_open(*a, **k)))

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    extracted, extract_notes = materialize(path, members, dest_dir)

    assert len(extracted) == len(members) == 2
    expected = {"deck1.pptx": data_a, "deck2.pptx": data_b}
    for member in members:
        assert extracted[member].read_bytes() == expected[member.member_name]
    assert len(extract_notes) == 1
    assert "closing archive failed" in extract_notes[0]


def test_materialize_open_transient_oserror_retries_and_succeeds(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient OSError on the very first archive-open attempt is
    retried by _open_with_retry: the second (real) attempt lets
    materialize() succeed as if nothing had happened."""
    pptx_data = build_minimal_pptx(num_slides=1, media_bytes=2000)
    path = tmp_path / "archive.tar.gz"
    _write_tar(path, "w:gz", {"deck.pptx": pptx_data})
    members, notes = list_members(path)
    assert notes == []

    real_open = tarfile.open
    attempts = 0

    def _flaky_open(*args: object, **kwargs: object) -> tarfile.TarFile:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(22, "Invalid argument")
        return real_open(*args, **kwargs)

    monkeypatch.setattr(pptrepair.archive.tarfile, "open", _flaky_open)
    sleeps: list[float] = []
    monkeypatch.setattr(pptrepair.archive.time, "sleep", sleeps.append)

    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    extracted, extract_notes = materialize(path, members, dest_dir)

    assert extract_notes == []
    assert len(extracted) == 1
    assert extracted[members[0]].read_bytes() == pptx_data
    assert sleeps == [2.0]


# --- iter_materialized_members: one pass over the archive --------------------


class _Cancelled(Exception):
    """Stand-in for the caller's own cancellation exception."""


def _collect(archive_path: Path, dest_dir: Path,
             progress=None) -> tuple[list[tuple[ArchiveMember, Path]],
                                     list[str]]:
    """Drain the one-pass iterator, returning its pairs and its notes."""
    notes: list[str] = []
    pairs = list(iter_materialized_members(
        archive_path, dest_dir, on_note=notes.append, progress=progress))
    return pairs, notes


@pytest.mark.parametrize("filename", ["archive.zip", *_TAR_FORMATS])
def test_iter_materialized_members_matches_list_plus_materialize(
        tmp_path: Path, filename: str) -> None:
    """The one-pass iterator is equivalent to list_members + materialize:
    same members, same order, byte-identical payloads, same (empty) notes,
    and the same flattened destination directory -- in every format."""
    entries = {
        "notes.txt": b"unrelated plain text",
        "folder/deck.pptx": build_minimal_pptx(num_slides=1,
                                                media_bytes=500, seed=0),
        "deck2.pptm": build_minimal_pptx(num_slides=1,
                                          media_bytes=500, seed=1),
        "~$deck.pptx": b"lock file placeholder",
    }
    path = tmp_path / filename
    if filename == "archive.zip":
        _write_zip(path, entries, dirs=("subdir",))
    else:
        _write_tar(path, _TAR_FORMATS[filename], entries, dirs=("subdir",))

    old_dir = tmp_path / "old"
    old_dir.mkdir()
    old_members, list_notes = list_members(path)
    extracted, materialize_notes = materialize(path, old_members, old_dir)

    new_dir = tmp_path / "new"
    new_dir.mkdir()
    pairs, notes = _collect(path, new_dir)

    assert notes == list_notes + materialize_notes == []
    assert [member for member, _ in pairs] == old_members
    for member, dest_path in pairs:
        assert dest_path.parent == new_dir
        assert dest_path.read_bytes() == extracted[member].read_bytes()


def test_iter_materialized_members_opens_the_tar_stream_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tar.gz holding several members is decompressed in a single pass:
    tarfile.open is called exactly once for the whole extraction."""
    entries = {f"deck{n}.pptx": build_minimal_pptx(num_slides=1,
                                                    media_bytes=2000, seed=n)
               for n in range(3)}
    path = tmp_path / "archive.tar.gz"
    _write_tar(path, "w:gz", entries)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    opens: list[object] = []
    real_open = tarfile.open

    def _counting_open(*args: object, **kwargs: object) -> tarfile.TarFile:
        opens.append(kwargs.get("mode", args[1] if len(args) > 1 else None))
        return real_open(*args, **kwargs)

    # Patched only after the fixture is written, so the writer's own open
    # is not counted.
    monkeypatch.setattr(tarfile, "open", _counting_open)
    pairs, notes = _collect(path, dest_dir)

    assert notes == []
    assert len(pairs) == 3
    assert opens == ["r|*"]


def test_iter_materialized_members_opens_the_zip_once(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A zip holding several members is opened exactly once, instead of
    once per member as list_members + per-member materialize did."""
    entries = {f"deck{n}.pptx": build_minimal_pptx(num_slides=1,
                                                    media_bytes=2000, seed=n)
               for n in range(3)}
    path = tmp_path / "archive.zip"
    _write_zip(path, entries)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    opens: list[object] = []
    real_zipfile = zipfile.ZipFile

    class _CountingZipFile(real_zipfile):  # type: ignore[misc, valid-type]
        """ZipFile that records every construction."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            opens.append(args[0] if args else kwargs.get("file"))
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(zipfile, "ZipFile", _CountingZipFile)
    pairs, notes = _collect(path, dest_dir)

    assert notes == []
    assert len(pairs) == 3
    assert opens == [path]


def test_iter_materialized_members_tar_progress_reaches_archive_size(
        tmp_path: Path) -> None:
    """Progress over a tar is the compressed read position: it is reported
    inside a long member copy (not only at member boundaries), never goes
    backwards, never exceeds the archive size, and ends exactly on it."""
    entries = {
        "big.pptx": build_minimal_pptx(num_slides=1, media_bytes=3_000_000),
        "small.pptx": build_minimal_pptx(num_slides=1, media_bytes=500),
    }
    path = tmp_path / "archive.tar.gz"
    _write_tar(path, "w:gz", entries)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    seen: list[tuple[int, int]] = []
    pairs, notes = _collect(path, dest_dir, progress=lambda done, total:
                            seen.append((done, total)))
    total_bytes = path.stat().st_size

    assert notes == []
    assert len(pairs) == 2
    # More calls than there are members: the copy loop reports per chunk.
    assert len(seen) > len(pairs) + 1
    assert {total for _, total in seen} == {total_bytes}
    done_values = [done for done, _ in seen]
    assert done_values == sorted(done_values)
    assert done_values[0] >= 0
    assert done_values[-1] == total_bytes


def test_iter_materialized_members_zip_progress_sums_compressed_sizes(
        tmp_path: Path) -> None:
    """Progress over a zip accumulates the compressed size of each member
    processed, reported once per member and never exceeding the file."""
    entries = {
        "notes.txt": b"unrelated plain text",
        "deck1.pptx": build_minimal_pptx(num_slides=1, media_bytes=40_000,
                                          seed=0),
        "deck2.pptx": build_minimal_pptx(num_slides=1, media_bytes=40_000,
                                          seed=1),
    }
    path = tmp_path / "archive.zip"
    _write_zip(path, entries)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    with zipfile.ZipFile(path) as zf:
        expected = sum(info.compress_size for info in zf.infolist()
                       if info.filename.endswith(".pptx"))

    seen: list[tuple[int, int]] = []
    pairs, notes = _collect(path, dest_dir, progress=lambda done, total:
                            seen.append((done, total)))
    total_bytes = path.stat().st_size

    assert notes == []
    assert len(pairs) == 2
    assert len(seen) == 2  # one report per target member
    done_values = [done for done, _ in seen]
    assert done_values == sorted(done_values)
    assert {total for _, total in seen} == {total_bytes}
    assert done_values[-1] == expected <= total_bytes


def test_iter_materialized_members_cancelled_copy_leaves_no_partial_file(
        tmp_path: Path) -> None:
    """A progress callback that raises mid-copy propagates untouched, and
    the half-written destination file is removed rather than left behind
    for the caller to diagnose as a corrupted member."""
    path = tmp_path / "archive.tar.gz"
    _write_tar(path, "w:gz", {
        "big.pptx": build_minimal_pptx(num_slides=1, media_bytes=4_000_000)})
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    calls = 0

    def _cancel_during_copy(done: int, total: int) -> None:
        nonlocal calls
        calls += 1
        # Call 1 is the member boundary; call 2 lands after the first
        # chunk of the member's payload has already been written.
        if calls >= 2:
            raise _Cancelled("user requested cancellation")

    with pytest.raises(_Cancelled):
        for _ in iter_materialized_members(path, dest_dir,
                                           progress=_cancel_during_copy):
            pass

    assert calls == 2
    assert list(dest_dir.iterdir()) == []


def test_iter_materialized_members_notes_encrypted_zip_member(
        tmp_path: Path) -> None:
    """An encrypted member is noted and skipped with the same wording as
    list_members'; its readable sibling still comes through."""
    plain_data = build_minimal_pptx(num_slides=1, media_bytes=500, seed=0)
    secret_data = build_minimal_pptx(num_slides=1, media_bytes=500, seed=1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as zf:
        zf.writestr("plain.pptx", plain_data)
        zf.writestr("secret.pptx", secret_data)
    path = tmp_path / "archive.zip"
    path.write_bytes(_patch_encrypted_flag(buffer.getvalue(), "secret.pptx"))
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    pairs, notes = _collect(path, dest_dir)

    assert [member.member_name for member, _ in pairs] == ["plain.pptx"]
    assert notes == [f"encrypted member skipped: {path}::secret.pptx"]
    assert pairs[0][1].read_bytes() == plain_data


def test_iter_materialized_members_stops_at_tar_stream_damage(
        tmp_path: Path) -> None:
    """Damage inside a tar stream ends that archive's walk with a note --
    the stream cannot be resynchronised -- but every member read before
    the damage is still delivered intact."""
    good_data = build_minimal_pptx(num_slides=1, media_bytes=200_000, seed=0)
    late_data = build_minimal_pptx(num_slides=1, media_bytes=200_000, seed=1)
    path = tmp_path / "backup.tar.gz"
    _write_tar(path, "w:gz", {"early.pptx": good_data, "late.pptx": late_data})
    raw = path.read_bytes()
    path.write_bytes(raw[:int(len(raw) * 0.7)])
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    pairs, notes = _collect(path, dest_dir)

    assert [member.member_name for member, _ in pairs] == ["early.pptx"]
    assert pairs[0][1].read_bytes() == good_data
    assert len(notes) == 1
    assert notes[0].startswith(f"cannot read archive {path}: ")
    # No half-written leftover from the member the damage cut short.
    assert [p.name for p in dest_dir.iterdir()] == [pairs[0][1].name]


def test_iter_materialized_members_continues_past_damaged_zip_member(
        tmp_path: Path) -> None:
    """A zip is randomly accessible, so a damaged member is noted and
    skipped while the walk carries on to the healthy members after it."""
    bad_data = build_minimal_pptx(num_slides=1, media_bytes=20_000, seed=0)
    good_data = build_minimal_pptx(num_slides=1, media_bytes=20_000, seed=1)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w",
                         compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bad.pptx", bad_data)
        zf.writestr("good.pptx", good_data)
    path = tmp_path / "archive.zip"
    path.write_bytes(zero_entry_data_tail(buffer.getvalue(), "bad.pptx",
                                          keep_fraction=0.3))
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    pairs, notes = _collect(path, dest_dir)

    assert [member.member_name for member, _ in pairs] == ["good.pptx"]
    assert pairs[0][1].read_bytes() == good_data
    assert len(notes) == 1
    assert notes[0].startswith(
        f"failed to extract member {path}::bad.pptx: ")
    assert [p.name for p in dest_dir.iterdir()] == [pairs[0][1].name]


@pytest.mark.skipif(not hasattr(os, "pathconf"),
                    reason="needs a POSIX name-length probe")
def test_iter_materialized_members_survives_an_unwritable_destination(
        tmp_path: Path) -> None:
    """A member the destination filesystem cannot hold -- here a basename
    longer than its NAME_MAX -- is noted and skipped instead of aborting a
    sweep that may already have been running for hours."""
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    too_long = "x" * (os.pathconf(str(dest_dir), "PC_NAME_MAX") + 50)
    good_data = build_minimal_pptx(num_slides=1, media_bytes=500)
    path = tmp_path / "archive.zip"
    _write_zip(path, {f"{too_long}.pptx": b"payload", "deck.pptx": good_data})

    pairs, notes = _collect(path, dest_dir)

    assert [member.member_name for member, _ in pairs] == ["deck.pptx"]
    assert pairs[0][1].read_bytes() == good_data
    assert len(notes) == 1
    assert notes[0].startswith("failed to extract member ")
    assert too_long in notes[0]


@pytest.mark.parametrize("filename", ["broken.zip", "broken.tar.gz"])
def test_iter_materialized_members_unopenable_archive_yields_nothing(
        tmp_path: Path, filename: str) -> None:
    """An archive that cannot be opened at all degrades to a single note
    and an empty iteration, never an exception."""
    path = tmp_path / filename
    path.write_bytes(b"\x00" * 64)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    pairs, notes = _collect(path, dest_dir)

    assert pairs == []
    assert len(notes) == 1
    assert notes[0].startswith(f"cannot read archive {path}: ")
    assert list(dest_dir.iterdir()) == []


def test_iter_tar_close_failure_yields_all_members_and_notes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A raw file handle whose close() fails once the tar stream sweep is
    done is degraded to a note; every member reached before that point is
    still yielded intact."""
    data_a = build_minimal_pptx(num_slides=1, media_bytes=2000, seed=0)
    data_b = build_minimal_pptx(num_slides=1, media_bytes=2000, seed=1)
    path = tmp_path / "archive.tar.gz"
    _write_tar(path, "w:gz", {"deck1.pptx": data_a, "deck2.pptx": data_b})
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    monkeypatch.setattr(
        pptrepair.archive, "_open_with_retry",
        lambda call: _CloseFails(call()))

    pairs, notes = _collect(path, dest_dir)

    assert len(pairs) == 2
    expected = {"deck1.pptx": data_a, "deck2.pptx": data_b}
    for member, dest_path in pairs:
        assert dest_path.read_bytes() == expected[member.member_name]
    assert any("closing archive failed" in note for note in notes)


def test_iter_zip_close_failure_yields_all_members_and_notes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ZipFile handle whose close() fails once the walk is done is
    degraded to a note; every member reached before that point is still
    yielded intact."""
    data_a = build_minimal_pptx(num_slides=1, media_bytes=2000, seed=0)
    data_b = build_minimal_pptx(num_slides=1, media_bytes=2000, seed=1)
    path = tmp_path / "archive.zip"
    _write_zip(path, {"deck1.pptx": data_a, "deck2.pptx": data_b})
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()

    monkeypatch.setattr(
        pptrepair.archive, "_open_with_retry",
        lambda call: _CloseFails(call()))

    pairs, notes = _collect(path, dest_dir)

    assert len(pairs) == 2
    expected = {"deck1.pptx": data_a, "deck2.pptx": data_b}
    for member, dest_path in pairs:
        assert dest_path.read_bytes() == expected[member.member_name]
    assert any("closing archive failed" in note for note in notes)


# --- is_hidden_member ---------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("._x.pptx", True),
    (".x.pptx", True),
    ("dir/._x.pptx", True),
    ("__MACOSX/._x.pptx", True),
    ("x.pptx", False),
    ("dir/x.pptx", False),
    (".hidden/x.pptx", False),
])
def test_is_hidden_member(name: str, expected: bool) -> None:
    """Only the member's own basename is checked (via posixpath), so a
    member sitting inside a dot-prefixed directory is not itself
    considered hidden -- only its own leading dot matters."""
    assert is_hidden_member(name) is expected


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
