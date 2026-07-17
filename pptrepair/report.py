"""Human-readable and JSON rendering of diagnoses and repair outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.scanner import ZipStructure

if TYPE_CHECKING:  # avoid runtime import cycles with repair / scan
    from pptrepair.integrity import (RefIntegrityResult,
                                     StructureIntegrityResult,
                                     TimingIntegrityResult)
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
    Verdict.EMPTY_FILE: "corrupted: file is empty (all content lost)",
    Verdict.FULL_ZERO_FILL:
        "corrupted: file is (almost) entirely zero-filled",
    Verdict.INTERIOR_DAMAGE:
        "corrupted: interior region damaged, archive index intact",
    Verdict.TAIL_FOREIGN_DATA:
        "corrupted: intact archive followed by foreign data",
}


def render_text(diagnosis: Diagnosis,
               ref_integrity: "RefIntegrityResult | None" = None,
               timing: "TimingIntegrityResult | None" = None,
               structure: "StructureIntegrityResult | None" = None) -> str:
    """Render one diagnosis as a human-readable multi-line report.

    Layout contract (kept stable for tests):

    * first line: ``=== <path> ===``
    * second line: ``Verdict: <verdict value> (<label>)``
    * an ``Evidence:`` section with one ``  - <item>`` line per entry
      (omitted when there is no evidence)
    * when the salvage summary is non-empty, a final line
      ``Salvageable: <entries_ok>/<entries_total> entries, <slides_ok>/<slides_total> slides``
    * when *ref_integrity* is given and has at least one dangling
      reference (see :mod:`pptrepair.integrity`), two trailing lines:
      ``Reference integrity: <n> unresolved reference(s) in <m>
      part(s)`` followed by a PowerPoint hint. Omitted entirely when
      *ref_integrity* is None or carries no dangling references, so
      the output of a `check` run on a clean file is unchanged.
    * when *timing* is given and reports at least one issue (dangling
      ``spid`` reference or media/shape mismatch; see
      :func:`pptrepair.integrity.inspect_timing`), one trailing line:
      ``Timing integrity: <n> inconsistent reference(s) in <m>
      part(s)``. Omitted when *timing* is None or reports no issue.
    * when *structure* is given and has at least one missing required
      relationship (see :func:`pptrepair.integrity.inspect_structure`),
      a ``Structure integrity: <n> required relationship(s) missing``
      line followed by one ``  - <part>: no <required_type>
      relationship`` line per missing entry. Omitted when *structure*
      is None or reports nothing missing.

    All three integrity summaries are plain, untranslated English (like
    the rest of this function's output), since ``check`` never routes
    its text report through a translator.
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

    if ref_integrity is not None and ref_integrity.dangling:
        affected_parts = {ref.part for ref in ref_integrity.dangling}
        lines.append(
            f"Reference integrity: {len(ref_integrity.dangling)} unresolved "
            f"reference(s) in {len(affected_parts)} part(s)"
        )
        lines.append(
            "  PowerPoint may offer a one-time repair on first open.")

    if timing is not None and (timing.dangling or timing.media_mismatch):
        timing_count = len(timing.dangling) + len(timing.media_mismatch)
        affected_parts = {ref.part for ref in timing.dangling} | {
            mismatch.part for mismatch in timing.media_mismatch}
        lines.append(
            f"Timing integrity: {timing_count} inconsistent reference(s) "
            f"in {len(affected_parts)} part(s)"
        )

    if structure is not None and structure.missing:
        lines.append(
            f"Structure integrity: {len(structure.missing)} required "
            "relationship(s) missing"
        )
        lines.extend(
            f"  - {item.part}: no {item.required_type} relationship"
            for item in structure.missing
        )

    return "\n".join(lines)


def render_json(diagnoses: list[Diagnosis],
               integrities: "list[RefIntegrityResult | None] | None" = None,
               timings: "list[TimingIntegrityResult | None] | None" = None,
               structures: "list[StructureIntegrityResult | None] | None"
               = None) -> str:
    """Render diagnoses as a JSON array (schema kept stable for tests).

    *integrities*/*timings*/*structures*, each optional, must have the
    same length as *diagnoses* and pair up with it index-by-index (each
    entry is either the matching :mod:`pptrepair.integrity` result type
    or None). Left at their default None, every element's
    ``xml_ref_integrity``/``timing_integrity``/``structure_integrity``
    is null.

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
          },
          "xml_ref_integrity": {       # null when no integrity result
            "dangling_count": int,
            "parts": [str, ...]        # sorted, deduplicated part names
          } | null,
          "timing_integrity": {        # null when no timing result
            "dangling_count": int,
            "media_mismatch_count": int,
            "parts": [str, ...]        # sorted, deduplicated part names
          } | null,
          "structure_integrity": {     # null when no structure result
            "missing_count": int,
            "items": [{"part": str, "required": str}, ...]
          } | null
        }
    """
    if integrities is None:
        integrities = [None] * len(diagnoses)
    if timings is None:
        timings = [None] * len(diagnoses)
    if structures is None:
        structures = [None] * len(diagnoses)
    payload = [
        _to_dict(diagnosis, integrity, timing, structure_integrity)
        for diagnosis, integrity, timing, structure_integrity
        in zip(diagnoses, integrities, timings, structures)
    ]
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


def _to_dict(diagnosis: Diagnosis,
            integrity: "RefIntegrityResult | None" = None,
            timing: "TimingIntegrityResult | None" = None,
            structure_integrity: "StructureIntegrityResult | None" = None,
            ) -> dict:
    """Render one diagnosis as a JSON-schema dict.

    Used by :func:`render_json` (the ``check`` command) only;
    :func:`render_scan_json` builds its own, unrelated per-file dicts
    and is unaffected by *integrity*/*timing*/*structure_integrity*.
    """
    return {
        "path": str(diagnosis.path),
        "verdict": diagnosis.verdict.value,
        "label": VERDICT_LABELS[diagnosis.verdict],
        "evidence": list(diagnosis.evidence),
        "salvage": diagnosis.salvage_summary or None,
        "structure": _structure_to_dict(diagnosis.structure),
        "xml_ref_integrity": _integrity_to_dict(integrity),
        "timing_integrity": _timing_integrity_to_dict(timing),
        "structure_integrity":
            _structure_integrity_to_dict(structure_integrity),
    }


def _integrity_to_dict(
    integrity: "RefIntegrityResult | None",
) -> dict | None:
    """Render *integrity* as its JSON-schema dict, or None when absent."""
    if integrity is None:
        return None
    return {
        "dangling_count": len(integrity.dangling),
        "parts": sorted({ref.part for ref in integrity.dangling}),
    }


def _timing_integrity_to_dict(
    timing: "TimingIntegrityResult | None",
) -> dict | None:
    """Render *timing* as its JSON-schema dict, or None when absent."""
    if timing is None:
        return None
    affected_parts = {ref.part for ref in timing.dangling} | {
        mismatch.part for mismatch in timing.media_mismatch}
    return {
        "dangling_count": len(timing.dangling),
        "media_mismatch_count": len(timing.media_mismatch),
        "parts": sorted(affected_parts),
    }


def _structure_integrity_to_dict(
    structure: "StructureIntegrityResult | None",
) -> dict | None:
    """Render *structure* as its JSON-schema dict, or None when absent."""
    if structure is None:
        return None
    return {
        "missing_count": len(structure.missing),
        "items": [
            {"part": item.part, "required": item.required_type}
            for item in structure.missing
        ],
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
    * trim only: a note that the trailing foreign data was removed and
      the leading archive kept as-is;
    * unrepairable only: a hint for damage patterns where no content
      survives (``empty_file`` / ``full_zero_fill``);
    * salvage statistics (entries, slides recovered);
    * damage summary: lost slide numbers (exact list when known),
      lost entry count;
    * rebuild/trim only: re-check verdict of the artifact;
    * rebuild only, when the rebuild's own reference cleanup touched at
      least one part: a count of removed stale references and the
      number of rebuilt parts they were removed from;
    * rebuild/trim only, when the reference-integrity recheck ran: the
      number of unresolved relationship references left in the
      artifact;
    * trim only, when that count is positive: a note that these
      unresolved references already existed in the original archive
      and were left untouched, since the recovered file is
      byte-identical to it;
    * extract only: overview of the recovery-folder layout and the
      OneDrive version-history hint;
    * any warnings, one per line.

    ``recheck_timing_issues``/``recheck_structure_issues`` (see
    :mod:`pptrepair.integrity`) add no dedicated line here by design (no
    new translated string was introduced for them): a positive count on
    a rebuild artifact already surfaces through the warnings list above,
    and both counts are always available in full via
    :func:`render_repair_json`.
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
        if outcome.mode == "trim":
            lines.append(tr(
                "Removed {n} bytes of foreign data that followed the "
                "archive; the leading archive was kept unmodified."
            ).format(n=outcome.trimmed_bytes))
    elif outcome.success:  # mode == "none": input was already intact
        lines.append(tr(
            "Nothing to repair: the file is already an intact PowerPoint "
            "package."))
    else:
        lines.append(tr("Unrepairable: no recoverable content was found."))
        if diagnosis.verdict in (Verdict.EMPTY_FILE, Verdict.FULL_ZERO_FILL):
            lines.append(tr(
                "Hint: no content survives inside this file. Check the "
                "OneDrive recycle bin, other devices' local copies, and "
                "any backups; version history rarely helps with this "
                "damage pattern."))

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

    if (outcome.mode in ("rebuild", "trim")
            and outcome.recheck_verdict is not None):
        lines.append(tr("Re-check verdict: {verdict}").format(
            verdict=outcome.recheck_verdict))

    rebuild_result = outcome.rebuild_result
    if (outcome.mode == "rebuild" and rebuild_result is not None
            and rebuild_result.cleaned_parts):
        lines.append(tr(
            "Removed {e} stale reference(s) from {p} rebuilt part(s)."
        ).format(e=len(rebuild_result.removed_elements),
                 p=len(rebuild_result.cleaned_parts)))

    if (outcome.mode in ("rebuild", "trim")
            and outcome.recheck_dangling_refs is not None):
        lines.append(tr("Unresolved references after repair: {n}").format(
            n=outcome.recheck_dangling_refs))
        if outcome.mode == "trim" and outcome.recheck_dangling_refs > 0:
            lines.append(tr(
                "Note: these unresolved references existed in the "
                "original archive and were left untouched (the recovered "
                "file is byte-identical to the original)."))

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
          "mode": "rebuild" | "extract" | "trim" | "none",
          "success": bool,
          "output": str | null,
          "salvage": {"entries_ok": int, "entries_total": int,
                       "slides_ok": int, "slides_total": int,
                       "source": str} | null,
          "lost_slide_numbers": [int, ...],
          "lost_entries_total": int,
          "trimmed_bytes": int | null,      # trim mode only, else null
          "recheck_verdict": str | null,
          "recheck_dangling_refs": int | null,  # rebuild/trim, else null
          "recheck_timing_issues": int | null,  # rebuild/trim, else null
          "recheck_structure_issues": int | null,  # rebuild/trim, else null
          "synthesized_parts": [str, ...],
          "pruned_relationships": [str, ...],
          "pruned_slide_ids": [str, ...],
          "cleaned_parts": [str, ...],      # rebuild mode, else []
          "removed_elements": [str, ...],   # rebuild mode, else []
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
        "trimmed_bytes": outcome.trimmed_bytes,
        "recheck_verdict": outcome.recheck_verdict,
        "recheck_dangling_refs": outcome.recheck_dangling_refs,
        "recheck_timing_issues": outcome.recheck_timing_issues,
        "recheck_structure_issues": outcome.recheck_structure_issues,
        "synthesized_parts":
            list(rebuild_result.synthesized_parts) if rebuild_result else [],
        "pruned_relationships":
            list(rebuild_result.pruned_relationships) if rebuild_result
            else [],
        "pruned_slide_ids":
            list(rebuild_result.pruned_slide_ids) if rebuild_result else [],
        "cleaned_parts":
            list(rebuild_result.cleaned_parts) if rebuild_result else [],
        "removed_elements":
            list(rebuild_result.removed_elements) if rebuild_result else [],
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
