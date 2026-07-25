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

import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from pptrepair.archive import ArchiveMember, materialize
from pptrepair.batch import BatchItem, BatchResult, plan_output_bases, repair_paths
from pptrepair.cancel import OperationCancelled
from pptrepair.classify import Diagnosis, Verdict
from pptrepair.gui.merge_plan import ApprovedMerge
from pptrepair.merge import MERGE_SUFFIX, merge_restore
from pptrepair.repair import RepairOutcome, predict_auto_mode, repair_file
from pptrepair.scan import (
    ArchiveMaterial,
    FileOutcome,
    ScanResult,
    diagnose_archive_materials,
    diagnose_file,
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
                 parent: QObject | None = None) -> None:
        """Store *request* and prepare the cancellation flag.

        Runs on the UI thread (the thread that constructs the worker).

        :param request: the multi-source repair to run once
            :meth:`start` is called.
        :param parent: optional Qt parent object.
        """
        super().__init__(parent)
        self._request = request
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
        every archive donor materialized into it is cleaned up on exit
        (the merged/repaired artifacts themselves are written elsewhere,
        on disk, and survive). The cancellation flag is polled before
        materialization and at every item boundary.
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
        """Extract every archive donor once, grouped by source archive.

        Returns ``(material_paths, display)``: *material_paths* maps each
        materialized archive member to its temporary on-disk path, and
        *display* maps that temporary path back to the member's
        ``"<archive>::<member>"`` label -- the map
        :func:`pptrepair.merge.merge_restore`'s ``display`` argument uses
        to keep the temporary path out of every note. Each archive is
        opened once and its members streamed into their own subdirectory,
        so names never collide across archives. A member whose extraction
        fails is simply absent from *material_paths* (its donor is then
        dropped when the merge's sources are assembled).
        """
        members_by_archive: dict[Path, list[ArchiveMember]] = {}
        seen: set[ArchiveMember] = set()
        for merge in self._request.merges:
            for donor in merge.donors:
                material = donor.material
                if material is None or material.member in seen:
                    continue
                seen.add(material.member)
                members_by_archive.setdefault(
                    material.archive_path, []).append(material.member)

        material_paths: dict[ArchiveMember, Path] = {}
        display: dict[Path, str] = {}
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
                detail="merge failed: no output could be reconstructed")
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
