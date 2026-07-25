"""Shared pytest configuration for the test suite.

Guards against a collection-time failure of the GUI test modules
(``tests/test_gui_skeleton.py``, ``tests/test_gui_sources.py``,
``tests/test_gui_scan.py``, ``tests/test_gui_settings.py``,
``tests/test_gui_candidates.py``, ``tests/test_gui_repair.py``,
``tests/test_gui_multi_repair.py``) -- and, transitively, of the
pytest-qt plugin's own Qt-binding detection -- in an environment where
the optional ``[gui]`` extra (PySide6) is not installed.
"""

from __future__ import annotations

import importlib.util

import pytest

collect_ignore: list[str] = []

_HAVE_PYSIDE6 = importlib.util.find_spec("PySide6") is not None


@pytest.fixture(autouse=True)
def _isolated_qsettings(tmp_path_factory, monkeypatch):
    """Point QSettings' default stores at a per-test scratch directory.

    ``MainWindow`` constructs a default-backed
    :class:`pptrepair.gui.settings.Settings`, so without this guard
    every GUI integration test would read -- and, through the
    recent-folders feature, write -- the developer's real per-user
    preference store. Redirecting both scopes of both formats keeps
    the suite hermetic; non-GUI tests are unaffected (they never touch
    QSettings) and the fixture is a no-op when PySide6 is absent.
    """
    if not _HAVE_PYSIDE6:
        yield
        return
    from PySide6.QtCore import QSettings

    scratch = tmp_path_factory.mktemp("qsettings")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    for scope in (QSettings.Scope.UserScope, QSettings.Scope.SystemScope):
        QSettings.setPath(QSettings.Format.IniFormat, scope, str(scratch))
    yield


if not _HAVE_PYSIDE6:
    collect_ignore.append("test_gui_skeleton.py")
    collect_ignore.append("test_gui_sources.py")
    collect_ignore.append("test_gui_scan.py")
    collect_ignore.append("test_gui_settings.py")
    collect_ignore.append("test_gui_candidates.py")
    collect_ignore.append("test_gui_repair.py")
    collect_ignore.append("test_gui_multi_repair.py")
