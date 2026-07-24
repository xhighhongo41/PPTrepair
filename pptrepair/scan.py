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
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from pptrepair.archive import ArchiveMember, list_members, materialize
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


def diagnose_archive_materials(
    archives: Sequence[Path], *,
    on_download: Callable[[Path], None] | None = None,
    download_targets: Sequence[Path] = (),
) -> tuple[list[ArchiveMaterial], list[str]]:
    """Enumerate and diagnose the ``.pptx``/``.pptm`` members of *archives*.

    Each archive is opened just far enough to read its member index
    (:func:`pptrepair.archive.list_members`); every member is then, one
    at a time, streamed out to a plain file
    (:func:`pptrepair.archive.materialize` called with a single-element
    list), diagnosed via :func:`diagnose_file`, and the temporary file
    deleted immediately -- so the memory and disk peak stays at one
    member's worth regardless of how many members an archive holds. A
    single :class:`tempfile.TemporaryDirectory` spans the whole run and
    is removed on exit.

    *on_download* (when given) is invoked with an archive's path just
    before it is first read, but only for archives listed in
    *download_targets* (the cloud-only placeholders that
    :func:`discover_targets` cleared for hydration under
    ``allow_download``): reading such an archive blocks while the sync
    client downloads it, so the announcement must precede the read,
    exactly as the per-file target loop announces a placeholder target.

    :return: ``(materials, notes)`` -- one :class:`ArchiveMaterial` per
        member that could be extracted (its ``diagnosis`` may still be
        None if the pipeline failed on the member's bytes), plus every
        enumeration/extraction note collected along the way, in
        encounter order. Never raises: an unreadable archive or member
        degrades to a note, and the remaining archives/members are still
        processed.
    """
    materials: list[ArchiveMaterial] = []
    notes: list[str] = []
    if not archives:
        return materials, notes

    download_set = set(download_targets)
    with tempfile.TemporaryDirectory(prefix="pptrepair-scan-arc-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        for archive_path in archives:
            if on_download is not None and archive_path in download_set:
                on_download(archive_path)
            members, list_notes = list_members(archive_path)
            notes.extend(list_notes)
            for member in members:
                # One member at a time: materialize with a single-element
                # list, diagnose, then delete before touching the next.
                extracted, materialize_notes = materialize(
                    archive_path, [member], tmp_path)
                notes.extend(materialize_notes)
                dest_path = extracted.get(member)
                if dest_path is None:
                    continue  # extraction failed; already noted above
                diagnosis, error = diagnose_file(dest_path)
                materials.append(ArchiveMaterial(
                    archive_path=archive_path, member=member,
                    diagnosis=diagnosis, error=error))
                # Free the disk/memory footprint before the next member.
                dest_path.unlink(missing_ok=True)
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
               on_download: Callable[[Path], None] | None = None
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
    * *search_archives*: opt-in only. When True, backup archives found
      during the walk are enumerated and their members diagnosed as
      donor *material* (:func:`diagnose_archive_materials`), stored on
      ``ScanResult.materials`` / ``material_notes`` with
      ``search_archives`` set; the materials never enter the target
      loop, the fingerprint budget or any scanned/corrupted count.
      Left False (the default) this is a complete no-op: no archive is
      collected, opened or diagnosed, and every existing output byte is
      unchanged.
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
                            max_file_bytes=max_file_bytes)
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

        if diagnosis is not None and is_fingerprint_target(diagnosis):
            if diagnostics_dir is not None:
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
            download_targets=walk.download_targets)
        result.materials = materials
        result.material_notes = material_notes

    return result
