"""Tests for the GUI's scan worker, options, results and integration.

Covers :mod:`pptrepair.gui.worker`, :mod:`pptrepair.gui.run_options`,
:mod:`pptrepair.gui.results` and the scan wiring added to
:class:`pptrepair.gui.main_window.MainWindow`. Skipped wholesale when
PySide6 is not installed (the optional ``[gui]`` extra); see
:mod:`tests.conftest` for the matching collection guard.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, zero_prefix

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from pytestqt.qtbot import QtBot  # noqa: E402

from pptrepair.gui.main_window import MainWindow  # noqa: E402
from pptrepair.gui.results import ScanResultsModel  # noqa: E402
from pptrepair.gui.run_options import RepairMode, RunOptionsPanel  # noqa: E402
from pptrepair.gui.worker import (  # noqa: E402
    GuiScanResult,
    ScanRequest,
    ScanWorker,
)
from pptrepair.scan import (  # noqa: E402
    diagnose_archive_materials,
    scan_paths,
)

#: Media payload large enough that a 256 KiB head zero-fill still leaves
#: surviving tail bytes (classified head_zero_fill, not empty).
_MEDIA_BYTES = 600_000

#: Head length zero-filled to synthesise a size-preserving corruption.
_ZERO_HEAD = 262_144


# --------------------------------------------------------------------------
# fixture helpers
# --------------------------------------------------------------------------


def _write_normal(path: Path, *, seed: int = 0,
                  media_bytes: int = 4096) -> Path:
    """Write a structurally valid (NORMAL) .pptx to *path*."""
    path.write_bytes(build_minimal_pptx(num_slides=1, media_bytes=media_bytes,
                                        seed=seed))
    return path


def _write_corrupted(path: Path, *, seed: int = 0) -> Path:
    """Write a head-zero-filled (HEAD_ZERO_FILL) .pptx to *path*."""
    data = build_minimal_pptx(num_slides=1, media_bytes=_MEDIA_BYTES,
                              seed=seed)
    path.write_bytes(zero_prefix(data, _ZERO_HEAD))
    return path


def _write_zip_with_pptx(path: Path, member: str = "backup/deck.pptx") -> Path:
    """Write a zip archive containing one intact .pptx member to *path*."""
    with zipfile.ZipFile(path, mode="w") as zf:
        zf.writestr(member, build_minimal_pptx(num_slides=1, media_bytes=4096))
    return path


def _mkroot(tmp_path: Path, name: str = "root") -> Path:
    """Create and return an empty scan-root directory under *tmp_path*."""
    root = tmp_path / name
    root.mkdir()
    return root


# --------------------------------------------------------------------------
# ScanWorker
# --------------------------------------------------------------------------


def test_scan_worker_diagnoses_all_files(qtbot: QtBot, tmp_path: Path) -> None:
    """A worker over three on-disk files emits file_scanned once each."""
    root = _mkroot(tmp_path)
    _write_normal(root / "a.pptx", seed=1)
    _write_normal(root / "b.pptx", seed=2)
    _write_corrupted(root / "c.pptx", seed=3)

    worker = ScanWorker(ScanRequest(roots=(root,), archives=()))
    scanned: list[object] = []
    worker.file_scanned.connect(lambda outcome: scanned.append(outcome))

    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    result = blocker.args[0]
    assert isinstance(result, GuiScanResult)
    assert result.scan is not None
    assert len(result.scan.outcomes) == 3
    assert len(scanned) == 3
    assert result.materials == []
    assert worker.wait(5000)


def test_scan_worker_mines_archive_without_roots(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """An archives-only request yields materials with scan=None."""
    archive = _write_zip_with_pptx(tmp_path / "backup.zip")

    worker = ScanWorker(ScanRequest(roots=(), archives=(archive,)))
    mined: list[object] = []
    worker.material_scanned.connect(lambda material: mined.append(material))

    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    result = blocker.args[0]
    assert isinstance(result, GuiScanResult)
    assert result.scan is None
    assert len(result.materials) == 1
    assert len(mined) == 1
    assert "::" in result.materials[0].display()
    assert worker.wait(5000)


def test_scan_worker_cancellation_stops_early(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """Cancelling on the first result emits cancelled, never finished_ok."""
    root = _mkroot(tmp_path)
    for index in range(10):
        _write_normal(root / f"deck{index}.pptx", seed=index)

    worker = ScanWorker(ScanRequest(roots=(root,), archives=()))
    finished: list[object] = []
    worker.finished_ok.connect(lambda result: finished.append(result))

    # A blocking-queued connection makes the stop deterministic: the
    # worker thread parks at the first emit until this UI-thread slot has
    # set the cancel flag, so the *next* callback is guaranteed to raise.
    worker.file_scanned.connect(
        lambda _outcome: worker.cancel(),
        Qt.ConnectionType.BlockingQueuedConnection,
    )

    with qtbot.waitSignal(worker.cancelled, timeout=15000):
        worker.start()

    assert finished == []
    assert worker.wait(5000)


def test_scan_worker_respects_max_file_bytes(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """An oversized file is skipped, not diagnosed, under max_file_bytes."""
    root = _mkroot(tmp_path)
    small = _write_normal(root / "small.pptx", seed=1, media_bytes=1024)
    big = _write_normal(root / "big.pptx", seed=2, media_bytes=200_000)

    worker = ScanWorker(ScanRequest(
        roots=(root,), archives=(), max_file_bytes=50_000))

    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    result = blocker.args[0]
    assert result.scan is not None
    diagnosed = [outcome.path for outcome in result.scan.outcomes]
    assert small in diagnosed
    assert big not in diagnosed
    assert big in result.scan.walk.skipped_oversize
    assert worker.wait(5000)


# --------------------------------------------------------------------------
# RunOptionsPanel
# --------------------------------------------------------------------------


@pytest.fixture
def run_options(qtbot: QtBot) -> RunOptionsPanel:
    """Build a :class:`RunOptionsPanel`, registered with *qtbot*."""
    panel = RunOptionsPanel()
    qtbot.addWidget(panel)
    return panel


def test_run_options_default_max_file_bytes_is_two_gb(
    run_options: RunOptionsPanel,
) -> None:
    """The default 2 GB ceiling converts to 2_147_483_648 bytes."""
    assert run_options.max_file_bytes() == 2_147_483_648


def test_run_options_megabytes_conversion(
    run_options: RunOptionsPanel,
) -> None:
    """500 MB converts to 524_288_000 bytes (base 1024)."""
    run_options._size_spin.setValue(500)
    run_options._unit_combo.setCurrentText("MB")
    assert run_options.max_file_bytes() == 524_288_000


def test_run_options_no_limit_returns_none(
    run_options: RunOptionsPanel,
) -> None:
    """Ticking "No limit" reports no ceiling and disables the spin/unit."""
    run_options._no_limit_check.setChecked(True)
    assert run_options.max_file_bytes() is None
    assert not run_options._size_spin.isEnabled()
    assert not run_options._unit_combo.isEnabled()


def test_run_options_output_dir_toggle(
    run_options: RunOptionsPanel, tmp_path: Path
) -> None:
    """In-place reports no output dir; the folder option reports its path."""
    assert run_options.in_place() is True
    assert run_options.output_dir() is None

    run_options._into_folder_radio.setChecked(True)
    run_options._output_edit.setText(str(tmp_path))
    assert run_options.in_place() is False
    assert run_options.output_dir() == tmp_path


def test_run_options_repair_mode_selection(
    run_options: RunOptionsPanel,
) -> None:
    """The repair-mode combo returns the matching enum value."""
    assert run_options.repair_mode() is RepairMode.SINGLE
    run_options._mode_combo.setCurrentIndex(1)
    assert run_options.repair_mode() is RepairMode.MULTI


def test_run_options_disabled_while_running(
    run_options: RunOptionsPanel,
) -> None:
    """set_enabled_for_running(True) greys out every control."""
    run_options.set_enabled_for_running(True)
    assert not run_options._mode_combo.isEnabled()
    assert not run_options._download_check.isEnabled()
    assert not run_options._size_spin.isEnabled()

    run_options.set_enabled_for_running(False)
    assert run_options._mode_combo.isEnabled()
    assert run_options._download_check.isEnabled()
    assert run_options._size_spin.isEnabled()


# --------------------------------------------------------------------------
# ScanResultsModel
# --------------------------------------------------------------------------


def _display(model: ScanResultsModel, row: int, column: int) -> str:
    """Return the DisplayRole text for one cell of *model*."""
    return model.data(model.index(row, column), Qt.ItemDataRole.DisplayRole)


def test_results_model_rows_statuses_and_material(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """set_result populates one row per outcome and per mined member."""
    root = _mkroot(tmp_path)
    normal = _write_normal(root / "good.pptx", seed=1)
    corrupted = _write_corrupted(root / "bad.pptx", seed=2)
    archive = _write_zip_with_pptx(tmp_path / "backup.zip")

    scan_result = scan_paths([root])
    materials, notes = diagnose_archive_materials([archive])
    gui_result = GuiScanResult(scan=scan_result, materials=materials,
                               material_notes=notes)

    model = ScanResultsModel()
    model.set_result(gui_result)

    # One row per diagnosed file (2) plus one material row.
    assert model.rowCount() == len(scan_result.outcomes) + len(materials)
    assert model.columnCount() == 3

    statuses = {
        _display(model, row, 0): _display(model, row, 1)
        for row in range(model.rowCount())
    }
    assert statuses[str(normal)] == "normal"
    assert statuses[str(corrupted)] == "head_zero_fill"

    material_label = materials[0].display()
    assert "::" in material_label
    assert statuses[material_label] == "material"


def test_results_model_empty_result_has_no_rows() -> None:
    """A scan=None result with no materials renders an empty table."""
    model = ScanResultsModel()
    model.set_result(GuiScanResult(scan=None))
    assert model.rowCount() == 0


# --------------------------------------------------------------------------
# MainWindow integration
# --------------------------------------------------------------------------


@pytest.fixture
def main_window(qtbot: QtBot) -> MainWindow:
    """Build a :class:`MainWindow`, registered with *qtbot* for cleanup."""
    window = MainWindow()
    qtbot.addWidget(window)
    return window


def test_scan_button_enables_when_sources_added(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """The Scan button is disabled until at least one source exists."""
    assert not main_window._scan_button.isEnabled()

    deck = _write_normal(tmp_path / "deck.pptx", seed=1)
    main_window._sources.add_paths([deck])

    assert main_window._scan_button.isEnabled()


def test_scan_populates_results_panel(
    main_window: MainWindow, qtbot: QtBot, tmp_path: Path
) -> None:
    """Running a scan reveals the results panel with at least one row."""
    root = _mkroot(tmp_path)
    _write_normal(root / "good.pptx", seed=1)
    _write_corrupted(root / "bad.pptx", seed=2)
    main_window._sources.add_paths([root])

    main_window._start_scan()

    # Wait for the worker to finish and be cleaned up on the UI thread.
    qtbot.waitUntil(lambda: main_window._scan_worker is None, timeout=15000)

    assert not main_window._results_panel.isHidden()
    assert main_window._results_panel._model.rowCount() > 0
    assert main_window._scan_button.isEnabled()
