"""Tests for the desktop application's entry point (:mod:`pptrepair.gui.app`).

Covers :func:`pptrepair.gui.app._set_macos_menu_bar_name`'s platform and
dependency guards in isolation, without building a
:class:`~PySide6.QtWidgets.QApplication` or any widget. Skipped wholesale
when PySide6 is not installed (the optional ``[gui]`` extra); see
:mod:`tests.conftest` for the matching collection guard.
"""

from __future__ import annotations

import os
import sys

import pytest

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pptrepair.gui.app import _set_macos_menu_bar_name


def test_set_macos_menu_bar_name_noop_on_non_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a non-macOS platform, the function returns immediately."""
    monkeypatch.setattr(sys, "platform", "linux")

    # Must not raise, even though Foundation is never importable here.
    _set_macos_menu_bar_name("PPTrepair")


def test_set_macos_menu_bar_name_noop_without_pyobjc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On macOS without pyobjc, the missing import is swallowed."""
    monkeypatch.setattr(sys, "platform", "darwin")
    # Setting a module to None in sys.modules forces the next `import` of
    # it to raise ImportError, simulating pyobjc being unavailable.
    monkeypatch.setitem(sys.modules, "Foundation", None)

    # Must not raise even though the pyobjc-framework-Cocoa dependency
    # (an optional, darwin-only extra) is unavailable.
    _set_macos_menu_bar_name("PPTrepair")
