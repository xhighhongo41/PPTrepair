"""Tests for the GUI's scan worker, options, results and integration.

Covers :mod:`pptrepair.gui.worker`, :mod:`pptrepair.gui.run_options`,
:mod:`pptrepair.gui.results` and the scan wiring added to
:class:`pptrepair.gui.main_window.MainWindow`. Skipped wholesale when
PySide6 is not installed (the optional ``[gui]`` extra); see
:mod:`tests.conftest` for the matching collection guard.
"""

from __future__ import annotations

import io
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, zero_prefix

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from pytestqt.qtbot import QtBot

from pptrepair.gui import worker as worker_module
from pptrepair.gui.main_window import MainWindow
from pptrepair.gui.results import ScanResultsModel
from pptrepair.gui.run_options import RepairMode, RunOptionsPanel
from pptrepair.gui.worker import (
    GuiScanResult,
    ScanRequest,
    ScanWorker,
)
from pptrepair.scan import (
    ArchiveMaterialCache,
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


def _write_targz_with_pptx(path: Path, *members: str) -> Path:
    """Write a tar.gz archive holding one intact .pptx per *members* name."""
    with tarfile.open(path, mode="w:gz") as tf:
        for index, name in enumerate(members):
            data = build_minimal_pptx(num_slides=1, media_bytes=20_000,
                                      seed=index)
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return path


def _count_tar_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch tarfile.open to record the mode of every *read* open.

    Mirrors :func:`test_scan_archive._count_tar_reads`: returns the
    (initially empty) list the modes land in, ignoring write opens so a
    test may keep writing fixture archives after the patch is installed.
    """
    modes: list[str] = []
    real_open = tarfile.open

    def _counting_open(*args: object, **kwargs: object) -> tarfile.TarFile:
        mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
        if isinstance(mode, str) and mode.startswith("r"):
            modes.append(mode)
        return real_open(*args, **kwargs)

    monkeypatch.setattr(tarfile, "open", _counting_open)
    return modes


def _mkroot(tmp_path: Path, name: str = "root") -> Path:
    """Create and return an empty scan-root directory under *tmp_path*."""
    root = tmp_path / name
    root.mkdir()
    return root


def _run_worker(qtbot: QtBot, worker: ScanWorker) -> GuiScanResult:
    """Run *worker* to completion and return its :class:`GuiScanResult`."""
    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()
    assert worker.wait(5000)
    return blocker.args[0]


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


def test_scan_worker_emits_walk_progress_when_interval_is_zero(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the throttle interval at 0, every visited directory (root and
    each subdirectory) emits walk_progress."""
    monkeypatch.setattr(worker_module, "_WALK_PROGRESS_INTERVAL_S", 0)
    root = _mkroot(tmp_path)
    for name in ("a", "b"):
        sub_dir = root / name
        sub_dir.mkdir()
        _write_normal(sub_dir / "deck.pptx", seed=hash(name) % 1000)

    worker = ScanWorker(ScanRequest(roots=(root,), archives=()))
    emitted: list[str] = []
    worker.walk_progress.connect(emitted.append)

    with qtbot.waitSignal(worker.finished_ok, timeout=15000):
        worker.start()

    assert len(emitted) >= 3  # root + "a" + "b"
    assert str(root) in emitted
    assert worker.wait(5000)


def test_scan_worker_walk_progress_throttled_with_large_interval(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a very large throttle interval, only the first directory
    visited (the root) ever emits walk_progress."""
    monkeypatch.setattr(worker_module, "_WALK_PROGRESS_INTERVAL_S", 9999)
    root = _mkroot(tmp_path)
    for name in ("a", "b", "c"):
        sub_dir = root / name
        sub_dir.mkdir()
        _write_normal(sub_dir / "deck.pptx", seed=hash(name) % 1000)

    worker = ScanWorker(ScanRequest(roots=(root,), archives=()))
    emitted: list[str] = []
    worker.walk_progress.connect(emitted.append)

    with qtbot.waitSignal(worker.finished_ok, timeout=15000):
        worker.start()

    assert emitted == [str(root)]
    assert worker.wait(5000)


def test_scan_worker_cancellation_during_walk_stops_early(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """Cancelling from a walk_progress handler stops the scan before any
    file is diagnosed, emitting cancelled rather than finished_ok."""
    root = _mkroot(tmp_path)
    for name in ("a", "b"):
        sub_dir = root / name
        sub_dir.mkdir()
        _write_normal(sub_dir / "deck.pptx", seed=hash(name) % 1000)

    worker = ScanWorker(ScanRequest(roots=(root,), archives=()))
    scanned: list[object] = []
    worker.file_scanned.connect(lambda outcome: scanned.append(outcome))

    # A blocking-queued connection makes the stop deterministic: the
    # worker thread parks at the first walk_progress emit (the root
    # itself, always emitted) until this UI-thread slot has set the
    # cancel flag, so the *next* on_directory call is guaranteed to raise.
    worker.walk_progress.connect(
        lambda _path: worker.cancel(),
        Qt.ConnectionType.BlockingQueuedConnection,
    )

    with qtbot.waitSignal(worker.cancelled, timeout=15000):
        worker.start()

    assert scanned == []
    assert worker.wait(5000)


def test_scan_worker_cache_serves_a_rescan_without_reopening_the_archive(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scanning the same archive twice with one session cache reads it
    once: the second run is replayed from the cache, materials and all."""
    archive = _write_targz_with_pptx(
        tmp_path / "backup.tar.gz", "backup/one.pptx", "backup/two.pptx")
    cache = ArchiveMaterialCache(tmp_path / "cache")
    modes = _count_tar_reads(monkeypatch)
    request = ScanRequest(roots=(), archives=(archive,))

    first = _run_worker(qtbot, ScanWorker(request, cache=cache))
    second = _run_worker(qtbot, ScanWorker(request, cache=cache))

    assert modes == ["r|*"]
    assert len(first.materials) == 2
    assert second.materials == first.materials
    assert second.material_notes == first.material_notes == []


def test_scan_worker_without_cache_reads_the_archive_every_time(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Left without a cache the worker keeps its pre-cache behaviour: one
    full read of the archive per run."""
    archive = _write_targz_with_pptx(tmp_path / "backup.tar.gz",
                                     "backup/one.pptx")
    modes = _count_tar_reads(monkeypatch)
    request = ScanRequest(roots=(), archives=(archive,))

    first = _run_worker(qtbot, ScanWorker(request))
    second = _run_worker(qtbot, ScanWorker(request))

    assert modes == ["r|*", "r|*"]
    assert len(first.materials) == len(second.materials) == 1


def test_scan_worker_cache_keeps_the_donor_bytes_for_the_repair_phase(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """Every member mined through the cache is still on disk afterwards,
    which is what lets the repair phase splice it without a second read."""
    archive = _write_targz_with_pptx(tmp_path / "backup.tar.gz",
                                     "backup/one.pptx")
    cache_root = tmp_path / "cache"
    cache = ArchiveMaterialCache(cache_root)

    result = _run_worker(qtbot, ScanWorker(
        ScanRequest(roots=(), archives=(archive,)), cache=cache))

    [material] = result.materials
    donor_path = cache.member_path(archive, material.member)
    assert donor_path is not None
    assert donor_path.is_file()
    assert cache_root in donor_path.parents


def test_scan_worker_emits_archive_progress_when_interval_is_zero(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the throttle interval at 0 every byte-position report reaches
    the UI, tagged with the archive and ending on its full size."""
    monkeypatch.setattr(worker_module, "_WALK_PROGRESS_INTERVAL_S", 0)
    archive = _write_targz_with_pptx(
        tmp_path / "backup.tar.gz", "backup/one.pptx", "backup/two.pptx")

    worker = ScanWorker(ScanRequest(roots=(), archives=(archive,)))
    emitted: list[tuple[str, int, int]] = []
    worker.archive_progress.connect(
        lambda path, done, total: emitted.append((path, done, total)))

    _run_worker(qtbot, worker)

    total_bytes = archive.stat().st_size
    assert len(emitted) > 1
    assert {path for path, _done, _total in emitted} == {str(archive)}
    assert {total for _path, _done, total in emitted} == {total_bytes}
    done_values = [done for _path, done, _total in emitted]
    assert done_values == sorted(done_values)
    assert done_values[-1] == total_bytes


def test_scan_worker_archive_progress_throttled_with_large_interval(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a very large throttle interval a single archive reports its
    position exactly once, however many times the core calls back."""
    monkeypatch.setattr(worker_module, "_WALK_PROGRESS_INTERVAL_S", 9999)
    archive = _write_targz_with_pptx(
        tmp_path / "backup.tar.gz", "backup/one.pptx", "backup/two.pptx")

    worker = ScanWorker(ScanRequest(roots=(), archives=(archive,)))
    emitted: list[tuple[str, int, int]] = []
    worker.archive_progress.connect(
        lambda path, done, total: emitted.append((path, done, total)))

    _run_worker(qtbot, worker)

    assert [path for path, _done, _total in emitted] == [str(archive)]


def test_scan_worker_archive_progress_always_emits_on_a_new_archive(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving on to the next archive emits immediately even under a
    throttle interval that suppresses everything else: that transition is
    what renames the file the user is told is being read."""
    monkeypatch.setattr(worker_module, "_WALK_PROGRESS_INTERVAL_S", 9999)
    first = _write_targz_with_pptx(tmp_path / "first.tar.gz",
                                   "backup/one.pptx", "backup/two.pptx")
    second = _write_targz_with_pptx(tmp_path / "second.tar.gz",
                                    "backup/three.pptx", "backup/four.pptx")

    worker = ScanWorker(ScanRequest(roots=(), archives=(first, second)))
    emitted: list[str] = []
    worker.archive_progress.connect(
        lambda path, _done, _total: emitted.append(path))

    _run_worker(qtbot, worker)

    assert emitted == [str(first), str(second)]


def test_scan_worker_cancellation_during_archive_read_stops_early(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling from an archive_progress handler stops the mining pass
    before the archive has been read out, emitting cancelled rather than
    finished_ok -- the point of polling that callback at all."""
    monkeypatch.setattr(worker_module, "_WALK_PROGRESS_INTERVAL_S", 0)
    archive = _write_targz_with_pptx(
        tmp_path / "backup.tar.gz", "backup/one.pptx", "backup/two.pptx")

    worker = ScanWorker(ScanRequest(roots=(), archives=(archive,)))
    mined: list[object] = []
    worker.material_scanned.connect(lambda material: mined.append(material))
    finished: list[object] = []
    worker.finished_ok.connect(lambda result: finished.append(result))
    # A blocking-queued connection makes the stop deterministic: the
    # worker thread parks at the first emit (the first member's tar
    # header, always emitted) until this UI-thread slot has set the
    # cancel flag, so the next callback is guaranteed to raise.
    worker.archive_progress.connect(
        lambda *_args: worker.cancel(),
        Qt.ConnectionType.BlockingQueuedConnection,
    )

    with qtbot.waitSignal(worker.cancelled, timeout=15000):
        worker.start()

    assert finished == []
    assert mined == []
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


def test_on_walk_progress_shows_path_on_status_bar(
    main_window: MainWindow,
) -> None:
    """_on_walk_progress puts the visited directory on the status bar."""
    main_window._on_walk_progress("X")

    assert "X" in main_window.statusBar().currentMessage()


def test_on_archive_progress_shows_a_percentage_when_the_total_is_known(
    main_window: MainWindow,
) -> None:
    """_on_archive_progress names the archive and its completion percent."""
    main_window._on_archive_progress("/backups/big.tar.gz", 25, 200)

    message = main_window.statusBar().currentMessage()
    assert "big.tar.gz" in message
    assert "12%" in message
    # The archive is named by its file name alone, never its full path.
    assert "/backups/" not in message


def test_on_archive_progress_omits_the_percentage_without_a_total(
    main_window: MainWindow,
) -> None:
    """An unknown archive size (total 0) drops the percentage rather than
    faking one, and never divides by zero."""
    main_window._on_archive_progress("/backups/big.tar.gz", 0, 0)

    message = main_window.statusBar().currentMessage()
    assert "big.tar.gz" in message
    assert "%" not in message


def test_close_event_removes_the_session_cache_directory(
    main_window: MainWindow,
) -> None:
    """Closing the window deletes the archive material cache it owns."""
    cache_root = Path(main_window._cache_dir.name)
    assert cache_root.is_dir()

    main_window.close()

    assert not cache_root.exists()


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
