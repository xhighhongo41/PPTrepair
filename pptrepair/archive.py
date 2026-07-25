"""Enumeration and safe extraction of PowerPoint files inside backup archives.

Users who back up a OneDrive tree often keep that backup as one or
more zip/tar archives rather than a plain directory. Those archives
can hold an intact twin or an older version of a file that is now
corrupted -- exactly the kind of donor material
:mod:`pptrepair.origin` and :mod:`pptrepair.merge` look for. This
module only *finds* and *materializes* ``.pptx``/``.pptm`` members
from such an archive; it never inspects their content and never
assumes the archive itself is trustworthy.

Two entry points cover the whole workflow:

* :func:`list_members` opens *archive_path* just far enough to read its
  member index (central directory / tar headers) and returns the
  candidate ``.pptx``/``.pptm`` members, in the order the archive
  records them;
* :func:`materialize` streams the payload of a chosen subset of those
  members out to plain files under a destination directory, so the
  rest of the pipeline can treat them exactly like any other file on
  disk.

Both functions are defensive against a hostile or damaged archive:
opening the archive itself never raises (a failure degrades to an
empty result plus a note), a single unreadable member never aborts
the rest, and extraction never trusts a member's own path (zip-slip
guard -- see :func:`materialize`).
"""

from __future__ import annotations

import posixpath
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from pptrepair.walker import TARGET_SUFFIXES, TEMP_PREFIX

#: Archive name suffixes handled by this module (matched
#: case-insensitively against the full file name, since several of
#: these -- e.g. ``.tar.gz`` -- span more than one dot-segment and
#: ``Path.suffix`` only ever returns the last one).
ARCHIVE_SUFFIXES = frozenset({
    ".zip",
    ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
})

#: Characters allowed, unescaped, in a materialized destination file
#: name; anything else is folded to ``_`` by :func:`_sanitize_basename`.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def is_archive(path: Path) -> bool:
    """Return True when *path*'s name ends with a recognised archive suffix.

    The comparison is case-insensitive and matches on the file name
    only (not the full path).
    """
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


@dataclass(frozen=True)
class ArchiveMember:
    """One candidate ``.pptx``/``.pptm`` member found inside an archive."""

    archive_path: Path
    """The archive file that contains this member."""
    member_name: str
    """The member's name exactly as recorded by the archive."""
    size: int | None
    """The member's recorded uncompressed size, if known."""

    def display(self) -> str:
        """Return a ``"<archive_path>::<member_name>"`` label for messages."""
        return f"{self.archive_path}::{self.member_name}"


def _is_zip_archive(archive_path: Path) -> bool:
    """Return True when *archive_path* should be opened with :mod:`zipfile`.

    Everything that is not a ``.zip`` by name is handed to
    :mod:`tarfile`'s auto-detecting ``"r:*"`` mode instead.
    """
    return archive_path.name.lower().endswith(".zip")


def _is_target_name(name: str) -> bool:
    """Return True when *name*'s suffix is one of ``TARGET_SUFFIXES``.

    Archive member names always use ``/`` as their separator regardless
    of the host platform, so :mod:`posixpath` is used rather than
    :mod:`pathlib` (which would use ``os.sep`` on Windows). This suffix
    check also transparently excludes any member that is itself an
    archive (e.g. a nested ``.zip``): its suffix is simply not in
    ``TARGET_SUFFIXES``, so no separate nesting check is needed.
    """
    return posixpath.splitext(name)[1].lower() in TARGET_SUFFIXES


def _list_zip_members(
    archive_path: Path,
) -> tuple[list[ArchiveMember], list[str]]:
    """Enumerate the target members of a ``.zip`` archive.

    See :func:`list_members` for the skip rules applied here.
    """
    members: list[ArchiveMember] = []
    notes: list[str] = []
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            if not _is_target_name(info.filename):
                continue
            if posixpath.basename(info.filename).startswith(TEMP_PREFIX):
                continue
            member = ArchiveMember(archive_path=archive_path,
                                    member_name=info.filename,
                                    size=info.file_size)
            if info.flag_bits & 0x1:
                # General purpose bit 0: the entry is encrypted. Its
                # data cannot be read without a password, so it is
                # reported rather than silently dropped.
                notes.append(f"encrypted member skipped: {member.display()}")
                continue
            members.append(member)
    return members, notes


def _list_tar_members(
    archive_path: Path,
) -> tuple[list[ArchiveMember], list[str]]:
    """Enumerate the target members of a tar-family archive.

    See :func:`list_members` for the skip rules applied here.
    """
    members: list[ArchiveMember] = []
    with tarfile.open(archive_path, mode="r:*") as tf:
        for info in tf:
            if not info.isfile():
                continue  # directories, symlinks, devices, etc.
            if not _is_target_name(info.name):
                continue
            if posixpath.basename(info.name).startswith(TEMP_PREFIX):
                continue
            members.append(ArchiveMember(archive_path=archive_path,
                                          member_name=info.name,
                                          size=info.size))
    return members, []


def list_members(archive_path: Path) -> tuple[list[ArchiveMember], list[str]]:
    """List the ``.pptx``/``.pptm`` members recorded in *archive_path*.

    Uses :class:`zipfile.ZipFile` for ``.zip`` archives and
    :func:`tarfile.open` (auto-detecting compression, ``"r:*"``) for
    every other recognised suffix. Members are returned in the order
    the archive itself records them.

    Skipped silently, without a note: directories; non-regular tar
    members (symlinks, devices, ...); members whose suffix is not
    ``.pptx``/``.pptm`` (this also covers nested archives, which are
    simply another unrelated suffix); and Office ``~$`` owner/lock
    temp files.

    Skipped with a note: encrypted zip members (their data cannot be
    read without a password).

    Never raises: if the archive cannot be opened or read at all
    (corrupt header, truncated file, wrong format, ...), ``([], [note])``
    is returned instead.
    """
    try:
        if _is_zip_archive(archive_path):
            return _list_zip_members(archive_path)
        return _list_tar_members(archive_path)
    except Exception as exc:
        # Deliberately broad: damage in the *middle* of a compressed tar
        # stream surfaces during enumeration as raw decompression errors
        # (zlib.error, EOFError, lzma.LZMAError, ...) rather than
        # tarfile.TarError, and this boundary must never raise.
        return [], [f"cannot read archive {archive_path}: {exc}"]


def _sanitize_basename(member_name: str) -> str:
    """Return a filesystem-safe basename derived from *member_name*.

    Any directory structure recorded in *member_name* is discarded
    entirely (the zip-slip guard: the caller never combines this
    result with the member's own path, only with an index prefix), and
    every character outside ``[A-Za-z0-9._-]`` is replaced with ``_``.
    """
    base = posixpath.basename(member_name)
    return _SAFE_NAME_RE.sub("_", base)


def _dest_filename(index: int, member_name: str) -> str:
    """Return the ``memberNNNN-<sanitized-basename>`` destination name.

    The zero-padded index prefix is what actually guarantees
    collision-free names across members (the sanitized basename alone
    could repeat, e.g. two ``image1.png`` members from different
    directories).
    """
    return f"member{index:04d}-{_sanitize_basename(member_name)}"


def _copy_member_data(opener, is_zip: bool, member_name: str,
                       dst: IO[bytes]) -> None:
    """Stream *member_name*'s payload from *opener* into *dst*, chunk-wise.

    :raises OSError: when a tar member has no extractable data stream
        (e.g. it changed type between listing and extraction).
    :raises Exception: any decoding/verification failure surfaced by
        :mod:`zipfile` or :mod:`tarfile` while reading a corrupt or
        truncated member is deliberately left to propagate to the
        caller, which handles it generically (see :func:`_extract_member`).
    """
    if is_zip:
        with opener.open(member_name) as src:
            shutil.copyfileobj(src, dst)
        return
    src = opener.extractfile(member_name)
    if src is None:
        raise OSError(f"member has no extractable data stream: {member_name}")
    with src:
        shutil.copyfileobj(src, dst)


def _extract_member(opener, is_zip: bool, member: ArchiveMember,
                     dest_path: Path, extracted: dict[ArchiveMember, Path],
                     notes: list[str]) -> None:
    """Extract one *member* to *dest_path*, recording the outcome.

    *dest_path* is created exclusively (``"xb"``) so a destination-name
    collision -- never expected given the ``memberNNNN-`` index prefix,
    but guarded against regardless -- is reported instead of silently
    overwriting existing data. A failure while reading the member's
    payload (corrupt/truncated data can surface as several different
    exception types depending on where the damage sits, e.g.
    ``zipfile.BadZipFile``, ``tarfile.TarError``, ``zlib.error``,
    ``EOFError``) removes the partially written file and is recorded
    as a note; *extracted*/*notes* are updated in place and the
    remaining members are always still processed by the caller.
    """
    try:
        dst = dest_path.open("xb")
    except FileExistsError:
        notes.append(
            f"destination name collision, member skipped: {member.display()}")
        return

    try:
        with dst:
            _copy_member_data(opener, is_zip, member.member_name, dst)
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        notes.append(f"failed to extract member {member.display()}: {exc}")
        return

    extracted[member] = dest_path


def materialize(
    archive_path: Path, members: list[ArchiveMember], dest_dir: Path,
) -> tuple[dict[ArchiveMember, Path], list[str]]:
    """Extract *members* from *archive_path* to plain files under *dest_dir*.

    Every member is streamed chunk-wise (never read fully into memory)
    to ``dest_dir / "memberNNNN-<sanitized-basename>"`` -- the index
    prefix is what makes the destination name collision-free, since a
    member's own path is never used to build it (zip-slip guard: a
    hostile ``member_name`` such as ``"../../etc/passwd"`` can only
    ever influence the sanitized *basename* half of the destination
    name, never its directory). *dest_dir* must already exist; creating
    and cleaning it up is the caller's responsibility.

    A member whose data cannot be read (CRC mismatch, truncation, ...)
    is left out of the returned mapping and reported as a note;
    processing continues with the remaining members. If *archive_path*
    itself cannot be opened, ``({}, [note])`` is returned and no member
    is touched.

    :return: a mapping from each successfully extracted member to its
        destination path, plus the list of notes collected along the
        way (in encounter order).
    """
    extracted: dict[ArchiveMember, Path] = {}
    notes: list[str] = []
    is_zip = _is_zip_archive(archive_path)

    try:
        # The handle is context-managed by the ``with opener:`` below;
        # assignment and open are split only so open-time damage can be
        # reported as a note instead of aborting the whole batch.
        opener = (zipfile.ZipFile(archive_path) if is_zip
                  else tarfile.open(archive_path, mode="r:*"))  # noqa: SIM115
    except Exception as exc:
        # Broad for the same reason as in list_members: opening a
        # compressed tar already reads its first blocks, so mid-stream
        # damage can surface here as a raw decompression error.
        return {}, [f"cannot open archive {archive_path}: {exc}"]

    with opener:
        for index, member in enumerate(members):
            dest_path = dest_dir / _dest_filename(index, member.member_name)
            _extract_member(opener, is_zip, member, dest_path, extracted,
                             notes)

    return extracted, notes
