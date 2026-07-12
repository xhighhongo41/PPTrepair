"""Human-readable and JSON rendering of diagnoses."""

from __future__ import annotations

import json

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.scanner import ZipStructure

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
