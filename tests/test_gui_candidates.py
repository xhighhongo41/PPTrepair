"""Tests for the GUI results panel's Candidates tab (:mod:`pptrepair.gui.results`).

Covers the twin-/lineage-/merge-restoration candidate tree that
:class:`~pptrepair.gui.results.ResultsPanel` builds from a
:class:`~pptrepair.gui.worker.GuiScanResult`, computed through the same
:mod:`pptrepair.report_candidates` functions the CLI's scan report uses.
Skipped wholesale when PySide6 is not installed (the optional ``[gui]``
extra); see :mod:`tests.conftest` for the matching collection guard.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, make_corrupted_copies

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pytestqt.qtbot import QtBot

from pptrepair.gui.results import ResultsPanel
from pptrepair.gui.worker import GuiScanResult
from pptrepair.scan import scan_paths


def _write_merge_scenario(root: Path) -> None:
    """Populate *root* with two same-size corrupted copies plus the
    intact original they were both derived from.

    Mirrors ``tests/test_report.py``'s own merge-group fixture: two
    ``foreign_prefix`` corruptions of the same base archive preserve
    its byte length, so the corrupted copies land in the same
    merge-candidate group; the untouched third copy gives the twin
    scorer an intact same-size donor too.
    """
    base = build_minimal_pptx(num_slides=3, media_bytes=100_000)
    copy_a, copy_b = make_corrupted_copies(base, [
        [("foreign_prefix", 4096)],
        [("foreign_prefix", 8192)],
    ])
    (root / "a.pptx").write_bytes(copy_a)
    (root / "b.pptx").write_bytes(copy_b)
    (root / "c.pptx").write_bytes(base)


@pytest.fixture
def results_panel(qtbot: QtBot) -> ResultsPanel:
    """Build a :class:`ResultsPanel`, registered with *qtbot* for cleanup."""
    panel = ResultsPanel()
    qtbot.addWidget(panel)
    return panel


def test_candidates_tab_shows_merge_group_for_same_size_corruptions(
    results_panel: ResultsPanel, tmp_path: Path
) -> None:
    """Two same-size corrupted files produce a "Merge groups" branch.

    Also exercises :meth:`ResultsPanel.last_result`, which the repair
    step of a later milestone will read the same scan back through.
    """
    root = tmp_path / "root"
    root.mkdir()
    _write_merge_scenario(root)

    scan_result = scan_paths([root])
    gui_result = GuiScanResult(scan=scan_result)

    results_panel.show_result(gui_result)

    assert results_panel.last_result() is gui_result
    assert (results_panel._candidates_stack.currentWidget()
            is results_panel._candidates_tree)

    top_level_labels = [
        results_panel._candidates_tree.topLevelItem(index).text(0)
        for index in range(results_panel._candidates_tree.topLevelItemCount())
    ]
    # At least one of the twin/merge branches must appear; the fixture
    # is specifically built to guarantee a merge group (two corrupted
    # files sharing an exact byte size).
    assert "Merge groups" in top_level_labels

    merge_item = next(
        results_panel._candidates_tree.topLevelItem(index)
        for index in range(results_panel._candidates_tree.topLevelItemCount())
        if results_panel._candidates_tree.topLevelItem(index).text(0)
        == "Merge groups"
    )
    assert merge_item.childCount() == 1
    group_item = merge_item.child(0)
    assert group_item.text(0).startswith("group (size ")
    assert group_item.childCount() == 2
    child_labels = {group_item.child(i).text(0)
                    for i in range(group_item.childCount())}
    assert child_labels == {str(root / "a.pptx"), str(root / "b.pptx")}


def test_candidates_tab_shows_placeholder_when_no_candidates(
    results_panel: ResultsPanel, tmp_path: Path
) -> None:
    """A scan with only unrelated, differently sized files shows no tree."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "solo.pptx").write_bytes(
        build_minimal_pptx(num_slides=1, media_bytes=4096, seed=1))

    scan_result = scan_paths([root])
    gui_result = GuiScanResult(scan=scan_result)

    results_panel.show_result(gui_result)

    assert (results_panel._candidates_stack.currentWidget()
            is results_panel._candidates_placeholder)
    assert results_panel._candidates_placeholder.text() == \
        "(no candidates found)"
    assert results_panel._candidates_tree.topLevelItemCount() == 0


def test_clear_resets_candidates_tab_and_last_result(
    results_panel: ResultsPanel, tmp_path: Path
) -> None:
    """clear() drops the last result and falls back to the placeholder."""
    root = tmp_path / "root"
    root.mkdir()
    _write_merge_scenario(root)
    scan_result = scan_paths([root])
    results_panel.show_result(GuiScanResult(scan=scan_result))
    assert results_panel._candidates_tree.topLevelItemCount() > 0

    results_panel.clear()

    assert results_panel.last_result() is None
    assert results_panel._candidates_tree.topLevelItemCount() == 0
    assert (results_panel._candidates_stack.currentWidget()
            is results_panel._candidates_placeholder)
