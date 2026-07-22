"""Human-readable and JSON rendering of diagnoses and repair outcomes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.scanner import ZipStructure
from pptrepair.twin import TwinCandidate, build_twin_index, find_twin_candidates

if TYPE_CHECKING:  # avoid runtime import cycles with repair / scan / batch
    from pptrepair.batch import BatchItem, BatchResult
    from pptrepair.integrity import (RefIntegrityResult,
                                     StructureIntegrityResult,
                                     TimingIntegrityResult)
    from pptrepair.repair import RepairOutcome
    from pptrepair.scan import FileOutcome, ScanResult

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
    Verdict.FOREIGN_ZIP_OVERWRITE:
        "corrupted: overwritten with fragments of another ZIP archive",
    Verdict.SCATTERED_OVERWRITE:
        "corrupted: archive body largely overwritten in place",
}

#: Number of twin-restoration candidates listed individually per file in
#: a text report before the remainder collapses into one "+n more" line.
_TWIN_CANDIDATES_DISPLAY_LIMIT = 3


def _twin_reason_label(confidence: str, tr: Callable[[str], str]) -> str:
    """Return the translated one-line reason for a twin candidate's confidence.

    Each branch calls ``tr()`` with a literal string, matching every
    other translatable string in this module, so the message extractor
    (:mod:`tools.extract_messages`, driven by static AST analysis) can
    find it without a dedicated dynamic-message table.
    """
    if confidence == "high":
        return tr("same name and size")
    if confidence == "medium":
        return tr("same size only")
    return tr("same name only")


def _twin_candidates_map(
    outcomes: "Sequence[FileOutcome]",
) -> dict[Path, list[TwinCandidate]]:
    """Map each corrupted file's path to its twin-restoration candidates.

    Builds one :class:`~pptrepair.twin.TwinIndex` from *outcomes* (see
    :func:`~pptrepair.twin.build_twin_index`) and queries it, via
    :func:`~pptrepair.twin.find_twin_candidates`, for every outcome
    whose verdict is not :attr:`Verdict.NORMAL` (an outcome with no
    ``diagnosis`` -- a failed pipeline -- is skipped). The queried size
    is ``diagnosis.structure.size``, or None when ``structure`` is
    None. Only paths with at least one candidate are kept in the
    returned mapping.
    """
    index = build_twin_index(outcomes)
    candidates_map: dict[Path, list[TwinCandidate]] = {}
    for outcome in outcomes:
        diagnosis = outcome.diagnosis
        if diagnosis is None or diagnosis.verdict == Verdict.NORMAL:
            continue
        size = (
            diagnosis.structure.size if diagnosis.structure is not None
            else None
        )
        candidates = find_twin_candidates(outcome.path, size, index)
        if candidates:
            candidates_map[outcome.path] = candidates
    return candidates_map


def _twin_candidate_text_lines(
    path: Path, twin_map: dict[Path, list[TwinCandidate]],
    tr: Callable[[str], str],
) -> list[str]:
    """Render the indented twin-candidate lines that follow *path*'s own
    line in a text report.

    Up to :data:`_TWIN_CANDIDATES_DISPLAY_LIMIT` candidates are listed
    individually, each as a translated ``restore candidate: <path>
    (<reason>)`` line; any remaining candidates collapse into a single
    translated ``(+<n> more restore candidates)`` line. Returns an
    empty list when *path* has no entry in *twin_map*.
    """
    candidates = twin_map.get(path, [])
    shown = candidates[:_TWIN_CANDIDATES_DISPLAY_LIMIT]
    lines = [
        tr("  restore candidate: {path} ({reason})").format(
            path=candidate.path,
            reason=_twin_reason_label(candidate.confidence, tr))
        for candidate in shown
    ]
    remaining = len(candidates) - len(shown)
    if remaining > 0:
        lines.append(
            tr("  (+{n} more restore candidates)").format(n=remaining))
    return lines


def _twin_candidates_to_json(candidates: list[TwinCandidate]) -> list[dict]:
    """Render *candidates* as the JSON-schema list for a "twin_candidates" key."""
    return [
        {"path": str(candidate.path), "confidence": candidate.confidence,
         "size": candidate.size}
        for candidate in candidates
    ]


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
      ``  - {path}: {verdict}`` per corrupted outcome, each immediately
      followed by that file's twin-restoration candidates, when any
      (see :func:`_twin_candidate_text_lines`), and an ``Errors:``
      section listing walk errors and per-file pipeline errors as
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


def _scan_summary_lines(result: "ScanResult", tr: Callable[[str], str],
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

    if include_files:
        corrupted = result.corrupted()
        if corrupted:
            lines.append(tr("Corrupted files:"))
            twin_map = _twin_candidates_map(result.outcomes)
            for outcome in corrupted:
                lines.append(
                    f"  - {outcome.path}: {outcome.diagnosis.verdict.value}")
                lines.extend(
                    _twin_candidate_text_lines(outcome.path, twin_map, tr))

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

    return lines


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
                      "fingerprint": str | null,
                      "twin_candidates": [    # present only when >= 1
                        {"path": str, "confidence": "high" | "medium"
                                                     | "low", "size": int}
                      ]}, ...],
          "skipped_cloud": [str, ...],
          "skipped_legacy": [str, ...],
          "skipped_temp": [str, ...],
          "errors": [{"path": str, "error": str}, ...],
          "report_dir": str | null
        }

    ``files`` covers every diagnosed file in walk order; ``errors``
    merges walk errors and per-file pipeline failures in that order.
    """
    return json.dumps(_scan_payload(result), indent=2)


def _scan_payload(result: "ScanResult") -> dict:
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
    twin_map = _twin_candidates_map(result.outcomes)

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
            _scan_file_entry(outcome, twin_map)
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
    return payload


def _scan_file_entry(
    outcome: "FileOutcome", twin_map: dict[Path, list[TwinCandidate]],
) -> dict:
    """Render one diagnosed file's entry in :func:`_scan_payload`'s ``files`` list.

    ``twin_candidates`` is added only when ``outcome.path`` has at
    least one entry in *twin_map*; a file with none carries no such
    key at all (see :func:`render_scan_json`'s schema).
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
    return entry


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
    * *include_files* only: a translated ``Repairs:`` header followed by
      one line per :class:`~pptrepair.batch.BatchItem`, in batch order
      (see :func:`_repair_item_line`); an ``"unrepairable"``/``"failed"``
      item's line is immediately followed by that file's
      twin-restoration candidates, when any (see
      :func:`_twin_candidate_text_lines`), using the twin index built
      from ``result.scan.outcomes`` (every diagnosed file, not just the
      corrupted ones this batch attempted to repair).
    """
    lines = _scan_summary_lines(result.scan, tr, include_files=False)

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
        for item in result.items:
            lines.append(_repair_item_line(item))
            if item.action in ("unrepairable", "failed"):
                lines.extend(
                    _twin_candidate_text_lines(
                        item.source.path, twin_map, tr))

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
          "schema_version": 2,
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
              ]
            },
            ...
          ]
        }

    ``scan`` embeds :func:`_scan_payload`'s dict verbatim, so it matches
    :func:`render_scan_json`'s own top-level schema field for field.
    ``repairs`` covers every corrupted file in batch order; for an item
    whose repair was never executed (``skipped_existing``, ``failed``,
    a CFB ``unrepairable``, or a dry-run ``planned``), every field that
    comes from :class:`~pptrepair.repair.RepairOutcome` is None or an
    empty list, per the key-by-key null noted above. ``schema_version``
    became 2 when ``twin_candidates`` was added.
    """
    twin_map = _twin_candidates_map(result.scan.outcomes)
    payload = {
        "schema_version": 2,
        "dry_run": result.dry_run,
        "in_place": result.in_place,
        "output_dir": str(result.output_dir)
        if result.output_dir is not None else None,
        "scan": _scan_payload(result.scan),
        "counts": result.counts(),
        "unrepaired_remaining": result.unrepaired_remaining(),
        "warnings": list(result.warnings),
        "repairs": [
            _batch_item_to_dict(item, twin_map) for item in result.items
        ],
    }
    return json.dumps(payload, indent=2)


def _batch_item_to_dict(
    item: "BatchItem", twin_map: dict[Path, list[TwinCandidate]],
) -> dict:
    """Render one :class:`~pptrepair.batch.BatchItem` as its JSON-schema dict.

    See :func:`render_batch_json` for the field-by-field schema; every
    :class:`~pptrepair.repair.RepairOutcome`-derived field falls back to
    None (or an empty list) when ``item.repair`` is None, i.e. the item
    was never handed to :func:`~pptrepair.repair.repair_file`.
    ``twin_candidates`` is added, mirroring
    :func:`render_batch_text`'s own restore-candidate lines, only for
    an ``"unrepairable"``/``"failed"`` item whose source path has at
    least one candidate in *twin_map*.
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
    return entry
