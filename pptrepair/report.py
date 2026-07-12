"""Human-readable and JSON rendering of diagnoses and repair outcomes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.scanner import ZipStructure

if TYPE_CHECKING:  # avoid a runtime import cycle with pptrepair.repair
    from pptrepair.repair import RepairOutcome

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
