"""Shared pytest configuration for the test suite.

Guards against a collection-time failure of ``tests/test_gui_skeleton.py``
(and, transitively, of the pytest-qt plugin's own Qt-binding detection)
in an environment where the optional ``[gui]`` extra (PySide6) is not
installed.
"""

from __future__ import annotations

import importlib.util

collect_ignore: list[str] = []

if importlib.util.find_spec("PySide6") is None:
    collect_ignore.append("test_gui_skeleton.py")
