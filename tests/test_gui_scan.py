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
import threading
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
# ScanWorker: per-device parallel scanning
# --------------------------------------------------------------------------


def _patch_devices(monkeypatch: pytest.MonkeyPatch,
                   devices: dict[Path, int | None]) -> list[Path]:
    """Make ``_device_of`` report a synthetic device for each path.

    Every real path in a test lives on the one volume ``tmp_path`` is
    on, so the multi-device layouts the parallel path exists for can
    only be synthesised. A path absent from *devices* reports device 0.

    :return: the list every queried path is appended to, so a test can
        assert that a fallback never even asked.
    """
    asked: list[Path] = []

    def _fake_device_of(path: Path) -> int | None:
        asked.append(Path(path))
        return devices.get(Path(path), 0)

    monkeypatch.setattr(worker_module, "_device_of", _fake_device_of)
    return asked


def _device_threads() -> list[threading.Thread]:
    """Return every still-running :class:`ScanWorker` device thread.

    They carry a ``pptrepair-scan-deviceN`` name, so this sees exactly
    the threads the parallel path started -- unlike
    ``threading.active_count()``, which also counts pytest's, Qt's and
    the interpreter's own.
    """
    return [thread for thread in threading.enumerate()
            if thread.name.startswith("pptrepair-scan-device")]


def _result_shape(result: GuiScanResult) -> dict[str, object]:
    """Reduce a :class:`GuiScanResult` to what scheduling must not change.

    Every list bucket is flattened to plain strings/values *in order*,
    so an assertion over two runs compares contents and ordering alike.
    Materials are named by archive and member rather than by their
    ``diagnosis.path``: that one is the temporary file the member was
    extracted to, which legitimately differs between two runs.
    """
    shape: dict[str, object] = {
        "materials": [
            (str(material.archive_path), material.member.member_name,
             material.member.size,
             None if material.diagnosis is None
             else material.diagnosis.verdict.value,
             material.error)
            for material in result.materials
        ],
        "material_notes": list(result.material_notes),
    }
    scan = result.scan
    if scan is None:
        shape["scan"] = None
        return shape
    walk = scan.walk
    shape["scan"] = {
        "roots": [str(path) for path in scan.roots],
        "report_dir": scan.report_dir,
        "fingerprints_skipped": scan.fingerprints_skipped,
        "search_archives": scan.search_archives,
        "materials": len(scan.materials),
        "material_notes": list(scan.material_notes),
        "outcomes": [
            (str(outcome.path),
             None if outcome.diagnosis is None
             else outcome.diagnosis.verdict.value,
             outcome.error)
            for outcome in scan.outcomes
        ],
        "targets": [str(path) for path in walk.targets],
        "skipped_legacy": [str(path) for path in walk.skipped_legacy],
        "skipped_temp": [str(path) for path in walk.skipped_temp],
        "skipped_cloud": [str(path) for path in walk.skipped_cloud],
        "download_targets": [str(path) for path in walk.download_targets],
        "archives": [str(path) for path in walk.archives],
        "errors": [(str(path), message) for path, message in walk.errors],
        "skipped_oversize": [str(path) for path in walk.skipped_oversize],
    }
    return shape


def _two_device_request(tmp_path: Path) -> tuple[ScanRequest,
                                                 dict[Path, int | None]]:
    """Build a two-root/two-archive request spread over two devices.

    The roots hold files that land in several discovery buckets (a
    normal and a corrupted target, a legacy ``.ppt``, an Office ``~$``
    temp file), and one of them does not exist at all so the ``errors``
    bucket is populated too: a merge that dropped or reordered any
    bucket then shows up immediately.

    :return: ``(request, devices)`` -- the request and the ``_device_of``
        map that puts the first root/archive on device 1 and the second
        root, the missing root and the second archive on device 2.
    """
    first = _mkroot(tmp_path, "alpha")
    _write_normal(first / "a1.pptx", seed=1)
    _write_corrupted(first / "a2.pptx", seed=2)
    (first / "a3.ppt").write_bytes(b"legacy binary powerpoint")
    (first / "~$a1.pptx").write_bytes(b"owner lock")
    nested = first / "nested"
    nested.mkdir()
    _write_normal(nested / "a4.pptx", seed=3)

    second = _mkroot(tmp_path, "beta")
    _write_normal(second / "b1.pptx", seed=4)
    _write_corrupted(second / "b2.pptx", seed=5)
    missing = tmp_path / "gamma"

    first_archive = _write_targz_with_pptx(
        tmp_path / "alpha.tar.gz", "backup/one.pptx", "backup/two.pptx")
    second_archive = _write_zip_with_pptx(tmp_path / "beta.zip")

    request = ScanRequest(roots=(first, second, missing),
                          archives=(first_archive, second_archive))
    devices: dict[Path, int | None] = {
        first: 1, first_archive: 1,
        second: 2, missing: 2, second_archive: 2,
    }
    return request, devices


def test_parallel_scan_result_matches_the_sequential_one(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spreading the same sources over two devices changes only the
    scheduling: every bucket of the GuiScanResult -- contents and order
    alike -- is what the sequential run produced."""
    request, devices = _two_device_request(tmp_path)

    # One device for every source: the sequential path, unchanged.
    _patch_devices(monkeypatch, {})
    assert ScanWorker(request)._plan_device_groups() is None
    sequential = _run_worker(qtbot, ScanWorker(request))

    # The very same sources, now on two devices: the parallel path.
    _patch_devices(monkeypatch, devices)
    assert ScanWorker(request)._plan_device_groups() is not None
    parallel = _run_worker(qtbot, ScanWorker(request))

    assert _result_shape(parallel) == _result_shape(sequential)
    assert parallel.scan is not None
    assert len(parallel.scan.outcomes) == 5
    assert len(parallel.materials) == 3
    # The non-trivial buckets really were populated (an all-empty
    # comparison would prove nothing).
    assert len(parallel.scan.walk.skipped_legacy) == 1
    assert len(parallel.scan.walk.skipped_temp) == 1
    assert len(parallel.scan.walk.errors) == 1
    assert _device_threads() == []


def test_parallel_archives_only_request_keeps_scan_none(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Archives on two devices and no root at all: the materials come
    back in request order and ``scan`` stays None, exactly as it does on
    the sequential path (an empty ScanResult would claim a walk that
    never happened)."""
    first = _write_targz_with_pptx(tmp_path / "first.tar.gz",
                                   "backup/one.pptx", "backup/two.pptx")
    second = _write_zip_with_pptx(tmp_path / "second.zip")
    _patch_devices(monkeypatch, {first: 1, second: 2})

    result = _run_worker(qtbot, ScanWorker(
        ScanRequest(roots=(), archives=(first, second))))

    assert result.scan is None
    assert [material.member.member_name for material in result.materials] == [
        "backup/one.pptx", "backup/two.pptx", "backup/deck.pptx"]
    assert result.material_notes == []
    assert _device_threads() == []


def test_two_devices_are_scanned_at_the_same_time(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two device groups really do overlap: each root's scan waits
    for the other one to have started, which only concurrent threads can
    satisfy -- a sequential run would break the barrier and fail."""
    first = _mkroot(tmp_path, "first")
    _write_normal(first / "a.pptx", seed=1)
    second = _mkroot(tmp_path, "second")
    _write_normal(second / "b.pptx", seed=2)
    _patch_devices(monkeypatch, {first: 1, second: 2})

    both_started = threading.Barrier(2)
    idents: set[int] = set()
    real_scan_paths = worker_module.scan_paths

    def _rendezvous(paths: list[Path], **kwargs: object):
        """Refuse to scan either root until both are under way."""
        idents.add(threading.get_ident())
        both_started.wait(timeout=10)
        return real_scan_paths(paths, **kwargs)

    monkeypatch.setattr(worker_module, "scan_paths", _rendezvous)

    result = _run_worker(qtbot, ScanWorker(
        ScanRequest(roots=(first, second), archives=())))

    assert len(idents) == 2
    assert result.scan is not None
    assert [outcome.path for outcome in result.scan.outcomes] == [
        first / "a.pptx", second / "b.pptx"]
    assert _device_threads() == []


def test_follow_symlinks_falls_back_to_the_sequential_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With follow_symlinks the walk shares one visited-directory set
    across roots, so the run stays sequential -- decided before any path
    is even stat'ed for its device."""
    request, devices = _two_device_request(tmp_path)
    asked = _patch_devices(monkeypatch, devices)

    worker = ScanWorker(ScanRequest(roots=request.roots,
                                    archives=request.archives,
                                    follow_symlinks=True))

    assert worker._plan_device_groups() is None
    assert asked == []


def test_one_device_falls_back_to_the_sequential_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sources sharing a device -- or all failing to stat, which is the
    same "cannot tell them apart" group -- have nothing to overlap."""
    request, _devices = _two_device_request(tmp_path)
    asked = _patch_devices(monkeypatch, {})

    assert ScanWorker(request)._plan_device_groups() is None
    assert asked == [*request.roots, *request.archives]

    _patch_devices(monkeypatch,
                   dict.fromkeys([*request.roots, *request.archives], None))
    assert ScanWorker(request)._plan_device_groups() is None


def test_an_empty_request_falls_back_to_the_sequential_path(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A request with no source at all yields no group, hence no thread."""
    _patch_devices(monkeypatch, {})

    assert ScanWorker(ScanRequest(roots=(), archives=()))._plan_device_groups() \
        is None


def test_device_groups_keep_input_order_with_roots_before_archives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within one device the units run in request order, every root
    before every archive -- the sequential path's own phase order."""
    request, devices = _two_device_request(tmp_path)
    _patch_devices(monkeypatch, devices)

    buckets = ScanWorker(request)._plan_device_groups()

    assert buckets is not None
    assert [[(unit.path, unit.is_archive) for unit in bucket]
            for bucket in buckets] == [
        [(request.roots[0], False), (request.archives[0], True)],
        [(request.roots[1], False), (request.roots[2], False),
         (request.archives[1], True)],
    ]


def test_device_groups_are_capped_at_the_thread_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More devices than threads share the threads out: no unit is lost,
    none is scheduled twice, and no extra thread is created."""
    limit = worker_module._MAX_PARALLEL_DEVICE_SCANS
    roots = tuple(_mkroot(tmp_path, f"disk{index}")
                  for index in range(limit * 2 + 1))
    _patch_devices(monkeypatch,
                   {root: index for index, root in enumerate(roots)})

    buckets = ScanWorker(ScanRequest(roots=roots,
                                     archives=()))._plan_device_groups()

    assert buckets is not None
    assert len(buckets) == limit
    scheduled = [unit.path for bucket in buckets for unit in bucket]
    assert sorted(scheduled) == sorted(roots)


def test_parallel_scan_still_emits_every_progress_signal(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The queued events reach the UI as the same four signals the
    sequential path emits, throttling included (interval 0 here)."""
    monkeypatch.setattr(worker_module, "_WALK_PROGRESS_INTERVAL_S", 0)
    request, devices = _two_device_request(tmp_path)
    _patch_devices(monkeypatch, devices)

    worker = ScanWorker(request)
    files: list[object] = []
    materials: list[object] = []
    directories: list[str] = []
    positions: list[tuple[str, int, int]] = []
    worker.file_scanned.connect(files.append)
    worker.material_scanned.connect(materials.append)
    worker.walk_progress.connect(directories.append)
    worker.archive_progress.connect(
        lambda path, done, total: positions.append((path, done, total)))

    result = _run_worker(qtbot, worker)

    assert result.scan is not None
    assert len(files) == len(result.scan.outcomes) == 5
    assert len(materials) == len(result.materials) == 3
    # Both existing roots are announced (the third one does not exist,
    # so it is walked -- and reported -- by nobody).
    assert set(directories) >= {str(path) for path in request.roots[:2]}
    assert {path for path, _done, _total in positions} == {
        str(path) for path in request.archives}


def test_cancelling_a_parallel_scan_ends_every_device_thread(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancel during a parallel run reaches every device thread: the
    run ends on cancelled, never finished_ok, and leaves no thread."""
    fast = _mkroot(tmp_path, "fast")
    _write_normal(fast / "a.pptx", seed=1)
    held = _mkroot(tmp_path, "held")
    _write_normal(held / "b.pptx", seed=2)
    _patch_devices(monkeypatch, {fast: 1, held: 2})

    released = threading.Event()
    real_scan_paths = worker_module.scan_paths

    def _hold_the_second_device(paths: list[Path], **kwargs: object):
        """Keep the second device busy until the cancel has landed.

        Without it the whole (tiny) fixture could be scanned before the
        first signal is even delivered, and there would be nothing left
        to cancel.
        """
        if Path(paths[0]) == held:
            assert released.wait(timeout=10)
        return real_scan_paths(paths, **kwargs)

    monkeypatch.setattr(worker_module, "scan_paths", _hold_the_second_device)

    worker = ScanWorker(ScanRequest(roots=(fast, held), archives=()))
    finished: list[object] = []
    worker.finished_ok.connect(finished.append)

    def _cancel_then_release(_outcome: object) -> None:
        """Cancel from the UI thread, then let the held device resume."""
        worker.cancel()
        released.set()

    # Blocking-queued, so the cancel is guaranteed to be in effect
    # before the held device thread is released.
    worker.file_scanned.connect(_cancel_then_release,
                                Qt.ConnectionType.BlockingQueuedConnection)

    with qtbot.waitSignal(worker.cancelled, timeout=15000):
        worker.start()

    assert finished == []
    assert worker.wait(5000)
    assert _device_threads() == []


def test_a_failing_device_group_fails_the_whole_parallel_run(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exception on one device stops the others and surfaces as
    failed, with no device thread left behind."""
    good = _mkroot(tmp_path, "good")
    for index in range(5):
        _write_normal(good / f"deck{index}.pptx", seed=index)
    bad = _mkroot(tmp_path, "bad")
    _write_normal(bad / "deck.pptx", seed=9)
    _patch_devices(monkeypatch, {good: 1, bad: 2})

    real_scan_paths = worker_module.scan_paths

    def _explode_on_the_second_device(paths: list[Path], **kwargs: object):
        """Fail the "bad" device the way an unplugged volume would."""
        if Path(paths[0]) == bad:
            raise RuntimeError("device offline")
        return real_scan_paths(paths, **kwargs)

    monkeypatch.setattr(worker_module, "scan_paths",
                        _explode_on_the_second_device)

    worker = ScanWorker(ScanRequest(roots=(good, bad), archives=()))
    finished: list[object] = []
    worker.finished_ok.connect(finished.append)

    with qtbot.waitSignal(worker.failed, timeout=15000) as blocker:
        worker.start()

    assert blocker.args[0] == "RuntimeError: device offline"
    assert finished == []
    assert worker.wait(5000)
    assert _device_threads() == []


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
