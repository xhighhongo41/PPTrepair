"""Tests for the GUI's single-file repair worker, results tab and wiring.

Covers :class:`~pptrepair.gui.repair_workers.RepairWorker`,
:class:`~pptrepair.gui.results.RepairResultsModel` and the repair
wiring added to :class:`pptrepair.gui.main_window.MainWindow`. Skipped
wholesale when PySide6 is not installed (the optional ``[gui]``
extra); see :mod:`tests.conftest` for the matching collection guard.
"""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, truncate

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from pptrepair.batch import BatchResult
from pptrepair.gui.main_window import MainWindow
from pptrepair.gui.repair_workers import RepairRequest, RepairWorker
from pptrepair.gui.run_options import RepairMode

# --------------------------------------------------------------------------
# fixture helpers
# --------------------------------------------------------------------------


def _mkroot(tmp_path: Path, name: str = "root") -> Path:
    """Create and return an empty scan-root directory under *tmp_path*."""
    root = tmp_path / name
    root.mkdir()
    return root


def _write(dir_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``dir_path / name`` and return the resulting path."""
    path = dir_path / name
    path.write_bytes(data)
    return path


def _write_normal(path: Path, *, seed: int = 0) -> Path:
    """Write a structurally valid (NORMAL) .pptx to *path*."""
    path.write_bytes(build_minimal_pptx(num_slides=1, media_bytes=4096,
                                        seed=seed))
    return path


def _header_offset(data: bytes, name: str) -> int:
    """Return the local-file-header offset of *name* inside *data*."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.getinfo(name).header_offset


def _rebuildable_truncated(num_slides: int = 3, seed: int = 0) -> bytes:
    """Build a TAIL_TRUNCATED fixture that rebuilds with every slide intact.

    Mirrors ``tests/test_batch.py``'s own ``_rebuildable_truncated``
    helper: truncating right at the first media entry's local header
    leaves every slide's XML intact, so ``repair_paths`` always reports
    this fixture ``"repaired"`` (mode ``"rebuild"``), never
    ``"unrepairable"``/``"failed"``.
    """
    data = build_minimal_pptx(num_slides=num_slides, media_bytes=4096,
                              seed=seed)
    cutoff = _header_offset(data, "ppt/media/image1.png")
    return truncate(data, cutoff)


# --------------------------------------------------------------------------
# RepairWorker
# --------------------------------------------------------------------------


def test_repair_worker_aggregate_mode_writes_output(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """A repairable + an intact file: exactly one artifact lands in output_dir."""
    root = _mkroot(tmp_path)
    _write_normal(root / "good.pptx", seed=1)
    _write(root, "bad.pptx", _rebuildable_truncated())
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    worker = RepairWorker(RepairRequest(
        roots=(root,), output_dir=output_dir, in_place=False))
    repaired_items: list[object] = []
    worker.file_repaired.connect(lambda item: repaired_items.append(item))

    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    result = blocker.args[0]
    assert isinstance(result, BatchResult)
    assert result.counts()["repaired"] == 1
    assert len(repaired_items) == 1

    [repaired] = [item for item in result.items if item.action == "repaired"]
    assert repaired.planned_output is not None
    assert repaired.planned_output.exists()
    assert repaired.planned_output.is_relative_to(output_dir)
    assert worker.wait(5000)


def test_repair_worker_in_place_writes_next_to_source(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """In-place mode writes the repaired artifact beside its source file."""
    root = _mkroot(tmp_path)
    bad = _write(root, "bad.pptx", _rebuildable_truncated())

    worker = RepairWorker(RepairRequest(
        roots=(root,), output_dir=None, in_place=True))

    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    result = blocker.args[0]
    assert result.counts()["repaired"] == 1
    [repaired] = result.items
    assert repaired.planned_output is not None
    assert repaired.planned_output.parent == bad.parent
    assert repaired.planned_output.exists()
    assert worker.wait(5000)


def test_repair_worker_cancellation_stops_early(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """Cancelling during file_repaired stops the run before the last file.

    ``repair_paths`` calls its ``repair_progress`` callback for each file
    *after* that file's own artifact has already been written (see
    ``pptrepair.batch.repair_paths``'s loop and its own cancellation
    docstring: "every artifact phase 2 already wrote ... is left in
    place"). So requesting cancellation from the first file's callback
    cannot un-write that first file's own artifact, and -- since the
    flag is only checked at the *start* of the next callback, which
    fires only after that next file has itself already been processed
    -- it cannot prevent the second file's artifact either. With three
    corrupted files, cancelling on the first callback therefore leaves
    the first two repaired and only stops the run before the third is
    ever attempted.
    """
    root = _mkroot(tmp_path)
    for index in range(3):
        _write(root, f"bad{index}.pptx", _rebuildable_truncated(seed=index))

    worker = RepairWorker(RepairRequest(
        roots=(root,), output_dir=None, in_place=True))
    finished: list[object] = []
    worker.finished_ok.connect(lambda result: finished.append(result))

    # A blocking-queued connection makes the stop deterministic: the
    # worker thread parks at the first emit until this UI-thread slot has
    # set the cancel flag, so the *next* callback is guaranteed to raise.
    worker.file_repaired.connect(
        lambda _item: worker.cancel(),
        Qt.ConnectionType.BlockingQueuedConnection,
    )

    with qtbot.waitSignal(worker.cancelled, timeout=15000):
        worker.start()

    assert finished == []
    repaired_artifacts = sorted(root.glob("*.repaired.pptx"))
    assert len(repaired_artifacts) == 2
    assert worker.wait(5000)


def test_repair_worker_failure_prints_traceback_to_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unexpected exception from _execute prints its full traceback to
    stderr, while the failed signal still carries only the short summary
    the UI shows."""
    root = _mkroot(tmp_path)
    worker = RepairWorker(RepairRequest(
        roots=(root,), output_dir=None, in_place=True))

    def _boom() -> BatchResult:
        raise ValueError("boom")

    monkeypatch.setattr(worker, "_execute", _boom)
    messages: list[str] = []
    worker.failed.connect(messages.append)

    # Called directly on this thread (not started as a QThread), so the
    # run is synchronous and capsys can capture stderr from it.
    worker.run()

    captured = capsys.readouterr()
    assert "Traceback" in captured.err
    assert "ValueError: boom" in captured.err
    assert messages == ["ValueError: boom"]


# --------------------------------------------------------------------------
# MainWindow integration
# --------------------------------------------------------------------------


@pytest.fixture
def main_window(qtbot: QtBot) -> MainWindow:
    """Build a :class:`MainWindow`, registered with *qtbot* for cleanup."""
    window = MainWindow()
    qtbot.addWidget(window)
    return window


def _scan_and_wait(main_window: MainWindow, qtbot: QtBot) -> None:
    """Run a scan on *main_window*'s accumulated sources and wait for it."""
    main_window._start_scan()
    qtbot.waitUntil(lambda: main_window._scan_worker is None, timeout=15000)


def test_repair_button_enables_after_scan_in_single_mode(
    main_window: MainWindow, qtbot: QtBot, tmp_path: Path
) -> None:
    """A scan that finds a corrupted file enables Repair in single mode."""
    root = _mkroot(tmp_path)
    _write_normal(root / "good.pptx", seed=1)
    _write(root, "bad.pptx", _rebuildable_truncated())
    main_window._sources.add_paths([root])

    assert not main_window._repair_button.isEnabled()

    _scan_and_wait(main_window, qtbot)

    assert main_window._repair_button.isEnabled()
    assert main_window._repair_action.isEnabled()


def test_repair_button_enabled_for_both_repair_modes(
    main_window: MainWindow, qtbot: QtBot, tmp_path: Path
) -> None:
    """Repair stays enabled in single and multi-source mode after a scan.

    Multi-source repair now shares the same start condition as
    single-file repair (a scan that found at least one corrupted file),
    so switching modes no longer disables the Repair action, and it
    carries no "not yet" tooltip in either mode.
    """
    root = _mkroot(tmp_path)
    _write(root, "bad.pptx", _rebuildable_truncated())
    main_window._sources.add_paths([root])
    _scan_and_wait(main_window, qtbot)
    assert main_window._repair_button.isEnabled()
    assert main_window._repair_button.toolTip() == ""

    multi_index = main_window._run_options._mode_combo.findData(
        RepairMode.MULTI)
    main_window._run_options._mode_combo.setCurrentIndex(multi_index)
    assert main_window._repair_button.isEnabled()
    assert main_window._repair_action.isEnabled()
    assert main_window._repair_button.toolTip() == ""

    single_index = main_window._run_options._mode_combo.findData(
        RepairMode.SINGLE)
    main_window._run_options._mode_combo.setCurrentIndex(single_index)
    assert main_window._repair_button.isEnabled()
    assert main_window._repair_button.toolTip() == ""


def test_repair_execution_populates_repair_tab_and_open_output_button(
    main_window: MainWindow, qtbot: QtBot, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running a repair fills the Repair tab and enables Open Output Folder."""
    opened: list[object] = []
    monkeypatch.setattr(
        "pptrepair.gui.main_window.QDesktopServices.openUrl",
        lambda url: opened.append(url))

    root = _mkroot(tmp_path)
    _write(root, "bad.pptx", _rebuildable_truncated())
    main_window._sources.add_paths([root])
    _scan_and_wait(main_window, qtbot)
    assert main_window._repair_button.isEnabled()

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    main_window._run_options._into_folder_radio.setChecked(True)
    main_window._run_options._output_edit.setText(str(output_dir))

    assert main_window._open_output_button.isHidden()

    main_window._start_repair()
    qtbot.waitUntil(lambda: main_window._repair_worker is None, timeout=15000)

    assert main_window._results_panel._repair_model.rowCount() == 1
    assert main_window._open_output_button.isEnabled()
    assert not main_window._open_output_button.isHidden()

    main_window._open_output_button.click()
    assert len(opened) == 1
    assert opened[0].toLocalFile() == str(output_dir)


def test_repair_warns_when_output_folder_not_chosen(
    main_window: MainWindow, qtbot: QtBot, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repairing into an unset folder warns instead of starting a worker."""
    warnings: list[tuple] = []
    monkeypatch.setattr(
        "pptrepair.gui.main_window.QMessageBox.warning",
        lambda *args, **kwargs: warnings.append(args))

    root = _mkroot(tmp_path)
    _write(root, "bad.pptx", _rebuildable_truncated())
    main_window._sources.add_paths([root])
    _scan_and_wait(main_window, qtbot)

    main_window._run_options._into_folder_radio.setChecked(True)
    # Deliberately leave _output_edit empty: no destination chosen.

    main_window._start_repair()

    assert main_window._repair_worker is None
    assert len(warnings) == 1
