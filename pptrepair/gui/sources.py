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
    """

    kind: SourceKind | None
    reason: RejectReason | None = None
    detail: str = ""


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

    :ivar path: the resolved (absolute, symlink-normalised) path.
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
    therefore is known to exist). Such an error triggers exactly one
    retry after :data:`_STAT_RETRY_DELAY_S`; if that retry also fails
    with an ``OSError`` (but not a "not found" error), the path is
    classified by name alone, on the assumption that a just-dropped
    path exists even though its filesystem is currently unreachable.

    :param path: the path to classify.
    :returns: the resulting :class:`Classification`.
    """
    try:
        st = os.stat(path)
    except OSError:
        # Even a "not found" error gets the one retry: a bouncing
        # network mount can make a whole subtree vanish for a moment,
        # which surfaces as ENOENT just like a genuinely missing path.
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
        return Classification(SourceKind.FOLDER)
    if path.suffix.lower() in TARGET_SUFFIXES:
        return Classification(SourceKind.FILE)
    if is_archive(path):
        return Classification(SourceKind.ARCHIVE)
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
        are recognised as the same entry.

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
            if resolved in existing:
                result.duplicates.append(raw_path)
                continue
            entry = SourceEntry(path=resolved, kind=classification.kind)
            new_entries.append(entry)
            existing.add(resolved)

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
