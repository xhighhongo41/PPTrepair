"""Tests for the GUI's source accumulation (:mod:`pptrepair.gui.sources`).

Skipped wholesale when PySide6 is not installed (the optional ``[gui]``
extra); see :mod:`tests.conftest` for the matching collection guard.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from pytestqt.qtbot import QtBot

from pptrepair.gui.main_window import MainWindow
from pptrepair.gui.sources import (
    SourceKind,
    SourceListModel,
    classify_source,
)

# --------------------------------------------------------------------------
# classify_source
# --------------------------------------------------------------------------


def test_classify_source_folder(tmp_path: Path) -> None:
    """A directory is classified as a folder source, regardless of name."""
    folder = tmp_path / "some_dir"
    folder.mkdir()
    assert classify_source(folder) is SourceKind.FOLDER


def test_classify_source_pptx_file(tmp_path: Path) -> None:
    """A ``.pptx`` file is classified as a file source."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    assert classify_source(path) is SourceKind.FILE


def test_classify_source_pptm_uppercase(tmp_path: Path) -> None:
    """A ``.PPTM`` file (uppercase suffix) is still classified as a file."""
    path = tmp_path / "macro.PPTM"
    path.write_bytes(b"")
    assert classify_source(path) is SourceKind.FILE


def test_classify_source_zip_archive(tmp_path: Path) -> None:
    """A ``.zip`` file is classified as an archive source."""
    path = tmp_path / "backup.zip"
    path.write_bytes(b"")
    assert classify_source(path) is SourceKind.ARCHIVE


def test_classify_source_tar_gz_archive(tmp_path: Path) -> None:
    """A ``.tar.gz`` file is classified as an archive source."""
    path = tmp_path / "backup.tar.gz"
    path.write_bytes(b"")
    assert classify_source(path) is SourceKind.ARCHIVE


def test_classify_source_unsupported_text_file(tmp_path: Path) -> None:
    """A plain ``.txt`` file is not classifiable."""
    path = tmp_path / "notes.txt"
    path.write_bytes(b"")
    assert classify_source(path) is None


def test_classify_source_nonexistent_path(tmp_path: Path) -> None:
    """A path that does not exist on disk is not classifiable."""
    assert classify_source(tmp_path / "missing.pptx") is None


def test_classify_source_legacy_ppt_file(tmp_path: Path) -> None:
    """A legacy ``.ppt`` file (binary format) is not classifiable."""
    path = tmp_path / "legacy.ppt"
    path.write_bytes(b"")
    assert classify_source(path) is None


# --------------------------------------------------------------------------
# SourceListModel
# --------------------------------------------------------------------------


def test_add_paths_adds_and_counts_row(tmp_path: Path) -> None:
    """Adding a supported file appends one row and reports it as added."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")

    result = model.add_paths([path])

    assert model.rowCount() == 1
    assert len(result.added) == 1
    assert result.added[0].path == path.resolve()
    assert result.added[0].kind is SourceKind.FILE
    assert result.duplicates == []
    assert result.rejected == []


def test_add_paths_detects_exact_duplicate(tmp_path: Path) -> None:
    """Adding the same path twice reports the second as a duplicate."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")

    model.add_paths([path])
    result = model.add_paths([path])

    assert model.rowCount() == 1
    assert result.added == []
    assert result.duplicates == [path]


def test_add_paths_detects_duplicate_via_dot_dot(tmp_path: Path) -> None:
    """A ``sub/..``-relative path resolving to an existing entry is a dup."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    (tmp_path / "sub").mkdir()
    indirect_path = tmp_path / "sub" / ".." / "deck.pptx"

    model.add_paths([path])
    result = model.add_paths([indirect_path])

    assert model.rowCount() == 1
    assert result.added == []
    assert result.duplicates == [indirect_path]


def test_add_paths_rejects_unsupported(tmp_path: Path) -> None:
    """An unsupported path is reported as rejected, not added."""
    model = SourceListModel()
    path = tmp_path / "notes.txt"
    path.write_bytes(b"")

    result = model.add_paths([path])

    assert model.rowCount() == 0
    assert result.added == []
    assert result.rejected == [path]


def test_display_role_shows_plain_path_for_file(tmp_path: Path) -> None:
    """A file entry's display text is its plain resolved path."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    model.add_paths([path])

    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == str(path.resolve())


def test_display_role_shows_folder_suffix(tmp_path: Path) -> None:
    """A folder entry's display text ends with the ``[folder]`` tag."""
    model = SourceListModel()
    folder = tmp_path / "some_dir"
    folder.mkdir()
    model.add_paths([folder])

    index = model.index(0, 0)
    text = model.data(index, Qt.ItemDataRole.DisplayRole)
    assert text == f"{folder.resolve()} [folder]"


def test_display_role_shows_archive_suffix(tmp_path: Path) -> None:
    """An archive entry's display text ends with the ``[archive]`` tag."""
    model = SourceListModel()
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"")
    model.add_paths([archive])

    index = model.index(0, 0)
    text = model.data(index, Qt.ItemDataRole.DisplayRole)
    assert text == f"{archive.resolve()} [archive]"


def test_remove_rows_multiple_reverse_order_safe(tmp_path: Path) -> None:
    """Removing several rows works regardless of the order they're given in."""
    model = SourceListModel()
    paths = []
    for index in range(4):
        path = tmp_path / f"deck{index}.pptx"
        path.write_bytes(b"")
        paths.append(path)
    model.add_paths(paths)

    # Ascending order deliberately, to exercise the descending-sort guard.
    model.remove_rows([0, 2])

    remaining = [entry.path for entry in model.entries()]
    assert remaining == [paths[1].resolve(), paths[3].resolve()]


def test_clear_empties_the_model(tmp_path: Path) -> None:
    """clear() removes every entry from the model."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    model.add_paths([path])

    model.clear()

    assert model.rowCount() == 0
    assert model.entries() == []


def test_entries_returns_a_copy(tmp_path: Path) -> None:
    """entries() returns a list that mutating does not affect the model."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    model.add_paths([path])

    entries = model.entries()
    entries.clear()

    assert model.rowCount() == 1


# --------------------------------------------------------------------------
# MainWindow integration: drag-and-drop and menu wiring
# --------------------------------------------------------------------------


@pytest.fixture
def main_window(qtbot: QtBot) -> MainWindow:
    """Build a :class:`MainWindow`, registered with *qtbot* for cleanup."""
    window = MainWindow()
    qtbot.addWidget(window)
    return window


def _make_drop_mime_data(paths: list[Path]) -> QMimeData:
    """Build a :class:`QMimeData` carrying local file URLs for *paths*."""
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime_data


def test_drag_enter_accepts_local_file_urls(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """dragEnterEvent accepts a drag carrying at least one local file URL."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    mime_data = _make_drop_mime_data([path])
    event = QDragEnterEvent(
        QPoint(0, 0),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )

    main_window.dragEnterEvent(event)

    assert event.isAccepted()


def test_drop_event_adds_sources_and_updates_status_bar(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """dropEvent hands dropped files to the model and reports the summary."""
    good_path = tmp_path / "deck.pptx"
    good_path.write_bytes(b"")
    bad_path = tmp_path / "notes.txt"
    bad_path.write_bytes(b"")
    mime_data = _make_drop_mime_data([good_path, bad_path])
    event = QDropEvent(
        QPoint(0, 0),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )

    main_window.dropEvent(event)

    assert event.isAccepted()
    assert main_window._sources.rowCount() == 1
    message = main_window.statusBar().currentMessage()
    assert "Added 1 source(s)" in message
    assert "1 unsupported item(s) rejected" in message
    # No duplicates on this first drop, so that breakdown item is omitted.
    assert "duplicate" not in message


def test_drop_event_reports_duplicate_breakdown(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """A second drop of an already-added path is reported as a duplicate."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    main_window._sources.add_paths([path])

    mime_data = _make_drop_mime_data([path])
    event = QDropEvent(
        QPoint(0, 0),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main_window.dropEvent(event)

    message = main_window.statusBar().currentMessage()
    assert "1 duplicate(s) skipped" in message
    assert "Added" not in message


def test_file_menu_has_source_management_actions(
    main_window: MainWindow,
) -> None:
    """The File menu exposes Add Files…/Add Folder…/Clear Sources actions."""
    file_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "File":
            file_menu = action.menu()
            break
    assert file_menu is not None

    titles = [action.text() for action in file_menu.actions()]
    assert "Add Files…" in titles
    assert "Add Folder…" in titles
    assert "Clear Sources" in titles
    assert "Quit" in titles


def test_add_files_action_has_open_shortcut(main_window: MainWindow) -> None:
    """The "Add Files…" action is bound to the standard Open shortcut."""
    file_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "File":
            file_menu = action.menu()
            break
    assert file_menu is not None

    add_files_action = None
    for action in file_menu.actions():
        if action.text() == "Add Files…":
            add_files_action = action
            break
    assert isinstance(add_files_action, QAction)
    assert add_files_action.shortcut() == QKeySequence(
        QKeySequence.StandardKey.Open
    )
