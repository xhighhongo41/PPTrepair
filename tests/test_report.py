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
