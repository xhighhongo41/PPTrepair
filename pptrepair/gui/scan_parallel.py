"""Device-parallel scanning machinery for :class:`ScanWorker`.

Qt-independent -- built on :mod:`threading`, :mod:`queue` and
:mod:`dataclasses` alone -- this module is internal to the ``gui``
package: :class:`~pptrepair.gui.worker.ScanWorker` is the only expected
caller, and it stays the sole orchestrator (starting the device threads,
pumping their queued events back onto the UI thread as Qt signals). What
lives here is everything that orchestration needs but that touches
neither a Qt object nor ``self``:

* :func:`device_of` -- which physical device a path lives on, the key
  the units of work are grouped by.
* :class:`ScanUnit` -- one indivisible piece of work (a root to scan or
  an archive to mine).
* :class:`ScanEvent` and :class:`EventRelay` -- the progress-event
  record and the sink a device thread's callbacks feed it through, so
  every Qt emit still happens on the one thread that owns the signals.
* :class:`ParallelRun` -- the mutable state shared by every thread of
  one parallel run.
* :func:`merge_walk_results` and :func:`merge_scan_results` -- the pure
  functions that fold several per-unit results back into the one a
  sequential scan over the same sources would have produced.
"""

from __future__ import annotations

import os
import queue
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pptrepair.cancel import OperationCancelled
from pptrepair.scan import ArchiveMaterial, FileOutcome, ScanResult
from pptrepair.walker import WalkResult

#: Upper bound on how many devices :class:`~pptrepair.gui.worker.ScanWorker`
#: reads from at the same time. A slow VPN-mounted NAS is worth
#: overlapping with the local SSD, but a machine with a dozen mounted
#: volumes must not answer a scan with a dozen concurrent readers;
#: devices beyond the limit share a thread and are scanned one after
#: another.
MAX_PARALLEL_DEVICE_SCANS = 4

#: How long the worker thread waits for the next queued progress event
#: before re-checking whether its device threads are still running.
#: Short enough that the run ends promptly, long enough not to spin.
EVENT_POLL_INTERVAL_S = 0.05

#: Ceiling on progress events queued but not yet emitted. A device
#: thread that outruns the worker thread blocks on the full queue
#: instead of piling up an unbounded backlog -- the same backpressure
#: the sequential path gets for free by emitting inline.
EVENT_QUEUE_MAXSIZE = 256

#: Progress event kinds queued by :class:`EventRelay`, one per core
#: callback :class:`~pptrepair.gui.worker.ScanWorker` relays to the UI.
EVENT_DIRECTORY = "directory"
EVENT_ARCHIVE_PROGRESS = "archive_progress"
EVENT_FILE = "file"
EVENT_MATERIAL = "material"
EVENT_DOWNLOAD = "download"


def device_of(path: Path) -> int | None:
    """Return the id of the device *path* lives on, or None if unknown.

    ``st_dev`` identifies the filesystem/volume, which is the closest
    stand-in the standard library offers for "the physical device this
    read will queue on": two sources with different ``st_dev`` values
    sit -- barring exotic setups -- on different disks or network
    mounts, so reading them at once overlaps their latencies instead of
    adding them up.

    None means the path could not be stat'ed at all (deleted, permission
    denied): such sources are indistinguishable from each other, so they
    share one group and stay sequential with respect to one another.

    A module-level function rather than a method so a test can
    monkeypatch it to synthesise a multi-device layout on a
    single-volume machine.
    """
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def merge_walk_results(walks: Sequence[WalkResult]) -> WalkResult:
    """Concatenate per-root discovery results into one, in input order.

    Every bucket is a plain input-order concatenation, which reproduces
    what a single :func:`~pptrepair.walker.discover_targets` call over
    the same roots returns: the walk carries no state from one root to
    the next other than its visited-directory set, and that set is only
    consulted with ``follow_symlinks`` -- the very mode the parallel
    path refuses to take (see
    :meth:`pptrepair.gui.worker.ScanWorker._plan_device_groups`).
    """
    merged = WalkResult()
    for walk in walks:
        merged.targets.extend(walk.targets)
        merged.skipped_legacy.extend(walk.skipped_legacy)
        merged.skipped_temp.extend(walk.skipped_temp)
        merged.skipped_cloud.extend(walk.skipped_cloud)
        merged.download_targets.extend(walk.download_targets)
        merged.archives.extend(walk.archives)
        merged.errors.extend(walk.errors)
        merged.skipped_oversize.extend(walk.skipped_oversize)
    return merged


def merge_scan_results(parts: Sequence[ScanResult]) -> ScanResult:
    """Merge one-root scan results into one, in the original root order.

    *parts* must be non-empty and ordered like the request's roots; the
    merged result is then indistinguishable from what one
    :func:`~pptrepair.scan.scan_paths` call over those roots produces.

    Field by field:

    * ``roots``, ``outcomes``, ``materials``, ``material_notes`` and
      every ``walk`` bucket: input-order concatenation.
    * ``report_dir``: the same value in every part -- they all come from
      one request, and the GUI never asks for a report directory at all
      -- so the first part's is carried over.
    * ``fingerprints_skipped``: summed, each part having counted its own
      overflow past :data:`~pptrepair.scan.MAX_FINGERPRINTS`. Splitting
      the run does make that budget per root rather than per run, which
      is moot here: without a ``report_dir`` no fingerprint is ever
      written and every part reports 0.
    * ``search_archives``: True when any part mined archives. The GUI
      always scans roots with ``search_archives=False`` (dropped
      archives are mined by units of their own), so this stays False;
      ``any`` merely keeps the field honest.
    """
    merged = ScanResult(
        roots=[root for part in parts for root in part.roots],
        walk=merge_walk_results([part.walk for part in parts]),
        report_dir=parts[0].report_dir,
        fingerprints_skipped=sum(part.fingerprints_skipped
                                 for part in parts),
        search_archives=any(part.search_archives for part in parts),
    )
    for part in parts:
        merged.outcomes.extend(part.outcomes)
        merged.materials.extend(part.materials)
        merged.material_notes.extend(part.material_notes)
    return merged


@dataclass(frozen=True)
class ScanUnit:
    """One indivisible piece of work of a parallelised scan.

    :ivar path: the FILE/FOLDER root to scan, or the archive to mine.
    :ivar index: the unit's slot in
        :attr:`~pptrepair.gui.worker.ScanRequest.roots` or
        :attr:`~pptrepair.gui.worker.ScanRequest.archives`. Its result is
        written back to that slot, which is what restores the input
        order on merge however the device threads happened to
        interleave.
    :ivar is_archive: True for an archive-mining unit, False for a root.
    """

    path: Path
    index: int
    is_archive: bool


@dataclass(frozen=True)
class ScanEvent:
    """One progress callback captured on a device thread, for replay.

    :ivar kind: which callback fired (an ``EVENT_*`` constant).
    :ivar args: that callback's own arguments, replayed unchanged.
    """

    kind: str
    args: tuple[object, ...]


class EventRelay:
    """Progress sink the core is handed on a device thread.

    Qt signals are deliberately *not* emitted here: on the parallel path
    the core's callbacks run on one of several device threads, and
    funnelling them through a queue keeps every emit on the single
    thread :meth:`~pptrepair.gui.worker.ScanWorker.run` owns -- which is
    what keeps the throttling state coherent and the emit order the UI
    sees well-defined.

    Every callback polls the shared cancellation flag before queueing,
    exactly as :class:`~pptrepair.gui.worker.ScanWorker`'s own callbacks
    do, so a cancel request stops each device thread at its next
    callback boundary rather than at the end of its work unit.
    """

    def __init__(self, events: queue.Queue[ScanEvent],
                 cancel_event: threading.Event) -> None:
        """Bind the relay to one run's event queue and cancellation flag."""
        self._events = events
        self._cancel_event = cancel_event

    def _put(self, kind: str, *args: object) -> None:
        """Queue one event, honouring a pending cancellation first.

        The queue is bounded, so a device thread that outruns the
        worker thread's delivery blocks here until there is room again.
        That block is always finite: the worker thread keeps draining
        for as long as any device thread is alive.
        """
        if self._cancel_event.is_set():
            raise OperationCancelled("scan cancelled by user")
        self._events.put(ScanEvent(kind, args))

    def on_directory(self, path: Path) -> None:
        """Core ``on_directory`` callback: queue the visited directory."""
        self._put(EVENT_DIRECTORY, path)

    def on_archive_progress(self, path: Path, done: int,
                            total: int) -> None:
        """Core ``archive_progress`` callback: queue the byte position."""
        self._put(EVENT_ARCHIVE_PROGRESS, path, done, total)

    def on_file_scanned(self, outcome: FileOutcome) -> None:
        """Core ``progress`` callback: queue the diagnosed file."""
        self._put(EVENT_FILE, outcome)

    def on_material_scanned(self, material: ArchiveMaterial) -> None:
        """Core ``material_progress`` callback: queue the mined member."""
        self._put(EVENT_MATERIAL, material)

    def on_download(self, path: Path) -> None:
        """Core ``on_download`` callback: queue the placeholder read."""
        self._put(EVENT_DOWNLOAD, path)


@dataclass
class ParallelRun:
    """Mutable state shared by the threads of one parallel scan.

    :ivar events: progress events queued by the device threads and
        emitted by the worker thread.
    :ivar roots: one slot per
        :attr:`~pptrepair.gui.worker.ScanRequest.roots` entry, filled
        with that root's :class:`~pptrepair.scan.ScanResult`.
    :ivar archives: one slot per
        :attr:`~pptrepair.gui.worker.ScanRequest.archives` entry, filled
        with that archive's ``(materials, notes)`` pair.
    :ivar failures: exceptions raised by the device threads, earliest
        first; guarded by :attr:`lock`, since any of them may append.
    :ivar lock: guards :attr:`failures` only. The result slots need no
        lock: every unit owns exactly one slot, and every unit is
        processed by exactly one thread.
    """

    events: queue.Queue[ScanEvent]
    roots: list[ScanResult | None]
    archives: list[tuple[list[ArchiveMaterial], list[str]] | None]
    failures: list[BaseException] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
