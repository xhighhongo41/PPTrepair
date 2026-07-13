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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

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
class ScanResult:
    """Aggregate outcome of one ``scan_paths`` run."""

    roots: list[Path]
    walk: WalkResult
    outcomes: list[FileOutcome] = field(default_factory=list)
    report_dir: Path | None = None
    fingerprints_skipped: int = 0
    """Fingerprint targets beyond :data:`MAX_FINGERPRINTS` (not written)."""

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


def scan_paths(roots: Sequence[Path], *,
               report_dir: Path | None = None,
               force: bool = False,
               follow_symlinks: bool = False,
               allow_download: bool = False,
               include_filenames: bool = False,
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
      *follow_symlinks* / *allow_download* passed through.
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
                            allow_download=allow_download)
    result = ScanResult(roots=[Path(root) for root in roots],
                        walk=walk, report_dir=report_dir)

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

    return result
