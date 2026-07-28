"""Background scan/repair workers for the PySide6 desktop application.

Scanning a directory tree (and, optionally, mining dropped backup
archives for donor material), and repairing every corrupted file it
finds, can both take seconds to minutes, so they must run off the UI
thread to keep the window responsive. This module wraps
:func:`pptrepair.scan.scan_paths` /
:func:`pptrepair.scan.diagnose_archive_materials` (:class:`ScanWorker`)
and :func:`pptrepair.batch.repair_paths` (:class:`RepairWorker`) in
:class:`~PySide6.QtCore.QThread` subclasses that stream progress back
to the UI and support cooperative cancellation.

Thread model
------------
Both workers override :meth:`~PySide6.QtCore.QThread.run` (neither
starts a Qt event loop of its own): :meth:`run`, and every callback it
hands to the core, execute on the worker thread. All communication back
to the UI happens exclusively through the Qt signals declared on each
class, which -- because emitter and receiver live in different threads
-- Qt delivers with the default queued connection, so the connected
slots run on the UI thread. Neither worker ever touches a GUI object
directly. Cancellation goes the other way: :meth:`cancel` sets a
:class:`threading.Event`, which is safe to call from the UI thread
while :meth:`run` reads it on the worker thread.

:class:`ScanWorker` may fan its work out over further
:class:`threading.Thread` workers -- one per physical device, when the
sources span several (see :meth:`ScanWorker._plan_device_groups`). Those
threads never emit anything themselves: their progress callbacks queue
events that the worker thread replays, so the "all emits come from the
one thread that owns the signals" rule above still holds, and so does
the cancellation contract (every device thread polls the same
:class:`threading.Event`).
"""

from __future__ import annotations

import os
import queue
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from pptrepair.archive import ArchiveMember, materialize
from pptrepair.batch import BatchItem, BatchResult, plan_output_bases, repair_paths
from pptrepair.cancel import OperationCancelled
from pptrepair.classify import Diagnosis, Verdict
from pptrepair.gui.i18n import tr
from pptrepair.gui.merge_plan import ApprovedMerge
from pptrepair.merge import MERGE_SUFFIX, merge_restore
from pptrepair.repair import RepairOutcome, predict_auto_mode, repair_file
from pptrepair.scan import (
    ArchiveMaterial,
    ArchiveMaterialCache,
    FileOutcome,
    ScanResult,
    diagnose_archive_materials,
    diagnose_file,
    scan_paths,
)
from pptrepair.walker import WalkResult

#: Minimum time (seconds) between two ``walk_progress`` emits, so a huge
#: tree does not flood the UI thread with one signal per directory. A
#: module attribute (rather than a class constant) so tests can
#: monkeypatch it to make the throttling deterministic.
_WALK_PROGRESS_INTERVAL_S = 0.2

#: Upper bound on how many devices :class:`ScanWorker` reads from at the
#: same time. A slow VPN-mounted NAS is worth overlapping with the local
#: SSD, but a machine with a dozen mounted volumes must not answer a
#: scan with a dozen concurrent readers; devices beyond the limit share
#: a thread and are scanned one after another.
_MAX_PARALLEL_DEVICE_SCANS = 4

#: How long the worker thread waits for the next queued progress event
#: before re-checking whether its device threads are still running.
#: Short enough that the run ends promptly, long enough not to spin.
_EVENT_POLL_INTERVAL_S = 0.05

#: Ceiling on progress events queued but not yet emitted. A device
#: thread that outruns the worker thread blocks on the full queue
#: instead of piling up an unbounded backlog -- the same backpressure
#: the sequential path gets for free by emitting inline.
_EVENT_QUEUE_MAXSIZE = 256

#: Progress event kinds queued by :class:`_EventRelay`, one per core
#: callback :class:`ScanWorker` relays to the UI.
_EVENT_DIRECTORY = "directory"
_EVENT_ARCHIVE_PROGRESS = "archive_progress"
_EVENT_FILE = "file"
_EVENT_MATERIAL = "material"
_EVENT_DOWNLOAD = "download"


def _device_of(path: Path) -> int | None:
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


def _merge_walk_results(walks: Sequence[WalkResult]) -> WalkResult:
    """Concatenate per-root discovery results into one, in input order.

    Every bucket is a plain input-order concatenation, which reproduces
    what a single :func:`~pptrepair.walker.discover_targets` call over
    the same roots returns: the walk carries no state from one root to
    the next other than its visited-directory set, and that set is only
    consulted with ``follow_symlinks`` -- the very mode the parallel
    path refuses to take (see :meth:`ScanWorker._plan_device_groups`).
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


def _merge_scan_results(parts: Sequence[ScanResult]) -> ScanResult:
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
        walk=_merge_walk_results([part.walk for part in parts]),
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
class _ScanUnit:
    """One indivisible piece of work of a parallelised scan.

    :ivar path: the FILE/FOLDER root to scan, or the archive to mine.
    :ivar index: the unit's slot in :attr:`ScanRequest.roots` or
        :attr:`ScanRequest.archives`. Its result is written back to that
        slot, which is what restores the input order on merge however
        the device threads happened to interleave.
    :ivar is_archive: True for an archive-mining unit, False for a root.
    """

    path: Path
    index: int
    is_archive: bool


@dataclass(frozen=True)
class _ScanEvent:
    """One progress callback captured on a device thread, for replay.

    :ivar kind: which callback fired (an ``_EVENT_*`` constant).
    :ivar args: that callback's own arguments, replayed unchanged.
    """

    kind: str
    args: tuple[object, ...]


class _EventRelay:
    """Progress sink the core is handed on a device thread.

    Qt signals are deliberately *not* emitted here: on the parallel path
    the core's callbacks run on one of several device threads, and
    funnelling them through a queue keeps every emit on the single
    thread :meth:`ScanWorker.run` owns -- which is what keeps the
    throttling state coherent and the emit order the UI sees
    well-defined.

    Every callback polls the shared cancellation flag before queueing,
    exactly as :class:`ScanWorker`'s own callbacks do, so a cancel
    request stops each device thread at its next callback boundary
    rather than at the end of its work unit.
    """

    def __init__(self, events: queue.Queue[_ScanEvent],
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
        self._events.put(_ScanEvent(kind, args))

    def on_directory(self, path: Path) -> None:
        """Core ``on_directory`` callback: queue the visited directory."""
        self._put(_EVENT_DIRECTORY, path)

    def on_archive_progress(self, path: Path, done: int,
                            total: int) -> None:
        """Core ``archive_progress`` callback: queue the byte position."""
        self._put(_EVENT_ARCHIVE_PROGRESS, path, done, total)

    def on_file_scanned(self, outcome: FileOutcome) -> None:
        """Core ``progress`` callback: queue the diagnosed file."""
        self._put(_EVENT_FILE, outcome)

    def on_material_scanned(self, material: ArchiveMaterial) -> None:
        """Core ``material_progress`` callback: queue the mined member."""
        self._put(_EVENT_MATERIAL, material)

    def on_download(self, path: Path) -> None:
        """Core ``on_download`` callback: queue the placeholder read."""
        self._put(_EVENT_DOWNLOAD, path)


@dataclass
class _ParallelRun:
    """Mutable state shared by the threads of one parallel scan.

    :ivar events: progress events queued by the device threads and
        emitted by the worker thread.
    :ivar roots: one slot per :attr:`ScanRequest.roots` entry, filled
        with that root's :class:`~pptrepair.scan.ScanResult`.
    :ivar archives: one slot per :attr:`ScanRequest.archives` entry,
        filled with that archive's ``(materials, notes)`` pair.
    :ivar failures: exceptions raised by the device threads, earliest
        first; guarded by :attr:`lock`, since any of them may append.
    :ivar lock: guards :attr:`failures` only. The result slots need no
        lock: every unit owns exactly one slot, and every unit is
        processed by exactly one thread.
    """

    events: queue.Queue[_ScanEvent]
    roots: list[ScanResult | None]
    archives: list[tuple[list[ArchiveMaterial], list[str]] | None]
    failures: list[BaseException] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(frozen=True)
class ScanRequest:
    """Immutable description of one scan the UI asks the worker to run.

    Being frozen, an instance is safe to hand across the thread
    boundary: the worker only ever reads it.

    :ivar roots: FILE and FOLDER source paths, scanned on disk (files
        diagnosed directly, folders walked recursively).
    :ivar archives: ARCHIVE source paths the user dropped explicitly,
        mined for donor material. Archives merely *found inside* a
        scanned folder are deliberately left alone (see :meth:`run`).
    :ivar follow_symlinks: forwarded to the discovery walk.
    :ivar allow_download: hydrate cloud-only placeholders when reading
        them, rather than skipping them.
    :ivar max_file_bytes: per-file size ceiling; a larger file is
        skipped (counted in ``ScanResult.walk.skipped_oversize``).
        ``None`` disables the limit.
    """

    roots: tuple[Path, ...]
    archives: tuple[Path, ...]
    follow_symlinks: bool = False
    allow_download: bool = False
    max_file_bytes: int | None = None


@dataclass
class GuiScanResult:
    """Aggregate outcome carried by :attr:`ScanWorker.finished_ok`.

    :ivar scan: the on-disk scan result, or ``None`` when the request
        carried no FILE/FOLDER roots (archives-only run).
    :ivar materials: donor material mined from the dropped archives, in
        encounter order; empty when no archives were requested.
    :ivar material_notes: English notes gathered while enumerating and
        extracting archive members (encrypted/unreadable members, etc.).
    """

    scan: ScanResult | None
    materials: list[ArchiveMaterial] = field(default_factory=list)
    material_notes: list[str] = field(default_factory=list)


class ScanWorker(QThread):
    """Run a :class:`ScanRequest` off the UI thread, streaming progress.

    Signals (all delivered on the UI thread via queued connections):

    * :attr:`walk_progress` -- the directory (as a ``str`` path) the
      discovery walk is currently visiting, throttled to at most one
      emit per :data:`_WALK_PROGRESS_INTERVAL_S`. Fired before any file
      is diagnosed, so a large tree shows activity during the
      otherwise-silent discovery phase.
    * :attr:`archive_progress` -- how far into one backup archive
      (``str`` path, done bytes, total bytes) the mining pass has got,
      throttled the same way. The only sign of life while a single
      multi-gigabyte archive is being read, where whole minutes can
      pass between two :attr:`material_scanned` emits.
    * :attr:`file_scanned` -- one on-disk file was diagnosed
      (:class:`~pptrepair.scan.FileOutcome`).
    * :attr:`material_scanned` -- one archive member was mined as donor
      material (:class:`~pptrepair.scan.ArchiveMaterial`).
    * :attr:`download_started` -- a cloud-only placeholder
      (:class:`~pathlib.Path`) is about to be read, which blocks while
      the sync client hydrates it.
    * :attr:`finished_ok` -- the run completed; carries a
      :class:`GuiScanResult`.
    * :attr:`failed` -- the run raised an unexpected exception; carries
      a ``"<type>: <message>"`` summary.
    * :attr:`cancelled` -- the run was stopped by :meth:`cancel`.

    Exactly one of :attr:`finished_ok`, :attr:`failed` or
    :attr:`cancelled` is emitted per run, before the inherited
    :attr:`~PySide6.QtCore.QThread.finished` signal.

    Parallel device scans
    ---------------------
    Sources that live on *different* devices (a local SSD and a
    VPN-mounted NAS, say) are scanned concurrently, so the run is no
    longer the sum of every device's latency. The work is cut into units
    -- one per FILE/FOLDER root, one per dropped archive -- grouped by
    :func:`_device_of`, and each group is processed strictly
    sequentially by one of at most :data:`_MAX_PARALLEL_DEVICE_SCANS`
    threads (see :meth:`_plan_device_groups`). Two units of the same
    device are never read at once: that would only turn a sequential
    read into a seek storm on a spinning disk, and would multiply the
    one-member-at-a-time memory ceiling archive mining guarantees.

    The whole thing falls back to the plain sequential path -- byte for
    byte the pre-parallel behaviour -- whenever ``follow_symlinks`` is
    set or every source shares one device.

    Determinism: each unit's result is kept in its own slot and merged
    back in request order once every thread has finished, so the
    :class:`GuiScanResult` a parallel run produces (contents *and*
    order) is the one a sequential run would have produced. Only the
    interleaving of the progress signals differs, which also means the
    two throttled signals may report alternately from two devices.

    The session cache is shared by the device threads. Its own lock
    keeps its bookkeeping consistent, and the grouping guarantees the
    one thing that lock cannot: one archive path belongs to exactly one
    group, so no two threads ever mine the same archive.
    """

    walk_progress = Signal(str)
    archive_progress = Signal(str, int, int)
    file_scanned = Signal(object)
    material_scanned = Signal(object)
    download_started = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, request: ScanRequest,
                 parent: QObject | None = None, *,
                 cache: ArchiveMaterialCache | None = None) -> None:
        """Store *request* and prepare the cancellation flag.

        Runs on the UI thread (the thread that constructs the worker).

        :param request: the scan to run once :meth:`start` is called.
        :param parent: optional Qt parent object.
        :param cache: the session-lifetime archive material cache to mine
            into, typically owned by the window that starts this worker.
            The extracted donor bytes stay in it after the scan, so a
            rescan -- and the repair phase (see
            :class:`MultiRepairWorker`) -- never has to read the same
            archive twice. Its bookkeeping is lock-guarded, so this
            worker's device threads may mine into it concurrently (each
            archive belonging to exactly one of them); the UI must still
            not start a second *worker* on it while this one is alive,
            since nothing then guarantees the two would not mine the
            same archive. ``None`` restores the pre-cache behaviour:
            every archive mined into a private temporary directory that
            is deleted again on the way out.
        """
        super().__init__(parent)
        self._request = request
        self._cache = cache
        self._cancel_event = threading.Event()
        #: Monotonic timestamp of the last walk_progress emit, or None
        #: before the first directory is visited (so that first visit
        #: always emits regardless of the throttling interval).
        self._last_walk_progress: float | None = None
        #: Monotonic timestamp of the last archive_progress emit, kept
        #: apart from the walk's so neither phase can throttle the
        #: other, and the archive that emit reported (None before the
        #: first one).
        self._last_archive_progress: float | None = None
        self._last_archive_path: Path | None = None

    def cancel(self) -> None:
        """Request cooperative cancellation of the running scan.

        Thread-safe and non-blocking: sets a :class:`threading.Event`
        that the worker's callbacks poll. Typically called from the UI
        thread. The scan stops at the next callback boundary, after
        which :attr:`cancelled` is emitted. Calling this before or after
        the run, or more than once, is harmless.
        """
        self._cancel_event.set()

    def run(self) -> None:
        """Execute the request on the worker thread.

        Runs the two phases the design mandates and translates the
        outcome into exactly one terminal signal:

        1. When :attr:`ScanRequest.roots` is non-empty, scan those paths
           on disk with ``search_archives=False`` -- so archives that
           merely *happen to live inside* a scanned folder are never
           opened.
        2. When :attr:`ScanRequest.archives` is non-empty, mine only the
           explicitly dropped archives for donor material through a
           separate :func:`~pptrepair.scan.diagnose_archive_materials`
           call.

        This two-call split is the whole point: the user's intent is
        "diagnose these files/folders, and additionally treat *these*
        archives as donor material", not "spelunk every archive found
        underfoot". Sources spread over several devices split the two
        phases further, into one call per root and per archive run on
        one thread per device (see :meth:`_plan_device_groups`), which
        leaves both the work done and the result produced unchanged.

        Terminal signal: :attr:`cancelled` if a callback raised
        :class:`~pptrepair.cancel.OperationCancelled`, :attr:`failed`
        for any other exception, otherwise :attr:`finished_ok`.
        """
        try:
            result = self._execute()
        except OperationCancelled:
            # Cooperative stop requested through cancel(); not an error.
            self.cancelled.emit()
        except Exception as exc:
            # Any unexpected failure is summarised for the UI rather than
            # left to terminate the worker thread silently.
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.finished_ok.emit(result)

    def _execute(self) -> GuiScanResult:
        """Run the scan and assemble the :class:`GuiScanResult`.

        Runs on the worker thread. Dispatches between the two paths --
        one thread per device when the sources span several (see
        :meth:`_plan_device_groups`), the strictly sequential original
        otherwise -- both of which return the very same result.

        The cancellation flag is checked here first, and then inside
        every progress callback (plus at every unit boundary on the
        parallel path), so a cancel request never has to wait for a
        whole phase to complete.
        """
        self._raise_if_cancelled()
        buckets = self._plan_device_groups()
        if buckets is None:
            return self._execute_sequentially()
        return self._execute_in_parallel(buckets)

    def _execute_sequentially(self) -> GuiScanResult:
        """Run both scan phases on this thread, one after the other.

        The pre-parallel code path, unchanged: one
        :func:`~pptrepair.scan.scan_paths` call over every root, then one
        :func:`~pptrepair.scan.diagnose_archive_materials` call over
        every dropped archive, with the cancellation flag checked
        between the two.

        The archive phase mines into this worker's session cache (when
        it was given one), so an archive already mined in this session
        is replayed rather than reopened, and the members extracted here
        stay on disk for the repair phase to splice from.
        """
        scan_result: ScanResult | None = None
        if self._request.roots:
            scan_result = scan_paths(
                list(self._request.roots),
                follow_symlinks=self._request.follow_symlinks,
                allow_download=self._request.allow_download,
                max_file_bytes=self._request.max_file_bytes,
                search_archives=False,
                progress=self._on_file_scanned,
                on_download=self._on_download,
                on_directory=self._on_directory,
            )

        self._raise_if_cancelled()

        materials: list[ArchiveMaterial] = []
        material_notes: list[str] = []
        if self._request.archives:
            materials, material_notes = diagnose_archive_materials(
                list(self._request.archives),
                on_download=self._on_download,
                download_targets=(),
                material_progress=self._on_material_scanned,
                cache=self._cache,
                archive_progress=self._on_archive_progress,
            )

        return GuiScanResult(scan=scan_result, materials=materials,
                             material_notes=material_notes)

    def _plan_device_groups(self) -> list[list[_ScanUnit]] | None:
        """Return the per-thread work lists, or None to run sequentially.

        The request is cut into units -- one per FILE/FOLDER root, then
        one per dropped archive, so that within any device the roots are
        still scanned before the archives are mined, exactly as the
        sequential path orders its two phases -- and those units are
        grouped by the device :func:`_device_of` reports for their path.

        The groups are then dealt out round-robin over
        ``min(_MAX_PARALLEL_DEVICE_SCANS, len(groups))`` threads, so a
        thread may end up serving several devices; it processes them one
        group after another, and each group in input order.

        None -- meaning "take the sequential path, unchanged" -- is
        returned whenever

        * ``follow_symlinks`` is set: the discovery walk then shares one
          visited-directory set *across* roots to break symlink cycles
          and diamonds, and splitting the roots over separate
          :func:`~pptrepair.scan.scan_paths` calls would silently change
          that semantics. (This is decided before any path is stat'ed,
          so :func:`_device_of` is not even consulted.)
        * every unit lives on one device, or there are no units at all:
          nothing is left to overlap, and a lone group has no reason to
          leave the worker thread.
        """
        if self._request.follow_symlinks:
            return None

        units = [_ScanUnit(path=path, index=index, is_archive=False)
                 for index, path in enumerate(self._request.roots)]
        units.extend(_ScanUnit(path=path, index=index, is_archive=True)
                     for index, path in enumerate(self._request.archives))

        groups: dict[int | None, list[_ScanUnit]] = {}
        for unit in units:
            groups.setdefault(_device_of(unit.path), []).append(unit)
        if len(groups) <= 1:
            return None

        thread_count = min(_MAX_PARALLEL_DEVICE_SCANS, len(groups))
        buckets: list[list[_ScanUnit]] = [[] for _ in range(thread_count)]
        for index, group in enumerate(groups.values()):
            buckets[index % thread_count].extend(group)
        return buckets

    def _execute_in_parallel(
            self, buckets: Sequence[Sequence[_ScanUnit]]) -> GuiScanResult:
        """Run *buckets* on one thread each and merge their results.

        Runs on the worker thread, which starts the device threads and
        then does nothing but deliver the progress events they queue
        (:meth:`_pump_events`) until the last of them has ended -- so
        every Qt signal still originates here, on the one thread that
        owns them.

        No device thread ever outlives this call: the pump only returns
        once they are all gone, and the ``finally`` joins them on every
        path out, including the one where the pump itself raised.

        The first exception any thread recorded is re-raised here, which
        puts the run back on the terminal-signal contract :meth:`run`
        implements: :class:`~pptrepair.cancel.OperationCancelled` from a
        cancel request becomes :attr:`cancelled`, anything else
        :attr:`failed`.
        """
        state = _ParallelRun(
            events=queue.Queue(maxsize=_EVENT_QUEUE_MAXSIZE),
            roots=[None] * len(self._request.roots),
            archives=[None] * len(self._request.archives),
        )
        relay = _EventRelay(state.events, self._cancel_event)
        threads = [
            threading.Thread(
                target=self._run_device_group, args=(bucket, relay, state),
                name=f"pptrepair-scan-device{index}", daemon=True)
            for index, bucket in enumerate(buckets)
        ]
        for thread in threads:
            thread.start()
        try:
            self._pump_events(state.events, threads)
        finally:
            for thread in threads:
                thread.join()

        if state.failures:
            raise state.failures[0]
        return self._assemble(state)

    def _run_device_group(self, units: Sequence[_ScanUnit],
                          relay: _EventRelay, state: _ParallelRun) -> None:
        """Process one thread's units, one after another.

        Runs on a device thread. The cancellation flag is polled at
        every unit boundary as well as inside every callback, and each
        unit's result is written straight into its own slot, so nothing
        here needs a lock.

        Any exception -- the cancellation included -- is recorded and
        ends this thread: it is re-raised on the worker thread by
        :meth:`_execute_in_parallel`, which is the only thread allowed
        to speak to the UI. Setting the shared cancellation flag on the
        way out stops the *other* device threads too, since a run that
        has already failed (or been cancelled) has nothing left to gain
        from the sources they are still reading.
        """
        try:
            for unit in units:
                self._raise_if_cancelled()
                if unit.is_archive:
                    state.archives[unit.index] = self._mine_one_archive(
                        unit.path, relay)
                else:
                    state.roots[unit.index] = self._scan_one_root(
                        unit.path, relay)
        except BaseException as exc:
            with state.lock:
                state.failures.append(exc)
            self._cancel_event.set()

    def _scan_one_root(self, root: Path, relay: _EventRelay) -> ScanResult:
        """Scan one FILE/FOLDER root, queueing its progress through *relay*.

        Runs on a device thread. The knobs match
        :meth:`_execute_sequentially`'s single call exactly, including
        ``search_archives=False`` -- an archive that merely happens to
        live inside a scanned folder is still never opened.
        ``follow_symlinks`` is False by construction here (that mode
        never reaches the parallel path) but is passed through rather
        than hard-coded, so the two paths cannot drift apart.
        """
        return scan_paths(
            [root],
            follow_symlinks=self._request.follow_symlinks,
            allow_download=self._request.allow_download,
            max_file_bytes=self._request.max_file_bytes,
            search_archives=False,
            progress=relay.on_file_scanned,
            on_download=relay.on_download,
            on_directory=relay.on_directory,
        )

    def _mine_one_archive(
            self, archive: Path, relay: _EventRelay
    ) -> tuple[list[ArchiveMaterial], list[str]]:
        """Mine one dropped archive, queueing its progress through *relay*.

        Runs on a device thread, mining into the shared session cache
        just as :meth:`_execute_sequentially` does. No other thread can
        be mining *this* archive at the same time: a path belongs to
        exactly one device group, whose units run one after another.
        """
        return diagnose_archive_materials(
            [archive],
            on_download=relay.on_download,
            download_targets=(),
            material_progress=relay.on_material_scanned,
            cache=self._cache,
            archive_progress=relay.on_archive_progress,
        )

    def _pump_events(self, events: queue.Queue[_ScanEvent],
                     threads: Sequence[threading.Thread]) -> None:
        """Emit every queued progress event until the last thread ends.

        Runs on the worker thread. Polling *threads* (rather than
        waiting on the queue forever) is what lets the pump notice that
        the producers are gone; the queue is then drained to the last
        item, so an event queued just before a thread died is still
        delivered.

        A failure while emitting -- a slot that raised, say -- must not
        strand the device threads: the cancellation flag is set and the
        queue keeps being drained (silently) so that anything blocked on
        a full queue can finish, and the failure is re-raised only once
        every producer has ended.
        """
        failure: BaseException | None = None
        while any(thread.is_alive() for thread in threads):
            try:
                event = events.get(timeout=_EVENT_POLL_INTERVAL_S)
            except queue.Empty:
                continue
            if failure is not None:
                continue  # draining only, to unblock the producers
            try:
                self._dispatch_event(event)
            except BaseException as exc:
                failure = exc
                self._cancel_event.set()

        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if failure is None:
                self._dispatch_event(event)

        if failure is not None:
            raise failure

    def _dispatch_event(self, event: _ScanEvent) -> None:
        """Turn one queued event back into the emit it stands for.

        Runs on the worker thread, so the throttling state the relays
        consult is touched from that one thread only -- and an unknown
        kind, which only a programming error could produce, is ignored
        rather than allowed to abort a scan that has otherwise
        succeeded.
        """
        if event.kind == _EVENT_DIRECTORY:
            self._relay_directory(*event.args)
        elif event.kind == _EVENT_ARCHIVE_PROGRESS:
            self._relay_archive_progress(*event.args)
        elif event.kind == _EVENT_FILE:
            self.file_scanned.emit(*event.args)
        elif event.kind == _EVENT_MATERIAL:
            self.material_scanned.emit(*event.args)
        elif event.kind == _EVENT_DOWNLOAD:
            self.download_started.emit(*event.args)

    def _assemble(self, state: _ParallelRun) -> GuiScanResult:
        """Merge the finished units into one result, in request order.

        Runs on the worker thread once every device thread has ended.
        The slots are filled in unit order, never in completion order,
        so the outcome is the sequential run's outcome exactly (see
        :func:`_merge_scan_results`). An empty ``scan`` -- rather than
        an empty :class:`~pptrepair.scan.ScanResult` -- is what an
        archives-only request must produce.
        """
        parts = [result for result in state.roots if result is not None]
        materials: list[ArchiveMaterial] = []
        material_notes: list[str] = []
        for mined in state.archives:
            if mined is None:
                continue
            materials.extend(mined[0])
            material_notes.extend(mined[1])
        return GuiScanResult(
            scan=_merge_scan_results(parts) if parts else None,
            materials=materials, material_notes=material_notes)

    def _raise_if_cancelled(self) -> None:
        """Raise :class:`OperationCancelled` when cancellation is pending.

        Runs on the worker thread, and -- on the parallel path -- on
        every device thread; reading a :class:`threading.Event` is safe
        from any of them. Raising is the core's documented contract for
        aborting a run in progress; the exception is caught by
        :meth:`run` and turned into the :attr:`cancelled` signal.
        """
        if self._cancel_event.is_set():
            raise OperationCancelled("scan cancelled by user")

    def _on_directory(self, path: Path) -> None:
        """Core ``on_directory`` callback: relay the walk's current directory.

        Runs on the worker thread, during the discovery walk (before
        any file is diagnosed). Honours a pending cancellation first --
        this is what makes cancellation responsive even while the walk
        itself is still enumerating a large tree, with no file yet
        diagnosed to hang the check off of. Then hands the directory to
        :meth:`_relay_directory` for the throttled emit.

        The sequential path's callback; the parallel one queues the
        directory instead (:meth:`_EventRelay.on_directory`) and reaches
        :meth:`_relay_directory` from :meth:`_dispatch_event`.
        """
        self._raise_if_cancelled()
        self._relay_directory(path)

    def _relay_directory(self, path: Path) -> None:
        """Emit :attr:`walk_progress` for *path*, throttled.

        Runs on the worker thread (both paths). Emits only when at least
        :data:`_WALK_PROGRESS_INTERVAL_S` has elapsed since the previous
        emit -- the first directory visited always emits -- so a huge
        tree does not flood the UI thread with one signal per directory.
        The timestamp behind that decision is touched from this one
        thread only, which is why it needs no lock.
        """
        now = time.monotonic()
        if (self._last_walk_progress is not None
                and now - self._last_walk_progress
                < _WALK_PROGRESS_INTERVAL_S):
            return
        self._last_walk_progress = now
        self.walk_progress.emit(str(path))

    def _on_archive_progress(self, path: Path, done: int,
                             total: int) -> None:
        """Core ``archive_progress`` callback: relay one archive's position.

        Runs on the worker thread, while an archive is being swept.
        Honours a pending cancellation first -- and that check is the
        whole reason this callback polls at all: reading a single
        multi-gigabyte archive can take minutes during which no member
        is finished, so without it a Cancel click would appear to do
        nothing until the archive had been read in full.

        The sequential path's callback; the parallel one queues the
        position instead (:meth:`_EventRelay.on_archive_progress`) and
        reaches :meth:`_relay_archive_progress` from
        :meth:`_dispatch_event`.
        """
        self._raise_if_cancelled()
        self._relay_archive_progress(path, done, total)

    def _relay_archive_progress(self, path: Path, done: int,
                                total: int) -> None:
        """Emit :attr:`archive_progress` for one archive, throttled.

        Runs on the worker thread (both paths). The emit is throttled to
        at most one per :data:`_WALK_PROGRESS_INTERVAL_S`, off a
        timestamp of its own (the discovery walk's must not be able to
        swallow an archive update, or the other way round). Moving on to
        a *different* archive always emits, whatever the interval says:
        that transition is the one update the user must not miss, since
        it is what renames the file being read -- which, when two
        devices are mined at once, also means their alternating updates
        are not throttled against each other.
        """
        now = time.monotonic()
        if (path == self._last_archive_path
                and self._last_archive_progress is not None
                and now - self._last_archive_progress
                < _WALK_PROGRESS_INTERVAL_S):
            return
        self._last_archive_progress = now
        self._last_archive_path = path
        self.archive_progress.emit(str(path), done, total)

    def _on_file_scanned(self, outcome: FileOutcome) -> None:
        """Core ``progress`` callback: relay one diagnosed file.

        Runs on the worker thread. Honours a pending cancellation
        first, then emits :attr:`file_scanned` for delivery on the UI
        thread. The sequential path's callback; the parallel one queues
        the outcome (:meth:`_EventRelay.on_file_scanned`) for the very
        same emit, made from :meth:`_dispatch_event`.
        """
        self._raise_if_cancelled()
        self.file_scanned.emit(outcome)

    def _on_material_scanned(self, material: ArchiveMaterial) -> None:
        """Core ``material_progress`` callback: relay one mined member.

        Runs on the worker thread. Honours a pending cancellation
        first, then emits :attr:`material_scanned` for delivery on the
        UI thread. The sequential path's callback; the parallel one
        queues the member (:meth:`_EventRelay.on_material_scanned`) for
        the very same emit, made from :meth:`_dispatch_event`.
        """
        self._raise_if_cancelled()
        self.material_scanned.emit(material)

    def _on_download(self, path: Path) -> None:
        """Core ``on_download`` callback: announce a placeholder read.

        Runs on the worker thread. Honours a pending cancellation
        first, then emits :attr:`download_started` for delivery on the
        UI thread. The sequential path's callback; the parallel one
        queues the path (:meth:`_EventRelay.on_download`) for the very
        same emit, made from :meth:`_dispatch_event`.
        """
        self._raise_if_cancelled()
        self.download_started.emit(path)


# --------------------------------------------------------------------------
# Multi-source (merge) repair
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MultiRepairRequest:
    """Immutable description of one multi-source repair run.

    Being frozen, an instance is safe to hand across the thread
    boundary: the worker only ever reads it.

    :ivar merges: the approved per-target donor selections that carry at
        least one donor; each is byte-spliced through
        :func:`pptrepair.merge.merge_restore`.
    :ivar fallback_targets: corrupted files whose approved selection held
        no donor, repaired on their own bytes through
        :func:`pptrepair.repair.repair_file`.
    :ivar roots: the FILE/FOLDER roots the scan ran over, used only to
        mirror the input tree into *output_dir* (unused in *in_place*
        mode).
    :ivar output_dir: the aggregate output directory, or None in
        *in_place* mode (where each artifact is written next to its own
        source).
    :ivar in_place: True to write next to each source rather than into
        *output_dir*.
    :ivar lang: language code forwarded to
        :func:`pptrepair.merge.merge_restore` and
        :func:`pptrepair.repair.repair_file`.
    """

    merges: tuple[ApprovedMerge, ...]
    fallback_targets: tuple[Path, ...]
    roots: tuple[Path, ...]
    output_dir: Path | None
    in_place: bool
    lang: str = "en"


@dataclass
class MergeItemOutcome:
    """Per-target result of one :func:`pptrepair.merge.merge_restore` call.

    :ivar target: the corrupted file this merge reconstructed.
    :ivar success: True when a merged artifact was produced (any
        ``full`` / ``partial`` / ``hybrid`` guarantee), False on a
        ``failed`` guarantee or a raised exception.
    :ivar output_path: the merged artifact's path on success, else None.
    :ivar detail: a short human-readable summary -- the artifact name on
        success, the guarantee/reason on failure.
    """

    target: Path
    success: bool
    output_path: Path | None
    detail: str


@dataclass
class MultiRepairResult:
    """Aggregate outcome carried by :attr:`MultiRepairWorker.finished_ok`.

    :ivar merges: one :class:`MergeItemOutcome` per processed merge, in
        request order.
    :ivar fallbacks: one :class:`~pptrepair.repair.RepairOutcome` per
        processed fallback target, in request order.
    """

    merges: list[MergeItemOutcome] = field(default_factory=list)
    fallbacks: list[RepairOutcome] = field(default_factory=list)


class MultiRepairWorker(QThread):
    """Run a :class:`MultiRepairRequest` off the UI thread.

    Signals (all delivered on the UI thread via queued connections):

    * :attr:`merge_done` -- one merge finished (:class:`MergeItemOutcome`).
    * :attr:`file_repaired` -- one fallback target was repaired, skipped
      or found unrepairable (:class:`~pptrepair.repair.RepairOutcome`).
    * :attr:`finished_ok` -- the run completed; carries a
      :class:`MultiRepairResult`.
    * :attr:`failed` -- the run raised an unexpected exception; carries a
      ``"<type>: <message>"`` summary.
    * :attr:`cancelled` -- the run was stopped by :meth:`cancel`.

    Exactly one of :attr:`finished_ok`, :attr:`failed` or
    :attr:`cancelled` is emitted per run, before the inherited
    :attr:`~PySide6.QtCore.QThread.finished` signal.

    Isolation contract: a single merge or fallback that raises is
    recorded as an unsuccessful item and the run continues -- only a
    :class:`~pptrepair.cancel.OperationCancelled` (raised at an
    item boundary by :meth:`cancel`) aborts the whole run.
    """

    merge_done = Signal(object)
    file_repaired = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, request: MultiRepairRequest,
                 parent: QObject | None = None, *,
                 cache: ArchiveMaterialCache | None = None) -> None:
        """Store *request* and prepare the cancellation flag.

        Runs on the UI thread (the thread that constructs the worker).

        :param request: the multi-source repair to run once
            :meth:`start` is called.
        :param parent: optional Qt parent object.
        :param cache: the session-lifetime archive material cache the
            preceding scan mined into, whose extracted donors this run
            splices straight from (see :meth:`_materialize_donors`).
            This worker only ever reads from it, and does so from its
            own thread alone; the GUI runs one worker at a time (a
            repair starts from a finished scan's result), so nothing is
            mining into it meanwhile either. ``None`` -- or a member the
            cache can no longer vouch for -- falls back to extracting
            the donor afresh.
        """
        super().__init__(parent)
        self._request = request
        self._cache = cache
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Request cooperative cancellation of the running repair.

        Thread-safe and non-blocking: sets a :class:`threading.Event`
        the worker polls at each item boundary. The run stops before the
        next merge/fallback begins, after which :attr:`cancelled` is
        emitted; every artifact already written stays in place. Calling
        this before or after the run, or more than once, is harmless.
        """
        self._cancel_event.set()

    def run(self) -> None:
        """Execute the request on the worker thread.

        Materializes any archive donors, runs every merge, then every
        fallback repair, and translates the outcome into exactly one
        terminal signal.

        Terminal signal: :attr:`cancelled` when a cancellation was
        requested at an item boundary, :attr:`failed` for any other
        unexpected exception, otherwise :attr:`finished_ok`.
        """
        try:
            result = self._execute()
        except OperationCancelled:
            # Cooperative stop requested through cancel(); not an error.
            self.cancelled.emit()
        except Exception as exc:
            # Any unexpected failure is summarised for the UI rather than
            # left to terminate the worker thread silently.
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.finished_ok.emit(result)

    def _execute(self) -> MultiRepairResult:
        """Run the merges then the fallbacks and assemble the result.

        Runs on the worker thread under a single
        :class:`tempfile.TemporaryDirectory` spanning the whole run, so
        every archive donor extracted *here* is cleaned up on exit (the
        merged/repaired artifacts themselves are written elsewhere, on
        disk, and survive). A donor served from the session cache lives
        outside this directory and is deliberately left alone: the cache
        belongs to the window, not to this run. The cancellation flag is
        polled before materialization and at every item boundary.
        """
        self._raise_if_cancelled()
        result = MultiRepairResult()
        bases = self._plan_output_bases()
        with tempfile.TemporaryDirectory(
                prefix="pptrepair-gui-merge-") as tmp_dir:
            material_paths, display = self._materialize_donors(Path(tmp_dir))
            for merge in self._request.merges:
                self._raise_if_cancelled()
                item = self._run_one_merge(merge, material_paths, display,
                                           bases)
                result.merges.append(item)
                self.merge_done.emit(item)
            for target in self._request.fallback_targets:
                self._raise_if_cancelled()
                outcome = self._run_one_fallback(target, bases)
                result.fallbacks.append(outcome)
                self.file_repaired.emit(outcome)
        return result

    def _plan_output_bases(self) -> dict[Path, Path] | None:
        """Return a ``target -> suffix-less output base`` map, or None.

        None in *in_place* mode (each artifact is written next to its own
        source through the core's own default naming). Otherwise the
        aggregate bases are computed with
        :func:`pptrepair.batch.plan_output_bases` over every target --
        merges and fallbacks alike -- so the output tree mirrors the
        input exactly as single-file :func:`pptrepair.batch.repair_paths`
        would.
        """
        if self._request.in_place or self._request.output_dir is None:
            return None
        targets = [merge.target for merge in self._request.merges]
        targets.extend(self._request.fallback_targets)
        bases, _warnings = plan_output_bases(
            targets, list(self._request.roots), self._request.output_dir)
        return dict(zip(targets, bases))

    def _materialize_donors(
            self, tmp_root: Path
    ) -> tuple[dict[ArchiveMember, Path], dict[Path, str]]:
        """Obtain every archive donor's bytes, reusing the scan's extraction.

        Returns ``(material_paths, display)``: *material_paths* maps each
        archive member to the plain on-disk file holding its payload, and
        *display* maps that file back to the member's
        ``"<archive>::<member>"`` label -- the map
        :func:`pptrepair.merge.merge_restore`'s ``display`` argument uses
        to keep the temporary path out of every note.

        A member the session cache still vouches for is taken from there
        as-is, never copied: the scan already streamed it out of the
        archive, and re-reading a hundred-gigabyte ``.tar.gz`` a second
        time -- which, being a single compressed stream, means
        decompressing it from its very first byte -- purely to fetch
        bytes that are already sitting on disk is exactly the cost this
        cache exists to avoid. Only the members it cannot serve (no
        cache at all, an archive rewritten since the scan, an extraction
        that had failed) are grouped by archive and extracted into their
        own subdirectory of *tmp_root*, one open per archive, so names
        never collide across archives. A member whose extraction fails
        too is simply absent from *material_paths* (its donor is then
        dropped when the merge's sources are assembled).

        The cache is only ever read here, on the worker thread -- see
        :meth:`__init__` for why no other thread is touching it at the
        time.
        """
        material_paths: dict[ArchiveMember, Path] = {}
        display: dict[Path, str] = {}
        members_by_archive: dict[Path, list[ArchiveMember]] = {}
        seen: set[ArchiveMember] = set()
        for merge in self._request.merges:
            for donor in merge.donors:
                material = donor.material
                if material is None or material.member in seen:
                    continue
                seen.add(material.member)
                cached_path = self._cached_member_path(
                    material.archive_path, material.member)
                if cached_path is not None:
                    material_paths[material.member] = cached_path
                    display[cached_path] = material.member.display()
                    continue
                members_by_archive.setdefault(
                    material.archive_path, []).append(material.member)

        for index, (archive_path, members) in enumerate(
                members_by_archive.items()):
            self._raise_if_cancelled()
            member_dir = tmp_root / f"archive{index:04d}"
            member_dir.mkdir()
            extracted, _notes = materialize(archive_path, members, member_dir)
            for member, dest_path in extracted.items():
                material_paths[member] = dest_path
                display[dest_path] = member.display()
        return material_paths, display

    def _cached_member_path(self, archive_path: Path,
                            member: ArchiveMember) -> Path | None:
        """Return *member*'s already-extracted file, when the cache has it.

        None whenever this run has no cache, or the cache cannot vouch
        for that member any more, which tells
        :meth:`_materialize_donors` to extract it from the archive
        itself.
        """
        if self._cache is None:
            return None
        return self._cache.member_path(archive_path, member)

    def _run_one_merge(
            self, merge: ApprovedMerge,
            material_paths: dict[ArchiveMember, Path],
            display: dict[Path, str],
            bases: dict[Path, Path] | None) -> MergeItemOutcome:
        """Byte-splice one target against its approved donors.

        Assembles ``[target, *donor paths]`` (an archive donor whose
        member failed to materialize is dropped), resolves the output
        path, and calls :func:`pptrepair.merge.merge_restore`. Any
        exception other than :class:`~pptrepair.cancel.OperationCancelled`
        is caught and reported as an unsuccessful outcome so the run
        continues.
        """
        sources = [merge.target]
        for donor in merge.donors:
            if donor.path is not None:
                sources.append(donor.path)
            elif donor.material is not None:
                dest_path = material_paths.get(donor.material.member)
                if dest_path is not None:
                    sources.append(dest_path)

        output = self._merge_output_path(merge.target, bases)
        try:
            outcome = merge_restore(
                sources, output=output, force=False,
                allow_candidate=merge.allow_candidate,
                allow_lineage=merge.allow_lineage,
                lang=self._request.lang, display=display)
        except OperationCancelled:
            raise
        except Exception as exc:
            return MergeItemOutcome(
                target=merge.target, success=False, output_path=None,
                detail=f"{type(exc).__name__}: {exc}")

        if outcome.guarantee == "failed":
            return MergeItemOutcome(
                target=merge.target, success=False, output_path=None,
                detail=tr(
                    "merge failed: no output could be reconstructed"))
        detail = outcome.guarantee
        if outcome.output_path is not None:
            detail = f"{outcome.guarantee}: {outcome.output_path.name}"
        return MergeItemOutcome(
            target=merge.target, success=True,
            output_path=outcome.output_path, detail=detail)

    def _merge_output_path(self, target: Path,
                           bases: dict[Path, Path] | None) -> Path | None:
        """Return the merge output path for *target*, or None for in-place.

        None lets :func:`pptrepair.merge.merge_restore` write its default
        ``<stem>.merged.pptx`` next to the target. In aggregate mode the
        merge suffix is appended to the mirrored base and the base's
        parent directory is created first (merge_restore writes the file
        directly, without creating parents).
        """
        if bases is None:
            return None
        base = bases[target]
        base.parent.mkdir(parents=True, exist_ok=True)
        return base.with_name(base.name + MERGE_SUFFIX)

    def _run_one_fallback(self, target: Path,
                          bases: dict[Path, Path] | None) -> RepairOutcome:
        """Repair one donor-less target on its own bytes.

        Diagnoses *target* once (reusing that diagnosis for
        :func:`pptrepair.repair.repair_file`), mirrors its output base in
        aggregate mode, and repairs it. Any exception is caught and
        turned into an unsuccessful :class:`~pptrepair.repair.RepairOutcome`
        so the run continues.
        """
        diagnosis, _error = diagnose_file(target)
        output_base = None
        if bases is not None:
            base = bases[target]
            # Only create the mirrored directory when auto selection will
            # actually write something, so an unrepairable file never
            # leaves an empty directory behind (matching batch.py).
            if diagnosis is not None and predict_auto_mode(diagnosis) != "none":
                base.parent.mkdir(parents=True, exist_ok=True)
            output_base = base
        try:
            return repair_file(target, lang=self._request.lang,
                               output_base=output_base, diagnosis=diagnosis)
        except OperationCancelled:
            raise
        except Exception as exc:
            return self._failed_repair_outcome(
                target, diagnosis, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _failed_repair_outcome(target: Path, diagnosis: Diagnosis | None,
                               message: str) -> RepairOutcome:
        """Build an unsuccessful :class:`~pptrepair.repair.RepairOutcome`.

        Used when :func:`pptrepair.repair.repair_file` raises. Reuses the
        earlier diagnosis when available, falling back to a minimal
        placeholder when even diagnosis failed, so the caller always has
        a well-formed outcome to record and render.
        """
        if diagnosis is None:
            diagnosis = Diagnosis(path=target, verdict=Verdict.NOT_A_ZIP)
        return RepairOutcome(src=target, diagnosis=diagnosis, mode="failed",
                             success=False, warnings=[message])

    def _raise_if_cancelled(self) -> None:
        """Raise :class:`OperationCancelled` when cancellation is pending.

        Runs on the worker thread. Polled before materialization and at
        every item boundary; the exception is caught by :meth:`run` and
        turned into the :attr:`cancelled` signal.
        """
        if self._cancel_event.is_set():
            raise OperationCancelled("multi-source repair cancelled by user")


@dataclass(frozen=True)
class RepairRequest:
    """Immutable description of one single-file repair run.

    Being frozen, an instance is safe to hand across the thread
    boundary: the worker only ever reads it.

    :ivar roots: FILE and FOLDER source paths to diagnose and repair.
        Explicit ARCHIVE sources are never included here -- donor
        material mined from a dropped archive is only ever consumed by
        multi-source repair (see :class:`MultiRepairRequest`).
    :ivar output_dir: the aggregate output directory, or ``None`` in
        *in_place* mode (where each artifact is written next to its
        own source).
    :ivar in_place: True to overwrite/write next to each source rather
        than into *output_dir*.
    :ivar follow_symlinks: forwarded to the discovery walk.
    :ivar allow_download: hydrate cloud-only placeholders when reading
        them, rather than skipping them.
    :ivar max_file_bytes: per-file size ceiling; a larger file is
        skipped before it ever reaches the repair phase. ``None``
        disables the limit.
    :ivar lang: the language code used to translate the ``REPORT.txt``
        written into a successful extract's recovery folder.
    """

    roots: tuple[Path, ...]
    output_dir: Path | None
    in_place: bool
    follow_symlinks: bool = False
    allow_download: bool = False
    max_file_bytes: int | None = None
    lang: str = "en"


class RepairWorker(QThread):
    """Run a :class:`RepairRequest` off the UI thread, streaming progress.

    Signals (all delivered on the UI thread via queued connections):

    * :attr:`file_scanned` -- one on-disk file was diagnosed during the
      run's read-only checking phase
      (:class:`~pptrepair.scan.FileOutcome`).
    * :attr:`file_repaired` -- one corrupted file was repaired, skipped
      or found unrepairable during the run's repairing phase
      (:class:`~pptrepair.batch.BatchItem`).
    * :attr:`download_started` -- a cloud-only placeholder
      (:class:`~pathlib.Path`) is about to be read, which blocks while
      the sync client hydrates it.
    * :attr:`finished_ok` -- the run completed; carries a
      :class:`~pptrepair.batch.BatchResult`.
    * :attr:`failed` -- the run raised an unexpected exception; carries
      a ``"<type>: <message>"`` summary.
    * :attr:`cancelled` -- the run was stopped by :meth:`cancel`.

    Exactly one of :attr:`finished_ok`, :attr:`failed` or
    :attr:`cancelled` is emitted per run, before the inherited
    :attr:`~PySide6.QtCore.QThread.finished` signal.
    """

    file_scanned = Signal(object)
    file_repaired = Signal(object)
    download_started = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, request: RepairRequest,
                 parent: QObject | None = None) -> None:
        """Store *request* and prepare the cancellation flag.

        Runs on the UI thread (the thread that constructs the worker).

        :param request: the repair to run once :meth:`start` is called.
        :param parent: optional Qt parent object.
        """
        super().__init__(parent)
        self._request = request
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """Request cooperative cancellation of the running repair.

        Thread-safe and non-blocking: sets a :class:`threading.Event`
        that the worker's callbacks poll. Typically called from the UI
        thread. The run stops at the next callback boundary, after
        which :attr:`cancelled` is emitted. Every artifact already
        written before that point is left in place (see
        :func:`pptrepair.batch.repair_paths`'s own cancellation
        contract). Calling this before or after the run, or more than
        once, is harmless.
        """
        self._cancel_event.set()

    def run(self) -> None:
        """Execute the request on the worker thread.

        Runs :func:`~pptrepair.batch.repair_paths` once and translates
        the outcome into exactly one terminal signal.

        Terminal signal: :attr:`cancelled` if a callback raised
        :class:`~pptrepair.cancel.OperationCancelled`, :attr:`failed`
        for any other exception, otherwise :attr:`finished_ok`.
        """
        try:
            result = self._execute()
        except OperationCancelled:
            # Cooperative stop requested through cancel(); not an error.
            self.cancelled.emit()
        except Exception as exc:
            # Any unexpected failure is summarised for the UI rather than
            # left to terminate the worker thread silently.
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self.finished_ok.emit(result)

    def _execute(self) -> BatchResult:
        """Run :func:`repair_paths` and return its :class:`BatchResult`.

        Runs on the worker thread. The cancellation flag is checked at
        the start and inside every progress callback, so a cancel
        request never has to wait for a whole phase to complete. The
        force/dry-run/report-directory/archive-search knobs are fixed
        for this milestone's single-file repair: never force an
        overwrite (a pre-existing artifact is reported
        ``skipped_existing``), always actually write (never dry-run),
        never write a fingerprint report, and never mine archives (the
        request carries only FILE/FOLDER roots to begin with).
        """
        self._raise_if_cancelled()
        return repair_paths(
            list(self._request.roots),
            output_dir=self._request.output_dir,
            in_place=self._request.in_place,
            report_dir=None,
            force=False,
            follow_symlinks=self._request.follow_symlinks,
            allow_download=self._request.allow_download,
            max_file_bytes=self._request.max_file_bytes,
            lang=self._request.lang,
            progress=self._on_file_scanned,
            repair_progress=self._on_file_repaired,
            on_download=self._on_download,
        )

    def _raise_if_cancelled(self) -> None:
        """Raise :class:`OperationCancelled` when cancellation is pending.

        Runs on the worker thread. Raising is the core's documented
        contract for aborting a run in progress; the exception is caught
        by :meth:`run` and turned into the :attr:`cancelled` signal.
        """
        if self._cancel_event.is_set():
            raise OperationCancelled("repair cancelled by user")

    def _on_file_scanned(self, outcome: FileOutcome) -> None:
        """Core ``progress`` callback: relay one diagnosed file.

        Runs on the worker thread, during the run's read-only checking
        phase. Honours a pending cancellation first, then emits
        :attr:`file_scanned` for delivery on the UI thread.
        """
        self._raise_if_cancelled()
        self.file_scanned.emit(outcome)

    def _on_file_repaired(self, item: BatchItem) -> None:
        """Core ``repair_progress`` callback: relay one processed file.

        Runs on the worker thread, during the run's repairing phase.
        Honours a pending cancellation first, then emits
        :attr:`file_repaired` for delivery on the UI thread.
        """
        self._raise_if_cancelled()
        self.file_repaired.emit(item)

    def _on_download(self, path: Path) -> None:
        """Core ``on_download`` callback: announce a placeholder read.

        Runs on the worker thread. Honours a pending cancellation
        first, then emits :attr:`download_started` for delivery on the
        UI thread.
        """
        self._raise_if_cancelled()
        self.download_started.emit(path)
