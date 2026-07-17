"""Tests for the pure rendering functions in :mod:`pptrepair.report`.

Diagnosis and RepairOutcome objects are built directly here (no file
I/O, no scan/census/classify pipeline): these tests are about the
rendering layer itself -- verdict labels, repair-report layout and the
JSON schema -- not about how a diagnosis or outcome was produced. See
:mod:`test_cli` / :mod:`test_scan_cli` / :mod:`test_repair` for
end-to-end coverage of the same rendering through the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.i18n import get_translator
from pptrepair.integrity import (DanglingRef, MediaMismatch, MissingStructure,
                                 RefIntegrityResult, StructureIntegrityResult,
                                 TimingIntegrityResult, TimingRef)
from pptrepair.rebuild import RebuildResult
from pptrepair.repair import RepairOutcome
from pptrepair.report import (VERDICT_LABELS, render_json, render_repair_json,
                              render_repair_text, render_text)

#: The four v1.1.1 verdicts, mapped to their confirmed English wording.
_NEW_VERDICT_LABELS = {
    Verdict.EMPTY_FILE: "corrupted: file is empty (all content lost)",
    Verdict.FULL_ZERO_FILL:
        "corrupted: file is (almost) entirely zero-filled",
    Verdict.INTERIOR_DAMAGE:
        "corrupted: interior region damaged, archive index intact",
    Verdict.TAIL_FOREIGN_DATA:
        "corrupted: intact archive followed by foreign data",
}

#: Identity translator, matching ``--lang en`` (the default).
_TR = get_translator("en")


def _diagnosis(verdict: Verdict, path: str = "sample.pptx") -> Diagnosis:
    """Build a minimal :class:`Diagnosis` for rendering tests."""
    return Diagnosis(path=Path(path), verdict=verdict,
                     evidence=["synthetic evidence for a rendering test"])


# --- new verdict labels -------------------------------------------------


@pytest.mark.parametrize(("verdict", "label"), _NEW_VERDICT_LABELS.items())
def test_new_verdict_labels_match_the_confirmed_wording(
    verdict: Verdict, label: str,
) -> None:
    """Each new verdict's label is exactly the confirmed English wording."""
    assert VERDICT_LABELS[verdict] == label


@pytest.mark.parametrize("verdict", list(_NEW_VERDICT_LABELS))
def test_new_verdict_label_appears_in_render_text(verdict: Verdict) -> None:
    """render_text includes the new verdict's label."""
    text = render_text(_diagnosis(verdict))

    assert VERDICT_LABELS[verdict] in text


@pytest.mark.parametrize("verdict", list(_NEW_VERDICT_LABELS))
def test_new_verdict_label_appears_in_render_json(verdict: Verdict) -> None:
    """render_json reports the new verdict's code and label."""
    payload = json.loads(render_json([_diagnosis(verdict)]))

    assert payload[0]["verdict"] == verdict.value
    assert payload[0]["label"] == VERDICT_LABELS[verdict]


# --- trim outcome rendering ----------------------------------------------


def test_trim_outcome_text_reports_removed_bytes_and_recheck() -> None:
    """A successful trim's report names the bytes removed and the
    re-check verdict of the leading archive that was kept."""
    diagnosis = _diagnosis(Verdict.TAIL_FOREIGN_DATA, "broken.pptx")
    outcome = RepairOutcome(
        src=Path("broken.pptx"),
        diagnosis=diagnosis,
        mode="trim",
        success=True,
        output_path=Path("broken.repaired.pptx"),
        trimmed_bytes=131072,
        recheck_verdict="normal",
    )

    text = render_repair_text(outcome, _TR)

    assert "Removed 131072 bytes" in text
    assert "Re-check verdict" in text
    assert "normal" in text


# --- unrepairable "nothing survives" hint --------------------------------


@pytest.mark.parametrize("verdict", [Verdict.EMPTY_FILE, Verdict.FULL_ZERO_FILL])
def test_no_content_survives_failure_text_includes_hint(
    verdict: Verdict,
) -> None:
    """An unrepairable EMPTY_FILE / FULL_ZERO_FILL outcome's report
    includes the "nothing survives" hint, in the confirmed wording."""
    diagnosis = _diagnosis(verdict, "broken.pptx")
    outcome = RepairOutcome(
        src=Path("broken.pptx"), diagnosis=diagnosis,
        mode="none", success=False,
    )

    text = render_repair_text(outcome, _TR)

    expected_hint = (
        "Hint: no content survives inside this file. Check the OneDrive "
        "recycle bin, other devices' local copies, and any backups; "
        "version history rarely helps with this damage pattern."
    )
    assert expected_hint in text


def test_hint_absent_for_other_unrepairable_verdicts() -> None:
    """The "nothing survives" hint is scoped to EMPTY_FILE/FULL_ZERO_FILL
    only -- an ordinary NOT_A_ZIP failure must not show it."""
    diagnosis = _diagnosis(Verdict.NOT_A_ZIP, "junk.pptx")
    outcome = RepairOutcome(
        src=Path("junk.pptx"), diagnosis=diagnosis,
        mode="none", success=False,
    )

    text = render_repair_text(outcome, _TR)

    assert "no content survives" not in text


# --- render_repair_json schema -------------------------------------------


def test_render_repair_json_includes_trimmed_bytes_for_a_trim_outcome() -> None:
    """render_repair_json reports the trimmed byte count for a trim outcome."""
    diagnosis = _diagnosis(Verdict.TAIL_FOREIGN_DATA, "broken.pptx")
    outcome = RepairOutcome(
        src=Path("broken.pptx"),
        diagnosis=diagnosis,
        mode="trim",
        success=True,
        output_path=Path("broken.repaired.pptx"),
        trimmed_bytes=131072,
        recheck_verdict="normal",
    )

    payload = json.loads(render_repair_json(outcome))

    assert "trimmed_bytes" in payload
    assert payload["trimmed_bytes"] == 131072


def test_render_repair_json_trimmed_bytes_is_null_outside_trim_mode() -> None:
    """render_repair_json reports trimmed_bytes as null for a non-trim mode."""
    diagnosis = _diagnosis(Verdict.NORMAL, "normal.pptx")
    outcome = RepairOutcome(
        src=Path("normal.pptx"), diagnosis=diagnosis,
        mode="none", success=True,
    )

    payload = json.loads(render_repair_json(outcome))

    assert "trimmed_bytes" in payload
    assert payload["trimmed_bytes"] is None


# --- v1.1.2: reference-integrity fields in render_repair_json ------------


def test_render_repair_json_includes_reference_integrity_fields() -> None:
    """render_repair_json reports recheck_dangling_refs plus the
    rebuild's cleaned_parts/removed_elements when a RebuildResult is
    attached."""
    diagnosis = _diagnosis(Verdict.INTERIOR_DAMAGE, "broken.pptx")
    rebuild_result = RebuildResult(
        output_path=Path("broken.repaired.pptx"),
        cleaned_parts=["ppt/slides/slide2.xml"],
        removed_elements=["ppt/slides/slide2.xml: pic (rId3)"],
    )
    outcome = RepairOutcome(
        src=Path("broken.pptx"),
        diagnosis=diagnosis,
        mode="rebuild",
        success=True,
        output_path=Path("broken.repaired.pptx"),
        rebuild_result=rebuild_result,
        recheck_verdict="normal",
        recheck_dangling_refs=0,
    )

    payload = json.loads(render_repair_json(outcome))

    assert payload["recheck_dangling_refs"] == 0
    assert payload["cleaned_parts"] == ["ppt/slides/slide2.xml"]
    assert payload["removed_elements"] == ["ppt/slides/slide2.xml: pic (rId3)"]


def test_render_repair_json_reference_integrity_defaults_are_empty() -> None:
    """Outside rebuild mode (no RebuildResult attached), cleaned_parts
    and removed_elements are empty lists, and recheck_dangling_refs is
    null when the recheck never ran (mode "none")."""
    diagnosis = _diagnosis(Verdict.NORMAL, "normal.pptx")
    outcome = RepairOutcome(
        src=Path("normal.pptx"), diagnosis=diagnosis,
        mode="none", success=True,
    )

    payload = json.loads(render_repair_json(outcome))

    assert payload["recheck_dangling_refs"] is None
    assert payload["cleaned_parts"] == []
    assert payload["removed_elements"] == []


# --- v1.1.2 addendum: timing/structure recheck fields in render_repair_json


def test_render_repair_json_includes_timing_and_structure_recheck_counts() -> None:
    """render_repair_json reports the recheck_timing_issues and
    recheck_structure_issues counts when the recheck ran."""
    diagnosis = _diagnosis(Verdict.INTERIOR_DAMAGE, "broken.pptx")
    outcome = RepairOutcome(
        src=Path("broken.pptx"),
        diagnosis=diagnosis,
        mode="rebuild",
        success=True,
        output_path=Path("broken.repaired.pptx"),
        recheck_verdict="normal",
        recheck_dangling_refs=0,
        recheck_timing_issues=0,
        recheck_structure_issues=0,
    )

    payload = json.loads(render_repair_json(outcome))

    assert payload["recheck_timing_issues"] == 0
    assert payload["recheck_structure_issues"] == 0


def test_render_repair_json_timing_and_structure_recheck_counts_are_null_by_default() -> None:
    """Outside rebuild/trim mode (recheck never ran), recheck_timing_issues
    and recheck_structure_issues are both null."""
    diagnosis = _diagnosis(Verdict.NORMAL, "normal.pptx")
    outcome = RepairOutcome(
        src=Path("normal.pptx"), diagnosis=diagnosis,
        mode="none", success=True,
    )

    payload = json.loads(render_repair_json(outcome))

    assert payload["recheck_timing_issues"] is None
    assert payload["recheck_structure_issues"] is None


# --- v1.1.2: reference-integrity lines in render_repair_text -------------


def test_render_repair_text_reports_cleanup_and_recheck_dangling_refs() -> None:
    """A rebuild outcome whose RebuildResult cleaned up stale references
    reports the cleanup count and the remaining unresolved-reference
    count from the post-repair recheck."""
    diagnosis = _diagnosis(Verdict.INTERIOR_DAMAGE, "broken.pptx")
    rebuild_result = RebuildResult(
        output_path=Path("broken.repaired.pptx"),
        cleaned_parts=["ppt/slides/slide2.xml", "ppt/slides/slide3.xml"],
        removed_elements=[
            "ppt/slides/slide2.xml: pic (rId3)",
            "ppt/slides/slide3.xml: videoFile (rId4)",
            "ppt/slides/slide3.xml: p14:media (rId5)",
        ],
    )
    outcome = RepairOutcome(
        src=Path("broken.pptx"),
        diagnosis=diagnosis,
        mode="rebuild",
        success=True,
        output_path=Path("broken.repaired.pptx"),
        rebuild_result=rebuild_result,
        recheck_verdict="normal",
        recheck_dangling_refs=0,
    )

    text = render_repair_text(outcome, _TR)

    assert "Removed 3 stale reference(s) from 2 rebuilt part(s)." in text
    assert "Unresolved references after repair: 0" in text


def test_render_repair_text_trim_notes_preexisting_dangling_refs() -> None:
    """A trim outcome with unresolved references left after the recheck
    notes that they predate this tool, since trim reproduces the
    original bytes exactly."""
    diagnosis = _diagnosis(Verdict.TAIL_FOREIGN_DATA, "broken.pptx")
    outcome = RepairOutcome(
        src=Path("broken.pptx"),
        diagnosis=diagnosis,
        mode="trim",
        success=True,
        output_path=Path("broken.repaired.pptx"),
        trimmed_bytes=131072,
        recheck_verdict="normal",
        recheck_dangling_refs=4,
    )

    text = render_repair_text(outcome, _TR)

    assert "Unresolved references after repair: 4" in text
    assert (
        "Note: these unresolved references existed in the original "
        "archive and were left untouched (the recovered file is "
        "byte-identical to the original)."
    ) in text


def test_render_repair_text_omits_reference_integrity_lines_by_default() -> None:
    """A rebuild outcome with no RebuildResult and no recheck result
    (recheck_dangling_refs left at its None default) adds none of the
    v1.1.2 reference-integrity lines."""
    diagnosis = _diagnosis(Verdict.INTERIOR_DAMAGE, "broken.pptx")
    outcome = RepairOutcome(
        src=Path("broken.pptx"),
        diagnosis=diagnosis,
        mode="rebuild",
        success=True,
        output_path=Path("broken.repaired.pptx"),
        recheck_verdict="normal",
    )

    text = render_repair_text(outcome, _TR)

    assert "stale reference" not in text
    assert "Unresolved references" not in text


# --- v1.1.2: ref_integrity summary in render_text (check) ----------------


def test_render_text_unchanged_without_ref_integrity() -> None:
    """render_text's output is unchanged when ref_integrity is omitted,
    keeping the plain check report layout stable."""
    diagnosis = _diagnosis(Verdict.NORMAL, "clean.pptx")

    assert render_text(diagnosis) == render_text(diagnosis, ref_integrity=None)


def test_render_text_unchanged_for_clean_ref_integrity() -> None:
    """A RefIntegrityResult with no dangling references adds nothing to
    render_text's output."""
    diagnosis = _diagnosis(Verdict.NORMAL, "clean.pptx")
    clean = RefIntegrityResult(parts_scanned=5, dangling=[],
                               missing_rels_parts=[], parse_errors=[])

    text = render_text(diagnosis, ref_integrity=clean)

    assert text == render_text(diagnosis)


def test_render_text_reports_dangling_references() -> None:
    """A RefIntegrityResult with dangling references adds a summary line
    (count of references and distinct parts) and the PowerPoint hint."""
    diagnosis = _diagnosis(Verdict.NORMAL, "clean.pptx")
    integrity = RefIntegrityResult(
        parts_scanned=5,
        dangling=[
            DanglingRef(part="ppt/slides/slide1.xml", attribute="embed",
                        rid="rId99", element="blip"),
            DanglingRef(part="ppt/slides/slide2.xml", attribute="embed",
                        rid="rId42", element="blip"),
        ],
        missing_rels_parts=[], parse_errors=[],
    )

    text = render_text(diagnosis, ref_integrity=integrity)

    assert (
        "Reference integrity: 2 unresolved reference(s) in 2 part(s)"
        in text
    )
    assert "PowerPoint may offer a one-time repair on first open." in text


# --- v1.1.2: xml_ref_integrity in render_json (check --json) -------------


def test_render_json_xml_ref_integrity_is_null_without_integrities() -> None:
    """Without an integrities list, every entry's xml_ref_integrity is
    null (the pre-v1.1.2 default)."""
    payload = json.loads(render_json([_diagnosis(Verdict.NORMAL)]))

    assert payload[0]["xml_ref_integrity"] is None


def test_render_json_reports_dangling_count_and_parts() -> None:
    """With a RefIntegrityResult supplied, xml_ref_integrity reports the
    dangling count and the sorted, deduplicated list of affected parts."""
    diagnosis = _diagnosis(Verdict.NORMAL)
    integrity = RefIntegrityResult(
        parts_scanned=5,
        dangling=[
            DanglingRef(part="ppt/slides/slide2.xml", attribute="embed",
                        rid="rId9", element="blip"),
            DanglingRef(part="ppt/slides/slide1.xml", attribute="embed",
                        rid="rId9", element="blip"),
        ],
        missing_rels_parts=[], parse_errors=[],
    )

    payload = json.loads(render_json([diagnosis], [integrity]))

    assert payload[0]["xml_ref_integrity"] == {
        "dangling_count": 2,
        "parts": ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml"],
    }


# --- v1.1.2 addendum: timing/structure summary in render_text (check) ----


def test_render_text_unchanged_without_timing_or_structure() -> None:
    """render_text's output is unchanged when timing/structure are
    omitted, keeping the plain check report layout stable."""
    diagnosis = _diagnosis(Verdict.NORMAL, "clean.pptx")

    assert render_text(diagnosis) == render_text(
        diagnosis, timing=None, structure=None)


def test_render_text_unchanged_for_clean_timing_and_structure() -> None:
    """A TimingIntegrityResult/StructureIntegrityResult with nothing to
    report add nothing to render_text's output."""
    diagnosis = _diagnosis(Verdict.NORMAL, "clean.pptx")
    clean_timing = TimingIntegrityResult(
        parts_scanned=5, dangling=[], media_mismatch=[], parse_errors=[])
    clean_structure = StructureIntegrityResult(
        parts_checked=5, missing=[], parse_errors=[])

    text = render_text(diagnosis, timing=clean_timing,
                       structure=clean_structure)

    assert text == render_text(diagnosis)


def test_render_text_reports_timing_inconsistencies() -> None:
    """A TimingIntegrityResult with dangling refs and media mismatches
    adds a single summary line covering both, counting distinct parts
    across the two lists."""
    diagnosis = _diagnosis(Verdict.NORMAL, "clean.pptx")
    timing = TimingIntegrityResult(
        parts_scanned=5,
        dangling=[
            TimingRef(part="ppt/slides/slide1.xml", element="spTgt",
                      spid="99"),
        ],
        media_mismatch=[
            MediaMismatch(part="ppt/slides/slide2.xml", kind="video",
                          spid="6"),
        ],
        parse_errors=[],
    )

    text = render_text(diagnosis, timing=timing)

    assert (
        "Timing integrity: 2 inconsistent reference(s) in 2 part(s)" in text
    )


def test_render_text_reports_missing_structural_relationships() -> None:
    """A StructureIntegrityResult with missing entries adds a summary
    line plus one indented line per missing relationship."""
    diagnosis = _diagnosis(Verdict.NORMAL, "clean.pptx")
    structure = StructureIntegrityResult(
        parts_checked=5,
        missing=[
            MissingStructure(part="ppt/slideMasters/slideMaster2.xml",
                             required_type="theme"),
        ],
        parse_errors=[],
    )

    text = render_text(diagnosis, structure=structure)

    assert "Structure integrity: 1 required relationship(s) missing" in text
    assert (
        "  - ppt/slideMasters/slideMaster2.xml: no theme relationship"
        in text
    )


# --- v1.1.2 addendum: timing_integrity/structure_integrity in render_json --


def test_render_json_timing_and_structure_integrity_are_null_by_default() -> None:
    """Without timings/structures lists, every entry's timing_integrity
    and structure_integrity are null."""
    payload = json.loads(render_json([_diagnosis(Verdict.NORMAL)]))

    assert payload[0]["timing_integrity"] is None
    assert payload[0]["structure_integrity"] is None


def test_render_json_reports_timing_integrity_counts_and_parts() -> None:
    """With a TimingIntegrityResult supplied, timing_integrity reports
    the dangling/media-mismatch counts and the sorted, deduplicated
    list of affected parts."""
    diagnosis = _diagnosis(Verdict.NORMAL)
    timing = TimingIntegrityResult(
        parts_scanned=5,
        dangling=[
            TimingRef(part="ppt/slides/slide2.xml", element="spTgt",
                      spid="99"),
        ],
        media_mismatch=[
            MediaMismatch(part="ppt/slides/slide1.xml", kind="audio",
                          spid="8"),
        ],
        parse_errors=[],
    )

    payload = json.loads(
        render_json([diagnosis], timings=[timing]))

    assert payload[0]["timing_integrity"] == {
        "dangling_count": 1,
        "media_mismatch_count": 1,
        "parts": ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml"],
    }


def test_render_json_reports_structure_integrity_missing_items() -> None:
    """With a StructureIntegrityResult supplied, structure_integrity
    reports the missing count and each missing (part, required) item."""
    diagnosis = _diagnosis(Verdict.NORMAL)
    structure = StructureIntegrityResult(
        parts_checked=5,
        missing=[
            MissingStructure(part="ppt/slideMasters/slideMaster2.xml",
                             required_type="theme"),
        ],
        parse_errors=[],
    )

    payload = json.loads(
        render_json([diagnosis], structures=[structure]))

    assert payload[0]["structure_integrity"] == {
        "missing_count": 1,
        "items": [
            {"part": "ppt/slideMasters/slideMaster2.xml",
             "required": "theme"},
        ],
    }
