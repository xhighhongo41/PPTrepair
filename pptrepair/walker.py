"""Recursive discovery of PowerPoint files for ``pptrepair scan``.

This module only *finds* candidate files; it never opens them and it
never invokes the diagnosis pipeline. Keeping discovery free of file
reads matters because cloud-sync placeholders (OneDrive Files
On-Demand, iCloud Drive, and other clients built on the OS-standard
placeholder mechanisms) download their content as soon as the file is
opened. Discovery relies on ``os.stat`` metadata only, which does not
trigger a download.

Cloud-placeholder detection is best-effort and OS-mechanism based:

* macOS — dataless files carry ``SF_DATALESS`` in ``st_flags``
  (File Provider based clients: OneDrive, iCloud Drive since Sonoma,
  Google Drive, Box Drive, migrated Dropbox).
* Windows — Cloud Filter API placeholders carry
  ``FILE_ATTRIBUTE_RECALL_ON_OPEN`` / ``_RECALL_ON_DATA_ACCESS`` in
  ``st_file_attributes`` (OneDrive, recent Dropbox; NOT Google Drive
  for desktop, which uses a proprietary driver).

Clients that bypass the OS mechanisms cannot be detected; reading such
files may still trigger a download, which the README documents.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

#: File suffixes handled by the diagnosis pipeline (matched
#: case-insensitively). Both are OOXML ZIP containers.
TARGET_SUFFIXES = frozenset({".pptx", ".pptm"})

#: Legacy binary PowerPoint format — not ZIP-based, counted separately.
LEGACY_SUFFIXES = frozenset({".ppt"})

#: Prefix of Office owner/lock temp files (``~$presentation.pptx``).
TEMP_PREFIX = "~$"

#: macOS ``st_flags`` bit: the file is a dataless (cloud-only) object.
#: ``stat.SF_DATALESS`` exists only on Python 3.13+, hence the literal.
SF_DATALESS = 0x40000000

#: Windows ``st_file_attributes`` bits marking Cloud Filter API
#: placeholders. Absent from the ``stat`` module, hence the literals.
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

_RECALL_MASK = (FILE_ATTRIBUTE_RECALL_ON_OPEN
                | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)


@dataclass
class WalkResult:
    """Outcome of one discovery pass over the requested roots."""

    targets: list[Path] = field(default_factory=list)
    """Files to diagnose (.pptx/.pptm), in deterministic walk order."""
    skipped_legacy: list[Path] = field(default_factory=list)
    """Legacy .ppt files (not ZIP-based, out of scope)."""
    skipped_temp: list[Path] = field(default_factory=list)
    """Office ``~$`` owner/lock temp files."""
    skipped_cloud: list[Path] = field(default_factory=list)
    """Cloud-only placeholders (files and directories) left untouched."""
    download_targets: list[Path] = field(default_factory=list)
    """Subset of ``targets`` that are cloud-only placeholders and will
    be downloaded when read (populated only with ``allow_download``)."""
    errors: list[tuple[Path, str]] = field(default_factory=list)
    """Paths that could not be examined, with the error message."""


def is_cloud_placeholder(st: os.stat_result) -> bool:
    """Return True when *st* describes a cloud-only placeholder.

    Checks ``st_flags`` for :data:`SF_DATALESS` (macOS) and
    ``st_file_attributes`` for the RECALL bits (Windows). Both
    attributes are read with ``getattr`` defaults so the check is a
    no-op (False) on platforms that do not expose them.
    """
    if getattr(st, "st_flags", 0) & SF_DATALESS:
        return True
    if getattr(st, "st_file_attributes", 0) & _RECALL_MASK:
        return True
    return False


def _record_error(result: WalkResult, path: Path, exc: OSError) -> None:
    """Append *path* and *exc*'s message to ``result.errors``."""
    result.errors.append((path, str(exc)))


def _lstat_or_record(path: Path, result: WalkResult) -> os.stat_result | None:
    """Return ``os.lstat(path)``, or None with the failure recorded.

    Never follows a final symlink component; used as the first probe
    for every path so a broken or permission-denied entry never raises.
    """
    try:
        return os.lstat(path)
    except OSError as exc:
        _record_error(result, path, exc)
        return None


def _resolve_stat(path: Path, lst: os.stat_result, *,
                   follow_symlinks: bool,
                   result: WalkResult) -> os.stat_result | None:
    """Return the stat to classify *path* with, or None to ignore it.

    Non-symlinks are returned as-is (*lst* already carries their
    metadata). A symlink is ignored (returns None) unless
    *follow_symlinks* is true, in which case it is followed with
    ``os.stat``; a failure while following (dangling link, permission
    error) is recorded in ``result.errors`` and also yields None.
    """
    if not stat.S_ISLNK(lst.st_mode):
        return lst
    if not follow_symlinks:
        return None
    try:
        return os.stat(path)
    except OSError as exc:
        _record_error(result, path, exc)
        return None


def _classify_file(result: WalkResult, path: Path, st: os.stat_result, *,
                    allow_download: bool) -> None:
    """Sort *path* into the appropriate bucket of *result*.

    Applies the cloud -> temp -> suffix classification order described
    in :func:`discover_targets`.
    """
    cloud = is_cloud_placeholder(st)
    if cloud and not allow_download:
        result.skipped_cloud.append(path)
        return
    if path.name.startswith(TEMP_PREFIX):
        result.skipped_temp.append(path)
        return
    suffix = path.suffix.lower()
    if suffix in TARGET_SUFFIXES:
        result.targets.append(path)
        if cloud:
            # Reading this target will make the sync client download
            # it; recorded so the CLI can announce the download.
            result.download_targets.append(path)
    elif suffix in LEGACY_SUFFIXES:
        result.skipped_legacy.append(path)
    # Unrelated suffixes are neither a target nor an error: ignored.


def _enter_directory(result: WalkResult, path: Path, st: os.stat_result, *,
                      follow_symlinks: bool, allow_download: bool,
                      visited: set[tuple[int, int]]) -> bool:
    """Return True when *path* should be descended into.

    Records *path* in ``skipped_cloud`` (without recursing) when it is
    a cloud placeholder. When *follow_symlinks* is true, also guards
    against revisiting the same directory identity twice, which is
    what makes cyclic and diamond symlinks safe to follow.
    """
    if follow_symlinks:
        key = (st.st_dev, st.st_ino)
        if key in visited:
            return False
        visited.add(key)
    if not allow_download and is_cloud_placeholder(st):
        result.skipped_cloud.append(path)
        return False
    return True


def _walk_directory(root: Path, result: WalkResult, *, follow_symlinks: bool,
                     allow_download: bool,
                     visited: set[tuple[int, int]]) -> None:
    """Recursively walk *root* (already confirmed to be a directory).

    Uses ``os.walk`` for the traversal itself but takes over both the
    symlink-following decision and the cloud-placeholder guard so that
    a placeholder directory is never listed (``os.scandir`` would
    materialize it) and a followed symlink cycle never repeats.
    """

    def _onerror(exc: OSError) -> None:
        _record_error(result, Path(exc.filename or root), exc)

    for top, dirs, files in os.walk(root, followlinks=follow_symlinks,
                                     onerror=_onerror):
        top_path = Path(top)
        # Sort in place: os.walk uses `dirs` to decide what to recurse
        # into next, and both lists must be in deterministic order.
        dirs.sort()
        files.sort()

        kept_dirs = []
        for name in dirs:
            dir_path = top_path / name
            lst = _lstat_or_record(dir_path, result)
            if lst is None:
                continue
            child_st = _resolve_stat(dir_path, lst,
                                      follow_symlinks=follow_symlinks,
                                      result=result)
            if child_st is None:
                continue  # symlink ignored, or following it failed
            if _enter_directory(result, dir_path, child_st,
                                 follow_symlinks=follow_symlinks,
                                 allow_download=allow_download,
                                 visited=visited):
                kept_dirs.append(name)
        dirs[:] = kept_dirs

        for name in files:
            file_path = top_path / name
            lst = _lstat_or_record(file_path, result)
            if lst is None:
                continue
            file_st = _resolve_stat(file_path, lst,
                                     follow_symlinks=follow_symlinks,
                                     result=result)
            if file_st is None:
                continue  # symlink ignored, or following it failed
            _classify_file(result, file_path, file_st,
                            allow_download=allow_download)


def discover_targets(roots: Sequence[Path], *,
                     follow_symlinks: bool = False,
                     allow_download: bool = False) -> WalkResult:
    """Discover PowerPoint files under *roots* without opening any file.

    Implementation requirements:

    * Each root may be a file (classified directly into the result
      buckets) or a directory (walked recursively). A nonexistent root
      is recorded in ``errors``.
    * Classification order per file: cloud placeholder (unless
      *allow_download*) -> ``~$`` temp -> suffix match (targets /
      skipped_legacy / no bucket for unrelated suffixes). Suffixes are
      compared case-insensitively; only ``os.lstat`` /
      ``os.stat(..., follow_symlinks=False)`` metadata may be used.
    * Directories whose stat shows a cloud placeholder are recorded in
      ``skipped_cloud`` and NOT descended into (listing a dataless
      directory can materialize it), unless *allow_download* is true.
    * With ``allow_download=True`` placeholder files become ordinary
      candidates and are additionally recorded in ``download_targets``
      (so callers can announce the impending download); placeholder
      directories are descended into normally.
    * Symbolic links (both to files and to directories) found during
      the walk are ignored unless *follow_symlinks* is true. When
      following, a visited set of ``(st_dev, st_ino)`` directory
      identities must prevent revisiting a directory twice (cycles,
      diamond links). A *root* that is itself a symlink is always
      followed (POSIX ``find -H`` convention): the user named it
      explicitly, and silently scanning nothing would mislead.
    * Deterministic order: directory entries are processed in sorted
      order (byte order of the entry name); ``os.walk`` callers must
      sort ``dirs`` in place and ``files`` before use.
    * Unreadable directories or files (``PermissionError``,
      ``FileNotFoundError`` from a race, ``OSError``) append
      ``(path, str(exc))`` to ``errors`` and never abort the walk.
    """
    result = WalkResult()
    visited: set[tuple[int, int]] = set()

    for raw_root in roots:
        root = Path(raw_root)
        lst = _lstat_or_record(root, result)
        if lst is None:
            continue  # nonexistent (or otherwise unstatable) root
        # An explicitly named root always has its own symlink followed
        # (find -H convention); only links *inside* the tree obey
        # follow_symlinks.
        root_st = _resolve_stat(root, lst, follow_symlinks=True,
                                 result=result)
        if root_st is None:
            continue  # following the symlink root failed

        if stat.S_ISDIR(root_st.st_mode):
            if _enter_directory(result, root, root_st,
                                 follow_symlinks=follow_symlinks,
                                 allow_download=allow_download,
                                 visited=visited):
                _walk_directory(root, result,
                                 follow_symlinks=follow_symlinks,
                                 allow_download=allow_download,
                                 visited=visited)
        elif stat.S_ISREG(root_st.st_mode):
            _classify_file(result, root, root_st,
                            allow_download=allow_download)
        # Other entry types (sockets, devices, FIFOs) are neither files
        # nor directories we can classify; silently ignored.

    return result
