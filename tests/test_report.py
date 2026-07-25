"""Tests for the pure rendering functions in :mod:`pptrepair.report`.

Diagnosis and RepairOutcome objects are built directly here (no file
I/O, no scan/census/classify pipeline): these tests are about the
rendering layer itself -- verdict labels, repair-report layout and the
JSON schema -- not about how a diagnosis or outcome was produced. See
:mod:`test_cli` / :mod:`test_scan_cli` / :mod:`test_repair` for
end-to-end coverage of the same rendering through the CLI.

The v1.3 merge-candidate / lineage-candidate sections are the
exception: they need real central-directory/local-file-header census
data for :func:`pptrepair.origin.score_origin` to compare, so those
tests run the real scan pipeline (:func:`pptrepair.scan.scan_paths`)
over a small synthetic corpus written under ``tmp_path``, the same way
:mod:`test_merge` exercises :func:`pptrepair.origin.score_origin`.
"""

from __future__ import annotations

import json
from pathlib import Path

import fixtures
import pytest

from pptrepair.batch import BatchItem, BatchResult
from pptrepair.classify import Diagnosis, Verdict
from pptrepair.i18n import get_translator
from pptrepair.integrity import (
    DanglingRef,
    MediaMismatch,
    MissingStructure,
    RefIntegrityResult,
    StructureIntegrityResult,
    TimingIntegrityResult,
    TimingRef,
)
from pptrepair.rebuild import RebuildResult
from pptrepair.repair import RepairOutcome
from pptrepair.report import (
    VERDICT_LABELS,
    render_batch_json,
    render_batch_text,
    render_json,
    render_repair_json,
    render_repair_text,
    render_scan_json,
    render_scan_text,
    render_text,
)
from pptrepair.scan import FileOutcome, ScanResult, scan_paths
from pptrepair.scanner import ZipStructure
from pptrepair.walker import WalkResult

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


# --- v1.2: render_batch_text / render_batch_json (repair-all summary) ----

#: A CFB (encrypted/legacy) structure, for the "unrepairable_cfb" branch.
_CFB_STRUCTURE = ZipStructure(size=8, head_kind="cfb", zero_runs=[],
                              lfh_offsets=[], cd_sig_count=0, eocd=None)


def _batch_outcome(path: str, verdict: Verdict,
                   structure: ZipStructure | None = None) -> FileOutcome:
    """Build a minimal :class:`FileOutcome` for batch-report tests."""
    diagnosis = Diagnosis(path=Path(path), verdict=verdict,
                          evidence=["synthetic evidence"], structure=structure)
    return FileOutcome(path=Path(path), diagnosis=diagnosis)


def _live_batch_result() -> BatchResult:
    """Build a BatchResult exercising every render_batch_text/json branch.

    One item per action: rebuild/trim/extract successes ("repaired"),
    an encrypted/legacy CFB and a no-content EMPTY_FILE (both
    "unrepairable"), a pre-existing artifact ("skipped_existing") and
    one exception ("failed"), plus a collision-fallback warning.
    """
    rebuild_outcome = _batch_outcome("root/a.pptx", Verdict.TAIL_TRUNCATED)
    trim_outcome = _batch_outcome("root/b.pptx", Verdict.TAIL_FOREIGN_DATA)
    extract_outcome = _batch_outcome("root/c.pptx", Verdict.HEAD_ZERO_FILL)
    cfb_outcome = _batch_outcome("root/d.pptx", Verdict.NOT_A_ZIP,
                                 _CFB_STRUCTURE)
    empty_outcome = _batch_outcome("root/e.pptx", Verdict.EMPTY_FILE)
    skip_outcome = _batch_outcome("root/f.pptx", Verdict.TAIL_TRUNCATED)
    fail_outcome = _batch_outcome("root/g.pptx", Verdict.TAIL_TRUNCATED)

    items = [
        BatchItem(
            source=rebuild_outcome, planned_output=Path("out/a.repaired.pptx"),
            action="repaired",
            repair=RepairOutcome(
                src=rebuild_outcome.path, diagnosis=rebuild_outcome.diagnosis,
                mode="rebuild", success=True,
                output_path=Path("out/a.repaired.pptx"),
                recheck_verdict="normal", recheck_dangling_refs=0,
                recheck_timing_issues=0, recheck_structure_issues=0),
        ),
        BatchItem(
            source=trim_outcome, planned_output=Path("out/b.repaired.pptx"),
            action="repaired",
            repair=RepairOutcome(
                src=trim_outcome.path, diagnosis=trim_outcome.diagnosis,
                mode="trim", success=True,
                output_path=Path("out/b.repaired.pptx"),
                trimmed_bytes=1024, recheck_verdict="normal"),
        ),
        BatchItem(
            source=extract_outcome, planned_output=Path("out/c.salvaged"),
            action="repaired",
            repair=RepairOutcome(
                src=extract_outcome.path, diagnosis=extract_outcome.diagnosis,
                mode="extract", success=True, output_path=Path("out/c.salvaged"),
                lost_slide_numbers=[2], lost_entries_total=1),
        ),
        BatchItem(source=cfb_outcome, planned_output=None,
                 action="unrepairable"),
        BatchItem(
            source=empty_outcome, planned_output=None, action="unrepairable",
            repair=RepairOutcome(
                src=empty_outcome.path, diagnosis=empty_outcome.diagnosis,
                mode="none", success=False),
        ),
        BatchItem(source=skip_outcome, planned_output=Path("out/f.repaired.pptx"),
                 action="skipped_existing"),
        BatchItem(source=fail_outcome, planned_output=None, action="failed",
                 error="RuntimeError: boom"),
    ]
    scan = ScanResult(
        roots=[Path("root")], walk=WalkResult(),
        outcomes=[item.source for item in items],
    )
    return BatchResult(
        scan=scan, items=items, output_dir=Path("out"), in_place=False,
        dry_run=False,
        warnings=[("output name collision under out: root/h.pptx falls back "
                 "to base 'h.pptx' because 'h' is already taken by root/i.pptx")],
    )


def _dry_run_batch_result() -> BatchResult:
    """Build a dry-run BatchResult with one planned item."""
    planned_outcome = _batch_outcome("root/h.pptx", Verdict.TAIL_TRUNCATED)
    items = [BatchItem(source=planned_outcome,
                       planned_output=Path("out/h.repaired.pptx"),
                       action="planned")]
    scan = ScanResult(roots=[Path("root")], walk=WalkResult(),
                      outcomes=[planned_outcome])
    return BatchResult(scan=scan, items=items, output_dir=Path("out"),
                       in_place=False, dry_run=True)


def test_render_batch_text_reports_repaired_breakdown() -> None:
    """The repair summary reports the repaired total and its mode breakdown."""
    text = render_batch_text(_live_batch_result(), _TR)

    assert "Repaired: 3 file(s)" in text
    assert "  rebuild: 1" in text
    assert "  trim: 1" in text
    assert "  extract: 1" in text


def test_render_batch_text_reports_unrepairable_breakdown() -> None:
    """Unrepairable total plus the CFB and no-content sub-notes appear."""
    text = render_batch_text(_live_batch_result(), _TR)

    assert "Unrepairable: 2 file(s)" in text
    assert "Encrypted or legacy Office file(s) (not attempted): 1" in text
    assert "No content survives: 1 file(s)" in text


def test_render_batch_text_reports_skipped_with_force_hint() -> None:
    """A skipped-existing artifact is counted and paired with the --force hint."""
    text = render_batch_text(_live_batch_result(), _TR)

    assert "Skipped (output exists): 1 file(s)" in text
    assert "Hint: pass --force to overwrite the existing output." in text


def test_render_batch_text_reports_failed() -> None:
    """A failed repair is counted in the summary."""
    text = render_batch_text(_live_batch_result(), _TR)

    assert "Failed: 1 file(s)" in text


def test_render_batch_text_reports_warnings() -> None:
    """Batch-level warnings (e.g. collision fallbacks) are listed verbatim."""
    result = _live_batch_result()

    text = render_batch_text(result, _TR)

    assert "Warnings:" in text
    assert result.warnings[0] in text


def test_render_batch_text_omits_per_file_lines_by_default() -> None:
    """include_files=False (the default) prints no per-file section."""
    text = render_batch_text(_live_batch_result(), _TR)

    assert "Repairs:" not in text
    assert "root/a.pptx" not in text


def test_render_batch_text_include_files_lists_every_item() -> None:
    """include_files=True lists one line per item, including its error."""
    result = _live_batch_result()

    text = render_batch_text(result, _TR, include_files=True)

    assert "Repairs:" in text
    for item in result.items:
        assert str(item.source.path) in text
    assert "error=RuntimeError: boom" in text


def test_render_batch_text_dry_run_shows_planned_and_notice() -> None:
    """dry_run replaces the Repaired line with Planned plus a notice line."""
    text = render_batch_text(_dry_run_batch_result(), _TR)

    assert "Planned: 1 file(s)" in text
    assert "dry run: nothing was written." in text
    assert "Repaired:" not in text


def test_render_batch_json_top_level_keys_and_counts() -> None:
    """The top-level schema fields mirror the BatchResult they describe."""
    result = _live_batch_result()

    payload = json.loads(render_batch_json(result))

    assert payload["schema_version"] == 3
    assert payload["dry_run"] is False
    assert payload["in_place"] is False
    assert payload["output_dir"] == str(result.output_dir)
    assert payload["counts"] == result.counts()
    assert payload["unrepaired_remaining"] == result.unrepaired_remaining()
    assert payload["warnings"] == result.warnings


def test_render_batch_json_scan_matches_render_scan_json() -> None:
    """The embedded "scan" object is exactly render_scan_json's own payload."""
    result = _live_batch_result()

    payload = json.loads(render_batch_json(result))

    assert payload["scan"] == json.loads(render_scan_json(result.scan))


def test_render_batch_json_repairs_array_reflects_each_item() -> None:
    """Each repairs[] entry reports its item's action/mode/output/recheck."""
    result = _live_batch_result()

    repairs = json.loads(render_batch_json(result))["repairs"]

    assert len(repairs) == len(result.items)

    rebuild_entry = repairs[0]
    assert rebuild_entry["path"] == "root/a.pptx"
    assert rebuild_entry["verdict"] == Verdict.TAIL_TRUNCATED.value
    assert rebuild_entry["action"] == "repaired"
    assert rebuild_entry["mode"] == "rebuild"
    assert rebuild_entry["output"] == "out/a.repaired.pptx"
    assert rebuild_entry["recheck_verdict"] == "normal"
    assert rebuild_entry["recheck_dangling_refs"] == 0

    skipped_entry = repairs[5]
    assert skipped_entry["action"] == "skipped_existing"
    assert skipped_entry["mode"] is None
    assert skipped_entry["output"] == "out/f.repaired.pptx"
    assert skipped_entry["recheck_verdict"] is None
    assert skipped_entry["lost_slide_numbers"] == []
    assert skipped_entry["warnings"] == []

    failed_entry = repairs[6]
    assert failed_entry["action"] == "failed"
    assert failed_entry["mode"] is None
    assert failed_entry["output"] is None
    assert failed_entry["error"] == "RuntimeError: boom"


def test_render_batch_json_dry_run_planned_item_has_null_mode() -> None:
    """A dry-run "planned" item reports mode=null (repair_file never ran)."""
    result = _dry_run_batch_result()

    payload = json.loads(render_batch_json(result))

    assert payload["dry_run"] is True
    planned_entry = payload["repairs"][0]
    assert planned_entry["action"] == "planned"
    assert planned_entry["mode"] is None
    assert planned_entry["output"] == "out/h.repaired.pptx"


# --- v1.2.1: twin-restoration candidates in scan / repair-all reports ----


def _twin_structure(size: int) -> ZipStructure:
    """Build a minimal :class:`ZipStructure` carrying only *size*."""
    return ZipStructure(size=size, head_kind="zip", zero_runs=[],
                        lfh_offsets=[], cd_sig_count=0, eocd=None)


def _twin_outcome(path: str, verdict: Verdict,
                  size: int | None) -> FileOutcome:
    """Build a :class:`FileOutcome` whose diagnosis carries only *size*."""
    structure = _twin_structure(size) if size is not None else None
    diagnosis = Diagnosis(path=Path(path), verdict=verdict, structure=structure)
    return FileOutcome(path=Path(path), diagnosis=diagnosis)


def _twin_scan_result(outcomes: list[FileOutcome]) -> ScanResult:
    """Build a minimal :class:`ScanResult` wrapping *outcomes*."""
    return ScanResult(roots=[Path("root")], walk=WalkResult(), outcomes=outcomes)


def test_scan_text_lists_a_high_confidence_restore_candidate() -> None:
    """A corrupted file's line is immediately followed by a same-name,
    same-size normal twin, reported as a high-confidence candidate."""
    broken = _twin_outcome("root/a.pptx", Verdict.TAIL_TRUNCATED, 1000)
    twin = _twin_outcome("root/backup/a.pptx", Verdict.NORMAL, 1000)
    result = _twin_scan_result([broken, twin])

    text = render_scan_text(result, _TR, include_files=True)

    assert (
        "restore candidate: root/backup/a.pptx (same name and size)" in text
    )


def test_scan_json_reports_twin_candidates_for_a_corrupted_file() -> None:
    """The corrupted file's per-file entry carries a twin_candidates list
    with the expected path/confidence/size."""
    broken = _twin_outcome("root/a.pptx", Verdict.TAIL_TRUNCATED, 1000)
    twin = _twin_outcome("root/backup/a.pptx", Verdict.NORMAL, 1000)
    result = _twin_scan_result([broken, twin])

    payload = json.loads(render_scan_json(result))

    entry = next(f for f in payload["files"] if f["path"] == "root/a.pptx")
    assert entry["twin_candidates"] == [
        {"path": "root/backup/a.pptx", "confidence": "high", "size": 1000},
    ]


def test_scan_report_omits_candidates_when_none_exist() -> None:
    """A corrupted file with no matching twin gets neither a text line
    nor a twin_candidates key in its JSON entry."""
    broken = _twin_outcome("root/a.pptx", Verdict.TAIL_TRUNCATED, 1000)
    result = _twin_scan_result([broken])

    text = render_scan_text(result, _TR, include_files=True)
    payload = json.loads(render_scan_json(result))

    assert "restore candidate" not in text
    assert "twin_candidates" not in payload["files"][0]


def test_scan_report_never_lists_candidates_for_a_normal_file_itself() -> None:
    """Only corrupted files carry a twin_candidates section; the normal
    file that IS the candidate has none of its own."""
    broken = _twin_outcome("root/a.pptx", Verdict.TAIL_TRUNCATED, 1000)
    twin = _twin_outcome("root/backup/a.pptx", Verdict.NORMAL, 1000)
    result = _twin_scan_result([broken, twin])

    payload = json.loads(render_scan_json(result))

    normal_entry = next(
        f for f in payload["files"] if f["path"] == "root/backup/a.pptx")
    assert "twin_candidates" not in normal_entry


def test_scan_text_collapses_extra_candidates_into_a_count_line() -> None:
    """More than 3 candidates: only 3 are listed individually, the rest
    collapse into a single "+n more" line."""
    broken = _twin_outcome("root/a.pptx", Verdict.TAIL_TRUNCATED, 1000)
    twins = [
        _twin_outcome(f"root/backup{i}/a.pptx", Verdict.NORMAL, 1000)
        for i in range(5)
    ]
    result = _twin_scan_result([broken, *twins])

    text = render_scan_text(result, _TR, include_files=True)

    assert text.count("restore candidate:") == 3
    assert "(+2 more restore candidates)" in text


def test_batch_json_schema_version_is_3_and_lists_twin_candidates() -> None:
    """render_batch_json's schema_version is 3 (see the v1.3 merge-report
    tests below) and it attaches twin_candidates to an unrepairable item
    with a matching twin."""
    broken = _twin_outcome("root/a.pptx", Verdict.TAIL_TRUNCATED, 1000)
    twin = _twin_outcome("root/backup/a.pptx", Verdict.NORMAL, 1000)
    scan = _twin_scan_result([broken, twin])
    items = [BatchItem(source=broken, planned_output=None,
                       action="unrepairable")]
    result = BatchResult(scan=scan, items=items, output_dir=Path("out"),
                         in_place=False, dry_run=False)

    payload = json.loads(render_batch_json(result))

    assert payload["schema_version"] == 3
    repair_entry = payload["repairs"][0]
    assert repair_entry["twin_candidates"] == [
        {"path": "root/backup/a.pptx", "confidence": "high", "size": 1000},
    ]


def test_batch_text_lists_restore_candidate_for_unrepairable_item() -> None:
    """include_files=True's per-item Repairs: section shows the restore
    candidate line right after an unrepairable item's own line."""
    broken = _twin_outcome("root/a.pptx", Verdict.TAIL_TRUNCATED, 1000)
    twin = _twin_outcome("root/backup/a.pptx", Verdict.NORMAL, 1000)
    scan = _twin_scan_result([broken, twin])
    items = [BatchItem(source=broken, planned_output=None,
                       action="unrepairable")]
    result = BatchResult(scan=scan, items=items, output_dir=Path("out"),
                         in_place=False, dry_run=False)

    text = render_batch_text(result, _TR, include_files=True)

    assert (
        "restore candidate: root/backup/a.pptx (same name and size)" in text
    )


# --- v1.3: merge_groups / lineage_candidates in scan / repair-all reports --


def _new_slide_xml() -> bytes:
    """Return a slide1 body distinctly different (and larger) than the
    fixture default, used to build a genuinely different-sized lineage
    version of a synthetic .pptx."""
    return (
        b"<p:sld><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/><p:sp>"
        b"<p:txBody><a:p><a:r><a:t>Edited slide body for the lineage "
        b"version, padded so the archive size clearly differs.</a:t>"
        b"</a:r></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )


def test_scan_report_merge_groups_groups_same_size_corrupted_files(
    tmp_path: Path,
) -> None:
    """Two corrupted files sharing an exact byte size are grouped as a
    merge candidate, in text and JSON; a differently sized corrupted
    file is excluded."""
    base = fixtures.build_minimal_pptx(num_slides=3, media_bytes=100_000)
    copy_a, copy_b = fixtures.make_corrupted_copies(base, [
        [("foreign_prefix", 4096)],
        [("foreign_prefix", 8192)],
    ])
    other_size = fixtures.build_minimal_pptx(num_slides=5, media_bytes=250_000)
    (copy_c,) = fixtures.make_corrupted_copies(
        other_size, [[("foreign_prefix", 4096)]])
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.pptx").write_bytes(copy_a)
    (root / "b.pptx").write_bytes(copy_b)
    (root / "c.pptx").write_bytes(copy_c)

    result = scan_paths([root])

    text = render_scan_text(result, _TR, include_files=True)
    payload = json.loads(render_scan_json(result))

    assert payload["merge_groups"] == [
        {"size": len(copy_a),
         "files": [str(root / "a.pptx"), str(root / "b.pptx")]},
    ]
    assert "Merge candidates:" in text
    assert str(root / "a.pptx") in text
    assert str(root / "b.pptx") in text
    assert str(root / "c.pptx") not in text.split("Merge candidates:")[1]
    assert f'pptrepair merge "{root / "a.pptx"}" "{root / "b.pptx"}"' in text


def test_scan_report_lineage_candidates_lists_related_version(
    tmp_path: Path,
) -> None:
    """A corrupted file's lineage-tier version is listed as a lineage
    candidate, in text and JSON; an unrelated normal file is not."""
    original = fixtures.build_minimal_pptx(num_slides=3, media_bytes=60_000)
    version = fixtures.make_edited_version(
        original, replace={"ppt/slides/slide1.xml": _new_slide_xml()})
    assert len(version) != len(original)
    (corrupted_target,) = fixtures.make_corrupted_copies(
        original, [[("foreign_prefix", 4096)]])
    unrelated = fixtures.build_minimal_pptx(
        num_slides=6, media_bytes=400_000, seed=99)
    root = tmp_path / "root"
    root.mkdir()
    broken_path = root / "broken.pptx"
    version_path = root / "version.pptx"
    unrelated_path = root / "unrelated.pptx"
    broken_path.write_bytes(corrupted_target)
    version_path.write_bytes(version)
    unrelated_path.write_bytes(unrelated)

    result = scan_paths([root])

    text = render_scan_text(result, _TR, include_files=True)
    payload = json.loads(render_scan_json(result))

    assert f"lineage candidate: {version_path}" in text
    assert f"lineage candidate: {unrelated_path}" not in text
    broken_entry = next(
        f for f in payload["files"] if f["path"] == str(broken_path))
    lineage_paths = {
        c["path"] for c in broken_entry["lineage_candidates"]}
    assert str(version_path) in lineage_paths
    assert str(unrelated_path) not in lineage_paths
    version_entry = next(
        f for f in payload["files"] if f["path"] == str(version_path))
    assert "lineage_candidates" not in version_entry


def test_batch_report_propagates_merge_groups_and_lineage_candidates(
    tmp_path: Path,
) -> None:
    """render_batch_text/json surface the same merge-candidate groups and
    lineage candidates as the scan report, and schema_version is 3."""
    base = fixtures.build_minimal_pptx(num_slides=3, media_bytes=100_000)
    copy_a, copy_b = fixtures.make_corrupted_copies(base, [
        [("foreign_prefix", 4096)],
        [("foreign_prefix", 8192)],
    ])
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.pptx").write_bytes(copy_a)
    (root / "b.pptx").write_bytes(copy_b)
    scan_result = scan_paths([root])
    batch_result = BatchResult(
        scan=scan_result, items=[], output_dir=root, in_place=False,
        dry_run=True)

    text = render_batch_text(batch_result, _TR, include_files=True)
    payload = json.loads(render_batch_json(batch_result))

    assert payload["schema_version"] == 3
    assert payload["scan"]["merge_groups"] == [
        {"size": len(copy_a),
         "files": [str(root / "a.pptx"), str(root / "b.pptx")]},
    ]
    assert "Merge candidates:" in text
