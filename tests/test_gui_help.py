"""Tests for the Help menu (:mod:`pptrepair.gui.main_window`).

Covers the Help menu's action order (Quick Guide, a separator, Open
GitHub Page, Report Unknown Corruption…, a separator, License
Information, About PPTrepair) and the wiring of each new action:
the two dialog actions must invoke :class:`QMessageBox`, and the two
URL actions must hand the expected URL to
:class:`QDesktopServices`. Skipped wholesale when PySide6 is not
installed (the optional ``[gui]`` extra); see :mod:`tests.conftest`
for the matching collection guard.

Every test below looks up the Help menu -- and, where needed, one of
its actions -- with a plain inline ``for`` loop rather than a helper
function that would return the ``QMenu``/``QAction`` object across a
call boundary. As documented in ``tests/test_gui_settings.py``'s
``test_run_menu_has_scan_repair_cancel_actions``, an intermediate
scope's teardown has been observed to invalidate such a handle before
it is used, raising a spurious shiboken "already deleted"
``RuntimeError``; this was confirmed here too when the lookup was
first factored into a helper function.
"""

from __future__ import annotations

import os

import pytest

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pytestqt.qtbot import QtBot

from pptrepair.gui.main_window import MainWindow
from pptrepair.report import ISSUE_URL

#: The URL "Open GitHub Page" is expected to open, mirroring
#: :data:`pptrepair.gui.main_window._GITHUB_URL`.
_GITHUB_URL = "https://github.com/xhighhongo41/PPTrepair"


@pytest.fixture
def main_window(qtbot: QtBot) -> MainWindow:
    """Build a :class:`MainWindow`, registered with *qtbot* for cleanup."""
    window = MainWindow()
    qtbot.addWidget(window)
    return window


def test_help_menu_action_order(main_window: MainWindow) -> None:
    """The Help menu lists every action, in the documented order."""
    help_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "Help":
            help_menu = action.menu()
            break
    assert help_menu is not None

    entries = [
        "" if action.isSeparator() else action.text()
        for action in help_menu.actions()
    ]
    assert entries == [
        "Quick Guide",
        "",
        "Open GitHub Page",
        "Report Unknown Corruption…",
        "",
        "License Information",
        "About PPTrepair",
    ]


def test_quick_guide_action_shows_dialog(
    main_window: MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triggering "Quick Guide" shows an informational message box."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "pptrepair.gui.main_window.QMessageBox.information",
        lambda *args, **kwargs: calls.append(args))

    help_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "Help":
            help_menu = action.menu()
            break
    assert help_menu is not None

    quick_guide_action = None
    for action in help_menu.actions():
        if action.text() == "Quick Guide":
            quick_guide_action = action
            break
    assert quick_guide_action is not None

    quick_guide_action.trigger()

    assert len(calls) == 1
    assert calls[0][1] == "Quick Guide"


def test_license_information_action_shows_dialog(
    main_window: MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triggering "License Information" shows an informational message box."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        "pptrepair.gui.main_window.QMessageBox.information",
        lambda *args, **kwargs: calls.append(args))

    help_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "Help":
            help_menu = action.menu()
            break
    assert help_menu is not None

    license_action = None
    for action in help_menu.actions():
        if action.text() == "License Information":
            license_action = action
            break
    assert license_action is not None

    license_action.trigger()

    assert len(calls) == 1
    assert calls[0][1] == "License Information"


def test_open_github_page_action_opens_expected_url(
    main_window: MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triggering "Open GitHub Page" opens the PPTrepair repository URL."""
    opened: list[object] = []
    monkeypatch.setattr(
        "pptrepair.gui.main_window.QDesktopServices.openUrl",
        lambda url: opened.append(url))

    help_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "Help":
            help_menu = action.menu()
            break
    assert help_menu is not None

    github_action = None
    for action in help_menu.actions():
        if action.text() == "Open GitHub Page":
            github_action = action
            break
    assert github_action is not None

    github_action.trigger()

    assert len(opened) == 1
    assert opened[0].toString() == _GITHUB_URL


def test_report_unknown_corruption_action_opens_issue_url(
    main_window: MainWindow, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Triggering "Report Unknown Corruption…" opens the GitHub issue URL."""
    opened: list[object] = []
    monkeypatch.setattr(
        "pptrepair.gui.main_window.QDesktopServices.openUrl",
        lambda url: opened.append(url))

    help_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "Help":
            help_menu = action.menu()
            break
    assert help_menu is not None

    report_action = None
    for action in help_menu.actions():
        if action.text() == "Report Unknown Corruption…":
            report_action = action
            break
    assert report_action is not None

    report_action.trigger()

    assert len(opened) == 1
    assert opened[0].toString() == ISSUE_URL
