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
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from pptrepair.batch import BatchItem, BatchResult, repair_paths
from pptrepair.cancel import OperationCancelled
from pptrepair.scan import (
    ArchiveMaterial,
    FileOutcome,
    ScanResult,
    diagnose_archive_materials,
    scan_paths,
)


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
    """

    file_scanned = Signal(object)
    material_scanned = Signal(object)
    download_started = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, request: ScanRequest,
                 parent: QObject | None = None) -> None:
        """Store *request* and prepare the cancellation flag.

        Runs on the UI thread (the thread that constructs the worker).

        :param request: the scan to run once :meth:`start` is called.
        :param parent: optional Qt parent object.
        """
        super().__init__(parent)
        self._request = request
        self._cancel_event = threading.Event()

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
        underfoot".

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
        """Run both scan phases and assemble the :class:`GuiScanResult`.

        Runs on the worker thread. The cancellation flag is checked at
        the start, between the two phases, and inside every progress
        callback, so a cancel request never has to wait for a whole
        phase to complete.
        """
        self._raise_if_cancelled()

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
            )

        return GuiScanResult(scan=scan_result, materials=materials,
                             material_notes=material_notes)

    def _raise_if_cancelled(self) -> None:
        """Raise :class:`OperationCancelled` when cancellation is pending.

        Runs on the worker thread. Raising is the core's documented
        contract for aborting a run in progress; the exception is caught
        by :meth:`run` and turned into the :attr:`cancelled` signal.
        """
        if self._cancel_event.is_set():
            raise OperationCancelled("scan cancelled by user")

    def _on_file_scanned(self, outcome: FileOutcome) -> None:
        """Core ``progress`` callback: relay one diagnosed file.

        Runs on the worker thread. Honours a pending cancellation
        first, then emits :attr:`file_scanned` for delivery on the UI
        thread.
        """
        self._raise_if_cancelled()
        self.file_scanned.emit(outcome)

    def _on_material_scanned(self, material: ArchiveMaterial) -> None:
        """Core ``material_progress`` callback: relay one mined member.

        Runs on the worker thread. Honours a pending cancellation
        first, then emits :attr:`material_scanned` for delivery on the
        UI thread.
        """
        self._raise_if_cancelled()
        self.material_scanned.emit(material)

    def _on_download(self, path: Path) -> None:
        """Core ``on_download`` callback: announce a placeholder read.

        Runs on the worker thread. Honours a pending cancellation
        first, then emits :attr:`download_started` for delivery on the
        UI thread.
        """
        self._raise_if_cancelled()
        self.download_started.emit(path)


@dataclass(frozen=True)
class RepairRequest:
    """Immutable description of one single-file repair run.

    Being frozen, an instance is safe to hand across the thread
    boundary: the worker only ever reads it.

    :ivar roots: FILE and FOLDER source paths to diagnose and repair.
        Explicit ARCHIVE sources are never included here -- donor
        material mined from a dropped archive is only ever consumed by
        multi-source repair, a later milestone.
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
