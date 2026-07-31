"""Background scan worker for the PySide6 desktop application.

Scanning a directory tree (and, optionally, mining dropped backup
archives for donor material) can take seconds to minutes, so it must
run off the UI thread to keep the window responsive. This module wraps
:func:`pptrepair.scan.scan_paths` /
:func:`pptrepair.scan.diagnose_archive_materials` in :class:`ScanWorker`,
a :class:`~PySide6.QtCore.QThread` subclass that streams progress back
to the UI and supports cooperative cancellation.

The repairing counterparts --
:class:`~pptrepair.gui.repair_workers.RepairWorker` and
:class:`~pptrepair.gui.repair_workers.MultiRepairWorker` -- live in
:mod:`pptrepair.gui.repair_workers`; the device-parallel scanning
machinery :class:`ScanWorker` orchestrates lives in
:mod:`pptrepair.gui.scan_parallel`.

Thread model
------------
:class:`ScanWorker` overrides :meth:`~PySide6.QtCore.QThread.run` (it
never starts a Qt event loop of its own): :meth:`run`, and every
callback it hands to the core, execute on the worker thread. All
communication back to the UI happens exclusively through the Qt signals
declared on the class, which -- because emitter and receiver live in
different threads -- Qt delivers with the default queued connection, so
the connected slots run on the UI thread. The worker never touches a
GUI object directly. Cancellation goes the other way: :meth:`cancel`
sets a :class:`threading.Event`, which is safe to call from the UI
thread while :meth:`run` reads it on the worker thread.

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

import queue
import threading
import time
import traceback
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from pptrepair.cancel import OperationCancelled
from pptrepair.gui import scan_parallel
from pptrepair.scan import (
    ArchiveMaterial,
    ArchiveMaterialCache,
    FileOutcome,
    ScanResult,
    diagnose_archive_materials,
    scan_paths,
)

#: Minimum time (seconds) between two ``walk_progress`` emits, so a huge
#: tree does not flood the UI thread with one signal per directory. A
#: module attribute (rather than a class constant) so tests can
#: monkeypatch it to make the throttling deterministic.
_WALK_PROGRESS_INTERVAL_S = 0.2


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
    :ivar ignore_hidden: skip hidden files/archive members (name
        starting with ``.``), forwarded to
        :func:`~pptrepair.scan.scan_paths`.
    """

    roots: tuple[Path, ...]
    archives: tuple[Path, ...]
    follow_symlinks: bool = False
    allow_download: bool = False
    max_file_bytes: int | None = None
    ignore_hidden: bool = True


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
    :func:`~pptrepair.gui.scan_parallel.device_of`, and each group is
    processed strictly sequentially by one of at most
    :data:`~pptrepair.gui.scan_parallel.MAX_PARALLEL_DEVICE_SCANS`
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
    # The byte counters must be 64-bit: a backup archive can exceed the
    # 2 GiB a C++ ``int`` holds, and shiboken delivers overflowing values
    # wrapped (with a console "OverflowError") rather than raising.
    archive_progress = Signal(str, "qulonglong", "qulonglong")
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
            :class:`~pptrepair.gui.repair_workers.MultiRepairWorker`) --
            never has to read the same archive twice. Its bookkeeping
            is lock-guarded, so this worker's device threads may mine
            into it concurrently (each archive belonging to exactly one
            of them); the UI must still
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
            # left to terminate the worker thread silently -- but the
            # summary alone cannot say *where* it happened, so the full
            # traceback goes to stderr for the terminal the app was
            # launched from.
            traceback.print_exc()
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
                ignore_hidden=self._request.ignore_hidden,
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
                ignore_hidden=self._request.ignore_hidden,
            )

        return GuiScanResult(scan=scan_result, materials=materials,
                             material_notes=material_notes)

    def _plan_device_groups(self) -> list[list[scan_parallel.ScanUnit]] | None:
        """Return the per-thread work lists, or None to run sequentially.

        The request is cut into units -- one per FILE/FOLDER root, then
        one per dropped archive, so that within any device the roots are
        still scanned before the archives are mined, exactly as the
        sequential path orders its two phases -- and those units are
        grouped by the device :func:`~pptrepair.gui.scan_parallel.device_of`
        reports for their path.

        The groups are then dealt out round-robin over
        ``min(scan_parallel.MAX_PARALLEL_DEVICE_SCANS, len(groups))``
        threads, so a thread may end up serving several devices; it
        processes them one group after another, and each group in input
        order.

        None -- meaning "take the sequential path, unchanged" -- is
        returned whenever

        * ``follow_symlinks`` is set: the discovery walk then shares one
          visited-directory set *across* roots to break symlink cycles
          and diamonds, and splitting the roots over separate
          :func:`~pptrepair.scan.scan_paths` calls would silently change
          that semantics. (This is decided before any path is stat'ed,
          so :func:`~pptrepair.gui.scan_parallel.device_of` is not even
          consulted.)
        * every unit lives on one device, or there are no units at all:
          nothing is left to overlap, and a lone group has no reason to
          leave the worker thread.
        """
        if self._request.follow_symlinks:
            return None

        units = [scan_parallel.ScanUnit(path=path, index=index,
                                        is_archive=False)
                 for index, path in enumerate(self._request.roots)]
        units.extend(scan_parallel.ScanUnit(path=path, index=index,
                                            is_archive=True)
                     for index, path in enumerate(self._request.archives))

        groups: dict[int | None, list[scan_parallel.ScanUnit]] = {}
        for unit in units:
            groups.setdefault(
                scan_parallel.device_of(unit.path), []).append(unit)
        if len(groups) <= 1:
            return None

        thread_count = min(scan_parallel.MAX_PARALLEL_DEVICE_SCANS,
                           len(groups))
        buckets: list[list[scan_parallel.ScanUnit]] = [
            [] for _ in range(thread_count)]
        for index, group in enumerate(groups.values()):
            buckets[index % thread_count].extend(group)
        return buckets

    def _execute_in_parallel(
            self, buckets: Sequence[Sequence[scan_parallel.ScanUnit]]
    ) -> GuiScanResult:
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
        state = scan_parallel.ParallelRun(
            events=queue.Queue(maxsize=scan_parallel.EVENT_QUEUE_MAXSIZE),
            roots=[None] * len(self._request.roots),
            archives=[None] * len(self._request.archives),
        )
        relay = scan_parallel.EventRelay(state.events, self._cancel_event)
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

    def _run_device_group(self, units: Sequence[scan_parallel.ScanUnit],
                          relay: scan_parallel.EventRelay,
                          state: scan_parallel.ParallelRun) -> None:
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

    def _scan_one_root(self, root: Path,
                       relay: scan_parallel.EventRelay) -> ScanResult:
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
            ignore_hidden=self._request.ignore_hidden,
            search_archives=False,
            progress=relay.on_file_scanned,
            on_download=relay.on_download,
            on_directory=relay.on_directory,
        )

    def _mine_one_archive(
            self, archive: Path, relay: scan_parallel.EventRelay
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
            ignore_hidden=self._request.ignore_hidden,
        )

    def _pump_events(self, events: queue.Queue[scan_parallel.ScanEvent],
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
                event = events.get(timeout=scan_parallel.EVENT_POLL_INTERVAL_S)
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

    def _dispatch_event(self, event: scan_parallel.ScanEvent) -> None:
        """Turn one queued event back into the emit it stands for.

        Runs on the worker thread, so the throttling state the relays
        consult is touched from that one thread only -- and an unknown
        kind, which only a programming error could produce, is ignored
        rather than allowed to abort a scan that has otherwise
        succeeded.
        """
        if event.kind == scan_parallel.EVENT_DIRECTORY:
            self._relay_directory(*event.args)
        elif event.kind == scan_parallel.EVENT_ARCHIVE_PROGRESS:
            self._relay_archive_progress(*event.args)
        elif event.kind == scan_parallel.EVENT_FILE:
            self.file_scanned.emit(*event.args)
        elif event.kind == scan_parallel.EVENT_MATERIAL:
            self.material_scanned.emit(*event.args)
        elif event.kind == scan_parallel.EVENT_DOWNLOAD:
            self.download_started.emit(*event.args)

    def _assemble(self, state: scan_parallel.ParallelRun) -> GuiScanResult:
        """Merge the finished units into one result, in request order.

        Runs on the worker thread once every device thread has ended.
        The slots are filled in unit order, never in completion order,
        so the outcome is the sequential run's outcome exactly (see
        :func:`~pptrepair.gui.scan_parallel.merge_scan_results`). An
        empty ``scan`` -- rather than an empty
        :class:`~pptrepair.scan.ScanResult` -- is what an archives-only
        request must produce.
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
            scan=scan_parallel.merge_scan_results(parts) if parts else None,
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
        directory instead
        (:meth:`~pptrepair.gui.scan_parallel.EventRelay.on_directory`)
        and reaches :meth:`_relay_directory` from
        :meth:`_dispatch_event`.
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
        position instead
        (:meth:`~pptrepair.gui.scan_parallel.EventRelay.on_archive_progress`)
        and reaches :meth:`_relay_archive_progress` from
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
        the outcome
        (:meth:`~pptrepair.gui.scan_parallel.EventRelay.on_file_scanned`)
        for the very same emit, made from :meth:`_dispatch_event`.
        """
        self._raise_if_cancelled()
        self.file_scanned.emit(outcome)

    def _on_material_scanned(self, material: ArchiveMaterial) -> None:
        """Core ``material_progress`` callback: relay one mined member.

        Runs on the worker thread. Honours a pending cancellation
        first, then emits :attr:`material_scanned` for delivery on the
        UI thread. The sequential path's callback; the parallel one
        queues the member
        (:meth:`~pptrepair.gui.scan_parallel.EventRelay.on_material_scanned`)
        for the very same emit, made from :meth:`_dispatch_event`.
        """
        self._raise_if_cancelled()
        self.material_scanned.emit(material)

    def _on_download(self, path: Path) -> None:
        """Core ``on_download`` callback: announce a placeholder read.

        Runs on the worker thread. Honours a pending cancellation
        first, then emits :attr:`download_started` for delivery on the
        UI thread. The sequential path's callback; the parallel one
        queues the path
        (:meth:`~pptrepair.gui.scan_parallel.EventRelay.on_download`)
        for the very same emit, made from :meth:`_dispatch_event`.
        """
        self._raise_if_cancelled()
        self.download_started.emit(path)
