"""The PPTrepair desktop application's main window.

Owns the whole scan lifecycle on the UI thread: it builds a
:class:`~pptrepair.gui.worker.ScanRequest` from the accumulated
sources, starts a :class:`~pptrepair.gui.worker.ScanWorker` on a
background thread, and reacts to that worker's signals (all delivered
here on the UI thread via Qt's queued connections) to stream progress,
show results, and re-enable the UI. Cancellation and window-close are
handled cooperatively so a running scan never leaves a dangling thread.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QMimeData
from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import pptrepair
from pptrepair.gui.results import ResultsPanel
from pptrepair.gui.run_options import RunOptionsPanel
from pptrepair.gui.source_panel import SourcePanel
from pptrepair.gui.sources import AddResult, SourceKind, SourceListModel
from pptrepair.gui.worker import GuiScanResult, ScanRequest, ScanWorker

#: How long (ms) window-close waits for a running worker to stop.
_CLOSE_WAIT_MS = 5000


class MainWindow(QMainWindow):
    """Top-level window of the PPTrepair desktop application.

    Hosts the source list panel, a run-options panel, an (initially
    hidden) progress row with a Cancel button, an (initially hidden)
    results panel, and a Scan/Repair action row, plus a File/Help menu
    bar and status bar. All user-facing strings are plain English
    literals for now; gettext-based translation is planned for a later
    milestone.
    """

    def __init__(self) -> None:
        """Build the window's central widget, menu bar and status bar."""
        super().__init__()
        self.setWindowTitle("PPTrepair")
        self.resize(900, 600)
        self.setAcceptDrops(True)

        #: The running worker, or None when idle. Only ever touched on
        #: the UI thread; doubles as the "is a scan running?" flag.
        self._scan_worker: ScanWorker | None = None
        self._files_diagnosed = 0
        self._materials_mined = 0

        self._sources = SourceListModel(self)
        self._build_central_widget()
        self._build_menu_bar()

        self._sources.rowsInserted.connect(self._sync_scan_button)
        self._sources.rowsRemoved.connect(self._sync_scan_button)
        self._sources.modelReset.connect(self._sync_scan_button)
        self._sync_scan_button()

        self.statusBar().showMessage("Ready")

    # -- construction --------------------------------------------------

    def _build_central_widget(self) -> None:
        """Assemble the stacked central layout.

        Top to bottom: source panel, run-options panel, progress row,
        results panel and the Scan/Repair action row.
        """
        central = QWidget()
        layout = QVBoxLayout(central)

        self._source_panel = SourcePanel(self._sources, self)
        self._source_panel.sources_added.connect(self._on_sources_added)
        layout.addWidget(self._source_panel, stretch=2)

        self._run_options = RunOptionsPanel()
        layout.addWidget(self._run_options)

        layout.addWidget(self._build_progress_row())

        self._results_panel = ResultsPanel()
        self._results_panel.hide()
        layout.addWidget(self._results_panel, stretch=3)

        layout.addLayout(self._build_action_row())

        self.setCentralWidget(central)

    def _build_progress_row(self) -> QWidget:
        """Return the indeterminate progress row (hidden until scanning)."""
        self._progress_row = QWidget()
        row = QHBoxLayout(self._progress_row)
        row.setContentsMargins(0, 0, 0, 0)

        self._progress_bar = QProgressBar()
        # range 0,0 renders an indeterminate "busy" animation: the total
        # file count is unknown until the walk completes.
        self._progress_bar.setRange(0, 0)
        row.addWidget(self._progress_bar)

        self._progress_label = QLabel("")
        row.addWidget(self._progress_label, stretch=1)

        self._cancel_button = QPushButton("Cancel")
        self._cancel_button.clicked.connect(self._cancel_scan)
        row.addWidget(self._cancel_button)

        self._progress_row.hide()
        return self._progress_row

    def _build_action_row(self) -> QHBoxLayout:
        """Return the bottom Scan/Repair button row."""
        row = QHBoxLayout()
        row.addStretch(1)

        self._scan_button = QPushButton("Scan")
        self._scan_button.setEnabled(False)
        self._scan_button.clicked.connect(self._start_scan)
        row.addWidget(self._scan_button)

        self._repair_button = QPushButton("Repair")
        self._repair_button.setEnabled(False)
        self._repair_button.setToolTip("Repair comes in a later milestone")
        row.addWidget(self._repair_button)

        return row

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
            f"PPTrepair {pptrepair.__version__}\n"
            "Diagnose and repair PowerPoint files corrupted while "
            "stored on OneDrive.\n"
            "Licensed under the GNU General Public License v3.0.",
        )

    # -- scan lifecycle ------------------------------------------------

    def _start_scan(self) -> None:
        """Build a :class:`ScanRequest` and start the background worker.

        Guarded against re-entry while a scan already runs (the Scan
        button is also disabled for the duration). Splits the sources
        into FILE/FOLDER roots and explicit ARCHIVE donor material.
        """
        if self._scan_worker is not None:
            return  # a scan is already running

        entries = self._sources.entries()
        roots = tuple(
            entry.path for entry in entries
            if entry.kind in (SourceKind.FILE, SourceKind.FOLDER)
        )
        archives = tuple(
            entry.path for entry in entries
            if entry.kind is SourceKind.ARCHIVE
        )
        if not roots and not archives:
            return

        request = ScanRequest(
            roots=roots,
            archives=archives,
            allow_download=self._run_options.allow_download(),
            max_file_bytes=self._run_options.max_file_bytes(),
        )

        self._files_diagnosed = 0
        self._materials_mined = 0
        self._results_panel.clear()
        self._results_panel.hide()

        worker = ScanWorker(request, self)
        worker.file_scanned.connect(self._on_file_scanned)
        worker.material_scanned.connect(self._on_material_scanned)
        worker.download_started.connect(self._on_download_started)
        worker.finished_ok.connect(self._on_scan_finished_ok)
        worker.failed.connect(self._on_scan_failed)
        worker.cancelled.connect(self._on_scan_cancelled)
        worker.finished.connect(self._on_worker_finished)
        self._scan_worker = worker

        self._set_running(True)
        self._progress_label.setText("Scanning…")
        self.statusBar().showMessage("Scanning…")
        worker.start()

    def _cancel_scan(self) -> None:
        """Ask the running worker to stop, if any."""
        if self._scan_worker is not None:
            self._cancel_button.setEnabled(False)
            self._scan_worker.cancel()
            self.statusBar().showMessage("Cancelling…")

    def _set_running(self, running: bool) -> None:
        """Toggle the UI between the idle and scanning states.

        Disables the source and run-options panels and shows the
        progress row while *running*; the Scan button's own enabled
        state is derived through :meth:`_sync_scan_button` (which also
        accounts for whether any sources are present).
        """
        self._source_panel.setEnabled(not running)
        self._run_options.set_enabled_for_running(running)
        self._progress_row.setVisible(running)
        if running:
            self._cancel_button.setEnabled(True)
        self._sync_scan_button()

    def _finish_ui(self, status_message: str) -> None:
        """Return the UI to the idle state after a scan ends.

        Called on every terminal outcome (success, cancel, failure);
        the worker object itself is cleaned up separately in
        :meth:`_on_worker_finished`.
        """
        self._set_running(False)
        self.statusBar().showMessage(status_message)

    def _sync_scan_button(self, *_args: object) -> None:
        """Enable Scan only when sources exist and no scan is running."""
        running = self._scan_worker is not None
        self._scan_button.setEnabled(
            self._sources.rowCount() > 0 and not running)

    # -- worker signal handlers (UI thread) ----------------------------

    def _on_file_scanned(self, outcome: object) -> None:
        """Count one diagnosed file and refresh the progress readout."""
        self._files_diagnosed += 1
        self._update_progress_label()
        path = getattr(outcome, "path", None)
        if path is not None:
            self.statusBar().showMessage(f"Diagnosed {Path(path).name}")

    def _on_material_scanned(self, material: object) -> None:
        """Count one mined archive member and refresh the readout."""
        self._materials_mined += 1
        self._update_progress_label()
        display = getattr(material, "display", None)
        if callable(display):
            self.statusBar().showMessage(f"Mined {display()}")

    def _on_download_started(self, path: object) -> None:
        """Report that a cloud-only placeholder is being downloaded."""
        self.statusBar().showMessage(f"Downloading {Path(path).name}…")

    def _update_progress_label(self) -> None:
        """Refresh the progress label from the running counters."""
        self._progress_label.setText(
            f"Scanning… {self._files_diagnosed} file(s) diagnosed, "
            f"{self._materials_mined} material(s) mined")

    def _on_scan_finished_ok(self, result: object) -> None:
        """Show the results and restore the idle UI after a clean run."""
        assert isinstance(result, GuiScanResult)
        self._results_panel.show_result(result)
        self._results_panel.show()
        self._finish_ui(self._results_panel.summary_text())

    def _on_scan_cancelled(self) -> None:
        """Restore the idle UI after a cancelled run."""
        self._finish_ui("Scan cancelled")

    def _on_scan_failed(self, message: str) -> None:
        """Report an unexpected worker failure and restore the idle UI."""
        self._finish_ui(f"Scan failed: {message}")
        QMessageBox.warning(
            self, "Scan failed",
            f"The scan stopped unexpectedly:\n{message}")

    def _on_worker_finished(self) -> None:
        """Drop the finished worker and re-evaluate the Scan button.

        Wired to :attr:`QThread.finished`, which fires after the
        worker's own terminal signal, so the result has already been
        consumed by the time the object is scheduled for deletion.
        """
        worker = self._scan_worker
        self._scan_worker = None
        if worker is not None:
            worker.deleteLater()
        self._sync_scan_button()

    # -- drag and drop / add reporting ---------------------------------

    def _on_sources_added(self, result: object) -> None:
        """Summarise a panel/menu add on the status bar."""
        assert isinstance(result, AddResult)
        self.statusBar().showMessage(self._format_add_result(result))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """Accept a drag that carries at least one local file URL.

        :param event: Qt's drag-enter event; not consumed when the
            drag carries no local file URL (e.g. dragged text).
        """
        if self._has_local_file_urls(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
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

    # -- shutdown ------------------------------------------------------

    def closeEvent(self, event: object) -> None:
        """Stop a running scan before the window closes.

        Requests cancellation and blocks (briefly) for the worker to
        unwind so the process never exits with a live scan thread. Runs
        on the UI thread.

        :param event: Qt's close event.
        """
        worker = self._scan_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            worker.wait(_CLOSE_WAIT_MS)
        super().closeEvent(event)
