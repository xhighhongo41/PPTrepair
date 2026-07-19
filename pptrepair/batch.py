"""Batch repair orchestration for ``pptrepair repair-all``.

Two-phase driver over a set of roots: phase 1 diagnoses every discovered
file via :func:`pptrepair.scan.scan_paths` (read-only), phase 2 applies
:func:`pptrepair.repair.repair_file` to each corrupted file in walk
order, writing artifacts into an aggregate output directory (mirroring
the input tree) or, with *in_place*, next to each source.

Filesystem contract: phase 1 is strictly read-only (its only writes are
the fingerprints inside *report_dir*, and only when one is given and
*dry_run* is off). Phase 2 writes exclusively under each artifact's own
output base (the aggregate output directory, or the source's own
directory in *in_place* mode) and, in extract mode, a ``REPORT.txt``
inside the recovery folder. Nothing is ever written next to a scanned
input in aggregate mode, and *dry_run* suppresses every phase-2 write.

Implementation requirements:

* The aggregate output layout follows a fixed set of rules (see
  :func:`plan_output_base`): a lone directory root is mirrored directly
  under the output directory; several roots each get a subdirectory
  named after the root, numbered deterministically in CLI order when
  names clash; a file root's artifact lands directly in the output
  directory; and two inputs whose stems would collide in one output
  directory are disambiguated by falling back to the later file's full
  name (with a recorded warning).
* CFB inputs (encrypted or legacy Office documents: verdict
  ``NOT_A_ZIP`` with a ``cfb`` head) are never handed to
  :func:`repair_file`; they are reported ``unrepairable`` and counted
  separately.
* A corrupted file whose artifact already exists (and *force* is off) is
  skipped without touching it; one repair failure never aborts the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.i18n import get_translator
from pptrepair.report import render_repair_text
from pptrepair.repair import (EXTRACT_SUFFIX, REBUILD_SUFFIX, OutputExistsError,
                              RepairOutcome, predict_auto_mode, repair_file)
from pptrepair.scan import FileOutcome, ScanResult, scan_paths


@dataclass
class BatchItem:
    """Per-file outcome of one batch repair pass over a corrupted input."""

    source: FileOutcome
    """Phase-1 diagnosis of this file (carries the path and verdict)."""
    planned_output: Path | None
    """The artifact path: produced (repaired), predicted (planned), the
    pre-existing artifact (skipped_existing), or None when nothing could
    or would be written."""
    action: str
    """One of ``"repaired"`` | ``"unrepairable"`` | ``"skipped_existing"``
    | ``"failed"`` | ``"planned"`` (the last only in *dry_run*)."""
    repair: RepairOutcome | None = None
    """The executed repair outcome; present only when :func:`repair_file`
    actually ran (``"repaired"`` / non-CFB ``"unrepairable"``)."""
    error: str | None = None
    """``"{ExcType}: {message}"`` summary, set only on ``"failed"``."""


@dataclass
class BatchResult:
    """Aggregate outcome of one :func:`repair_paths` run."""

    scan: ScanResult
    items: list[BatchItem]
    output_dir: Path | None
    """The aggregate output directory, or None in *in_place* mode."""
    in_place: bool
    dry_run: bool
    warnings: list[str] = field(default_factory=list)
    """Batch-level warnings, e.g. output-name collision fallbacks."""

    def counts(self) -> dict[str, int]:
        """Return per-action tallies plus repaired-mode and CFB breakdowns.

        Keys: ``repaired`` (with ``repaired_rebuild`` / ``repaired_trim``
        / ``repaired_extract`` sub-tallies), ``unrepairable`` (with
        ``unrepairable_cfb`` for encrypted/legacy CFB inputs),
        ``skipped_existing``, ``failed`` and ``planned``.
        """
        counts = {
            "repaired": 0,
            "repaired_rebuild": 0,
            "repaired_trim": 0,
            "repaired_extract": 0,
            "unrepairable": 0,
            "unrepairable_cfb": 0,
            "skipped_existing": 0,
            "failed": 0,
            "planned": 0,
        }
        for item in self.items:
            action = item.action
            if action == "repaired":
                counts["repaired"] += 1
                mode = item.repair.mode if item.repair is not None else None
                key = f"repaired_{mode}"
                if key in counts:
                    counts[key] += 1
            elif action == "unrepairable":
                counts["unrepairable"] += 1
                if _is_cfb(item.source.diagnosis):
                    counts["unrepairable_cfb"] += 1
            elif action in counts:
                counts[action] += 1
        return counts

    def had_errors(self) -> bool:
        """True when the scan reported errors or any repair failed."""
        if self.scan.had_errors():
            return True
        return any(item.action == "failed" for item in self.items)

    def unrepaired_remaining(self) -> int:
        """Return the number of corrupted files still not repaired.

        Every item whose action is not ``"repaired"`` counts (skipped,
        unrepairable and failed alike). In *dry_run*, ``"planned"`` items
        also count as handled, since a plan is the dry-run equivalent of
        a successful repair. Drives the CLI's exit-code-1 decision.
        """
        handled = {"repaired"}
        if self.dry_run:
            handled.add("planned")
        return sum(1 for item in self.items if item.action not in handled)


def _is_cfb(diagnosis: Diagnosis | None) -> bool:
    """Return True when *diagnosis* is an encrypted/legacy CFB document.

    Such files carry the ``NOT_A_ZIP`` verdict with an OLE compound-file
    head; they are out of repair scope (the batch reports them
    unrepairable without attempting anything).
    """
    if diagnosis is None or diagnosis.verdict != Verdict.NOT_A_ZIP:
        return False
    structure = diagnosis.structure
    return structure is not None and structure.head_kind == "cfb"


def assign_root_labels(roots: Sequence[Path]) -> list[str | None]:
    """Return each root's aggregate output subdirectory label, by index.

    A directory root in multi-root mode maps to a subdirectory named
    after ``root.name``; when several directory roots share a name they
    are numbered ``name``, ``name-2``, ``name-3`` ... in CLI order. A
    lone root (the only root) and every file root map to None, meaning
    "no subdirectory": the lone directory tree is mirrored directly and
    file-root artifacts land in the output directory itself.
    """
    roots = list(roots)
    if len(roots) <= 1:
        return [None for _ in roots]
    labels: list[str | None] = []
    used: dict[str, int] = {}
    for root in roots:
        # File roots write directly into the output directory (no
        # subdirectory), so they never consume a name slot.
        if not root.is_dir():
            labels.append(None)
            continue
        name = root.name
        count = used.get(name, 0) + 1
        used[name] = count
        labels.append(name if count == 1 else f"{name}-{count}")
    return labels


def _relative_within(path: Path, root: Path) -> Path:
    """Return *path* relative to *root*, tolerating resolved/plain mixes.

    The scanned path is normally a plain join under *root*, so the direct
    ``relative_to`` succeeds; the resolved fallback covers a *root* given
    in a different (relative vs absolute, symlinked) form.
    """
    try:
        return path.relative_to(root)
    except ValueError:
        return path.resolve().relative_to(root.resolve())


def _owning_root_index(path: Path, roots: Sequence[Path]) -> int:
    """Return the index of the first root in CLI order that owns *path*.

    A root owns *path* when it equals it (a file root) or is one of its
    ancestors (a directory root). Comparison is done on resolved forms so
    a relative or symlinked root still matches. Falls back to 0 when no
    root owns *path* (not expected for a scanned file).
    """
    resolved_path = path.resolve()
    for index, root in enumerate(roots):
        resolved_root = root.resolve()
        if resolved_path == resolved_root:
            return index
        if resolved_root in resolved_path.parents:
            return index
    return 0


def plan_output_base(path: Path, roots: Sequence[Path], output_dir: Path,
                     labels: Sequence[str | None] | None = None) -> Path:
    """Return the suffix-less aggregate output base for one corrupted file.

    The base is the artifact path without its ``.repaired.pptx`` /
    ``.salvaged`` suffix; :func:`repair_file` appends the right one once
    the mode is settled. Layout (per this project's output-path rules):

    * a file root (``root == path``): ``output_dir / path.stem``;
    * the only root, a directory: ``output_dir`` mirrors the tree, so the
      base is ``output_dir / rel.parent / path.stem`` where ``rel`` is
      *path* relative to the root;
    * one of several roots, a directory: the same, prefixed by the root's
      subdirectory label (see :func:`assign_root_labels`).

    Stem collisions *between* files are not resolved here (this is a pure
    per-file mapping); :func:`plan_output_bases` layers that on top.
    *labels* may be supplied to avoid recomputing them per call.
    """
    if labels is None:
        labels = assign_root_labels(roots)
    index = _owning_root_index(path, roots)
    root = Path(roots[index])

    # A file root (the root is the file itself) drops its artifact
    # straight into the output directory, with no mirrored subtree.
    if path.resolve() == root.resolve():
        return output_dir / path.stem

    rel = _relative_within(path, root)
    label = labels[index] if index < len(labels) else None
    base_dir = output_dir if label is None else output_dir / label
    return base_dir / rel.parent / path.stem


def plan_output_bases(paths: Sequence[Path], roots: Sequence[Path],
                      output_dir: Path) -> tuple[list[Path], list[str]]:
    """Return collision-resolved output bases for *paths*, in order.

    Applies :func:`plan_output_base` to every path, then disambiguates
    any two whose bases land on the same path in the same directory: the
    later file (in *paths* order) falls back to its full name as its base
    (so ``A.pptx`` keeps ``.../A`` while a following ``A.pptm`` becomes
    ``.../A.pptm``), and a warning is recorded for each such fallback.

    :return: ``(bases, warnings)``, ``bases`` aligned with *paths*.
    """
    labels = assign_root_labels(roots)
    bases: list[Path] = []
    warnings: list[str] = []
    taken: dict[Path, Path] = {}
    for path in paths:
        base = plan_output_base(path, roots, output_dir, labels)
        if base in taken:
            # Two inputs share a stem in one output directory: keep the
            # first, disambiguate the later one by its full name. The
            # full name itself can already be taken (several same-named
            # file roots), so keep numbering deterministically in input
            # order until the base is free.
            fallback = base.with_name(path.name)
            serial = 2
            while fallback in taken:
                fallback = base.with_name(f"{path.name}-{serial}")
                serial += 1
            warnings.append(
                f"output name collision under {base.parent}: {path} falls "
                f"back to base '{fallback.name}' because '{base.name}' is "
                f"already taken by {taken[base]}")
            base = fallback
        taken[base] = path
        bases.append(base)
    return bases, warnings


def _build_exclude(output_dir: Path | None, report_dir: Path | None,
                   in_place: bool) -> list[Path]:
    """Return the subtrees to keep out of phase-1 discovery.

    The aggregate output directory (unless *in_place*) and the report
    directory are excluded so the batch never diagnoses its own
    artifacts when either sits inside a scanned root.
    """
    exclude: list[Path] = []
    if not in_place and output_dir is not None:
        exclude.append(output_dir)
    if report_dir is not None:
        exclude.append(report_dir)
    return exclude


def _write_recovery_report(outcome: RepairOutcome, lang: str) -> None:
    """Write ``REPORT.txt`` inside a successful extract's recovery folder.

    Mirrors ``pptrepair repair``: the same translated repair text that
    the single-file command prints is stored in the recovery folder.
    """
    assert outcome.output_path is not None
    text = render_repair_text(outcome, get_translator(lang))
    (outcome.output_path / "REPORT.txt").write_text(text, encoding="utf-8")


def _process_one(outcome: FileOutcome, base: Path, *, force: bool,
                 dry_run: bool, lang: str) -> BatchItem:
    """Repair (or plan/skip) one corrupted file and return its BatchItem.

    Order: CFB inputs are reported unrepairable without a repair attempt;
    a pre-existing artifact (both suffixes checked) is skipped unless
    *force*; *dry_run* stops after predicting the mode and artifact path;
    otherwise :func:`repair_file` runs with the resolved *base*, its
    exceptions isolated so one failure never aborts the batch.
    """
    diagnosis = outcome.diagnosis
    assert diagnosis is not None  # corrupted() never yields a failed pipeline

    if _is_cfb(diagnosis):
        # Encrypted/legacy CFB documents are out of scope: never attempted.
        return BatchItem(source=outcome, planned_output=None,
                         action="unrepairable")

    rebuild_out = base.with_name(base.name + REBUILD_SUFFIX)
    extract_out = base.with_name(base.name + EXTRACT_SUFFIX)
    if not force:
        existing = None
        if rebuild_out.is_file():
            existing = rebuild_out
        elif extract_out.is_dir():
            existing = extract_out
        if existing is not None:
            return BatchItem(source=outcome, planned_output=existing,
                             action="skipped_existing")

    if dry_run:
        predicted = predict_auto_mode(diagnosis)
        if predicted == "none":
            return BatchItem(source=outcome, planned_output=None,
                             action="unrepairable")
        planned = extract_out if predicted == "extract" else rebuild_out
        return BatchItem(source=outcome, planned_output=planned,
                         action="planned")

    # Create the artifact's parent directory just before writing — and
    # only when auto selection will actually write something, so an
    # unrepairable file (mode "none": nothing salvageable) never leaves
    # an empty mirrored directory behind in the output tree.
    if predict_auto_mode(diagnosis) != "none":
        base.parent.mkdir(parents=True, exist_ok=True)
    try:
        repair_outcome = repair_file(outcome.path, mode="auto", force=force,
                                     lang=lang, output_base=base,
                                     diagnosis=diagnosis)
    except OutputExistsError:
        # The artifact appeared between the existence check and the write
        # (a race): treat it exactly like the pre-check skip.
        return BatchItem(source=outcome, planned_output=None,
                         action="skipped_existing")
    except Exception as exc:
        # Any other failure is isolated to this file; the batch continues.
        return BatchItem(source=outcome, planned_output=None, action="failed",
                         error=f"{type(exc).__name__}: {exc}")

    if not repair_outcome.success:
        return BatchItem(source=outcome,
                         planned_output=repair_outcome.output_path,
                         action="unrepairable", repair=repair_outcome)

    if repair_outcome.mode == "extract" and repair_outcome.output_path:
        _write_recovery_report(repair_outcome, lang)
    return BatchItem(source=outcome,
                     planned_output=repair_outcome.output_path,
                     action="repaired", repair=repair_outcome)


def repair_paths(roots: Sequence[Path], *, output_dir: Path | None,
                 in_place: bool = False, report_dir: Path | None = None,
                 force: bool = False, dry_run: bool = False,
                 follow_symlinks: bool = False, allow_download: bool = False,
                 include_filenames: bool = False, lang: str = "en",
                 progress: Callable[[FileOutcome], None] | None = None,
                 repair_progress: Callable[[BatchItem], None] | None = None,
                 on_download: Callable[[Path], None] | None = None
                 ) -> BatchResult:
    """Diagnose *roots* and repair every corrupted file found.

    Phase 1 scans *roots* with :func:`pptrepair.scan.scan_paths` (its own
    aggregate output directory and the report directory excluded from
    discovery); phase 2 walks ``scan.corrupted()`` in order, repairing
    each file into the aggregate output tree (or next to the source in
    *in_place* mode). Callbacks stream progress: *progress* is forwarded
    to the phase-1 scan, and *repair_progress* is invoked with each
    :class:`BatchItem` as phase 2 produces it.

    Implementation requirements:

    * *report_dir*: passed through to phase 1 as-is, except under
      *dry_run*, where it is replaced with None so no fingerprints (or
      any other file) are written.
    * *output_dir* is required for aggregate mode and ignored (may be
      None) in *in_place* mode; *in_place* and a None *output_dir* are
      only valid together.
    * *force* / *follow_symlinks* / *allow_download* / *include_filenames*
      / *lang* / *on_download* keep their :func:`scan_paths` /
      :func:`repair_file` meanings.
    * The returned :class:`BatchResult` records collision-fallback
      warnings and exposes per-action tallies via
      :meth:`BatchResult.counts`.
    """
    roots = [Path(root) for root in roots]
    effective_report_dir = None if dry_run else report_dir
    exclude = _build_exclude(output_dir, report_dir, in_place)

    scan = scan_paths(
        roots,
        report_dir=effective_report_dir,
        force=force,
        follow_symlinks=follow_symlinks,
        allow_download=allow_download,
        include_filenames=include_filenames,
        exclude=exclude,
        progress=progress,
        on_download=on_download,
    )

    corrupted = scan.corrupted()
    if in_place:
        # Each artifact sits next to its source, matching the single-file
        # command's default output path (default_output_path).
        bases = [item.path.parent / item.path.stem for item in corrupted]
        warnings: list[str] = []
    else:
        assert output_dir is not None  # required by aggregate mode
        bases, warnings = plan_output_bases(
            [item.path for item in corrupted], roots, output_dir)

    items: list[BatchItem] = []
    for outcome, base in zip(corrupted, bases):
        item = _process_one(outcome, base, force=force, dry_run=dry_run,
                            lang=lang)
        items.append(item)
        if repair_progress is not None:
            repair_progress(item)

    return BatchResult(
        scan=scan,
        items=items,
        output_dir=None if in_place else output_dir,
        in_place=in_place,
        dry_run=dry_run,
        warnings=list(warnings),
    )
