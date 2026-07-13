"""Human-readable and JSON rendering of diagnoses and repair outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.scanner import ZipStructure

if TYPE_CHECKING:  # avoid runtime import cycles with repair / scan
    from pptrepair.repair import RepairOutcome
    from pptrepair.scan import ScanResult

#: URL offered to users who hit an unknown corruption pattern.
ISSUE_URL = "https://github.com/xhighhongo41/PPTrepair/issues/new/choose"

#: One-line human description per verdict.
VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.NORMAL: "intact PowerPoint package",
    Verdict.HEAD_ZERO_FILL: "corrupted: leading region overwritten with zeros",
    Verdict.HEAD_FOREIGN_DATA:
        "corrupted: leading region overwritten with foreign data",
    Verdict.VERSION_MIX: "corrupted: mixture of two file versions",
    Verdict.TAIL_TRUNCATED: "corrupted: file tail truncated",
    Verdict.OTHER_CORRUPT: "corrupted: unrecognized damage pattern",
    Verdict.NOT_A_ZIP: "not a ZIP-based file",
}


def render_text(diagnosis: Diagnosis) -> str:
    """Render one diagnosis as a human-readable multi-line report.

    Layout contract (kept stable for tests):

    * first line: ``=== <path> ===``
    * second line: ``Verdict: <verdict value> (<label>)``
    * an ``Evidence:`` section with one ``  - <item>`` line per entry
      (omitted when there is no evidence)
    * when the salvage summary is non-empty, a final line
      ``Salvageable: <entries_ok>/<entries_total> entries, <slides_ok>/<slides_total> slides``
    """
    lines = [f"=== {diagnosis.path} ==="]
    label = VERDICT_LABELS[diagnosis.verdict]
    lines.append(f"Verdict: {diagnosis.verdict.value} ({label})")

    if diagnosis.evidence:
        lines.append("Evidence:")
        lines.extend(f"  - {item}" for item in diagnosis.evidence)

    salvage = diagnosis.salvage_summary
    if salvage:
        lines.append(
            f"Salvageable: {salvage['entries_ok']}/{salvage['entries_total']} "
            f"entries, {salvage['slides_ok']}/{salvage['slides_total']} slides"
        )

    return "\n".join(lines)


def render_json(diagnoses: list[Diagnosis]) -> str:
    """Render diagnoses as a JSON array (schema kept stable for tests).

    Each element::

        {
          "path": str,
          "verdict": str,              # Verdict.value
          "label": str,                # VERDICT_LABELS text
          "evidence": [str, ...],
          "salvage": {...} | null,     # salvage_summary or null if empty
          "structure": {               # null when structure is None
            "size": int,
            "head_kind": str,
            "zero_bytes": int,
            "eocd_present": bool,
            "lfh_count": int
          }
        }
    """
    payload = [_to_dict(diagnosis) for diagnosis in diagnoses]
    return json.dumps(payload, indent=2)


def _structure_to_dict(structure: ZipStructure | None) -> dict | None:
    """Render *structure* as its JSON-schema dict, or None when absent."""
    if structure is None:
        return None
    return {
        "size": structure.size,
        "head_kind": structure.head_kind,
        "zero_bytes": structure.zero_total(),
        "eocd_present": structure.eocd is not None,
        "lfh_count": len(structure.lfh_offsets),
    }


def _to_dict(diagnosis: Diagnosis) -> dict:
    """Render one diagnosis as a JSON-schema dict."""
    return {
        "path": str(diagnosis.path),
        "verdict": diagnosis.verdict.value,
        "label": VERDICT_LABELS[diagnosis.verdict],
        "evidence": list(diagnosis.evidence),
        "salvage": diagnosis.salvage_summary or None,
        "structure": _structure_to_dict(diagnosis.structure),
    }


def render_repair_text(outcome: "RepairOutcome",
                       tr: Callable[[str], str]) -> str:
    """Render one repair outcome as a human-readable, translated report.

    This exact text is printed to stdout by ``pptrepair repair`` and,
    in extract mode, also written as ``REPORT.txt`` inside the recovery
    folder. Layout contract (labels/sentences go through *tr*, numbers
    and paths are locale-independent):

    * header line with the source path;
    * verdict line (verdict code stays English, its explanation is
      translated);
    * executed mode and produced artifact path (or a translated
      "nothing to repair" / "unrepairable" statement);
    * salvage statistics (entries, slides recovered);
    * damage summary: lost slide numbers (exact list when known),
      lost entry count;
    * rebuild only: re-check verdict of the artifact;
    * extract only: overview of the recovery-folder layout and the
      OneDrive version-history hint;
    * any warnings, one per line.
    """
    diagnosis = outcome.diagnosis
    lines = [f"=== {outcome.src} ==="]

    label = tr(VERDICT_LABELS[diagnosis.verdict])
    lines.append(tr("Verdict: {verdict} ({label})").format(
        verdict=diagnosis.verdict.value, label=label))
    lines.append(tr("Mode: {mode}").format(mode=outcome.mode))

    if outcome.success and outcome.mode != "none":
        lines.append(
            tr("Output: {path}").format(path=outcome.output_path))
    elif outcome.success:  # mode == "none": input was already intact
        lines.append(tr(
            "Nothing to repair: the file is already an intact PowerPoint "
            "package."))
    else:
        lines.append(tr("Unrepairable: no recoverable content was found."))

    salvage = diagnosis.salvage_summary
    if salvage:
        lines.append(tr("Salvaged entries: {ok} of {total}").format(
            ok=salvage["entries_ok"], total=salvage["entries_total"]))
        lines.append(tr("Slides recovered: {ok} of {total}").format(
            ok=salvage["slides_ok"], total=salvage["slides_total"]))

    if outcome.lost_entries_total > 0:
        if outcome.lost_slide_numbers:
            numbers = ", ".join(str(n) for n in outcome.lost_slide_numbers)
            lines.append(
                tr("Lost slides: {numbers}").format(numbers=numbers))
        else:
            lines.append(tr("Lost entries: {n}").format(
                n=outcome.lost_entries_total))

    if outcome.mode == "rebuild" and outcome.recheck_verdict is not None:
        lines.append(tr("Re-check verdict: {verdict}").format(
            verdict=outcome.recheck_verdict))

    if outcome.mode == "extract" and outcome.success:
        lines.append(tr(
            "The recovery folder groups salvaged content by kind: "
            "images/ and media/ hold pictures, audio and video; texts/ "
            "holds best-effort recovered text; charts/ holds chart XML "
            "plus cached data; parts/ holds every salvaged part in its "
            "original, raw form."))
        lines.append(tr(
            "Hint: OneDrive's web version history may hold an older "
            "intact copy of this file."))

    if outcome.warnings:
        lines.append(tr("Warnings:"))
        lines.extend(f"  - {warning}" for warning in outcome.warnings)

    return "\n".join(lines)


def render_repair_json(outcome: "RepairOutcome") -> str:
    """Render a repair outcome as a language-neutral JSON object.

    Schema (stable for tests)::

        {
          "path": str,
          "verdict": str,
          "mode": "rebuild" | "extract" | "none",
          "success": bool,
          "output": str | null,
          "salvage": {"entries_ok": int, "entries_total": int,
                       "slides_ok": int, "slides_total": int,
                       "source": str} | null,
          "lost_slide_numbers": [int, ...],
          "lost_entries_total": int,
          "recheck_verdict": str | null,
          "synthesized_parts": [str, ...],
          "pruned_relationships": [str, ...],
          "pruned_slide_ids": [str, ...],
          "written_files": [str, ...],     # extract mode, else []
          "warnings": [str, ...]
        }
    """
    rebuild_result = outcome.rebuild_result
    extract_result = outcome.extract_result
    return json.dumps({
        "path": str(outcome.src),
        "verdict": outcome.diagnosis.verdict.value,
        "mode": outcome.mode,
        "success": outcome.success,
        "output": str(outcome.output_path)
        if outcome.output_path is not None else None,
        "salvage": outcome.diagnosis.salvage_summary or None,
        "lost_slide_numbers": list(outcome.lost_slide_numbers),
        "lost_entries_total": outcome.lost_entries_total,
        "recheck_verdict": outcome.recheck_verdict,
        "synthesized_parts":
            list(rebuild_result.synthesized_parts) if rebuild_result else [],
        "pruned_relationships":
            list(rebuild_result.pruned_relationships) if rebuild_result
            else [],
        "pruned_slide_ids":
            list(rebuild_result.pruned_slide_ids) if rebuild_result else [],
        "written_files":
            list(extract_result.written_files) if extract_result else [],
        "warnings": list(outcome.warnings),
    }, indent=2)


def render_scan_text(result: "ScanResult", tr: Callable[[str], str],
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
      ``  - {path}: {verdict}`` per corrupted outcome, and an
      ``Errors:`` section listing walk errors and per-file pipeline
      errors as ``  - {path}: {message}``;
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

    if include_files:
        corrupted = result.corrupted()
        if corrupted:
            lines.append(tr("Corrupted files:"))
            lines.extend(
                f"  - {outcome.path}: {outcome.diagnosis.verdict.value}"
                for outcome in corrupted
            )

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

    return "\n".join(lines)


def _collect_errors(result: "ScanResult") -> list[tuple[Path, str]]:
    """Merge walk errors and per-file pipeline failures, in that order."""
    errors = list(result.walk.errors)
    errors.extend(
        (outcome.path, outcome.error)
        for outcome in result.outcomes if outcome.error is not None
    )
    return errors


def render_scan_json(result: "ScanResult") -> str:
    """Render a scan result as a language-neutral JSON object.

    Schema (stable for tests)::

        {
          "roots": [str, ...],
          "summary": {
            "scanned": int,
            "verdicts": {str: int, ...},      # non-zero only
            "cfb_files": int,
            "skipped": {"legacy": int, "office_temp": int,
                         "cloud_placeholder": int},
            "errors": int,
            "unknown_pattern_files": int,
            "fingerprints_written": int,
            "fingerprints_skipped": int
          },
          "files": [{"path": str, "verdict": str, "label": str,
                      "salvage": {...} | null,
                      "fingerprint": str | null}, ...],
          "skipped_cloud": [str, ...],
          "skipped_legacy": [str, ...],
          "skipped_temp": [str, ...],
          "errors": [{"path": str, "error": str}, ...],
          "report_dir": str | null
        }

    ``files`` covers every diagnosed file in walk order; ``errors``
    merges walk errors and per-file pipeline failures in that order.
    """
    fingerprints_written = sum(
        1 for outcome in result.outcomes
        if outcome.fingerprint_path is not None
    )
    errors = _collect_errors(result)

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
            },
            "errors": len(errors),
            "unknown_pattern_files": len(result.unknown_pattern()),
            "fingerprints_written": fingerprints_written,
            "fingerprints_skipped": result.fingerprints_skipped,
        },
        "files": [
            {
                "path": str(outcome.path),
                "verdict": outcome.diagnosis.verdict.value,
                "label": VERDICT_LABELS[outcome.diagnosis.verdict],
                "salvage": outcome.diagnosis.salvage_summary or None,
                "fingerprint": str(outcome.fingerprint_path)
                if outcome.fingerprint_path is not None else None,
            }
            for outcome in result.outcomes if outcome.diagnosis is not None
        ],
        "skipped_cloud": [str(path) for path in result.walk.skipped_cloud],
        "skipped_legacy": [str(path) for path in result.walk.skipped_legacy],
        "skipped_temp": [str(path) for path in result.walk.skipped_temp],
        "errors": [
            {"path": str(path), "error": message}
            for path, message in errors
        ],
        "report_dir": str(result.report_dir)
        if result.report_dir is not None else None,
    }
    return json.dumps(payload, indent=2)
