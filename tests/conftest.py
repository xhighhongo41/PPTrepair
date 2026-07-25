"""Shared pytest configuration for the test suite.

Guards against a collection-time failure of the GUI test modules
(``tests/test_gui_skeleton.py``, ``tests/test_gui_sources.py``,
``tests/test_gui_scan.py``, ``tests/test_gui_settings.py``,
``tests/test_gui_candidates.py``) -- and, transitively, of the
pytest-qt plugin's own Qt-binding detection -- in an environment where
the optional ``[gui]`` extra (PySide6) is not installed.
"""

from __future__ import annotations

import importlib.util

collect_ignore: list[str] = []

if importlib.util.find_spec("PySide6") is None:
    collect_ignore.append("test_gui_skeleton.py")
    collect_ignore.append("test_gui_sources.py")
    collect_ignore.append("test_gui_scan.py")
    collect_ignore.append("test_gui_settings.py")
    collect_ignore.append("test_gui_candidates.py")
