"""Tests for the GUI's translation-state helper (:mod:`pptrepair.gui.i18n`).

Most cases exercise :mod:`pptrepair.gui.i18n` on its own, which never
imports PySide6 and therefore runs regardless of whether the optional
``[gui]`` extra is installed. The one case that builds a
:class:`~pptrepair.gui.main_window.MainWindow` is skipped when PySide6
is absent (see the local ``_HAVE_PYSIDE6`` guard below) rather than
relying on :mod:`tests.conftest`'s collection-time guard, since this
module is not in its ``collect_ignore`` list.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

from pptrepair.gui.i18n import set_language, tr

_HAVE_PYSIDE6 = importlib.util.find_spec("PySide6") is not None


@pytest.fixture(autouse=True)
def _restore_gui_language():
    """Reset the GUI-wide translator to English after every test.

    :mod:`pptrepair.gui.i18n` holds process-global translation state
    (see its own module docstring); without this reset, a test that
    calls :func:`set_language` would leak its choice into every test
    that runs afterwards, in this module and beyond.
    """
    yield
    set_language("en")


def test_tr_is_identity_before_any_set_language() -> None:
    """With the default ("en") language, tr() passes strings through."""
    assert tr("Quit") == "Quit"
    assert tr("Cancel") == "Cancel"


def test_set_language_translates_known_msgids() -> None:
    """set_language("ja") switches tr() to the Japanese catalog."""
    set_language("ja")
    assert tr("Quit") == "終了"
    assert tr("Cancel") == "キャンセル"
    assert tr("Preferences…") == "環境設定…"


def test_set_language_en_restores_passthrough() -> None:
    """Switching back to "en" restores the identity translator."""
    set_language("ja")
    assert tr("Quit") != "Quit"
    set_language("en")
    assert tr("Quit") == "Quit"


def test_tr_placeholder_left_for_caller_to_format() -> None:
    """tr() only translates; the caller still fills in placeholders."""
    set_language("ja")
    template = tr("Added {n} source(s)")
    assert "{n}" in template
    assert template.format(n=3) == "3 件のソースを追加しました"


@pytest.mark.skipif(
    not _HAVE_PYSIDE6, reason="requires the PySide6 [gui] extra")
def test_main_window_menu_translated_to_japanese(qtbot) -> None:
    """A MainWindow built after set_language("ja") shows translated menus."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from pptrepair.gui.main_window import MainWindow

    set_language("ja")
    window = MainWindow()
    qtbot.addWidget(window)
    titles = [action.text() for action in window.menuBar().actions()]
    assert tr("File") in titles
    assert tr("Run") in titles
    assert tr("Settings") in titles
    assert tr("Help") in titles
    assert "File" not in titles
