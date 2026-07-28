"""Source list model backing the GUI's drag-and-drop accumulation.

Users build up a work set by dropping files, folders and backup
archives onto the main window across several drag-and-drop gestures
(and/or the equivalent "Add Files…"/"Add Folder…" dialogs). This
module owns that accumulated list: :func:`classify_source` decides
what kind of thing a path is (without touching its content -- only
an ``os.stat`` call and the name are consulted), and
:class:`SourceListModel` is the Qt list model that stores the
resulting :class:`SourceEntry` objects for a ``QListView``.

Scanning a folder recursively and inspecting an archive's members are
both deferred to a later milestone; this module only classifies and
lists the paths the user dropped.
"""

from __future__ import annotations

import enum
import os
import stat
import time
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, Qt
from PySide6.QtWidgets import QApplication, QStyle

from pptrepair.archive import is_archive
from pptrepair.gui.i18n import tr
from pptrepair.walker import TARGET_SUFFIXES


class SourceKind(enum.Enum):
    """The three kinds of path a user may drop onto the main window."""

    #: A single ``.pptx``/``.pptm`` file.
    FILE = "file"
    #: A directory, scanned recursively for target files later.
    FOLDER = "folder"
    #: A backup archive (zip/tar variant) usable as donor material.
    ARCHIVE = "archive"


class RejectReason(enum.Enum):
    """Why :func:`classify_source` could not classify a dropped path."""

    #: Neither the initial ``os.stat`` nor its one retry found the path.
    NOT_FOUND = "not-found"
    #: Both stat attempts raised an ``OSError`` other than "not found"
    #: (e.g. a transient VPN/SMB mount hiccup) and the path's name did
    #: not match a supported suffix either.
    ACCESS_ERROR = "access-error"
    #: The path exists (or was accepted by name; see
    #: :func:`classify_source`) but is neither a folder, a supported
    #: file suffix nor a recognised archive name.
    UNSUPPORTED_SUFFIX = "unsupported-suffix"


@dataclass(frozen=True)
class Classification:
    """The outcome of classifying one dropped path.

    :ivar kind: the matching :class:`SourceKind`, or ``None`` when the
        path was rejected.
    :ivar reason: why the path was rejected; ``None`` when *kind* is
        not ``None``.
    :ivar detail: extra, human-readable context for *reason* (only
        populated for :attr:`RejectReason.ACCESS_ERROR`, where it holds
        the underlying ``OSError``'s message).
    :ivar store_path: on acceptance, the on-disk spelling to store in
        the model instead of the path that was passed in; non-``None``
        only when Unicode normalization recovery kicked in (see
        :func:`_renormalized`).
    """

    kind: SourceKind | None
    reason: RejectReason | None = None
    detail: str = ""
    store_path: Path | None = None


@dataclass(frozen=True)
class RejectedSource:
    """One input path :meth:`SourceListModel.add_paths` could not add.

    :ivar path: the rejected path, as passed in (not yet resolved).
    :ivar reason: why the path was rejected.
    :ivar detail: extra, human-readable context for *reason* (see
        :attr:`Classification.detail`).
    """

    path: Path
    reason: RejectReason
    detail: str = ""


@dataclass(frozen=True)
class SourceEntry:
    """One classified source path held by :class:`SourceListModel`.

    :ivar path: the resolved (absolute, symlink-normalised) path, in
        the spelling that actually stat'ed successfully (see
        :attr:`Classification.store_path`).
    :ivar kind: the classification produced by :func:`classify_source`.
    """

    path: Path
    kind: SourceKind


@dataclass
class AddResult:
    """Outcome of a single :meth:`SourceListModel.add_paths` call.

    :ivar added: entries newly appended to the model, in input order.
    :ivar duplicates: input paths that resolved to an already-present
        entry and were therefore skipped.
    :ivar rejected: input paths that :func:`classify_source` could not
        classify, together with why.
    """

    added: list[SourceEntry] = field(default_factory=list)
    duplicates: list[Path] = field(default_factory=list)
    rejected: list[RejectedSource] = field(default_factory=list)


#: Delay before the one-shot ``os.stat`` retry in :func:`classify_source`.
#: A module attribute (rather than a literal in the function body) so
#: tests can monkeypatch it down to ``0`` and keep the retry path fast.
_STAT_RETRY_DELAY_S = 1.0


def _match_normalized(parent: Path, name: str) -> str | None:
    """Return the on-disk spelling of *name* inside *parent*, or None.

    Compares under NFC normalization, so an NFC-spelled *name* finds an
    NFD-spelled directory entry and vice versa.
    """
    try:
        entries = os.listdir(parent)
    except OSError:
        # An unreadable (or non-existent, or unreachable) parent simply
        # means no recovery is possible from here.
        return None
    wanted = unicodedata.normalize("NFC", name)
    for entry in entries:
        if unicodedata.normalize("NFC", entry) == wanted:
            return entry
    return None


def _renormalized(path: Path) -> Path | None:
    """Rebuild *path* from the on-disk spellings of its components, or None.

    For a path whose stat fails with "not found" on a
    normalization-sensitive filesystem (an SMB mount, unlike APFS),
    the file frequently does exist -- under the other Unicode
    normalization form. Each component that cannot be stat'ed is
    looked up in its parent's directory listing under NFC-insensitive
    comparison and replaced with the entry's exact on-disk spelling.
    """
    anchor = path.anchor
    current = Path(anchor) if anchor else Path(".")
    # ``parts`` starts with the anchor (if any), which ``current``
    # already stands for; walk the remaining components.
    parts = path.parts[1:] if anchor else path.parts

    for part in parts:
        candidate = current / part
        try:
            # ``lstat`` rather than ``stat``: only the component's own
            # existence matters here, not what a symlink points at.
            os.lstat(candidate)
        except OSError:
            on_disk = _match_normalized(current, part)
            if on_disk is None:
                return None
            current = current / on_disk
        else:
            current = candidate

    # Nothing was respelled, so re-stat'ing this path would just repeat
    # the failure that brought us here; report "no recovery" instead.
    if current == path:
        return None
    return current


def _fstat_via_open(path: Path) -> os.stat_result | None:
    """Return fstat metadata via a read-only open, or None on failure.

    The last line of defence for a path whose every spelling fails a
    path-based ``os.stat``: an SMB mount has been measured handing out
    ENOENT for such a stat while ``os.open`` on the very same path
    succeeds (this is the route ``tar ztvf`` takes, which is why it
    keeps working on files the GUI called missing). Not a single byte
    is read -- the descriptor exists only to be ``os.fstat``ed and
    closed again -- so this stays as cheap as a stat even for a
    several-hundred-gigabyte file, and it warms the client's cache as
    a side effect, after which path-based stats start succeeding too.

    :param path: the path to open.
    :returns: the descriptor's ``os.fstat`` result, or ``None`` when
        either the open or the fstat failed.
    """
    try:
        # ``O_NONBLOCK`` changes nothing for a regular file or a
        # directory, but keeps a dropped FIFO (or a device node) from
        # parking the GUI thread until a writer shows up; nothing is
        # ever read from the descriptor anyway.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return os.fstat(fd)
    except OSError:
        return None
    finally:
        try:
            os.close(fd)
        except OSError:
            # A failing close (flaky mount) must not mask the outcome
            # -- but the descriptor must never be leaked.
            pass


def classify_source(path: Path) -> Classification:
    """Classify *path* as a file, folder or archive source.

    Directories are always :data:`SourceKind.FOLDER`, regardless of
    name. A file is :data:`SourceKind.FILE` when its suffix matches
    :data:`pptrepair.walker.TARGET_SUFFIXES` (case-insensitively) and
    :data:`SourceKind.ARCHIVE` when :func:`pptrepair.archive.is_archive`
    recognises its name. Anything else is rejected with
    :attr:`RejectReason.UNSUPPORTED_SUFFIX`.

    Classification is driven by ``os.stat`` rather than
    :meth:`Path.exists`/:meth:`Path.is_dir` so a single ``OSError`` --
    observed on VPN/SMB-mounted drives right after a drag-and-drop,
    where the mount can briefly bounce a fresh path -- does not
    immediately reject a path the user just dropped (and which
    therefore is known to exist). Such an error triggers, once the two
    recoveries described below have come up empty, exactly one retry
    after :data:`_STAT_RETRY_DELAY_S`; if that retry also fails
    with an ``OSError`` (but not a "not found" error), the path is
    classified by name alone, on the assumption that a just-dropped
    path exists even though its filesystem is currently unreachable.

    A failing first ``os.stat`` is also, before anything else, retried
    against the path rebuilt by :func:`_renormalized`: the GUI hands
    over NFC-spelled paths (from drops and file dialogs) while an SMB
    mount -- unlike APFS -- stores and matches names byte-exactly, so
    a name held on disk in NFD (any Japanese name with a dakuten, for
    instance) is otherwise reported as missing. That recovery costs
    one ``os.listdir`` per respelled component and only ever runs on
    the failure path, never on a healthy stat.

    Should that respelling not help either, and only when the failure
    was a "not found" one, the metadata is sought through
    :func:`_fstat_via_open` before the retry: the same SMB mount has
    been measured refusing a path-based stat under *every* spelling
    (the plain one, the respelled one, even the exact bytes
    ``os.listdir`` returned) while an ``os.open`` succeeded under
    both. The "not found" condition matters: that -- ENOENT -- is the
    pathology actually observed, whereas an ``OSError`` of any other
    kind typically means the mount is wedged, where adding an
    ``os.open`` would just block the GUI thread a second time. Those
    errors already have their safety net in the retry-then-classify-
    by-name flow. A cloud placeholder file is unaffected by this
    branch too -- its stat succeeds, so classification never gets
    here and no unintended download is triggered.

    :param path: the path to classify.
    :returns: the resulting :class:`Classification`; on a successful
        normalization recovery, its :attr:`Classification.store_path`
        carries the on-disk spelling the caller should keep.
    """
    # The path actually stat'ed successfully, which is *path* itself
    # unless normalization recovery had to respell it.
    target = path
    store_path: Path | None = None
    try:
        st = os.stat(path)
    except OSError as first_error:
        # First line of defence: the same file under the filesystem's
        # own Unicode normalization form. Worth trying whatever the
        # error was, since a failed listing costs nothing.
        recovered = _renormalized(path)
        rescued: tuple[os.stat_result, Path] | None = None
        if recovered is not None:
            try:
                rescued = (os.stat(recovered), recovered)
            except OSError:
                # The respelling did not stat either; the open-based
                # fallback below still gets to try it.
                rescued = None

        not_found = isinstance(
            first_error, (FileNotFoundError, NotADirectoryError))
        if rescued is None and not_found:
            # Second line of defence: metadata through a read-only
            # open, which an SMB mount can serve when no path-based
            # stat does. The respelled path goes first: when both
            # spellings open, the on-disk one is the one to keep.
            candidates = [path] if recovered is None else [recovered, path]
            for candidate in candidates:
                opened_st = _fstat_via_open(candidate)
                if opened_st is not None:
                    rescued = (opened_st, candidate)
                    break

        if rescued is not None:
            st, target = rescued
            store_path = target if target != path else None
        else:
            # Even a "not found" error gets the one retry: a bouncing
            # network mount can make a whole subtree vanish for a
            # moment, which surfaces as ENOENT just like a genuinely
            # missing path.
            time.sleep(_STAT_RETRY_DELAY_S)
            try:
                st = os.stat(path)
            except (FileNotFoundError, NotADirectoryError):
                return Classification(None, RejectReason.NOT_FOUND)
            except OSError as exc:
                # Both attempts failed for a reason other than "not
                # found" -- classify by name alone; see the docstring.
                if path.suffix.lower() in TARGET_SUFFIXES:
                    return Classification(SourceKind.FILE)
                if is_archive(path):
                    return Classification(SourceKind.ARCHIVE)
                return Classification(
                    None, RejectReason.ACCESS_ERROR, detail=str(exc))

    if stat.S_ISDIR(st.st_mode):
        return Classification(SourceKind.FOLDER, store_path=store_path)
    if target.suffix.lower() in TARGET_SUFFIXES:
        return Classification(SourceKind.FILE, store_path=store_path)
    if is_archive(target):
        return Classification(SourceKind.ARCHIVE, store_path=store_path)
    return Classification(None, RejectReason.UNSUPPORTED_SUFFIX)


class SourceListModel(QAbstractListModel):
    """Qt list model holding the accumulated, classified source paths.

    Backs a ``QListView`` in the main window: each row is one
    :class:`SourceEntry`, added through repeated calls to
    :meth:`add_paths` (one per drag-and-drop gesture or "Add
    Files…"/"Add Folder…" dialog use) and removable through
    :meth:`remove_rows` or :meth:`clear`.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        """Initialize an empty model.

        :param parent: optional Qt parent object.
        """
        super().__init__(parent)
        self._entries: list[SourceEntry] = []

    def rowCount(
        self, parent: QModelIndex = QModelIndex()
    ) -> int:
        """Return the number of accumulated source entries.

        :param parent: unused; this is a flat (non-tree) list model.
        """
        if parent.isValid():
            return 0
        return len(self._entries)

    def data(
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        """Return the display text or icon for *index*.

        :param index: row to query; must be valid and in range.
        :param role: ``Qt.DisplayRole`` for the path text (with a
            ``[folder]``/``[archive]`` suffix for non-file kinds), or
            ``Qt.DecorationRole`` for a standard Qt icon matching the
            entry's kind.
        :returns: the requested value, or ``None`` for any other role
            or an out-of-range index.
        """
        if not index.isValid() or not (0 <= index.row() < len(self._entries)):
            return None
        entry = self._entries[index.row()]

        if role == Qt.ItemDataRole.DisplayRole:
            if entry.kind is SourceKind.FOLDER:
                suffix = tr(" [folder]")
            elif entry.kind is SourceKind.ARCHIVE:
                suffix = tr(" [archive]")
            else:
                suffix = ""
            return f"{entry.path}{suffix}"

        if role == Qt.ItemDataRole.DecorationRole:
            return self._icon_for(entry.kind)

        return None

    def _icon_for(self, kind: SourceKind) -> Any:
        """Return the standard Qt icon matching *kind*.

        :param kind: the source kind to pick an icon for.
        """
        style = QApplication.style()
        icon_map = {
            SourceKind.FOLDER: QStyle.StandardPixmap.SP_DirIcon,
            SourceKind.FILE: QStyle.StandardPixmap.SP_FileIcon,
            SourceKind.ARCHIVE: QStyle.StandardPixmap.SP_DriveHDIcon,
        }
        return style.standardIcon(icon_map[kind])

    def add_paths(self, paths: Iterable[Path]) -> AddResult:
        """Classify and append *paths*, skipping duplicates and rejects.

        Each path is normalised with :meth:`Path.resolve` before
        classification and comparison, so ``a/b/../c`` and ``a/c``
        are recognised as the same entry. When
        :func:`classify_source` had to recover a path through Unicode
        normalization, the on-disk spelling it reports is what gets
        stored and compared, so the same file dropped once in NFC and
        once in NFD form is stored once and reported as a duplicate
        the second time.

        :param paths: candidate paths, in the order to try them
            (typically the URLs from one drop or dialog selection).
        :returns: the resulting :class:`AddResult`, partitioning
            *paths* into newly added entries, duplicates of entries
            already present and paths that could not be classified.
        """
        result = AddResult()
        existing = {entry.path for entry in self._entries}
        new_entries: list[SourceEntry] = []

        for raw_path in paths:
            resolved = raw_path.resolve()
            classification = classify_source(resolved)
            if classification.kind is None:
                result.rejected.append(RejectedSource(
                    raw_path, classification.reason, classification.detail))
                continue
            stored = (
                resolved if classification.store_path is None
                else classification.store_path)
            if stored in existing:
                result.duplicates.append(raw_path)
                continue
            entry = SourceEntry(path=stored, kind=classification.kind)
            new_entries.append(entry)
            existing.add(stored)

        if new_entries:
            first_row = len(self._entries)
            last_row = first_row + len(new_entries) - 1
            self.beginInsertRows(QModelIndex(), first_row, last_row)
            self._entries.extend(new_entries)
            self.endInsertRows()
            result.added = new_entries

        return result

    def remove_rows(self, rows: Sequence[int]) -> None:
        """Remove the entries at *rows*.

        Duplicate indices are ignored; out-of-range indices are
        silently skipped. Rows are removed highest-index-first
        internally so the removal is safe regardless of the order
        *rows* is given in.

        :param rows: zero-based row indices to remove.
        """
        # Deduplicate and sort descending so each removal's row index
        # stays valid for the ones that follow it.
        unique_rows = sorted(set(rows), reverse=True)
        for row in unique_rows:
            if not (0 <= row < len(self._entries)):
                continue
            self.beginRemoveRows(QModelIndex(), row, row)
            del self._entries[row]
            self.endRemoveRows()

    def clear(self) -> None:
        """Remove every entry from the model."""
        if not self._entries:
            return
        self.beginResetModel()
        self._entries.clear()
        self.endResetModel()

    def entries(self) -> list[SourceEntry]:
        """Return a copy of the currently accumulated entries.

        :returns: a new list; mutating it does not affect the model.
        """
        return list(self._entries)
