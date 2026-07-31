"""The PPTrepair desktop application's main window.

Owns the whole scan and single-file-repair lifecycle on the UI thread:
it builds a :class:`~pptrepair.gui.worker.ScanRequest` /
:class:`~pptrepair.gui.repair_workers.RepairRequest` from the
accumulated sources, starts a :class:`~pptrepair.gui.worker.ScanWorker`
/ :class:`~pptrepair.gui.repair_workers.RepairWorker` on a background
thread, and reacts to that worker's signals (all delivered here on the
UI thread via Qt's queued connections) to stream progress, show
results, and re-enable the UI. Cancellation and window-close are
handled cooperatively so a running worker never leaves a dangling
thread.
"""

from __future__ import annotations

import contextlib
import tempfile
from collections.abc import Sequence
from pathlib import Path

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import pptrepair
from pptrepair.batch import BatchResult
from pptrepair.gui.donor_dialog import DonorApprovalDialog
from pptrepair.gui.i18n import tr
from pptrepair.gui.merge_plan import build_target_plans
from pptrepair.gui.repair_workers import (
    MultiRepairRequest,
    MultiRepairResult,
    MultiRepairWorker,
    RepairRequest,
    RepairWorker,
)
from pptrepair.gui.results import ResultsPanel
from pptrepair.gui.run_options import RepairMode, RunOptionsPanel
from pptrepair.gui.settings import Settings, SettingsDialog
from pptrepair.gui.source_panel import SourcePanel
from pptrepair.gui.sources import (
    AddResult,
    RejectedSource,
    RejectReason,
    SourceKind,
    SourceListModel,
)
from pptrepair.gui.worker import GuiScanResult, ScanRequest, ScanWorker
from pptrepair.report import ISSUE_URL
from pptrepair.scan import ArchiveMaterialCache

#: How long (ms) window-close waits for a running worker to stop.
_CLOSE_WAIT_MS = 5000

#: The project's GitHub repository, opened by the Help menu's
#: "Open GitHub Page" action.
_GITHUB_URL = "https://github.com/xhighhongo41/PPTrepair"


class MainWindow(QMainWindow):
    """Top-level window of the PPTrepair desktop application.

    Hosts the source list panel, a run-options panel, an (initially
    hidden) progress row with a Cancel button, an (initially hidden)
    results panel, and a Scan/Repair/Open Output Folder action row, plus
    a File/Run/Settings/Help menu bar and status bar. Every user-facing
    string is passed through :func:`~pptrepair.gui.i18n.tr`.
    """

    def __init__(self) -> None:
        """Build the window's central widget, menu bar and status bar.

        Also opens the session-lifetime archive material cache the scan
        and repair workers share (see :attr:`_material_cache`), which
        lives exactly as long as this window does.
        """
        super().__init__()
        self.setWindowTitle("PPTrepair")
        self.resize(900, 600)
        self.setAcceptDrops(True)

        #: The running scan worker, or None when idle. Only ever touched
        #: on the UI thread; doubles as the "is a scan running?" flag.
        self._scan_worker: ScanWorker | None = None
        self._files_diagnosed = 0
        self._materials_mined = 0

        #: The running repair worker, or None when idle. Only ever
        #: touched on the UI thread; doubles as the "is a repair
        #: running?" flag. A scan and a repair never run concurrently.
        self._repair_worker: RepairWorker | None = None
        self._repair_checked = 0
        self._repair_processed = 0

        #: The running multi-source (merge) repair worker, or None when
        #: idle. Only ever touched on the UI thread; a scan, a single
        #: repair and a multi-source repair never run concurrently.
        self._multi_repair_worker: MultiRepairWorker | None = None
        self._merge_processed = 0
        self._merge_total = 0
        self._fallback_processed = 0
        self._fallback_total = 0
        #: The folder "Open Output Folder" should reveal, set right
        #: before starting a repair (see :meth:`_compute_open_target`);
        #: None until the first repair of this session starts.
        self._repair_open_target: Path | None = None

        #: Session-lifetime scratch directory backing
        #: :attr:`_material_cache`, and the cache itself. Every archive
        #: member mined by a scan is kept here so the repair phase can
        #: splice a donor's bytes without re-reading (and, for a
        #: compressed tar, re-decompressing) the whole backup, and so a
        #: rescan of the same archive costs nothing. The window owns
        #: both: they are created here and removed in
        #: :meth:`closeEvent`.
        self._cache_dir = tempfile.TemporaryDirectory(
            prefix="pptrepair-cache-")
        self._material_cache = ArchiveMaterialCache(Path(self._cache_dir.name))

        #: Persisted preferences (QSettings-backed), loaded once here
        #: and applied to the run-options panel below; also reached
        #: through the Preferences dialog and the recent-folders menu.
        self._settings = Settings()

        self._sources = SourceListModel(self)
        self._build_central_widget()
        self._build_menu_bar()

        self._sources.rowsInserted.connect(self._on_sources_changed)
        self._sources.rowsRemoved.connect(self._on_sources_changed)
        self._sources.modelReset.connect(self._on_sources_changed)
        # Re-evaluate the Repair button the instant the user switches
        # repair modes, through RunOptionsPanel's own public signal.
        self._run_options.mode_changed.connect(self._sync_repair_button)
        self._sync_scan_button()
        self._sync_repair_button()

        self.statusBar().showMessage(tr("Ready"))

    # -- construction --------------------------------------------------

    def _build_central_widget(self) -> None:
        """Assemble the stacked central layout.

        Top to bottom: source panel, run-options panel, progress row,
        results panel and the Scan/Repair/Open Output Folder action row.
        """
        central = QWidget()
        layout = QVBoxLayout(central)

        self._source_panel = SourcePanel(self._sources, self)
        self._source_panel.sources_added.connect(self._on_sources_added)
        layout.addWidget(self._source_panel, stretch=2)

        self._run_options = RunOptionsPanel()
        self._run_options.apply_settings(self._settings)
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

        self._cancel_button = QPushButton(tr("Cancel"))
        self._cancel_button.clicked.connect(self._cancel_scan)
        row.addWidget(self._cancel_button)

        self._progress_row.hide()
        return self._progress_row

    def _build_action_row(self) -> QHBoxLayout:
        """Return the bottom Scan/Repair/Open Output Folder button row."""
        row = QHBoxLayout()
        row.addStretch(1)

        self._scan_button = QPushButton(tr("Scan"))
        self._scan_button.setEnabled(False)
        self._scan_button.clicked.connect(self._start_scan)
        row.addWidget(self._scan_button)

        self._repair_button = QPushButton(tr("Repair"))
        self._repair_button.setEnabled(False)
        self._repair_button.clicked.connect(self._start_repair)
        row.addWidget(self._repair_button)

        # Hidden until a repair completes successfully (see
        # _on_repair_finished_ok); there is nothing to open before then.
        self._open_output_button = QPushButton(tr("Open Output Folder"))
        self._open_output_button.setEnabled(False)
        self._open_output_button.hide()
        self._open_output_button.clicked.connect(self._open_output_folder)
        row.addWidget(self._open_output_button)

        return row

    def _build_menu_bar(self) -> None:
        """Populate the menu bar's File, Run, Settings and Help menus."""
        file_menu = self.menuBar().addMenu(tr("File"))

        add_files_action = QAction(tr("Add Files…"), self)
        add_files_action.setShortcut(QKeySequence.StandardKey.Open)
        add_files_action.triggered.connect(self._source_panel.add_files)
        file_menu.addAction(add_files_action)

        add_folder_action = QAction(tr("Add Folder…"), self)
        add_folder_action.triggered.connect(self._source_panel.add_folder)
        file_menu.addAction(add_folder_action)

        self._recent_folders_menu = self._build_recent_folders_menu(file_menu)

        file_menu.addAction(self._build_separator())

        clear_sources_action = QAction(tr("Clear Sources"), self)
        clear_sources_action.triggered.connect(self._sources.clear)
        file_menu.addAction(clear_sources_action)

        file_menu.addAction(self._build_separator())

        quit_action = QAction(tr("Quit"), self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        self._build_run_menu()

        settings_menu = self.menuBar().addMenu(tr("Settings"))
        preferences_action = QAction(tr("Preferences…"), self)
        preferences_action.setShortcut(QKeySequence.StandardKey.Preferences)
        preferences_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        preferences_action.triggered.connect(self._show_preferences_dialog)
        settings_menu.addAction(preferences_action)

        help_menu = self.menuBar().addMenu(tr("Help"))

        quick_guide_action = QAction(tr("Quick Guide"), self)
        quick_guide_action.triggered.connect(self._show_quick_guide_dialog)
        help_menu.addAction(quick_guide_action)

        help_menu.addAction(self._build_separator())

        github_action = QAction(tr("Open GitHub Page"), self)
        github_action.triggered.connect(self._open_github_page)
        help_menu.addAction(github_action)

        report_action = QAction(tr("Report Unknown Corruption…"), self)
        report_action.triggered.connect(self._open_report_issue_page)
        help_menu.addAction(report_action)

        help_menu.addAction(self._build_separator())

        license_action = QAction(tr("License Information"), self)
        license_action.triggered.connect(self._show_license_dialog)
        help_menu.addAction(license_action)

        about_action = QAction(tr("About PPTrepair"), self)
        about_action.setMenuRole(QAction.MenuRole.AboutRole)
        about_action.triggered.connect(self._show_about_dialog)
        help_menu.addAction(about_action)

    def _build_recent_folders_menu(self, file_menu: QMenu) -> QMenu:
        """Return the "Recent Folders" submenu, rebuilt on every show.

        Built through ``file_menu.addMenu(title)`` -- which both
        inserts and returns a *new*, ``file_menu``-owned ``QMenu`` --
        rather than constructing a standalone :class:`QMenu` and
        handing it to ``addMenu(QMenu)``: the latter's own returned
        (and here otherwise-unused) ``QAction`` has been observed to
        end up in an invalid, already-deleted state, corrupting later
        traversal of ``file_menu.actions()`` -- the same class of
        issue :meth:`_build_separator` already works around.

        Wired to :attr:`QMenu.aboutToShow` rather than kept in sync
        incrementally, so its contents always reflect the latest
        :attr:`_settings` state regardless of what changed it.

        :param file_menu: the File menu to add this submenu to.
        """
        menu = file_menu.addMenu(tr("Recent Folders"))
        menu.aboutToShow.connect(self._rebuild_recent_folders_menu)
        return menu

    def _rebuild_recent_folders_menu(self) -> None:
        """Repopulate the Recent Folders submenu from :attr:`_settings`."""
        menu = self._recent_folders_menu
        menu.clear()

        folders = self._settings.recent_folders()
        if not folders:
            empty_action = QAction(tr("(empty)"), menu)
            empty_action.setEnabled(False)
            menu.addAction(empty_action)
            return

        for folder in folders:
            action = QAction(folder, menu)
            # Bind *folder* as a default argument so every action
            # closes over its own path rather than the loop variable.
            action.triggered.connect(
                lambda checked=False, folder=folder:
                self._add_recent_folder(folder))
            menu.addAction(action)

        menu.addAction(self._build_separator())
        clear_action = QAction(tr("Clear Menu"), menu)
        clear_action.triggered.connect(self._settings.clear_recent_folders)
        menu.addAction(clear_action)

    def _build_run_menu(self) -> None:
        """Build the Run menu, mirroring the Scan/Repair/Cancel buttons.

        Every action shares the button's own slot, and
        :meth:`_sync_scan_button`/:meth:`_set_running` keep the
        actions' enabled state in lockstep with the buttons'.
        """
        run_menu = self.menuBar().addMenu(tr("Run"))

        self._scan_action = QAction(tr("Scan"), self)
        self._scan_action.setShortcut(QKeySequence("Ctrl+R"))
        self._scan_action.setEnabled(False)
        self._scan_action.triggered.connect(self._start_scan)
        run_menu.addAction(self._scan_action)

        self._repair_action = QAction(tr("Repair"), self)
        self._repair_action.setEnabled(False)
        self._repair_action.triggered.connect(self._start_repair)
        run_menu.addAction(self._repair_action)

        self._cancel_action = QAction(tr("Cancel"), self)
        self._cancel_action.setShortcut(QKeySequence("Esc"))
        self._cancel_action.setEnabled(False)
        self._cancel_action.triggered.connect(self._cancel_scan)
        run_menu.addAction(self._cancel_action)

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

    def _show_quick_guide_dialog(self) -> None:
        """Show a short usage guide covering the scan/repair workflow."""
        QMessageBox.information(
            self,
            tr("Quick Guide"),
            tr("PPTrepair diagnoses and repairs PowerPoint files damaged "
               "while stored on OneDrive.")
            + "\n\n"
            + tr("1. Drop .pptx/.pptm files onto the window, or add a "
                 "folder (scanned recursively for matching files).")
            + "\n"
            + tr("2. Backup archives can also be dropped as donor "
                 "material for multi-source repair.")
            + "\n"
            + tr("3. Click Scan to diagnose everything, then review the "
                 "Files and Candidates tabs.")
            + "\n"
            + tr("4. Pick Single-file or Multi-source repair mode.")
            + "\n"
            + tr("5. Choose in-place or folder output, then click "
                 "Repair.")
            + "\n"
            + tr("6. Multi-source repair asks you to approve donors "
                 "mined from other copies and archives before "
                 "repairing."),
        )

    def _open_github_page(self) -> None:
        """Open the PPTrepair GitHub repository in the default browser."""
        QDesktopServices.openUrl(QUrl(_GITHUB_URL))

    def _open_report_issue_page(self) -> None:
        """Open a GitHub issue template for reporting unknown corruption.

        Points at :data:`pptrepair.report.ISSUE_URL`, the same template
        the CLI's own reports refer users to.
        """
        QDesktopServices.openUrl(QUrl(ISSUE_URL))

    def _show_license_dialog(self) -> None:
        """Show PPTrepair's and its GUI dependency's licensing terms."""
        QMessageBox.information(
            self,
            tr("License Information"),
            tr("PPTrepair is licensed under the GNU General Public "
               "License v3.0.")
            + "\n\n"
            + tr("This application uses Qt for Python (PySide6), "
                 "licensed under the GNU LGPL v3.")
            + "\n\n"
            + tr("See the LICENSE file in the GitHub repository for "
                 "details."),
        )

    def _show_about_dialog(self) -> None:
        """Show an About dialog with the app name, version and license."""
        QMessageBox.about(
            self,
            tr("About PPTrepair"),
            f"PPTrepair {pptrepair.__version__}\n"
            + tr("Diagnose and repair PowerPoint files corrupted while "
                 "stored on OneDrive.")
            + "\n"
            + tr("Licensed under the GNU General Public License v3.0."),
        )

    def _show_preferences_dialog(self) -> None:
        """Open the Preferences dialog and re-apply the run options on OK.

        Cancelling the dialog leaves :attr:`_settings` -- and hence the
        run-options panel -- untouched. When the language was changed
        and the dialog was accepted, the status bar tells the user the
        change only takes effect after restarting the application,
        since already-built widgets are never retranslated (see
        :mod:`pptrepair.gui.i18n`).
        """
        previous_language = self._settings.language()
        dialog = SettingsDialog(self._settings, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._run_options.apply_settings(self._settings)
            if self._settings.language() != previous_language:
                self.statusBar().showMessage(
                    tr("Language change takes effect after restart"))

    # -- scan lifecycle ------------------------------------------------

    def _start_scan(self) -> None:
        """Build a :class:`ScanRequest` and start the background worker.

        Guarded against re-entry while a scan or repair already runs
        (the Scan button is also disabled for the duration). Splits the
        sources into FILE/FOLDER roots and explicit ARCHIVE donor
        material.
        """
        if self._busy():
            return  # a scan or repair is already running

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
            follow_symlinks=self._settings.follow_symlinks(),
            allow_download=self._run_options.allow_download(),
            max_file_bytes=self._run_options.max_file_bytes(),
            ignore_hidden=self._run_options.ignore_hidden(),
        )

        self._files_diagnosed = 0
        self._materials_mined = 0
        self._results_panel.clear()
        self._results_panel.hide()

        worker = ScanWorker(request, self, cache=self._material_cache)
        worker.walk_progress.connect(self._on_walk_progress)
        worker.archive_progress.connect(self._on_archive_progress)
        worker.file_scanned.connect(self._on_file_scanned)
        worker.material_scanned.connect(self._on_material_scanned)
        worker.download_started.connect(self._on_download_started)
        worker.finished_ok.connect(self._on_scan_finished_ok)
        worker.failed.connect(self._on_scan_failed)
        worker.cancelled.connect(self._on_scan_cancelled)
        worker.finished.connect(self._on_worker_finished)
        self._scan_worker = worker

        self._set_running(True)
        self._progress_label.setText(tr("Scanning…"))
        self.statusBar().showMessage(tr("Scanning…"))
        worker.start()

    def _busy(self) -> bool:
        """Return True while any scan/repair worker is running.

        The single "is anything running?" predicate the action buttons,
        the start guards and the cancel path all share, so a scan, a
        single-file repair and a multi-source repair are mutually
        exclusive.
        """
        return (self._scan_worker is not None
                or self._repair_worker is not None
                or self._multi_repair_worker is not None)

    def _on_sources_changed(self, *_args: object) -> None:
        """Re-evaluate the Scan button and drop a now-stale scan result.

        Wired to the source model's row-insert/remove/reset signals. When
        the source list changes after a scan produced a result, that
        result no longer describes the current sources, so it is cleared
        (disabling Repair) and the user is told to scan again -- this
        prevents a repair from acting on a stale scan. A change while a
        scan/repair is running, or before any result exists, only
        refreshes the Scan button.
        """
        self._sync_scan_button()
        if not self._busy() and self._results_panel.last_result() is not None:
            self._results_panel.clear()
            self._results_panel.hide()
            self.statusBar().showMessage(
                tr("Sources changed — please scan again"))
        self._sync_repair_button()

    def _cancel_scan(self) -> None:
        """Ask the running worker (scan or repair) to stop, if any."""
        worker = (self._scan_worker or self._repair_worker
                  or self._multi_repair_worker)
        if worker is not None:
            self._cancel_button.setEnabled(False)
            self._cancel_action.setEnabled(False)
            worker.cancel()
            self.statusBar().showMessage(tr("Cancelling…"))

    def _set_running(self, running: bool) -> None:
        """Toggle the UI between the idle and running (scan/repair) states.

        Disables the source and run-options panels and shows the
        progress row while *running*; the Scan/Repair buttons' (and
        their Run menu counterparts') own enabled state is derived
        through :meth:`_sync_scan_button`/:meth:`_sync_repair_button`
        (which also account for sources present / a repairable scan
        result / the selected repair mode).
        """
        self._source_panel.setEnabled(not running)
        self._run_options.set_enabled_for_running(running)
        self._progress_row.setVisible(running)
        if running:
            self._cancel_button.setEnabled(True)
            self._cancel_action.setEnabled(True)
        self._sync_scan_button()
        self._sync_repair_button()

    def _finish_ui(self, status_message: str) -> None:
        """Return the UI to the idle state after a scan or repair ends.

        Called on every terminal outcome (success, cancel, failure);
        the worker object itself is cleaned up separately in
        :meth:`_on_worker_finished`/:meth:`_on_repair_worker_finished`.
        """
        self._set_running(False)
        self.statusBar().showMessage(status_message)

    def _sync_scan_button(self, *_args: object) -> None:
        """Enable Scan (button and Run-menu action) with sources present.

        Enabled only when at least one source is accumulated and neither
        a scan nor a repair is currently running.
        """
        enabled = self._sources.rowCount() > 0 and not self._busy()
        self._scan_button.setEnabled(enabled)
        self._scan_action.setEnabled(enabled)

    # -- worker signal handlers (UI thread) ----------------------------

    def _on_walk_progress(self, path: str) -> None:
        """Report which directory the discovery walk is currently visiting.

        Fires during the otherwise-silent discovery phase, before any
        file has been diagnosed; the initial :meth:`_start_scan`
        ``tr("Scanning…")`` message is overwritten by this once the
        walk reaches its first (throttled) directory.
        """
        self.statusBar().showMessage(tr("Scanning {path}…").format(path=path))

    def _on_archive_progress(self, path: str, done: int, total: int) -> None:
        """Report how far into a backup archive the mining pass has got.

        The only feedback available while a single huge archive is being
        read, where minutes can pass between two mined members. *total*
        is the archive's size in bytes and *done* the position reached
        in it; a *total* of 0 means the size could not be determined
        (see :func:`pptrepair.archive.iter_materialized_members`), so
        the percentage is dropped rather than faked.

        :param path: the archive being read, named to the user by its
            file name alone.
        :param done: bytes of *path* processed so far.
        :param total: *path*'s total size in bytes, or 0 when unknown.
        """
        name = Path(path).name
        if total > 0:
            self.statusBar().showMessage(
                tr("Reading archive {name}… {percent}%").format(
                    name=name, percent=done * 100 // total))
        else:
            self.statusBar().showMessage(
                tr("Reading archive {name}…").format(name=name))

    def _on_file_scanned(self, outcome: object) -> None:
        """Count one diagnosed file and refresh the progress readout."""
        self._files_diagnosed += 1
        self._update_progress_label()
        path = getattr(outcome, "path", None)
        if path is not None:
            self.statusBar().showMessage(
                tr("Diagnosed {name}").format(name=Path(path).name))

    def _on_material_scanned(self, material: object) -> None:
        """Count one mined archive member and refresh the readout."""
        self._materials_mined += 1
        self._update_progress_label()
        display = getattr(material, "display", None)
        if callable(display):
            self.statusBar().showMessage(
                tr("Mined {name}").format(name=display()))

    def _on_download_started(self, path: object) -> None:
        """Report that a cloud-only placeholder is being downloaded."""
        self.statusBar().showMessage(
            tr("Downloading {name}…").format(name=Path(path).name))

    def _update_progress_label(self) -> None:
        """Refresh the progress label from the running counters."""
        self._progress_label.setText(
            tr("Scanning… {files} file(s) diagnosed, "
               "{materials} material(s) mined").format(
                   files=self._files_diagnosed,
                   materials=self._materials_mined))

    def _on_scan_finished_ok(self, result: object) -> None:
        """Show the results and restore the idle UI after a clean run."""
        assert isinstance(result, GuiScanResult)
        self._results_panel.show_result(result)
        self._results_panel.show()
        self._finish_ui(self._results_panel.summary_text())

    def _on_scan_cancelled(self) -> None:
        """Restore the idle UI after a cancelled run."""
        self._finish_ui(tr("Scan cancelled"))

    def _on_scan_failed(self, message: str) -> None:
        """Report an unexpected worker failure and restore the idle UI."""
        self._finish_ui(tr("Scan failed: {message}").format(message=message))
        QMessageBox.warning(
            self, tr("Scan failed"),
            tr("The scan stopped unexpectedly:\n{message}").format(
                message=message))

    def _on_worker_finished(self) -> None:
        """Drop the finished scan worker and re-evaluate the action buttons.

        Wired to :attr:`QThread.finished`, which fires after the
        worker's own terminal signal, so the result has already been
        consumed by the time the object is scheduled for deletion.
        """
        worker = self._scan_worker
        self._scan_worker = None
        if worker is not None:
            worker.deleteLater()
        self._sync_scan_button()
        self._sync_repair_button()

    # -- repair lifecycle ------------------------------------------------

    def _start_repair(self) -> None:
        """Dispatch to the single-file or multi-source repair flow.

        Guarded against re-entry while a scan or repair already runs (the
        Repair button/action are already disabled then, but the guard
        stays here too since this slot doubles as a Run-menu handler).
        The run-options panel's current repair mode chooses the flow.
        """
        if self._busy():
            return  # a scan or repair is already running
        if self._run_options.repair_mode() is RepairMode.MULTI:
            self._start_multi_repair()
        else:
            self._start_single_repair()

    def _start_single_repair(self) -> None:
        """Build a :class:`RepairRequest` and start the background worker.

        Splits the sources into FILE/FOLDER roots to diagnose and repair;
        an explicit ARCHIVE source is reported on the status bar and
        otherwise ignored (donor material is a multi-source concern).
        """
        entries = self._sources.entries()
        roots = tuple(
            entry.path for entry in entries
            if entry.kind in (SourceKind.FILE, SourceKind.FOLDER)
        )
        if not roots:
            return
        has_archives = any(
            entry.kind is SourceKind.ARCHIVE for entry in entries)

        in_place = self._run_options.in_place()
        output_dir = self._run_options.output_dir()
        if not in_place and output_dir is None:
            QMessageBox.warning(
                self, tr("No output folder"),
                tr('Choose an output folder before repairing, or switch '
                   'to "Repair in place".'))
            return

        request = RepairRequest(
            roots=roots,
            output_dir=output_dir,
            in_place=in_place,
            follow_symlinks=self._settings.follow_symlinks(),
            allow_download=self._run_options.allow_download(),
            max_file_bytes=self._run_options.max_file_bytes(),
            lang=self._settings.language(),
        )
        self._repair_open_target = self._compute_open_target(
            roots, in_place, output_dir)

        self._repair_checked = 0
        self._repair_processed = 0
        self._open_output_button.setEnabled(False)
        self._open_output_button.hide()

        worker = RepairWorker(request, self)
        worker.file_scanned.connect(self._on_repair_file_scanned)
        worker.file_repaired.connect(self._on_repair_file_repaired)
        worker.download_started.connect(self._on_download_started)
        worker.finished_ok.connect(self._on_repair_finished_ok)
        worker.failed.connect(self._on_repair_failed)
        worker.cancelled.connect(self._on_repair_cancelled)
        worker.finished.connect(self._on_repair_worker_finished)
        self._repair_worker = worker

        self._set_running(True)
        self._progress_label.setText(tr("Repairing…"))
        if has_archives:
            self.statusBar().showMessage(
                tr("Archives are used only by multi-source repair; "
                   "ignored in this mode"))
        else:
            self.statusBar().showMessage(tr("Repairing…"))
        worker.start()

    @staticmethod
    def _compute_open_target(
        roots: tuple[Path, ...], in_place: bool, output_dir: Path | None
    ) -> Path:
        """Return the folder "Open Output Folder" should reveal for a run.

        The aggregate output directory in aggregate mode; in *in_place*
        mode, the first root itself when it is a directory, else that
        root's own parent directory (a file root's artifact is written
        next to the file, not inside it).

        :param roots: the run's FILE/FOLDER roots, as passed to
            :class:`RepairRequest` (never empty).
        :param in_place: the run's :attr:`RepairRequest.in_place`.
        :param output_dir: the run's :attr:`RepairRequest.output_dir`
            (required, i.e. not None, when *in_place* is False).
        """
        if not in_place:
            assert output_dir is not None
            return output_dir
        first = roots[0]
        return first if first.is_dir() else first.parent

    def _on_repair_file_scanned(self, outcome: object) -> None:
        """Count one diagnosed file during a repair's checking phase."""
        self._repair_checked += 1
        self._progress_label.setText(
            tr("Checking… {n} file(s)").format(n=self._repair_checked))
        path = getattr(outcome, "path", None)
        if path is not None:
            self.statusBar().showMessage(
                tr("Checking {name}").format(name=Path(path).name))

    def _on_repair_file_repaired(self, item: object) -> None:
        """Count one processed file during a repair's repairing phase."""
        self._repair_processed += 1
        self._progress_label.setText(
            tr("Repairing… {n} file(s) processed").format(
                n=self._repair_processed))
        # item.action is a machine-facing code (matching the core batch
        # module's own convention), shown as-is rather than translated.
        action = getattr(item, "action", None)
        source = getattr(item, "source", None)
        path = getattr(source, "path", None) if source is not None else None
        if path is not None and action is not None:
            self.statusBar().showMessage(f"{action}: {Path(path).name}")

    def _on_repair_finished_ok(self, result: object) -> None:
        """Show the repair results and restore the idle UI after a clean run."""
        assert isinstance(result, BatchResult)
        self._results_panel.show_repair_result(result)
        self._results_panel.show()
        self._open_output_button.setEnabled(True)
        self._open_output_button.show()
        self._finish_ui(self._results_panel.summary_text())

    def _on_repair_cancelled(self) -> None:
        """Restore the idle UI after a cancelled repair.

        Any artifact already written before the cancellation point is
        left in place (see :meth:`RepairWorker.cancel`'s own contract);
        this status message deliberately does not claim otherwise.
        """
        self._finish_ui(tr("Repair cancelled"))

    def _on_repair_failed(self, message: str) -> None:
        """Report an unexpected repair-worker failure and restore the UI."""
        self._finish_ui(
            tr("Repair failed: {message}").format(message=message))
        QMessageBox.warning(
            self, tr("Repair failed"),
            tr("The repair stopped unexpectedly:\n{message}").format(
                message=message))

    def _on_repair_worker_finished(self) -> None:
        """Drop the finished repair worker and re-evaluate the action buttons.

        Wired to :attr:`QThread.finished`, which fires after the
        worker's own terminal signal, so the result has already been
        consumed by the time the object is scheduled for deletion.
        """
        worker = self._repair_worker
        self._repair_worker = None
        if worker is not None:
            worker.deleteLater()
        self._sync_scan_button()
        self._sync_repair_button()

    # -- multi-source (merge) repair lifecycle -------------------------

    def _start_multi_repair(self) -> None:
        """Review donors, then start a :class:`MultiRepairWorker`.

        Computes the per-target donor plans from the last scan, shows the
        :class:`~pptrepair.gui.donor_dialog.DonorApprovalDialog` for the
        user to confirm, and -- on acceptance -- splits the approved plans
        into merges (a target with at least one checked donor) and
        donor-less fallbacks (repaired on their own bytes), then launches
        the worker. Returns without starting anything if there is nothing
        corrupted to repair, no output folder was chosen, or the dialog
        was cancelled.
        """
        scan_result = self._results_panel.last_result()
        if scan_result is None or scan_result.scan is None:
            return

        plans = build_target_plans(scan_result)
        if not plans:
            QMessageBox.information(
                self, tr("Nothing to repair"),
                tr("The last scan found no corrupted files to repair."))
            return

        in_place = self._run_options.in_place()
        output_dir = self._run_options.output_dir()
        if not in_place and output_dir is None:
            QMessageBox.warning(
                self, tr("No output folder"),
                tr('Choose an output folder before repairing, or switch '
                   'to "Repair in place".'))
            return

        dialog = DonorApprovalDialog(plans, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        approved = dialog.approved_plans()

        merges = tuple(plan for plan in approved if plan.donors)
        fallback_targets = tuple(
            plan.target for plan in approved if not plan.donors)
        roots = tuple(Path(root) for root in scan_result.scan.roots)

        request = MultiRepairRequest(
            merges=merges,
            fallback_targets=fallback_targets,
            roots=roots,
            output_dir=output_dir,
            in_place=in_place,
            lang=self._settings.language(),
        )
        self._repair_open_target = self._compute_open_target(
            roots, in_place, output_dir)

        self._merge_processed = 0
        self._merge_total = len(merges)
        self._fallback_processed = 0
        self._fallback_total = len(fallback_targets)
        self._open_output_button.setEnabled(False)
        self._open_output_button.hide()

        worker = MultiRepairWorker(request, self, cache=self._material_cache)
        worker.merge_done.connect(self._on_merge_done)
        worker.file_repaired.connect(self._on_multi_file_repaired)
        worker.finished_ok.connect(self._on_multi_finished_ok)
        worker.failed.connect(self._on_multi_failed)
        worker.cancelled.connect(self._on_multi_cancelled)
        worker.finished.connect(self._on_multi_worker_finished)
        self._multi_repair_worker = worker

        self._set_running(True)
        self._progress_label.setText(tr("Merging…"))
        self.statusBar().showMessage(tr("Repairing…"))
        worker.start()

    def _on_merge_done(self, item: object) -> None:
        """Count one finished merge and refresh the progress readout."""
        self._merge_processed += 1
        self._progress_label.setText(
            tr("Merging… {done}/{total}").format(
                done=self._merge_processed, total=self._merge_total))
        target = getattr(item, "target", None)
        success = getattr(item, "success", None)
        if target is not None:
            # "merged"/"merge failed" mirror the Repair tab's own action
            # text for this run (see results.py's _merge_row_for_item)
            # and stay untranslated for the same reason.
            verb = "merged" if success else "merge failed"
            self.statusBar().showMessage(f"{verb}: {Path(target).name}")

    def _on_multi_file_repaired(self, outcome: object) -> None:
        """Count one finished fallback repair and refresh the readout."""
        self._fallback_processed += 1
        self._progress_label.setText(
            tr("Repairing… {done}/{total}").format(
                done=self._fallback_processed, total=self._fallback_total))
        src = getattr(outcome, "src", None)
        if src is not None:
            self.statusBar().showMessage(
                tr("Repaired {name}").format(name=Path(src).name))

    def _on_multi_finished_ok(self, result: object) -> None:
        """Show the merge results and restore the idle UI after a clean run."""
        assert isinstance(result, MultiRepairResult)
        self._results_panel.show_multi_repair_result(result)
        self._results_panel.show()
        self._open_output_button.setEnabled(True)
        self._open_output_button.show()
        self._finish_ui(self._results_panel.summary_text())

    def _on_multi_cancelled(self) -> None:
        """Restore the idle UI after a cancelled multi-source repair.

        Every artifact already written before the cancellation point is
        left in place (see :meth:`MultiRepairWorker.cancel`'s contract);
        this status message deliberately does not claim otherwise.
        """
        self._finish_ui(tr("Repair cancelled"))

    def _on_multi_failed(self, message: str) -> None:
        """Report an unexpected multi-repair failure and restore the UI."""
        self._finish_ui(
            tr("Repair failed: {message}").format(message=message))
        QMessageBox.warning(
            self, tr("Repair failed"),
            tr("The repair stopped unexpectedly:\n{message}").format(
                message=message))

    def _on_multi_worker_finished(self) -> None:
        """Drop the finished multi-repair worker and re-sync the buttons.

        Wired to :attr:`QThread.finished`, which fires after the worker's
        own terminal signal, so the result has already been consumed by
        the time the object is scheduled for deletion.
        """
        worker = self._multi_repair_worker
        self._multi_repair_worker = None
        if worker is not None:
            worker.deleteLater()
        self._sync_scan_button()
        self._sync_repair_button()

    def _sync_repair_button(self, *_args: object) -> None:
        """Enable Repair (button and Run-menu action) when a run can start.

        Enabled only when the last scan result (see
        :meth:`~pptrepair.gui.results.ResultsPanel.last_result`) found at
        least one corrupted on-disk file and neither a scan nor a repair
        is currently running. Both repair modes -- single-file and
        multi-source -- can start from the same result, so the mode no
        longer gates the button.
        """
        scan_result = self._results_panel.last_result()
        corrupted = 0
        if scan_result is not None and scan_result.scan is not None:
            corrupted = len(scan_result.scan.corrupted())

        enabled = corrupted > 0 and not self._busy()
        self._repair_button.setEnabled(enabled)
        self._repair_action.setEnabled(enabled)

    def _open_output_folder(self) -> None:
        """Reveal the last completed repair's output folder.

        A no-op before any repair has completed in this session (see
        :attr:`_repair_open_target`); the button itself stays hidden
        until then, so this only guards direct calls (e.g. from tests).
        """
        if self._repair_open_target is not None:
            QDesktopServices.openUrl(
                QUrl.fromLocalFile(str(self._repair_open_target)))

    # -- drag and drop / add reporting ---------------------------------

    def _on_sources_added(self, result: object) -> None:
        """Summarise a panel/menu add on the status bar."""
        assert isinstance(result, AddResult)
        self._register_add_result(result)

    def _add_recent_folder(self, folder: str) -> None:
        """Add *folder* to the sources, chosen from the Recent Folders menu.

        :param folder: one of the paths returned by
            :meth:`~pptrepair.gui.settings.Settings.recent_folders`.
        """
        result = self._sources.add_paths([Path(folder)])
        self._register_add_result(result)

    def _register_add_result(self, result: AddResult) -> None:
        """Report *result* on the status bar and remember any new folders.

        Shared by every way sources can be added -- drag-and-drop, the
        "Add Files…"/"Add Folder…" dialogs and the Recent Folders
        submenu -- so a freshly added folder is pushed onto
        :attr:`_settings`'s most-recently-used list exactly once,
        regardless of how it was added. A path already present (a
        duplicate, per :attr:`AddResult.duplicates`) is left where it
        is in that list. Any rejected source (per
        :attr:`AddResult.rejected`) is additionally reported through
        :meth:`_show_reject_details`, since the status bar's one-line
        summary has no room for individual reasons.

        :param result: the outcome of one
            :meth:`~pptrepair.gui.sources.SourceListModel.add_paths` call.
        """
        self.statusBar().showMessage(self._format_add_result(result))
        for entry in result.added:
            if entry.kind is SourceKind.FOLDER:
                self._settings.push_recent_folder(entry.path)
        if result.rejected:
            self._show_reject_details(result.rejected)

    def _show_reject_details(
        self, rejected: Sequence[RejectedSource]
    ) -> None:
        """Show a dialog detailing why each path in *rejected* was skipped.

        A thin wrapper around :func:`QMessageBox.warning` -- kept as its
        own method so tests can monkeypatch it and record its calls
        without having to drive a real modal dialog.

        :param rejected: the rejected sources to report, as returned by
            :meth:`~pptrepair.gui.sources.SourceListModel.add_paths`.
        """
        QMessageBox.warning(
            self, tr("Rejected sources"),
            self._format_reject_details(rejected))

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
        self._register_add_result(result)

    @staticmethod
    def _has_local_file_urls(mime_data: QMimeData) -> bool:
        """Return True when *mime_data* carries a local file URL.

        :param mime_data: the drag/drop event's MIME data.
        """
        if not mime_data.hasUrls():
            return False
        return any(url.isLocalFile() for url in mime_data.urls())

    @staticmethod
    def _format_reject_details(rejected: Sequence[RejectedSource]) -> str:
        """Render one reason line per rejected source, newline-joined.

        :param rejected: the rejected sources to describe, as returned
            by :meth:`~pptrepair.gui.sources.SourceListModel.add_paths`.
        :returns: the message body for :meth:`_show_reject_details`.
        """
        lines = []
        for item in rejected:
            if item.reason is RejectReason.NOT_FOUND:
                reason_text = tr("not found")
            elif item.reason is RejectReason.ACCESS_ERROR:
                reason_text = tr(
                    "could not be accessed ({detail})").format(
                        detail=item.detail)
            else:
                reason_text = tr("unsupported file type")
            lines.append(f"{item.path} — {reason_text}")
        return "\n".join(lines)

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
            parts.append(
                tr("Added {n} source(s)").format(n=len(result.added)))
        if result.duplicates:
            parts.append(tr("{n} duplicate(s) skipped").format(
                n=len(result.duplicates)))
        if result.rejected:
            parts.append(tr("{n} unsupported item(s) rejected").format(
                n=len(result.rejected)))
        return ", ".join(parts) if parts else tr("No sources added")

    # -- shutdown ------------------------------------------------------

    def closeEvent(self, event: object) -> None:
        """Stop a running scan or repair before the window closes.

        Requests cancellation and blocks (briefly) for each worker to
        unwind so the process never exits with a live scan/repair
        thread. Only then is the session's archive material cache
        deleted -- a worker still mining into it must never have the
        directory pulled out from under it. Runs on the UI thread.

        Deleting the cache is best effort (a file locked by a virus
        scanner, a vanished directory, ...): a leftover temporary
        directory must not stop the window from closing. Should this
        deletion never happen at all -- a hard crash, ``os._exit``, a
        close event that is not delivered -- :mod:`tempfile`'s own
        finalizer for the directory object is the backstop, and the
        operating system's temporary-directory policy the one after
        that.

        :param event: Qt's close event.
        """
        for worker in (self._scan_worker, self._repair_worker,
                       self._multi_repair_worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
                worker.wait(_CLOSE_WAIT_MS)
        with contextlib.suppress(OSError):
            self._cache_dir.cleanup()
        super().closeEvent(event)
