"""Rendering for the ``pptrepair scan`` command.

Split out of :mod:`pptrepair.report` to keep that facade's own module
small; see :mod:`pptrepair.cli_batch` for the caller.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pptrepair.classify import Verdict
from pptrepair.origin import OriginScore
from pptrepair.report_candidates import (
    _lineage_candidate_text_lines,
    _lineage_candidates_map,
    _lineage_candidates_to_json,
    _merge_group_map,
    _merge_group_text_lines,
    _merge_groups_to_json,
    _twin_candidate_text_lines,
    _twin_candidates_map,
    _twin_candidates_to_json,
)
from pptrepair.report_common import ISSUE_URL, VERDICT_LABELS
from pptrepair.twin import TwinCandidate

if TYPE_CHECKING:  # avoid runtime import cycles with scan
    from pptrepair.scan import FileOutcome, ScanResult


def render_scan_text(result: ScanResult, tr: Callable[[str], str],
                     include_files: bool = True) -> str:
    """Render a scan result as a human-readable, translated report.

    Used both for stdout (``include_files=False`` when per-file lines
    were already streamed during the scan) and for ``scan_report.txt``
    (``include_files=True``). Layout contract (labels go through *tr*;
    verdict codes, numbers and paths are locale-independent):

    * ``=== Scan summary ===`` header, then ``Scanned: {n} file(s)``;
    * one indented ``{verdict}: {n}`` line per non-zero verdict, in
      Verdict declaration order;
    * when ``cfb_count() > 0``: a translated note that {n} of the
      not_a_zip files look like encrypted or legacy Office documents
      (not OneDrive corruption);
    * non-zero skip counters: legacy .ppt, Office temp files;
    * ``include_files`` only: a ``Corrupted files:`` section listing
      ``  - {path}: {verdict}`` per corrupted outcome, each immediately
      followed by that file's twin-restoration candidates, when any
      (see :func:`_twin_candidate_text_lines`), and then its
      lineage-version candidates, when any (see
      :func:`_lineage_candidate_text_lines`); when at least one
      merge-candidate group exists (same-size corrupted files, see
      :func:`_merge_group_map`), a ``Merge candidates:`` section listing
      each group's size/files plus a ``pptrepair merge`` example command
      (see :func:`_merge_group_text_lines`); and an ``Errors:`` section
      listing walk errors and per-file pipeline errors as
      ``  - {path}: {message}``;
    * **always** when cloud placeholders were skipped (never omitted,
      even on an all-normal scan): ``Not examined: {n} cloud-only
      file(s) were skipped without downloading.`` plus the hint to
      re-run with ``--allow-download``;
    * when unknown-pattern files exist: their count, then — with a
      report dir — where the fingerprints were written and the
      invitation to attach them to a GitHub issue (:data:`ISSUE_URL`);
      without a report dir — the hint to re-run with ``--report DIR``;
      when ``fingerprints_skipped > 0`` — how many targets were not
      fingerprinted because of the per-run cap.
    """
    return "\n".join(_scan_summary_lines(result, tr, include_files))


def _scan_summary_lines(result: ScanResult, tr: Callable[[str], str],
                        include_files: bool) -> list[str]:
    """Build the line list rendered by :func:`render_scan_text`.

    Factored out so :func:`render_batch_text` can prepend the same scan
    summary to its own repair summary without duplicating this logic;
    see that function's docstring for how the two are combined.
    """
    # Local import: pptrepair.scan does not import this module, so this
    # is not a true cycle, but it keeps report.py's module-level
    # imports free of scan/repair as established above.
    from pptrepair.scan import DIAGNOSTICS_DIRNAME

    lines = [tr("=== Scan summary ===")]
    lines.append(tr("Scanned: {n} file(s)").format(n=len(result.outcomes)))

    verdict_counts = result.verdict_counts()
    for verdict in Verdict:
        count = verdict_counts.get(verdict.value, 0)
        if count == 0:
            continue
        lines.append(f"  {verdict.value}: {count}")

    cfb_count = result.cfb_count()
    if cfb_count > 0:
        lines.append(tr(
            "Note: {n} of the not_a_zip file(s) look like encrypted or "
            "legacy Office documents, not OneDrive corruption."
        ).format(n=cfb_count))

    legacy_count = len(result.walk.skipped_legacy)
    if legacy_count > 0:
        lines.append(
            tr("Skipped: {n} legacy .ppt file(s)").format(n=legacy_count))
    temp_count = len(result.walk.skipped_temp)
    if temp_count > 0:
        lines.append(
            tr("Skipped: {n} Office temp file(s)").format(n=temp_count))
    oversize_count = len(result.walk.skipped_oversize)
    if oversize_count > 0:
        lines.append(
            tr("Skipped: {n} file(s) over the size limit").format(
                n=oversize_count))

    if include_files:
        corrupted = result.corrupted()
        if corrupted:
            lines.append(tr("Corrupted files:"))
            twin_map = _twin_candidates_map(result.outcomes, result.materials)
            lineage_map = _lineage_candidates_map(
                result.outcomes, result.materials)
            for outcome in corrupted:
                lines.append(
                    f"  - {outcome.path}: {outcome.diagnosis.verdict.value}")
                lines.extend(
                    _twin_candidate_text_lines(outcome.path, twin_map, tr))
                lines.extend(
                    _lineage_candidate_text_lines(
                        outcome.path, lineage_map, tr))

        merge_groups = _merge_group_map(result.outcomes, result.materials)
        if merge_groups:
            lines.append(tr("Merge candidates:"))
            for group in merge_groups:
                lines.extend(_merge_group_text_lines(group, tr))

        errors = _collect_errors(result)
        if errors:
            lines.append(tr("Errors:"))
            lines.extend(f"  - {path}: {message}" for path, message in errors)

    cloud_count = len(result.walk.skipped_cloud)
    if cloud_count > 0:
        lines.append(tr(
            "Not examined: {n} cloud-only file(s) were skipped without "
            "downloading."
        ).format(n=cloud_count))
        lines.append(tr(
            "Hint: pass --allow-download to also examine cloud-only files "
            "(this may take long and use significant disk space)."))

    unknown = result.unknown_pattern()
    if unknown:
        lines.append(tr(
            "Unknown pattern: {n} file(s) did not match any known "
            "corruption pattern."
        ).format(n=len(unknown)))
        if result.report_dir is not None:
            diagnostics_dir = result.report_dir / DIAGNOSTICS_DIRNAME
            lines.append(tr(
                "Diagnostic fingerprints were written to {path}; please "
                "attach them to a new issue at {url}."
            ).format(path=diagnostics_dir, url=ISSUE_URL))
        else:
            lines.append(tr(
                "Hint: re-run with --report DIR to save shareable "
                "diagnostic fingerprints for these files."))
        if result.fingerprints_skipped > 0:
            lines.append(tr(
                "Note: {n} additional unknown-pattern file(s) were not "
                "fingerprinted because of the per-run cap."
            ).format(n=result.fingerprints_skipped))

    # Archive-material notes (unreadable archives/members) reuse the
    # existing "Notes:" heading and are shown in every mode. Gated on a
    # non-empty list, so a scan that mined no archive is byte-unchanged.
    if result.material_notes:
        lines.append(tr("Notes:"))
        lines.extend(f"  - {note}" for note in result.material_notes)

    return lines


def _collect_errors(result: ScanResult) -> list[tuple[Path, str]]:
    """Merge walk errors and per-file pipeline failures, in that order."""
    errors = list(result.walk.errors)
    errors.extend(
        (outcome.path, outcome.error)
        for outcome in result.outcomes if outcome.error is not None
    )
    return errors


def render_scan_json(result: ScanResult) -> str:
    """Render a scan result as a language-neutral JSON object.

    Schema (stable for tests)::

        {
          "roots": [str, ...],
          "summary": {
            "scanned": int,
            "verdicts": {str: int, ...},      # non-zero only
            "cfb_files": int,
            "skipped": {"legacy": int, "office_temp": int,
                         "cloud_placeholder": int, "oversize": int},
            "errors": int,
            "unknown_pattern_files": int,
            "fingerprints_written": int,
            "fingerprints_skipped": int
          },
          "files": [{"path": str, "verdict": str, "label": str,
                      "salvage": {...} | null,
                      "fingerprint": str | null,
                      "twin_candidates": [    # present only when >= 1
                        {"path": str, "confidence": "high" | "medium"
                                                     | "low", "size": int,
                         "origin_archive": str}  # archive members only
                      ],
                      "lineage_candidates": [    # present only when >= 1
                        {"path": str, "lineage_score": float,
                         "media_ratio": float,
                         "origin_archive": str}  # archive members only
                      ]}, ...],
          "merge_groups": [{"size": int, "files": [str, ...]}, ...],
          "skipped_cloud": [str, ...],
          "skipped_legacy": [str, ...],
          "skipped_temp": [str, ...],
          "skipped_oversize": [str, ...],
          "errors": [{"path": str, "error": str}, ...],
          "report_dir": str | null,
          "schema_version": 4,        # only with --search-archives
          "archive_notes": [str, ...] # only with --search-archives
        }

    ``files`` covers every diagnosed file in walk order; ``errors``
    merges walk errors and per-file pipeline failures in that order.
    ``merge_groups`` lists every group of corrupted files sharing an
    exact byte size (see :func:`_merge_group_map`), regardless of
    whether any file in ``files`` is itself unrelated. A twin-/lineage-
    candidate materialized from a backup archive is named by its
    ``"<archive>::<member>"`` label and carries an ``origin_archive``
    key (absent for on-disk candidates); ``schema_version`` (4) and
    ``archive_notes`` appear only when the scan ran with
    ``--search-archives`` (opt-in), leaving a default scan's JSON
    unchanged.
    """
    return json.dumps(_scan_payload(result), indent=2)


def _scan_payload(result: ScanResult) -> dict:
    """Build the payload dict serialized by :func:`render_scan_json`.

    Factored out so :func:`render_batch_json` can embed it unchanged as
    its ``"scan"`` key, without duplicating this dict construction or
    perturbing :func:`render_scan_json`'s own output.
    """
    fingerprints_written = sum(
        1 for outcome in result.outcomes
        if outcome.fingerprint_path is not None
    )
    errors = _collect_errors(result)
    twin_map = _twin_candidates_map(result.outcomes, result.materials)
    lineage_map = _lineage_candidates_map(result.outcomes, result.materials)

    payload = {
        "roots": [str(root) for root in result.roots],
        "summary": {
            "scanned": len(result.outcomes),
            "verdicts": result.verdict_counts(),
            "cfb_files": result.cfb_count(),
            "skipped": {
                "legacy": len(result.walk.skipped_legacy),
                "office_temp": len(result.walk.skipped_temp),
                "cloud_placeholder": len(result.walk.skipped_cloud),
                "oversize": len(result.walk.skipped_oversize),
            },
            "errors": len(errors),
            "unknown_pattern_files": len(result.unknown_pattern()),
            "fingerprints_written": fingerprints_written,
            "fingerprints_skipped": result.fingerprints_skipped,
        },
        "files": [
            _scan_file_entry(outcome, twin_map, lineage_map)
            for outcome in result.outcomes if outcome.diagnosis is not None
        ],
        "merge_groups": _merge_groups_to_json(
            _merge_group_map(result.outcomes, result.materials)),
        "skipped_cloud": [str(path) for path in result.walk.skipped_cloud],
        "skipped_legacy": [str(path) for path in result.walk.skipped_legacy],
        "skipped_temp": [str(path) for path in result.walk.skipped_temp],
        "skipped_oversize": [
            str(path) for path in result.walk.skipped_oversize],
        "errors": [
            {"path": str(path), "error": message}
            for path, message in errors
        ],
        "report_dir": str(result.report_dir)
        if result.report_dir is not None else None,
    }
    # Archive searching is an opt-in schema extension: only a scan that
    # actually mined archives carries the version bump and the note
    # array, so a default scan's JSON is byte-for-byte unchanged.
    if result.search_archives:
        payload["schema_version"] = 4
        payload["archive_notes"] = list(result.material_notes)
    return payload


def _scan_file_entry(
    outcome: FileOutcome, twin_map: dict[Path, list[TwinCandidate]],
    lineage_map: dict[Path, list[tuple[Path, OriginScore]]],
) -> dict:
    """Render one diagnosed file's entry in :func:`_scan_payload`'s ``files`` list.

    ``twin_candidates``/``lineage_candidates`` is added only when
    ``outcome.path`` has at least one entry in *twin_map*/*lineage_map*
    respectively; a file with none carries no such key at all (see
    :func:`render_scan_json`'s schema).
    """
    assert outcome.diagnosis is not None  # caller filters this out
    entry = {
        "path": str(outcome.path),
        "verdict": outcome.diagnosis.verdict.value,
        "label": VERDICT_LABELS[outcome.diagnosis.verdict],
        "salvage": outcome.diagnosis.salvage_summary or None,
        "fingerprint": str(outcome.fingerprint_path)
        if outcome.fingerprint_path is not None else None,
    }
    candidates = twin_map.get(outcome.path)
    if candidates:
        entry["twin_candidates"] = _twin_candidates_to_json(candidates)
    lineage_candidates = lineage_map.get(outcome.path)
    if lineage_candidates:
        entry["lineage_candidates"] = (
            _lineage_candidates_to_json(lineage_candidates))
    return entry
