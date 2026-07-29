"""Directory-tree scanning: discovery + diagnosis + report artifacts.

``pptrepair scan`` orchestration. Discovery is delegated to
:mod:`pptrepair.walker`, per-file diagnosis to the existing
scanner -> census -> classify pipeline, and fingerprint generation to
:mod:`pptrepair.diagnostics`.

Filesystem contract: with ``report_dir=None`` this module is strictly
read-only. With a report directory it writes only inside that
directory (never next to the scanned files).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from pptrepair.archive import ArchiveMember, iter_materialized_members
from pptrepair.census import from_central_directory, from_lfh_scan
from pptrepair.classify import Diagnosis, Verdict, classify
from pptrepair.diagnostics import build_fingerprint, file_id, is_fingerprint_target
from pptrepair.repair import OutputExistsError
from pptrepair.scanner import scan_structure
from pptrepair.walker import WalkResult, discover_targets

#: Hard cap on diagnostic fingerprints written per run, so a
#: misclassification burst cannot flood the user's disk. Overflow is
#: counted (never silently dropped) in ``ScanResult.fingerprints_skipped``.
MAX_FINGERPRINTS = 20

#: Subdirectory of the report dir holding shareable fingerprints.
DIAGNOSTICS_DIRNAME = "diagnostics"


@dataclass
class FileOutcome:
    """Diagnosis outcome for a single discovered file."""

    path: Path
    diagnosis: Diagnosis | None = None
    """None when the pipeline failed; see ``error``."""
    error: str | None = None
    fingerprint_path: Path | None = None
    """Where this file's fingerprint was written, when it was."""


@dataclass
class ArchiveMaterial:
    """Diagnosis outcome for one ``.pptx``/``.pptm`` member of a backup archive.

    Donor material only: an archive member is never a repair target and
    never enters the corrupted / scanned statistics. It participates in
    the report solely as a twin-, lineage- or merge-candidate for the
    files actually scanned on disk. The member is always named to the
    user through :meth:`display` (its ``"<archive>::<member>"`` label),
    never through the temporary path it was briefly extracted to.
    """

    archive_path: Path
    """The backup archive the member was found in."""
    member: ArchiveMember
    """The member's archive-recorded identity (name, size)."""
    diagnosis: Diagnosis | None = None
    """None when the pipeline failed for this member; see ``error``."""
    error: str | None = None

    def display(self) -> str:
        """Return the member's ``"<archive>::<member>"`` label for messages."""
        return self.member.display()


@dataclass
class CachedArchive:
    """Everything one backup archive contributed to a scan, kept for reuse.

    Held by :class:`ArchiveMaterialCache` for as long as the session
    lasts, so a rescan -- and above all the repair phase, which needs
    the donor bytes themselves -- can be served without touching the
    archive a second time.
    """

    materials: list[ArchiveMaterial]
    """The diagnosed members, in extraction order."""
    extracted: dict[ArchiveMember, Path]
    """Where each successfully extracted member's payload still sits."""
    notes: list[str]
    """The English notes that archive's single pass produced."""


class ArchiveMaterialCache:
    """Session-lifetime cache of extracted archive donor material.

    Mining a backup archive is by far the most expensive part of a scan:
    a compressed tar must be decompressed from its very first byte, and
    the repair phase used to pay that cost all over again just to get a
    donor's bytes back. This cache keeps every member extracted during a
    scan on disk, keyed by ``(resolved path, size, mtime)``, so one
    archive is read exactly once per session however often its material
    is asked for.

    Creating *root* and deleting it (with everything below it) belongs to
    the owner -- typically a :class:`tempfile.TemporaryDirectory` bound
    to the process (CLI) or to the application object (GUI). Nothing
    here ever writes outside *root*, and nothing here deletes *root*
    itself.

    Thread safety: an internal lock guards the bookkeeping -- the entry
    table, the per-archive subdirectory map and the ``arcNNNN``
    numbering -- so several threads may mine *different* archives into
    one cache at the same time. The mining itself (extraction into, and
    diagnosis out of, the directory :meth:`subdir_for` returned) happens
    outside the lock, which is exactly why two threads must never mine
    the *same* archive path concurrently: the second call would renumber
    the directory the first one is still writing into. The GUI's
    device-grouped parallel scan guarantees that on its own -- one path
    belongs to exactly one device group, and a group's work units run
    one after another.
    """

    def __init__(self, root: Path) -> None:
        """Bind the cache to *root*, the only directory it may write in."""
        self._root = root
        self._entries: dict[Path, tuple[int, int, CachedArchive]] = {}
        self._subdirs: dict[Path, Path] = {}
        self._next_index = 0
        #: Guards the three attributes above; see the class docstring.
        self._lock = threading.Lock()

    @staticmethod
    def _key(archive_path: Path) -> Path:
        """Return the identity an archive is cached under.

        ``Path.resolve()`` normalises symlinks and relative segments, so
        the same backup reached by two different paths shares one entry.
        It is non-strict, hence safe on a file that has since vanished.
        """
        return Path(archive_path).resolve()

    @staticmethod
    def _stamp(archive_path: Path) -> tuple[int, int] | None:
        """Return the archive's ``(size, mtime_ns)``, or None if unknown.

        None means the archive could not be stat'ed at all (deleted,
        unreadable, an unhydrated placeholder that errored): the caller
        treats that as "cannot be validated", never as a match.
        """
        try:
            status = archive_path.stat()
        except OSError:
            return None
        return status.st_size, status.st_mtime_ns

    def _discard(self, key: Path) -> None:
        """Drop the entry cached under *key*, deleting its extracted files.

        The caller must hold :attr:`_lock` (both call sites already do).
        """
        self._entries.pop(key, None)
        subdir = self._subdirs.pop(key, None)
        if subdir is not None:
            # Best effort: a file we cannot delete is a leak inside a
            # directory the owner removes wholesale anyway.
            shutil.rmtree(subdir, ignore_errors=True)

    def subdir_for(self, archive_path: Path) -> Path:
        """Create and return a fresh ``arcNNNN`` directory for *archive_path*.

        One directory per archive keeps the ``memberNNNN-`` destination
        names of different archives apart. Every call allocates a *new*
        number and discards whatever was cached for *archive_path*
        before: asking for a directory means the archive is about to be
        mined again, so the previous pass's entry is stale by definition
        and its files must not be able to collide with the new ones.

        *root* is created on demand here if the owner has not yet.

        The whole allocation happens under the lock, so two threads
        mining two different archives can never be handed the same
        number -- and thus never write their ``memberNNNN-`` files into
        one directory.
        """
        key = self._key(archive_path)
        with self._lock:
            self._discard(key)
            subdir = self._root / f"arc{self._next_index:04d}"
            self._next_index += 1
            subdir.mkdir(parents=True, exist_ok=True)
            self._subdirs[key] = subdir
        return subdir

    def store(self, archive_path: Path, entry: CachedArchive) -> None:
        """Remember *entry* as the result of mining *archive_path*.

        The archive is stat'ed here, and a future :meth:`lookup` hits
        only while that stamp still holds. A stat failure stores nothing
        at all: an entry that can never be validated would be worse than
        no entry, since it could only ever be served stale.
        """
        stamp = self._stamp(archive_path)
        if stamp is None:
            return
        size, mtime_ns = stamp
        key = self._key(archive_path)
        with self._lock:
            self._entries[key] = (size, mtime_ns, entry)

    def lookup(self, archive_path: Path) -> CachedArchive | None:
        """Return the cached result for *archive_path* while it is valid.

        Validity is ``(size, mtime_ns)`` equality with what
        :meth:`store` recorded. A mismatch means the backup was
        rewritten since it was mined: the stale entry *and* the
        extracted files under its ``arcNNNN`` directory are discarded,
        and None is returned so the caller mines the archive afresh. A
        stat failure also returns None, but leaves the entry alone --
        a temporarily unreachable archive should not cost the session
        the material it already holds.

        Lookup, validation and the invalidation it may trigger are one
        atomic step: two threads must not be able to discard the same
        stale entry (and its files) twice.
        """
        key = self._key(archive_path)
        with self._lock:
            record = self._entries.get(key)
            if record is None:
                return None
            stamp = self._stamp(archive_path)
            if stamp is None:
                return None
            size, mtime_ns, entry = record
            if stamp != (size, mtime_ns):
                self._discard(key)
                return None
            return entry

    def member_path(self, archive_path: Path,
                    member: ArchiveMember) -> Path | None:
        """Return where *member* was extracted to, while it is still cached.

        The repair phase's way of obtaining a donor's bytes without
        re-reading -- and, for a compressed tar, re-decompressing -- the
        whole archive. None stands for "not available" whatever the
        reason (no entry, a stale one, a member whose extraction had
        failed, or an extracted file that has since disappeared), and
        tells the caller to fall back to
        :func:`pptrepair.archive.materialize`.

        Locking is left entirely to :meth:`lookup`: the
        :class:`CachedArchive` it hands back is never mutated once
        stored, so reading its ``extracted`` map (and probing the file
        itself) outside the lock is safe and keeps the syscall off it.
        """
        entry = self.lookup(archive_path)
        if entry is None:
            return None
        dest_path = entry.extracted.get(member)
        if dest_path is None or not dest_path.is_file():
            return None
        return dest_path


@dataclass
class ScanResult:
    """Aggregate outcome of one ``scan_paths`` run."""

    roots: list[Path]
    walk: WalkResult
    outcomes: list[FileOutcome] = field(default_factory=list)
    report_dir: Path | None = None
    fingerprints_skipped: int = 0
    """Fingerprint targets beyond :data:`MAX_FINGERPRINTS` (not written)."""
    materials: list[ArchiveMaterial] = field(default_factory=list)
    """Archive members mined as donor material (only with
    ``search_archives``); never counted in any scanned/corrupted tally."""
    material_notes: list[str] = field(default_factory=list)
    """English notes from enumerating/extracting archive members
    (encrypted, corrupt or unreadable members, unreadable archives)."""
    search_archives: bool = False
    """True when this scan mined backup archives for donor material;
    drives the report's archive-aware schema version and note fields."""

    def verdict_counts(self) -> dict[str, int]:
        """Return ``verdict.value -> count`` over successful outcomes."""
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            if outcome.diagnosis is None:
                continue
            value = outcome.diagnosis.verdict.value
            counts[value] = counts.get(value, 0) + 1
        return counts

    def corrupted(self) -> list[FileOutcome]:
        """Return outcomes whose verdict is anything but NORMAL."""
        return [
            outcome for outcome in self.outcomes
            if outcome.diagnosis is not None
            and outcome.diagnosis.verdict != Verdict.NORMAL
        ]

    def unknown_pattern(self) -> list[FileOutcome]:
        """Return outcomes that qualify as fingerprint targets
        (:func:`pptrepair.diagnostics.is_fingerprint_target`)."""
        return [
            outcome for outcome in self.outcomes
            if outcome.diagnosis is not None
            and is_fingerprint_target(outcome.diagnosis)
        ]

    def cfb_count(self) -> int:
        """Return the number of NOT_A_ZIP outcomes with a CFB head
        (encrypted/legacy Office documents, reported separately)."""
        count = 0
        for outcome in self.outcomes:
            diagnosis = outcome.diagnosis
            if diagnosis is None or diagnosis.verdict != Verdict.NOT_A_ZIP:
                continue
            if diagnosis.structure is not None \
                    and diagnosis.structure.head_kind == "cfb":
                count += 1
        return count

    def had_errors(self) -> bool:
        """True when the walk or any per-file pipeline reported errors."""
        if self.walk.errors:
            return True
        return any(outcome.error is not None for outcome in self.outcomes)


def diagnose_file(path: Path) -> tuple[Diagnosis | None, str | None]:
    """Run the scan/census/classify pipeline on one file.

    Returns ``(diagnosis, None)`` on success, or ``(None, message)``
    when the path is unusable or the pipeline raises. Identical
    contract to the former ``pptrepair.cli._diagnose_file`` (which now
    delegates here).
    """
    if not path.exists():
        return None, f"{path}: no such file"
    if not path.is_file():
        return None, f"{path}: not a regular file"

    try:
        structure = scan_structure(path)
        cd_census = from_central_directory(path)
        lfh_census = from_lfh_scan(path)
        diagnosis = classify(path, structure, cd_census, lfh_census)
    except Exception as exc:
        # Any pipeline failure is reported for this file only; the scan
        # of the remaining files must continue.
        return None, f"{path}: {type(exc).__name__}: {exc}"
    return diagnosis, None


def _mine_archive(
    archive_path: Path, dest_dir: Path, *, keep_files: bool,
    material_progress: Callable[[ArchiveMaterial], None] | None,
    archive_progress: Callable[[Path, int, int], None] | None,
) -> CachedArchive:
    """Extract and diagnose one archive's members in a single pass.

    The archive is read exactly once
    (:func:`pptrepair.archive.iter_materialized_members`): every member
    is diagnosed the moment it lands in *dest_dir*. With *keep_files*
    the extracted files stay there afterwards, for a
    :class:`ArchiveMaterialCache` to serve later; without it each one is
    deleted as soon as it has been diagnosed, so the disk and memory
    peak stays at a single member's worth however many an archive holds.

    Callback exceptions (*material_progress*, *archive_progress*) are
    not caught; see :func:`diagnose_archive_materials`. (An
    :class:`OperationCancelled` they raise is not an :class:`OSError`,
    so the containment below never swallows a cancellation.)

    An :class:`OSError` surfacing anywhere else in the sweep -- the
    iterator's own guards cover its reads, but the file objects it
    drives live on whatever mount the archive sits on, and an unusable
    network handle can fail a syscall outside those guards (observed in
    the wild as ``EINVAL`` on an SMB mount hours into a read) -- is
    degraded to the same ``"cannot read archive"`` note the iterator
    uses for a read failure, and the members landed *before* it are
    returned: they are complete, diagnosed donor material, and a sweep
    that may already have run for hours must not lose them to its
    final stumble.
    """
    materials: list[ArchiveMaterial] = []
    extracted: dict[ArchiveMember, Path] = {}
    notes: list[str] = []

    progress: Callable[[int, int], None] | None = None
    if archive_progress is not None:
        def _forward(done: int, total: int) -> None:
            """Tag the iterator's byte counters with the archive read."""
            archive_progress(archive_path, done, total)

        progress = _forward

    try:
        for member, dest_path in iter_materialized_members(
                archive_path, dest_dir, on_note=notes.append,
                progress=progress):
            diagnosis, error = diagnose_file(dest_path)
            material = ArchiveMaterial(archive_path=archive_path,
                                       member=member,
                                       diagnosis=diagnosis, error=error)
            materials.append(material)
            if keep_files:
                extracted[member] = dest_path
            if material_progress is not None:
                material_progress(material)
            if not keep_files:
                # Free the disk/memory footprint before the next member.
                dest_path.unlink(missing_ok=True)
    except OSError as exc:
        notes.append(f"cannot read archive {archive_path}: {exc}")

    return CachedArchive(materials=materials, extracted=extracted, notes=notes)


def diagnose_archive_materials(
    archives: Sequence[Path], *,
    on_download: Callable[[Path], None] | None = None,
    download_targets: Sequence[Path] = (),
    material_progress: Callable[[ArchiveMaterial], None] | None = None,
    cache: ArchiveMaterialCache | None = None,
    archive_progress: Callable[[Path, int, int], None] | None = None,
) -> tuple[list[ArchiveMaterial], list[str]]:
    """Enumerate and diagnose the ``.pptx``/``.pptm`` members of *archives*.

    Every archive is swept exactly once
    (:func:`pptrepair.archive.iter_materialized_members`): each member
    met is streamed out to a plain file, diagnosed via
    :func:`diagnose_file` the moment it lands, and -- without a *cache*
    -- deleted again before the next member is touched, so the memory
    and disk peak stays at one member's worth regardless of how many
    members an archive holds. Reading the archive once, rather than once
    per member, is what makes a hundred-gigabyte compressed backup
    scannable at all.

    *cache*, when given, turns that single sweep into a single sweep
    *per session*: an archive whose ``(path, size, mtime)`` is already
    cached is not opened at all -- its stored materials and notes are
    replayed, *material_progress* included -- and one that is not gets
    mined into its own directory under the cache, with the extracted
    files deliberately left in place for the repair phase to reuse (see
    :meth:`ArchiveMaterialCache.member_path`). Without a *cache* the
    extraction happens in a private :class:`tempfile.TemporaryDirectory`
    that is removed on the way out, exactly as before.

    *on_download* (when given) is invoked with an archive's path just
    before it is first read, but only for archives listed in
    *download_targets* (the cloud-only placeholders that
    :func:`discover_targets` cleared for hydration under
    ``allow_download``): reading such an archive blocks while the sync
    client downloads it, so the announcement must precede the read,
    exactly as the per-file target loop announces a placeholder target.
    A cache hit reads nothing and therefore announces nothing.

    *material_progress* (when given) is invoked once per member that is
    actually diagnosed (i.e. once per element appended to *materials*),
    right after that :class:`ArchiveMaterial` is produced -- so a caller
    can stream progress across a long archive-mining run, or raise to
    cancel it.

    *archive_progress* (when given) is invoked as ``(archive path, done
    bytes, total bytes)`` while an archive is being swept, which is the
    only progress signal available inside a single huge archive (where
    *material_progress* may not fire for minutes). It is the iterator's
    own ``(done, total)`` tagged with the archive being read; see
    :func:`pptrepair.archive.iter_materialized_members` for what the
    counters mean per format.

    No callback exception is caught here: they propagate to the caller,
    see :class:`pptrepair.cancel.OperationCancelled` for the coordinated
    cancellation contract. An archive cancelled part way through is
    never stored in *cache* (and its directory is removed again), so the
    next run re-mines it in full rather than serving a truncated view of
    it.

    :return: ``(materials, notes)`` -- one :class:`ArchiveMaterial` per
        member that could be extracted (its ``diagnosis`` may still be
        None if the pipeline failed on the member's bytes), plus every
        enumeration/extraction note collected along the way, in
        encounter order. An unreadable archive or member degrades to a
        note on its own, and the remaining archives/members are still
        processed; the only way out early is a propagated callback
        exception (the cancellation contract above).
    """
    materials: list[ArchiveMaterial] = []
    notes: list[str] = []
    if not archives:
        return materials, notes

    download_set = set(download_targets)

    if cache is not None:
        for archive_path in archives:
            entry = cache.lookup(archive_path)
            if entry is not None:
                materials.extend(entry.materials)
                notes.extend(entry.notes)
                if material_progress is not None:
                    for material in entry.materials:
                        material_progress(material)
                continue
            if on_download is not None and archive_path in download_set:
                on_download(archive_path)
            dest_dir = cache.subdir_for(archive_path)
            try:
                mined = _mine_archive(
                    archive_path, dest_dir, keep_files=True,
                    material_progress=material_progress,
                    archive_progress=archive_progress)
            except BaseException:
                # Cancelled (or failed) part way through: the half-mined
                # material is thrown away rather than cached, so it can
                # never be mistaken for a complete view of the archive.
                shutil.rmtree(dest_dir, ignore_errors=True)
                raise
            cache.store(archive_path, mined)
            materials.extend(mined.materials)
            notes.extend(mined.notes)
        return materials, notes

    with tempfile.TemporaryDirectory(prefix="pptrepair-scan-arc-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        for archive_path in archives:
            if on_download is not None and archive_path in download_set:
                on_download(archive_path)
            mined = _mine_archive(
                archive_path, tmp_path, keep_files=False,
                material_progress=material_progress,
                archive_progress=archive_progress)
            materials.extend(mined.materials)
            notes.extend(mined.notes)
    return materials, notes


def _apply_exclusions(walk: WalkResult, exclude: Sequence[Path]) -> WalkResult:
    """Return a copy of *walk* with every path under *exclude* removed.

    Applied to all seven path buckets (``targets`` / ``skipped_legacy`` /
    ``skipped_temp`` / ``skipped_cloud`` / ``download_targets`` /
    ``archives`` / ``skipped_oversize``) plus ``errors`` (matched on its
    path element). *walker* itself is never touched; this only
    post-filters its output. Comparison resolves
    both sides with ``Path.resolve()``, a best-effort symlink-following
    normalisation, so an exclusion given as a relative path or through
    a symlinked ancestor still matches.
    """
    resolved_exclude = {Path(entry).resolve() for entry in exclude}

    def _is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        if resolved in resolved_exclude:
            return True
        return any(ex in resolved.parents for ex in resolved_exclude)

    def _filter(paths: list[Path]) -> list[Path]:
        return [path for path in paths if not _is_excluded(path)]

    return WalkResult(
        targets=_filter(walk.targets),
        skipped_legacy=_filter(walk.skipped_legacy),
        skipped_temp=_filter(walk.skipped_temp),
        skipped_cloud=_filter(walk.skipped_cloud),
        download_targets=_filter(walk.download_targets),
        archives=_filter(walk.archives),
        errors=[(path, message) for path, message in walk.errors
               if not _is_excluded(path)],
        skipped_oversize=_filter(walk.skipped_oversize),
    )


def scan_paths(roots: Sequence[Path], *,
               report_dir: Path | None = None,
               force: bool = False,
               follow_symlinks: bool = False,
               allow_download: bool = False,
               include_filenames: bool = False,
               search_archives: bool = False,
               exclude: Sequence[Path] = (),
               max_file_bytes: int | None = None,
               progress: Callable[[FileOutcome], None] | None = None,
               on_download: Callable[[Path], None] | None = None,
               material_progress: Callable[[ArchiveMaterial], None] | None = None,
               on_directory: Callable[[Path], None] | None = None,
               archive_cache: ArchiveMaterialCache | None = None,
               archive_progress: Callable[[Path, int, int], None] | None = None,
               ) -> ScanResult:
    """Scan *roots* and return the aggregate result.

    Implementation requirements:

    * When *report_dir* is given and exists, raise
      :class:`pptrepair.repair.OutputExistsError` unless *force* — and
      do so *before* any scanning starts. Create the directory
      (``parents=True``) up front; create ``diagnostics/`` lazily on
      the first fingerprint.
    * Discover targets via :func:`discover_targets` with
      *follow_symlinks* / *allow_download* / *max_file_bytes* passed
      through; a candidate over the limit is excluded before it ever
      reaches the diagnosis loop (see ``WalkResult.skipped_oversize``).
      Left at the default ``None`` this is a complete no-op.
    * *on_directory* (when given) is forwarded unchanged to
      :func:`discover_targets`, which invokes it once per directory
      visited during the walk (root included); a no-op whenever left at
      the default ``None``.
    * *search_archives*: opt-in only. When True, backup archives found
      during the walk are enumerated and their members diagnosed as
      donor *material* (:func:`diagnose_archive_materials`), stored on
      ``ScanResult.materials`` / ``material_notes`` with
      ``search_archives`` set; the materials never enter the target
      loop, the fingerprint budget or any scanned/corrupted count.
      Left False (the default) this is a complete no-op: no archive is
      collected, opened or diagnosed, and every existing output byte is
      unchanged.
    * *material_progress* (when given) is forwarded to
      :func:`diagnose_archive_materials`, which invokes it once per
      archive member actually diagnosed, right after that member's
      :class:`ArchiveMaterial` is produced. A no-op whenever
      *search_archives* is False, or left at the default ``None``.
    * *archive_cache* / *archive_progress* are forwarded unchanged to
      :func:`diagnose_archive_materials` (as its ``cache`` /
      ``archive_progress`` arguments), so a caller that owns a
      session-lifetime :class:`ArchiveMaterialCache` gets one read per
      archive per session -- and the extracted donor bytes kept for a
      later repair -- while a caller that wants byte-level progress
      inside one huge archive can display it. Both are no-ops whenever
      *search_archives* is False, or left at the default ``None``.
    * *exclude*: subtrees to leave out of every discovery bucket (e.g.
      a batch driver's own aggregate output directory, which would
      otherwise be diagnosed as part of the very tree it is writing
      into). Each entry is resolved with ``Path.resolve()`` — a
      best-effort, symlink-following comparison — and a discovered
      path is dropped when its own resolved form equals an excluded
      entry or has one as an ancestor. Left empty (the default) this
      is a no-op and :func:`discover_targets`'s result is used as-is,
      so :func:`pptrepair.cli.run_scan` sees no behaviour change.
    * Diagnose targets in walk order with :func:`diagnose_file`; wrap
      each into a :class:`FileOutcome` and invoke *progress* with it
      (when given) right after, so the CLI can stream results.
    * Invoke *on_download* (when given) with the path of every target
      listed in ``walk.download_targets`` right *before* diagnosing it:
      reading a cloud-only placeholder blocks while the sync client
      downloads it, and the announcement must appear first.
    * For each outcome that is a fingerprint target
      (:func:`is_fingerprint_target`), when *report_dir* is given:
      write ``<report_dir>/diagnostics/<file_id>.diag.json``
      (UTF-8, ``json.dumps(..., indent=2)`` + trailing newline) built
      by :func:`build_fingerprint` with *include_filenames*, and store
      the path in ``outcome.fingerprint_path``. Stop writing after
      :data:`MAX_FINGERPRINTS` files and count the overflow in
      ``fingerprints_skipped`` (targets without *report_dir* write
      nothing and are simply listed by ``unknown_pattern()``).
    * ``scan_report.txt`` / ``scan_report.json`` are NOT written here;
      the CLI renders them via :mod:`pptrepair.report` so that stdout
      and file output share one implementation.

    Coordinated cancellation: *progress*, *on_download*,
    *material_progress*, *on_directory* and *archive_progress* may raise
    to abort the run in progress -- none of their exceptions are caught
    here, so they propagate to the caller exactly like any other
    exception. That propagation is the supported, official contract for
    cooperative cancellation; see
    :class:`pptrepair.cancel.OperationCancelled`. Every temporary
    directory this function (or the archive-mining path it calls into)
    opens is a context manager, so it is always cleaned up whether the
    run completes or is cancelled partway through; whatever was already
    written under *report_dir* (created up front, plus any fingerprints
    written so far) before the cancellation point is left in place.
    """
    if report_dir is not None:
        if report_dir.exists() and not force:
            raise OutputExistsError(
                f"output directory already exists: {report_dir}")
        # exist_ok=True: with --force an existing directory is reused
        # in place, never cleared.
        report_dir.mkdir(parents=True, exist_ok=True)

    walk = discover_targets(roots, follow_symlinks=follow_symlinks,
                            allow_download=allow_download,
                            collect_archives=search_archives,
                            max_file_bytes=max_file_bytes,
                            on_directory=on_directory)
    if exclude:
        walk = _apply_exclusions(walk, exclude)
    result = ScanResult(roots=[Path(root) for root in roots],
                        walk=walk, report_dir=report_dir,
                        search_archives=search_archives)

    diagnostics_dir = (
        report_dir / DIAGNOSTICS_DIRNAME if report_dir is not None else None
    )
    fingerprints_written = 0

    download_set = set(walk.download_targets)

    for path in walk.targets:
        if on_download is not None and path in download_set:
            on_download(path)
        diagnosis, error = diagnose_file(path)
        outcome = FileOutcome(path=path, diagnosis=diagnosis, error=error)

        if (diagnosis is not None and is_fingerprint_target(diagnosis)
                and diagnostics_dir is not None):
            if fingerprints_written < MAX_FINGERPRINTS:
                diagnostics_dir.mkdir(parents=True, exist_ok=True)
                fingerprint = build_fingerprint(
                    diagnosis, include_filename=include_filenames)
                fingerprint_path = (
                    diagnostics_dir / f"{file_id(path)}.diag.json")
                fingerprint_path.write_text(
                    json.dumps(fingerprint, indent=2) + "\n",
                    encoding="utf-8")
                outcome.fingerprint_path = fingerprint_path
                fingerprints_written += 1
            else:
                result.fingerprints_skipped += 1

        result.outcomes.append(outcome)
        if progress is not None:
            progress(outcome)

    # Archive material is diagnosed after the on-disk targets so it never
    # perturbs the target loop, the fingerprint budget or any tally. A
    # placeholder archive cleared for hydration is announced before it
    # is read, the same way the target loop announces a placeholder file.
    if search_archives:
        materials, material_notes = diagnose_archive_materials(
            walk.archives, on_download=on_download,
            download_targets=walk.download_targets,
            material_progress=material_progress,
            cache=archive_cache, archive_progress=archive_progress)
        result.materials = materials
        result.material_notes = material_notes

    return result
