"""Rendering for the ``pptrepair repair-all`` command.

Split out of :mod:`pptrepair.report` to keep that facade's own module
small; see :mod:`pptrepair.cli_batch` for the caller.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pptrepair.classify import Verdict
from pptrepair.origin import OriginScore
from pptrepair.report_candidates import (_lineage_candidate_text_lines,
                                         _lineage_candidates_map,
                                         _lineage_candidates_to_json,
                                         _merge_group_map,
                                         _merge_group_text_lines,
                                         _twin_candidate_text_lines,
                                         _twin_candidates_map,
                                         _twin_candidates_to_json)
from pptrepair.report_scan import _scan_payload, _scan_summary_lines
from pptrepair.twin import TwinCandidate

if TYPE_CHECKING:  # avoid runtime import cycles with batch
    from pptrepair.batch import BatchItem, BatchResult


def render_batch_text(result: "BatchResult", tr: Callable[[str], str],
                      include_files: bool = False) -> str:
    """Render a ``repair-all`` batch result as a human-readable report.

    Used both for stdout and for ``repair_report.txt`` (with
    *include_files* True, mirroring :func:`render_scan_text`'s own
    ``scan_report.txt`` convention). Layout contract (labels go through
    *tr*; verdict/action/mode codes, numbers and paths stay
    locale-independent, matching the rest of this module):

    * the phase-1 scan summary, exactly as
      :func:`render_scan_text(result.scan, tr, include_files=False)
      <render_scan_text>` would render it (the per-file corrupted list
      is always omitted here since this function's own per-file section
      already carries the verdict alongside the repair action);
    * ``=== Repair summary ===``, then either (*dry_run*) a single
      ``Planned: {n} file(s)`` line, or ``Repaired: {n} file(s)``
      followed by one untranslated ``  {mode}: {n}`` line per non-zero
      rebuild/trim/extract sub-tally;
    * ``Unrepairable: {n} file(s)``, then -- each only when positive --
      a translated count of the encrypted/legacy CFB files among them
      and a translated count of those with no surviving content at all
      (``EMPTY_FILE`` / ``FULL_ZERO_FILL``, see
      :func:`render_repair_text`'s own "nothing survives" hint);
    * when positive, ``Skipped (output exists): {n} file(s)`` plus the
      same ``--force`` hint the single-file command prints on
      :class:`~pptrepair.repair.OutputExistsError`;
    * when positive, ``Failed: {n} file(s)``;
    * *dry_run* only: a final ``dry run: nothing was written.`` line;
    * ``result.warnings`` (collision-fallback notices etc.), one
      ``  - {warning}`` per entry, under a translated ``Warnings:``
      header, when non-empty;
    * *include_files* only, right after the (per-file-less) scan summary:
      when at least one merge-candidate group exists among
      ``result.scan.outcomes`` (see :func:`_merge_group_map`), the same
      translated ``Merge candidates:`` section :func:`render_scan_text`
      would show;
    * *include_files* only: a translated ``Repairs:`` header followed by
      one line per :class:`~pptrepair.batch.BatchItem`, in batch order
      (see :func:`_repair_item_line`); an ``"unrepairable"``/``"failed"``
      item's line is immediately followed by that file's
      twin-restoration candidates, when any (see
      :func:`_twin_candidate_text_lines`), and then its lineage-version
      candidates, when any (see :func:`_lineage_candidate_text_lines`),
      using the twin/lineage indexes built from ``result.scan.outcomes``
      (every diagnosed file, not just the corrupted ones this batch
      attempted to repair).
    """
    lines = _scan_summary_lines(result.scan, tr, include_files=False)

    if include_files:
        merge_groups = _merge_group_map(result.scan.outcomes)
        if merge_groups:
            lines.append(tr("Merge candidates:"))
            for group in merge_groups:
                lines.extend(_merge_group_text_lines(group, tr))

    lines.append(tr("=== Repair summary ==="))
    counts = result.counts()

    if result.dry_run:
        lines.append(tr("Planned: {n} file(s)").format(n=counts["planned"]))
    else:
        lines.append(
            tr("Repaired: {n} file(s)").format(n=counts["repaired"]))
        for mode in ("rebuild", "trim", "extract"):
            mode_count = counts[f"repaired_{mode}"]
            if mode_count:
                lines.append(f"  {mode}: {mode_count}")

    lines.append(
        tr("Unrepairable: {n} file(s)").format(n=counts["unrepairable"]))
    if counts["unrepairable_cfb"]:
        lines.append(tr(
            "Encrypted or legacy Office file(s) (not attempted): {n}"
        ).format(n=counts["unrepairable_cfb"]))
    lost_content = _count_lost_content(result.items)
    if lost_content:
        lines.append(
            tr("No content survives: {n} file(s)").format(n=lost_content))

    if counts["skipped_existing"]:
        lines.append(tr("Skipped (output exists): {n} file(s)").format(
            n=counts["skipped_existing"]))
        lines.append(
            tr("Hint: pass --force to overwrite the existing output."))

    if counts["failed"]:
        lines.append(tr("Failed: {n} file(s)").format(n=counts["failed"]))

    if result.dry_run:
        lines.append(tr("dry run: nothing was written."))

    if result.warnings:
        lines.append(tr("Warnings:"))
        lines.extend(f"  - {warning}" for warning in result.warnings)

    if include_files:
        lines.append(tr("Repairs:"))
        twin_map = _twin_candidates_map(result.scan.outcomes)
        lineage_map = _lineage_candidates_map(result.scan.outcomes)
        for item in result.items:
            lines.append(_repair_item_line(item))
            if item.action in ("unrepairable", "failed"):
                lines.extend(
                    _twin_candidate_text_lines(
                        item.source.path, twin_map, tr))
                lines.extend(
                    _lineage_candidate_text_lines(
                        item.source.path, lineage_map, tr))

    return "\n".join(lines)


def _count_lost_content(items: "list[BatchItem]") -> int:
    """Count unrepairable items whose diagnosis promises no surviving content.

    ``EMPTY_FILE`` and ``FULL_ZERO_FILL`` (see :mod:`pptrepair.classify`)
    are the two verdicts where nothing is recoverable regardless of
    repair strategy, matching :func:`render_repair_text`'s own
    "nothing survives" hint for a single unrepairable outcome.
    """
    count = 0
    for item in items:
        if item.action != "unrepairable":
            continue
        diagnosis = item.source.diagnosis
        if diagnosis is not None and diagnosis.verdict in (
                Verdict.EMPTY_FILE, Verdict.FULL_ZERO_FILL):
            count += 1
    return count


def _repair_item_line(item: "BatchItem") -> str:
    """Render one :class:`~pptrepair.batch.BatchItem` as one report line.

    The path, verdict code, action code and mode code are never
    translated (machine-facing values, like verdict codes elsewhere in
    this module); the artifact path is whichever of
    ``item.planned_output`` was produced, predicted or found
    pre-existing (None only when nothing could or would be written, and
    rendered as ``-``). *mode* is likewise ``-`` when :func:`repair_file`
    never ran for this item (skipped, CFB, failed or dry-run "planned").
    """
    diagnosis = item.source.diagnosis
    assert diagnosis is not None  # corrupted() never yields a failed pipeline
    mode = item.repair.mode if item.repair is not None else "-"
    output = item.planned_output if item.planned_output is not None else "-"
    line = (f"  - {item.source.path}: verdict={diagnosis.verdict.value} "
           f"action={item.action} mode={mode} output={output}")
    if item.error:
        line += f" error={item.error}"
    return line


def render_batch_json(result: "BatchResult") -> str:
    """Render a ``repair-all`` batch result as a language-neutral JSON object.

    Schema (stable for tests)::

        {
          "schema_version": 3,
          "dry_run": bool,
          "in_place": bool,
          "output_dir": str | null,      # None in --in-place mode
          "scan": {...},                 # exactly render_scan_json's payload
          "counts": {...},               # BatchResult.counts(), unchanged
          "unrepaired_remaining": int,
          "warnings": [str, ...],
          "repairs": [
            {
              "path": str,
              "verdict": str,            # Verdict.value
              "action": str,             # BatchItem.action
              "mode": str | null,        # null when repair_file never ran
              "output": str | null,      # produced / predicted / existing
              "recheck_verdict": str | null,
              "recheck_dangling_refs": int | null,
              "recheck_timing_issues": int | null,
              "recheck_structure_issues": int | null,
              "trimmed_bytes": int | null,
              "lost_slide_numbers": [int, ...],
              "lost_entries_total": int,
              "warnings": [str, ...],
              "error": str | null,
              "twin_candidates": [        # "unrepairable"/"failed" only,
                {"path": str, "confidence": "high" | "medium" | "low",  # present only when >= 1
                 "size": int}
              ],
              "lineage_candidates": [     # "unrepairable"/"failed" only,
                {"path": str, "lineage_score": float, "media_ratio": float}
              ]                           # present only when >= 1
            },
            ...
          ]
        }

    ``scan`` embeds :func:`_scan_payload`'s dict verbatim, so it matches
    :func:`render_scan_json`'s own top-level schema field for field
    (including its own ``"merge_groups"`` key). ``repairs`` covers every
    corrupted file in batch order; for an item whose repair was never
    executed (``skipped_existing``, ``failed``, a CFB ``unrepairable``,
    or a dry-run ``planned``), every field that comes from
    :class:`~pptrepair.repair.RepairOutcome` is None or an empty list,
    per the key-by-key null noted above. ``schema_version`` became 2
    when ``twin_candidates`` was added, and 3 when ``merge_groups``
    (inside ``scan``) and ``lineage_candidates`` were added.
    """
    twin_map = _twin_candidates_map(result.scan.outcomes)
    lineage_map = _lineage_candidates_map(result.scan.outcomes)
    payload = {
        "schema_version": 3,
        "dry_run": result.dry_run,
        "in_place": result.in_place,
        "output_dir": str(result.output_dir)
        if result.output_dir is not None else None,
        "scan": _scan_payload(result.scan),
        "counts": result.counts(),
        "unrepaired_remaining": result.unrepaired_remaining(),
        "warnings": list(result.warnings),
        "repairs": [
            _batch_item_to_dict(item, twin_map, lineage_map)
            for item in result.items
        ],
    }
    return json.dumps(payload, indent=2)


def _batch_item_to_dict(
    item: "BatchItem", twin_map: dict[Path, list[TwinCandidate]],
    lineage_map: dict[Path, list[tuple[Path, OriginScore]]],
) -> dict:
    """Render one :class:`~pptrepair.batch.BatchItem` as its JSON-schema dict.

    See :func:`render_batch_json` for the field-by-field schema; every
    :class:`~pptrepair.repair.RepairOutcome`-derived field falls back to
    None (or an empty list) when ``item.repair`` is None, i.e. the item
    was never handed to :func:`~pptrepair.repair.repair_file`.
    ``twin_candidates``/``lineage_candidates`` is added, mirroring
    :func:`render_batch_text`'s own restore-/lineage-candidate lines,
    only for an ``"unrepairable"``/``"failed"`` item whose source path
    has at least one candidate in *twin_map*/*lineage_map* respectively.
    """
    diagnosis = item.source.diagnosis
    assert diagnosis is not None  # corrupted() never yields a failed pipeline
    repair = item.repair
    entry = {
        "path": str(item.source.path),
        "verdict": diagnosis.verdict.value,
        "action": item.action,
        "mode": repair.mode if repair is not None else None,
        "output": str(item.planned_output)
        if item.planned_output is not None else None,
        "recheck_verdict":
            repair.recheck_verdict if repair is not None else None,
        "recheck_dangling_refs":
            repair.recheck_dangling_refs if repair is not None else None,
        "recheck_timing_issues":
            repair.recheck_timing_issues if repair is not None else None,
        "recheck_structure_issues":
            repair.recheck_structure_issues if repair is not None else None,
        "trimmed_bytes": repair.trimmed_bytes if repair is not None else None,
        "lost_slide_numbers":
            list(repair.lost_slide_numbers) if repair is not None else [],
        "lost_entries_total":
            repair.lost_entries_total if repair is not None else 0,
        "warnings": list(repair.warnings) if repair is not None else [],
        "error": item.error,
    }
    if item.action in ("unrepairable", "failed"):
        candidates = twin_map.get(item.source.path)
        if candidates:
            entry["twin_candidates"] = _twin_candidates_to_json(candidates)
        lineage_candidates = lineage_map.get(item.source.path)
        if lineage_candidates:
            entry["lineage_candidates"] = (
                _lineage_candidates_to_json(lineage_candidates))
    return entry
