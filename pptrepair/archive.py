"""Enumeration and safe extraction of PowerPoint files inside backup archives.

Users who back up a OneDrive tree often keep that backup as one or
more zip/tar archives rather than a plain directory. Those archives
can hold an intact twin or an older version of a file that is now
corrupted -- exactly the kind of donor material
:mod:`pptrepair.origin` and :mod:`pptrepair.merge` look for. This
module only *finds* and *materializes* ``.pptx``/``.pptm`` members
from such an archive; it never inspects their content and never
assumes the archive itself is trustworthy.

Three entry points cover the whole workflow:

* :func:`list_members` opens *archive_path* just far enough to read its
  member index (central directory / tar headers) and returns the
  candidate ``.pptx``/``.pptm`` members, in the order the archive
  records them;
* :func:`materialize` streams the payload of a chosen subset of those
  members out to plain files under a destination directory, so the
  rest of the pipeline can treat them exactly like any other file on
  disk;
* :func:`iter_materialized_members` fuses the two into a single
  forward-only pass -- each member is extracted and handed over the
  moment it is met -- which is the only workable shape for a
  multi-hundred-gigabyte compressed backup, where listing and
  extracting separately means decompressing the whole archive once per
  member.

An Evernote export (``.enex``) is accepted as another donor container
alongside zip/tar backups: it is not a compressed archive in that
sense, but :mod:`pptrepair.enex` mines PowerPoint attachments out of it
the same way this module mines files out of a zip/tar, and reports
them as the same :class:`ArchiveMember` values. All three entry points
below simply dispatch to that module for an ``.enex`` path (see
:func:`iter_materialized_members`), so every downstream consumer of
this module -- the GUI's drag-and-drop,
:func:`pptrepair.walker.discover_targets`'s ``collect_archives``, the
CLI's ``--search-archives``, ... -- picks up ``.enex`` support without
any change of its own.

All three are defensive against a hostile or damaged archive: opening
the archive itself never raises (a failure degrades to an empty result
plus a note), a single unreadable member never aborts the rest (on a
seekable archive; a forward-only tar stream can only give up on the
remainder -- see :func:`iter_materialized_members`), and extraction
never trusts a member's own path (zip-slip guard -- see
:func:`materialize`).
"""

from __future__ import annotations

import gzip
import posixpath
import re
import shutil
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from pptrepair.walker import HIDDEN_PREFIX, TARGET_SUFFIXES, TEMP_PREFIX

#: Archive name suffixes handled by this module (matched
#: case-insensitively against the full file name, since several of
#: these -- e.g. ``.tar.gz`` -- span more than one dot-segment and
#: ``Path.suffix`` only ever returns the last one). ``.enex``
#: (Evernote export) is included here too: it is not a compressed
#: container in the zip/tar sense, but it is accepted as another donor
#: container everywhere this set gates acceptance (GUI drag-and-drop,
#: :func:`pptrepair.walker.discover_targets`'s ``collect_archives``,
#: the CLI's ``--search-archives``); see :func:`iter_materialized_members`
#: for how it is actually read.
ARCHIVE_SUFFIXES = frozenset({
    ".zip",
    ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz",
    ".enex",
})

#: Characters allowed, unescaped, in a materialized destination file
#: name; anything else is folded to ``_`` by :func:`_sanitize_basename`.
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")

#: Chunk size used by the one-pass extractor when streaming a member's
#: payload to disk. One MiB keeps memory flat whatever the member's
#: size, while still firing the progress callback often enough for a
#: multi-gigabyte member to stay responsive (and cancellable).
_COPY_CHUNK_BYTES = 1024 * 1024


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


def _is_enex(path: Path) -> bool:
    """Return True when *path*'s name ends with ``.enex`` (case-insensitive).

    Used to route *archive_path* to :mod:`pptrepair.enex` instead of the
    zip/tar handling the rest of this module implements.
    """
    return path.name.lower().endswith(".enex")


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


def is_hidden_member(name: str) -> bool:
    """Return True when *name*'s basename starts with ``.``.

    Archive member names use ``/`` separators on every platform, so the
    basename is taken with :mod:`posixpath`. Catches macOS AppleDouble
    entries (``._foo.pptx``, including those filed under ``__MACOSX/``
    in Finder-made zips) and other hidden files, which are metadata or
    importer debris rather than presentations.

    Enumeration and mining in this module deliberately do NOT apply
    this predicate: the session cache must stay independent of the
    ignore-hidden option, so the filter runs at the consumption point
    instead (see :func:`pptrepair.scan.diagnose_archive_materials`).
    """
    return posixpath.basename(name).startswith(HIDDEN_PREFIX)


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


def _list_enex_members(
    archive_path: Path,
) -> tuple[list[ArchiveMember], list[str]]:
    """Enumerate the PowerPoint attachments of an ``.enex`` export.

    Unlike the zip/tar paths above, an export has no separate index to
    stop at: :mod:`pptrepair.enex` only ever learns whether an
    attachment is a presentation after decoding it (see
    :mod:`pptrepair.enex`'s module docstring), so listing it means a
    full parse of the whole export into a throwaway temporary
    directory, discarded once every attachment has been reported. This
    is expensive; callers that also need the payload should prefer the
    one-pass :func:`iter_materialized_members` over calling this and
    :func:`materialize` separately.
    """
    # Local import: pptrepair.enex imports ArchiveMember from this
    # module, so a top-level import here would be a cycle.
    from pptrepair.enex import iter_materialized_attachments

    members: list[ArchiveMember] = []
    notes: list[str] = []
    with tempfile.TemporaryDirectory() as tmp_dir:
        for member, _path in iter_materialized_attachments(
                archive_path, Path(tmp_dir), on_note=notes.append):
            members.append(member)
    return members, notes


def list_members(archive_path: Path) -> tuple[list[ArchiveMember], list[str]]:
    """List the ``.pptx``/``.pptm`` members recorded in *archive_path*.

    Uses :class:`zipfile.ZipFile` for ``.zip`` archives and
    :func:`tarfile.open` (auto-detecting compression, ``"r:*"``) for
    every other recognised suffix. Members are returned in the order
    the archive itself records them. For an ``.enex`` export this
    dispatches to :func:`_list_enex_members` instead -- see there for
    why that path is comparatively costly.

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
        if _is_enex(archive_path):
            return _list_enex_members(archive_path)
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


def _materialize_enex(
    archive_path: Path, members: list[ArchiveMember], dest_dir: Path,
) -> tuple[dict[ArchiveMember, Path], list[str]]:
    """Extract *members* from an ``.enex`` export via a full re-parse.

    An export has no random-access index to seek a chosen member out
    of (unlike a zip's central directory or a tar's headers), so the
    only way to honour an arbitrary *members* subset here is to sweep
    the whole export with :func:`pptrepair.enex.iter_materialized_attachments`
    and keep only what was asked for, discarding every other extracted
    attachment again. This is expensive and only meant as a fallback
    for a cache miss against an already-listed enex; a fresh caller
    should use the one-pass :func:`iter_materialized_members` instead.

    A requested member never met while sweeping the export (e.g. the
    export changed between the earlier :func:`list_members` call and
    this one) is reported as a note instead of silently missing from
    the result.
    """
    # Local import: pptrepair.enex imports ArchiveMember from this
    # module, so a top-level import here would be a cycle.
    from pptrepair.enex import iter_materialized_attachments

    wanted = set(members)
    extracted: dict[ArchiveMember, Path] = {}
    notes: list[str] = []
    for member, path in iter_materialized_attachments(
            archive_path, dest_dir, on_note=notes.append):
        if member in wanted:
            extracted[member] = path
        else:
            path.unlink(missing_ok=True)
    for member in members:
        if member not in extracted:
            notes.append(f"failed to extract member {member.display()}: "
                          "not found in export")
    return extracted, notes


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
    is touched -- after a brief retry ladder for transient environmental
    errors (see :func:`_open_with_retry`). A close that fails once the
    sweep is done is likewise only noted, never raised: the extracted
    members are already safely on disk (see :func:`_close_noting`).

    For an ``.enex`` export this dispatches to :func:`_materialize_enex`
    instead, which incurs a full re-parse of the export -- see there for
    why that path is only meant as a fallback.

    :return: a mapping from each successfully extracted member to its
        destination path, plus the list of notes collected along the
        way (in encounter order).
    """
    if _is_enex(archive_path):
        return _materialize_enex(archive_path, members, dest_dir)

    extracted: dict[ArchiveMember, Path] = {}
    notes: list[str] = []
    is_zip = _is_zip_archive(archive_path)

    try:
        # Assignment and open are split only so open-time damage can be
        # reported as a note instead of aborting the whole batch.
        opener = _open_with_retry(
            lambda: (zipfile.ZipFile(archive_path) if is_zip
                     else tarfile.open(archive_path, mode="r:*")))  # noqa: SIM115
    except Exception as exc:
        # Broad for the same reason as in list_members: opening a
        # compressed tar already reads its first blocks, so mid-stream
        # damage can surface here as a raw decompression error.
        return {}, [f"cannot open archive {archive_path}: {exc}"]

    # Not ``with opener:``, because a close that fails must not discard
    # the extraction it concludes -- see _close_noting.
    try:
        for index, member in enumerate(members):
            dest_path = dest_dir / _dest_filename(index, member.member_name)
            _extract_member(opener, is_zip, member, dest_path, extracted,
                             notes)
    finally:
        _close_noting(opener, archive_path, notes.append)

    return extracted, notes


class _MemberReadError(Exception):
    """Internal: a member's payload could not be read from the archive.

    Raised by :func:`_copy_stream` wrapping the original exception, so
    its ``str()`` stays message-compatible with the notes
    :func:`materialize` produces. Kept distinct from
    :class:`_MemberWriteError` because the two mean very different
    things on a forward-only tar stream: a *source* failure there means
    the decoder is desynchronised and nothing beyond this point can be
    read, while a *destination* failure only cost this one member.
    """


class _MemberWriteError(Exception):
    """Internal: a member's payload could not be written to *dest_dir*."""


_OPEN_RETRY_DELAYS = (2.0, 5.0)
"""Seconds slept between archive-open attempts (so three attempts total).

Kept short on purpose: while a retry sleeps, a cooperative cancellation
request cannot be observed, so the whole ladder must stay well under
what a user would read as "the app hung".
"""


def _open_with_retry[T](open_call: Callable[[], T]) -> T:
    """Call *open_call*, retrying transient ``OSError``s a few times.

    Opening an archive on a flaky network mount (observed: smbfs during
    a reconnection window) can fail with an environmental ``OSError``
    such as ``EINVAL`` or ``EIO`` that a moment later would not recur.
    Unlike a mid-read failure -- whose retry would mean re-streaming a
    compressed archive from its first byte -- retrying an *open* loses
    no work at all, so a few seconds of patience here can save re-running
    an hours-long sweep.

    Deterministic failures (missing file, permissions, not-a-file,
    corrupt gzip signature) are re-raised immediately: waiting cannot
    fix them. Anything else ``OSError``-shaped sleeps through
    :data:`_OPEN_RETRY_DELAYS` between attempts; the final attempt's
    exception propagates untouched, so every existing caller keeps its
    own note-degrading handler unchanged.
    """
    for delay in _OPEN_RETRY_DELAYS:
        try:
            return open_call()
        except (FileNotFoundError, PermissionError, IsADirectoryError,
                NotADirectoryError, gzip.BadGzipFile):
            raise
        except OSError:
            time.sleep(delay)
    return open_call()


def _close_noting(handle: zipfile.ZipFile | tarfile.TarFile | IO[bytes],
                  archive_path: Path,
                  on_note: Callable[[str], None] | None) -> None:
    """Close *handle*, degrading a failed close to a note.

    On an SMB mount a handle held open for hours can go stale, making
    the final ``close(2)`` fail (observed as ``EINVAL``) even though
    every read through it succeeded. By that point all useful work is
    done -- the extracted bytes are on local disk -- so raising would
    throw a completed extraction away over housekeeping. The worst a
    swallowed close can cost is one leaked descriptor.
    """
    try:
        handle.close()
    except OSError as exc:
        _emit(on_note,
              f"closing archive failed (extracted data intact): "
              f"{archive_path}: {exc}")


def _emit(on_note: Callable[[str], None] | None, note: str) -> None:
    """Hand *note* to *on_note*, when a note sink was supplied.

    A callback exception is deliberately not caught: raising from a
    callback is the supported cancellation contract (see
    :func:`iter_materialized_members`).
    """
    if on_note is not None:
        on_note(note)


def _report(progress: Callable[[int, int], None] | None,
            done: int, total: int) -> None:
    """Hand a ``(done, total)`` byte pair to *progress*, when supplied.

    *done* is clamped to *total* so a consumer can always derive a
    percentage in ``[0, 100]``, even if the archive grew between the
    ``stat`` that produced *total* and this call. Callback exceptions
    propagate, as above.
    """
    if progress is None:
        return
    if 0 < total < done:
        done = total
    progress(done, total)


def _archive_size(archive_path: Path) -> int:
    """Return *archive_path*'s size in bytes, or 0 when it is unknown.

    Zero is a deliberate "no denominator available" marker for the
    progress callback rather than an error: a stat failure must not stop
    an archive from being mined.
    """
    try:
        return archive_path.stat().st_size
    except OSError:
        return 0


def _copy_stream(src: IO[bytes], dst: IO[bytes],
                 on_chunk: Callable[[], None] | None) -> None:
    """Copy *src* into *dst*, one :data:`_COPY_CHUNK_BYTES` chunk at a time.

    *on_chunk* (when given) runs after every chunk written, which is
    what lets the caller report progress -- or cancel by raising -- part
    way through a member far too large to copy in one go.

    :raises _MemberReadError: *src* could not be read (corrupt or
        truncated payload, broken decompression stream, ...).
    :raises _MemberWriteError: *dst* could not be written (out of space,
        ...).
    """
    while True:
        try:
            chunk = src.read(_COPY_CHUNK_BYTES)
        except Exception as exc:
            raise _MemberReadError(exc) from exc
        if not chunk:
            return
        try:
            dst.write(chunk)
        except Exception as exc:
            raise _MemberWriteError(exc) from exc
        if on_chunk is not None:
            on_chunk()


def _materialize_one(src: IO[bytes], member: ArchiveMember, dest_path: Path,
                      on_note: Callable[[str], None] | None,
                      on_chunk: Callable[[], None] | None) -> bool:
    """Stream one member's payload from *src* to a fresh *dest_path*.

    *dest_path* is created exclusively (``"xb"``) for the same reason as
    in :func:`_extract_member`: a destination-name collision is reported
    instead of overwriting existing data (and, crucially, the colliding
    file is left untouched). Any other outcome than a complete copy
    removes the partially written file -- a write failure, an unreadable
    source, and a callback that raised to cancel the run alike -- so no
    half-member is ever handed on for diagnosis or left behind in a
    session cache.

    :raises _MemberReadError: propagated from :func:`_copy_stream`, for
        the caller to translate: recoverable on a seekable archive,
        terminal on a tar stream.
    :return: True when *dest_path* now holds the member's full payload.
    """
    try:
        dst = dest_path.open("xb")
    except FileExistsError:
        _emit(on_note,
              f"destination name collision, member skipped: "
              f"{member.display()}")
        return False
    except OSError as exc:
        # A member basename too long for the filesystem, a read-only or
        # full destination, ... : this member is lost, but a sweep that
        # may already have run for hours must not die of it.
        _emit(on_note, f"failed to extract member {member.display()}: {exc}")
        return False

    completed = False
    try:
        with dst:
            _copy_stream(src, dst, on_chunk)
        completed = True
    except _MemberWriteError as exc:
        _emit(on_note, f"failed to extract member {member.display()}: {exc}")
    finally:
        if not completed:
            dest_path.unlink(missing_ok=True)
    return completed


def _extract_zip_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo,
                         member: ArchiveMember, dest_path: Path,
                         on_note: Callable[[str], None] | None) -> bool:
    """Extract one zip member, degrading every payload failure to a note.

    A zip is randomly accessible, so damage confined to one member says
    nothing about the next one: the failure is noted, the partial file
    removed, and False returned so the caller simply moves on.
    """
    try:
        src = zf.open(info)
    except Exception as exc:
        # Includes a local file header that contradicts the central
        # directory, i.e. damage that only surfaces at open time.
        _emit(on_note, f"failed to extract member {member.display()}: {exc}")
        return False
    try:
        with src:
            return _materialize_one(src, member, dest_path, on_note, None)
    except _MemberReadError as exc:
        _emit(on_note, f"failed to extract member {member.display()}: {exc}")
        return False


def _iter_zip_materialized(
    archive_path: Path, dest_dir: Path,
    on_note: Callable[[str], None] | None,
    progress: Callable[[int, int], None] | None,
) -> Iterator[tuple[ArchiveMember, Path]]:
    """Drive :func:`iter_materialized_members` for a ``.zip`` archive.

    The archive is opened exactly once and its central directory walked
    in recorded order. Progress is reported as the running sum of the
    *compressed* sizes of the members processed so far against the
    archive file's size -- the closest cheap approximation of "how far
    through the file are we", since a zip is read by seeking to each
    member rather than sweeping the file front to back.
    """
    total = _archive_size(archive_path)
    try:
        zf = _open_with_retry(lambda: zipfile.ZipFile(archive_path))
    except Exception as exc:
        # Same wording as list_members': to the user an archive that
        # cannot be opened and one that cannot be read past its first
        # member are the same unusable backup.
        _emit(on_note, f"cannot read archive {archive_path}: {exc}")
        return

    done = 0
    index = 0
    # Not ``with zf:``, because a close that fails must not discard the
    # sweep it concludes -- see _close_noting.
    try:
        for info in zf.infolist():
            if info.is_dir() or not _is_target_name(info.filename):
                continue
            if posixpath.basename(info.filename).startswith(TEMP_PREFIX):
                continue
            member = ArchiveMember(archive_path=archive_path,
                                    member_name=info.filename,
                                    size=info.file_size)
            if info.flag_bits & 0x1:
                # General purpose bit 0: encrypted, unreadable without a
                # password (see _list_zip_members).
                _emit(on_note, f"encrypted member skipped: {member.display()}")
                continue
            dest_path = dest_dir / _dest_filename(index, info.filename)
            extracted = _extract_zip_member(zf, info, member, dest_path,
                                             on_note)
            # Charged whether or not the payload survived: those bytes
            # were read from the archive either way.
            done += info.compress_size
            _report(progress, done, total)
            if extracted:
                index += 1
                yield member, dest_path
    finally:
        _close_noting(zf, archive_path, on_note)


def _walk_tar_stream(
    tf: tarfile.TarFile, raw: IO[bytes], archive_path: Path, dest_dir: Path,
    on_note: Callable[[str], None] | None,
    progress: Callable[[int, int], None] | None, total: int,
) -> Iterator[tuple[ArchiveMember, Path]]:
    """Yield every target member of an already-open forward-only tar stream.

    Each header is read once, in stream order, and a target member's
    payload is copied out immediately -- before the next header is even
    looked at -- because a ``"r|"`` stream cannot go back for it later.
    Damage anywhere in the stream ends this archive (and only this
    archive): once the decoder has lost its place, every following
    header is meaningless.
    """
    def _notify_position() -> None:
        # raw.tell() is the position in the *compressed* file, which is
        # what a progress bar over the archive's size needs; the
        # uncompressed offsets tarfile tracks would be meaningless here.
        _report(progress, raw.tell(), total)

    index = 0
    while True:
        try:
            info = tf.next()
        except Exception as exc:
            _emit(on_note, f"cannot read archive {archive_path}: {exc}")
            return
        if info is None:
            break
        # tarfile appends every header it reads to .members; on an
        # archive with millions of entries that list alone would grow
        # without bound. Nothing here needs it: extractfile() is always
        # handed the TarInfo object itself, never a member name.
        tf.members.clear()
        _notify_position()

        if not info.isfile():
            continue  # directories, symlinks, devices, ...
        if not _is_target_name(info.name):
            continue
        if posixpath.basename(info.name).startswith(TEMP_PREFIX):
            continue

        member = ArchiveMember(archive_path=archive_path,
                                member_name=info.name, size=info.size)
        try:
            src = tf.extractfile(info)
        except Exception as exc:
            _emit(on_note, f"cannot read archive {archive_path}: {exc}")
            return
        if src is None:
            _emit(on_note, f"failed to extract member {member.display()}: "
                            "member has no extractable data stream")
            continue

        dest_path = dest_dir / _dest_filename(index, info.name)
        try:
            with src:
                extracted = _materialize_one(src, member, dest_path, on_note,
                                              _notify_position)
        except _MemberReadError as exc:
            _emit(on_note, f"cannot read archive {archive_path}: {exc}")
            return
        if extracted:
            index += 1
            yield member, dest_path

    # The walk ended on the archive's own end-of-data marker, which a
    # stream stops at without necessarily having consumed the trailing
    # padding: report completion explicitly so a progress bar always
    # lands on 100%.
    _report(progress, total, total)


def _iter_tar_materialized(
    archive_path: Path, dest_dir: Path,
    on_note: Callable[[str], None] | None,
    progress: Callable[[int, int], None] | None,
) -> Iterator[tuple[ArchiveMember, Path]]:
    """Drive :func:`iter_materialized_members` for a tar-family archive.

    The raw file is opened here rather than by :mod:`tarfile` so its
    ``tell()`` -- the compressed read position -- can feed the progress
    callback, and handed to ``tarfile.open(..., mode="r|*")``: the
    forward-only stream mode, which decodes the (possibly compressed)
    archive exactly once and never seeks back.
    """
    total = _archive_size(archive_path)
    try:
        raw = _open_with_retry(lambda: archive_path.open("rb"))
    except OSError as exc:
        _emit(on_note, f"cannot read archive {archive_path}: {exc}")
        return
    # Not ``with raw:``/``with tf:``, because a close that fails must
    # not discard the sweep it concludes -- see _close_noting.
    try:
        try:
            tf = tarfile.open(fileobj=raw, mode="r|*")  # noqa: SIM115
        except Exception as exc:
            # Broad for the same reason as in list_members: opening a
            # compressed tar already decodes its first blocks, so
            # damage can surface here as a raw decompression error.
            _emit(on_note, f"cannot read archive {archive_path}: {exc}")
            return
        try:
            yield from _walk_tar_stream(tf, raw, archive_path, dest_dir,
                                        on_note, progress, total)
        finally:
            _close_noting(tf, archive_path, on_note)
    finally:
        _close_noting(raw, archive_path, on_note)


def iter_materialized_members(
    archive_path: Path,
    dest_dir: Path,
    *,
    on_note: Callable[[str], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> Iterator[tuple[ArchiveMember, Path]]:
    """Extract *archive_path*'s ``.pptx``/``.pptm`` members in one pass.

    A generator fusing :func:`list_members` and :func:`materialize`:
    every target member met while sweeping the archive is streamed out
    to ``dest_dir / "memberNNNN-<sanitized-basename>"`` and yielded as
    ``(member, destination path)`` right away, so the archive is read
    exactly once no matter how many members it holds. That is what makes
    a hundred-gigabyte compressed backup tractable at all: listing and
    extracting separately would decompress the whole stream once per
    member. ``NNNN`` is the yield order of this call, which (as in
    :func:`materialize`) is what keeps destination names collision-free
    without ever deriving a directory from a member's own path (zip-slip
    guard). *dest_dir* must already exist; creating, and eventually
    cleaning up, is the caller's business -- as is deleting each yielded
    file once it has been consumed, if the caller does not want them to
    accumulate.

    Members are skipped by exactly the rules :func:`list_members`
    documents, and with the same note wording; a member whose payload
    cannot be extracted is likewise noted and left out.

    For an ``.enex`` export this dispatches straight to
    :func:`pptrepair.enex.iter_materialized_attachments`, which fuses
    listing and extraction the same way for that format; *on_note* and
    *progress* are passed through unchanged, and its own module
    docstring documents the skip rules and note wording that apply in
    that case instead.

    *on_note* receives those English notes as they happen, instead of
    the note *lists* the older two functions return -- a generator has
    nowhere to put a trailing list.

    *progress* receives ``(done, total)`` byte pairs as the sweep
    advances: *total* is the archive file's own size (0 when it could
    not be stat'ed), *done* the position reached in it, monotonically
    non-decreasing and clamped to *total*. For a tar family archive that
    is the true compressed read position, updated at every member
    boundary *and* every chunk copied, and it reaches *total* when the
    sweep completes; for a zip it is the running sum of the compressed
    sizes of the members processed, updated once per member (a zip is
    read by seeking, so there is no single "read position" to report).

    Damage is contained differently per format, following what each can
    actually recover from: on a zip, an unreadable member is noted and
    the walk continues with the next one; on a tar, whose stream cannot
    be resynchronised once the decoder loses its place, the failure is
    noted as ``"cannot read archive ..."`` and the walk ends -- the
    members yielded before the damage are still perfectly usable. An
    archive that cannot be opened at all yields nothing and produces
    that same note, after a brief retry ladder for transient
    environmental errors (see :func:`_open_with_retry`); a close that
    fails once the sweep is done is only noted, never raised (see
    :func:`_close_noting`).

    Never raises on its own account: every archive-level and
    member-level failure degrades to a note. The one exception that does
    propagate is one raised *by* *on_note* or *progress*, which is the
    supported cooperative-cancellation contract (see
    :class:`pptrepair.cancel.OperationCancelled`); the member being
    copied when that happens is removed rather than left half-written.
    """
    if _is_enex(archive_path):
        # Local import: pptrepair.enex imports ArchiveMember from this
        # module, so a top-level import here would be a cycle.
        from pptrepair.enex import iter_materialized_attachments
        yield from iter_materialized_attachments(archive_path, dest_dir,
                                                  on_note=on_note,
                                                  progress=progress)
    elif _is_zip_archive(archive_path):
        yield from _iter_zip_materialized(archive_path, dest_dir, on_note,
                                           progress)
    else:
        yield from _iter_tar_materialized(archive_path, dest_dir, on_note,
                                           progress)
