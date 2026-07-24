"""Tests for the PySide6 GUI skeleton (:mod:`pptrepair.gui`).

Skipped wholesale when PySide6 is not installed (the optional ``[gui]``
extra); see :mod:`tests.conftest` for the matching collection guard.
"""

from __future__ import annotations

import os

import pytest

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QAction  # noqa: E402
from pytestqt.qtbot import QtBot  # noqa: E402

from pptrepair.gui.main_window import MainWindow  # noqa: E402


@pytest.fixture
def main_window(qtbot: QtBot) -> MainWindow:
    """Build a :class:`MainWindow`, registered with *qtbot* for cleanup."""
    window = MainWindow()
    qtbot.addWidget(window)
    return window


def test_window_title(main_window: MainWindow) -> None:
    """The window title reads exactly "PPTrepair"."""
    assert main_window.windowTitle() == "PPTrepair"


def test_menu_bar_has_file_and_help_menus(main_window: MainWindow) -> None:
    """The menu bar exposes both a File and a Help menu."""
    titles = [action.text() for action in main_window.menuBar().actions()]
    assert "File" in titles
    assert "Help" in titles


def test_quit_action_has_quit_menu_role(main_window: MainWindow) -> None:
    """The File menu's Quit action is tagged with Qt's QuitRole.

    Uses plain ``for`` loops rather than generator expressions to look
    up the File menu and its Quit action: a bare (unassigned)
    generator gets torn down as soon as ``next()`` pulls its one
    value, and under pytest-qt's exception-capture wrapper that
    teardown has been observed to invalidate the ``QMenu`` handle
    before it is used, raising a spurious shiboken "already deleted"
    ``RuntimeError``.
    """
    file_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "File":
            file_menu = action.menu()
            break
    assert file_menu is not None

    quit_action = None
    for action in file_menu.actions():
        if action.text() == "Quit":
            quit_action = action
            break
    assert quit_action is not None

    assert quit_action.menuRole() == QAction.MenuRole.QuitRole


def test_status_bar_shows_ready(main_window: MainWindow) -> None:
    """The status bar greets the user with "Ready" on startup."""
    assert main_window.statusBar().currentMessage() == "Ready"
