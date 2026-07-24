"""The PPTrepair desktop application's main window."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QLabel, QMainWindow, QMessageBox

import pptrepair


class MainWindow(QMainWindow):
    """Top-level window of the PPTrepair desktop application.

    Hosts a placeholder drop-area label (drag-and-drop handling itself
    is wired up in a later milestone), a File/Help menu bar and a
    status bar. All user-facing strings are plain English literals for
    now; gettext-based translation is planned for a later milestone.
    """

    def __init__(self) -> None:
        """Build the window's central widget, menu bar and status bar."""
        super().__init__()
        self.setWindowTitle("PPTrepair")
        self.resize(900, 600)

        self._build_central_widget()
        self._build_menu_bar()
        self.statusBar().showMessage("Ready")

    def _build_central_widget(self) -> None:
        """Install the placeholder drop-area label as the central widget."""
        placeholder = QLabel("Drop PowerPoint files or folders here")
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(
            "border: 2px dashed gray; border-radius: 8px; color: gray;"
        )
        self.setCentralWidget(placeholder)

    def _build_menu_bar(self) -> None:
        """Populate the menu bar's File and Help menus."""
        file_menu = self.menuBar().addMenu("File")
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
