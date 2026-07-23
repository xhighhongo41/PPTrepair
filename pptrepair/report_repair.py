"""Rendering for the ``pptrepair repair`` command.

Split out of :mod:`pptrepair.report` to keep that facade's own module
small; see :mod:`pptrepair.cli_single` for the caller.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable

from pptrepair.classify import Verdict
from pptrepair.report_common import VERDICT_LABELS

if TYPE_CHECKING:  # avoid runtime import cycles with repair
    from pptrepair.repair import RepairOutcome


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
