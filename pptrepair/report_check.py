"""Rendering for the ``pptrepair check`` command.

Split out of :mod:`pptrepair.report` to keep that facade's own module
small; see :mod:`pptrepair.cli_single` for the caller.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from pptrepair.classify import Diagnosis
from pptrepair.report_common import VERDICT_LABELS
from pptrepair.scanner import ZipStructure

if TYPE_CHECKING:  # avoid runtime import cycles with integrity
    from pptrepair.integrity import (
        RefIntegrityResult,
        StructureIntegrityResult,
        TimingIntegrityResult,
    )


def _no_translation(message: str) -> str:
    """Return *message* unchanged (default translator for ``render_text``)."""
    return message


def render_text(diagnosis: Diagnosis,
               ref_integrity: RefIntegrityResult | None = None,
               timing: TimingIntegrityResult | None = None,
               structure: StructureIntegrityResult | None = None,
               tr: Callable[[str], str] | None = None) -> str:
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

    *tr* is the ``--lang`` translator (:func:`pptrepair.i18n.get_translator`),
    left at its default None for the plain, untranslated English report
    (identical to every ``check`` call before ``--lang`` existed). The
    path, the verdict code and every count/part-name/relationship-type
    value stay untranslated data either way; only the surrounding
    descriptive labels/sentences are passed through *tr*.
    """
    if tr is None:
        tr = _no_translation
    lines = [f"=== {diagnosis.path} ==="]
    label = tr(VERDICT_LABELS[diagnosis.verdict])
    lines.append(tr("Verdict: {verdict} ({label})").format(
        verdict=diagnosis.verdict.value, label=label))

    if diagnosis.evidence:
        lines.append(tr("Evidence:"))
        lines.extend(f"  - {item}" for item in diagnosis.evidence)

    salvage = diagnosis.salvage_summary
    if salvage:
        lines.append(tr(
            "Salvageable: {entries_ok}/{entries_total} entries, "
            "{slides_ok}/{slides_total} slides"
        ).format(entries_ok=salvage["entries_ok"],
                 entries_total=salvage["entries_total"],
                 slides_ok=salvage["slides_ok"],
                 slides_total=salvage["slides_total"]))

    if ref_integrity is not None and ref_integrity.dangling:
        affected_parts = {ref.part for ref in ref_integrity.dangling}
        lines.append(tr(
            "Reference integrity: {n} unresolved reference(s) in {m} "
            "part(s)"
        ).format(n=len(ref_integrity.dangling), m=len(affected_parts)))
        lines.append("  " + tr(
            "PowerPoint may offer a one-time repair on first open."))

    if timing is not None and (timing.dangling or timing.media_mismatch):
        timing_count = len(timing.dangling) + len(timing.media_mismatch)
        affected_parts = {ref.part for ref in timing.dangling} | {
            mismatch.part for mismatch in timing.media_mismatch}
        lines.append(tr(
            "Timing integrity: {n} inconsistent reference(s) in {m} "
            "part(s)"
        ).format(n=timing_count, m=len(affected_parts)))

    if structure is not None and structure.missing:
        lines.append(tr(
            "Structure integrity: {n} required relationship(s) missing"
        ).format(n=len(structure.missing)))
        lines.extend(
            f"  - {item.part}: "
            + tr("no {required} relationship").format(
                required=item.required_type)
            for item in structure.missing
        )

    return "\n".join(lines)


def render_json(diagnoses: list[Diagnosis],
               integrities: list[RefIntegrityResult | None] | None = None,
               timings: list[TimingIntegrityResult | None] | None = None,
               structures: list[StructureIntegrityResult | None] | None
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
            integrity: RefIntegrityResult | None = None,
            timing: TimingIntegrityResult | None = None,
            structure_integrity: StructureIntegrityResult | None = None,
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
    integrity: RefIntegrityResult | None,
) -> dict | None:
    """Render *integrity* as its JSON-schema dict, or None when absent."""
    if integrity is None:
        return None
    return {
        "dangling_count": len(integrity.dangling),
        "parts": sorted({ref.part for ref in integrity.dangling}),
    }


def _timing_integrity_to_dict(
    timing: TimingIntegrityResult | None,
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
    structure: StructureIntegrityResult | None,
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
