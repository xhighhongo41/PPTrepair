"""The PPTrepair desktop application's main window."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import QMainWindow, QMessageBox

import pptrepair
from pptrepair.gui.source_panel import SourcePanel
from pptrepair.gui.sources import AddResult, SourceListModel


class MainWindow(QMainWindow):
    """Top-level window of the PPTrepair desktop application.

    Hosts the source list panel (an empty-state placeholder that
    switches to a populated list once sources are added), accepts
    drag-and-drop of files/folders to accumulate sources across
    several drops, a File/Help menu bar and a status bar. All
    user-facing strings are plain English literals for now;
    gettext-based translation is planned for a later milestone.
    """

    def __init__(self) -> None:
        """Build the window's central widget, menu bar and status bar."""
        super().__init__()
        self.setWindowTitle("PPTrepair")
        self.resize(900, 600)
        self.setAcceptDrops(True)

        self._sources = SourceListModel(self)
        self._build_central_widget()
        self._build_menu_bar()
        self.statusBar().showMessage("Ready")

    def _build_central_widget(self) -> None:
        """Install the source list panel as the central widget."""
        self._source_panel = SourcePanel(self._sources, self)
        self.setCentralWidget(self._source_panel)

    def _build_menu_bar(self) -> None:
        """Populate the menu bar's File and Help menus."""
        file_menu = self.menuBar().addMenu("File")

        add_files_action = QAction("Add Files…", self)
        add_files_action.setShortcut(QKeySequence.StandardKey.Open)
        add_files_action.triggered.connect(self._source_panel.add_files)
        file_menu.addAction(add_files_action)

        add_folder_action = QAction("Add Folder…", self)
        add_folder_action.triggered.connect(self._source_panel.add_folder)
        file_menu.addAction(add_folder_action)

        file_menu.addAction(self._build_separator())

        clear_sources_action = QAction("Clear Sources", self)
        clear_sources_action.triggered.connect(self._sources.clear)
        file_menu.addAction(clear_sources_action)

        file_menu.addAction(self._build_separator())

        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("Help")
        about_action = QAction("About PPTrepair", self)
        about_action.setMenuRole(QAction.MenuRole.AboutRole)
        about_action.triggered.connect(self._show_about_dialog)
        help_menu.addAction(about_action)

    def _build_separator(self) -> QAction:
        """Build a menu separator action parented to this window.

        ``QMenu.addSeparator()`` returns a separator action with no
        Python-side owner, which has been observed to leave that
        action in an invalid (already-deleted) state once its
        ``QMenu`` is re-wrapped through ``QAction.menu()`` -- exactly
        the lookup pattern the test suite uses. Building the
        separator as a window-parented :class:`QAction` like every
        other menu action avoids that.
        """
        separator = QAction(self)
        separator.setSeparator(True)
        return separator

    def _show_about_dialog(self) -> None:
        """Show an About dialog with the app name, version and license."""
        QMessageBox.about(
            self,
            "About PPTrepair",
            "PPTrepair {version}\n"
            "Diagnose and repair PowerPoint files corrupted while "
            "stored on OneDrive.\n"
            "Licensed under the GNU General Public License v3.0.".format(
                version=pptrepair.__version__),
        )

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        """Accept a drag that carries at least one local file URL.

        :param event: Qt's drag-enter event; not consumed when the
            drag carries no local file URL (e.g. dragged text).
        """
        if self._has_local_file_urls(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        """Add the dropped local files/folders to the source list.

        :param event: Qt's drop event; its accepted URLs are resolved
            and handed to :meth:`SourceListModel.add_paths`, and the
            resulting counts are summarised on the status bar.
        """
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if not paths:
            return
        event.acceptProposedAction()
        result = self._sources.add_paths(paths)
        self.statusBar().showMessage(self._format_add_result(result))

    @staticmethod
    def _has_local_file_urls(mime_data: QMimeData) -> bool:
        """Return True when *mime_data* carries a local file URL.

        :param mime_data: the drag/drop event's MIME data.
        """
        if not mime_data.hasUrls():
            return False
        return any(url.isLocalFile() for url in mime_data.urls())

    @staticmethod
    def _format_add_result(result: AddResult) -> str:
        """Summarise *result* as an English status-bar message.

        Zero-count breakdown items are omitted entirely (e.g. a drop
        made up solely of duplicates reports only the duplicate
        count).

        :param result: the outcome of a :meth:`SourceListModel.add_paths`
            call.
        """
        parts = []
        if result.added:
            parts.append(f"Added {len(result.added)} source(s)")
        if result.duplicates:
            parts.append(f"{len(result.duplicates)} duplicate(s) skipped")
        if result.rejected:
            parts.append(f"{len(result.rejected)} unsupported item(s) rejected")
        return ", ".join(parts) if parts else "No sources added"
