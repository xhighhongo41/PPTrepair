"""Tests for :mod:`pptrepair.twin`.

All fixtures are built directly from the dataclasses defined in
:mod:`pptrepair.scan` / :mod:`pptrepair.classify` / :mod:`pptrepair.scanner`;
no file I/O and no scan/census/classify pipeline calls are involved.
"""

from __future__ import annotations

from pathlib import Path

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.scan import FileOutcome
from pptrepair.scanner import ZipStructure
from pptrepair.twin import TwinCandidate, build_twin_index, find_twin_candidates


def _structure(size: int) -> ZipStructure:
    """Build a minimal :class:`ZipStructure` carrying only *size*."""
    return ZipStructure(size=size, head_kind="zip", zero_runs=[],
                        lfh_offsets=[], cd_sig_count=0, eocd=None)


def _outcome(path: str, verdict: Verdict = Verdict.NORMAL,
             size: int | None = 1000, has_diagnosis: bool = True,
             has_structure: bool = True) -> FileOutcome:
    """Build a :class:`FileOutcome` for one already-diagnosed file.

    *has_diagnosis* / *has_structure* let a test opt out of the
    ``diagnosis`` / ``diagnosis.structure`` field respectively, to
    exercise the skip-safely contract of :func:`build_twin_index`.
    """
    if not has_diagnosis:
        return FileOutcome(path=Path(path), diagnosis=None)
    structure = _structure(size) if has_structure and size is not None else None
    diagnosis = Diagnosis(path=Path(path), verdict=verdict, structure=structure)
    return FileOutcome(path=Path(path), diagnosis=diagnosis)


# --- confidence tiers ------------------------------------------------------


def test_confidence_tiers_high_medium_low() -> None:
    """Same name+size is high, same size only is medium, same name only
    (different size) is low."""
    outcomes = [
        _outcome("/a/report.pptx", size=1000),  # high: name + size match
        _outcome("/b/report.pptx", size=2000),  # low: name only
        _outcome("/c/other.pptx", size=1000),   # medium: size only
    ]
    index = build_twin_index(outcomes)

    candidates = find_twin_candidates(
        Path("/query/report.pptx"), 1000, index)

    by_path = {str(candidate.path): candidate for candidate in candidates}
    assert by_path["/a/report.pptx"].confidence == "high"
    assert by_path["/a/report.pptx"].size == 1000
    assert by_path["/c/other.pptx"].confidence == "medium"
    assert by_path["/c/other.pptx"].size == 1000
    assert by_path["/b/report.pptx"].confidence == "low"
    assert by_path["/b/report.pptx"].size == 2000


# --- self exclusion ---------------------------------------------------------


def test_search_target_itself_is_excluded() -> None:
    """The queried path is never returned, even if it is itself indexed
    as a NORMAL match (same name and size)."""
    query = Path("/root/report.pptx")
    outcomes = [_outcome(str(query), size=1000)]
    index = build_twin_index(outcomes)

    candidates = find_twin_candidates(query, 1000, index)

    assert candidates == []


# --- verdict filtering -------------------------------------------------------


def test_non_normal_verdicts_are_not_indexed() -> None:
    """A file diagnosed as anything but NORMAL is not a twin candidate,
    even when name and size would otherwise match exactly."""
    outcomes = [
        _outcome("/a/report.pptx", verdict=Verdict.TAIL_TRUNCATED, size=1000),
    ]
    index = build_twin_index(outcomes)

    candidates = find_twin_candidates(
        Path("/query/report.pptx"), 1000, index)

    assert candidates == []


# --- skip-safely contract ----------------------------------------------------


def test_outcomes_without_diagnosis_or_structure_are_skipped() -> None:
    """Outcomes with diagnosis=None, or a diagnosis lacking a
    structure, are skipped without raising."""
    outcomes = [
        _outcome("/a/no_diagnosis.pptx", has_diagnosis=False),
        _outcome("/b/no_structure.pptx", has_structure=False),
        _outcome("/c/report.pptx", size=1000),  # the only valid entry
    ]

    index = build_twin_index(outcomes)
    candidates = find_twin_candidates(
        Path("/query/report.pptx"), 1000, index)

    assert [str(candidate.path) for candidate in candidates] == ["/c/report.pptx"]


# --- limit and ordering -------------------------------------------------------


def test_limit_and_ordering_across_tiers() -> None:
    """Results are ordered high -> medium -> low (ties by str(path)),
    and truncated to *limit*."""
    outcomes = [
        _outcome("/z/report.pptx", size=1000),  # high
        _outcome("/y/report.pptx", size=1000),  # high
        _outcome("/x/other.pptx", size=1000),   # medium
        _outcome("/w/report.pptx", size=2000),  # low
    ]
    index = build_twin_index(outcomes)

    candidates = find_twin_candidates(
        Path("/query/report.pptx"), 1000, index, limit=3)

    assert len(candidates) == 3
    assert [candidate.confidence for candidate in candidates] == [
        "high", "high", "medium"]
    # Ties within the same tier are broken by str(path) ascending.
    assert [str(candidate.path) for candidate in candidates[:2]] == [
        "/y/report.pptx", "/z/report.pptx"]


def test_each_candidate_returned_only_once_at_its_best_tier() -> None:
    """A file matching both by name and by size (i.e. the high tier) is
    not additionally reported at a weaker tier."""
    outcomes = [_outcome("/a/report.pptx", size=1000)]
    index = build_twin_index(outcomes)

    candidates = find_twin_candidates(
        Path("/query/report.pptx"), 1000, index)

    assert len(candidates) == 1
    assert candidates[0] == TwinCandidate(
        path=Path("/a/report.pptx"), confidence="high", size=1000)


# --- unknown size ------------------------------------------------------------


def test_unknown_size_yields_only_low_name_matches() -> None:
    """When the queried file's size is unknown (None), only name-based
    "low" matches are returned -- size-based tiers do not apply."""
    outcomes = [
        _outcome("/a/report.pptx", size=1000),
        _outcome("/b/report.pptx", size=2000),
        _outcome("/c/other.pptx", size=1000),  # would be medium if size known
    ]
    index = build_twin_index(outcomes)

    candidates = find_twin_candidates(Path("/query/report.pptx"), None, index)

    assert {str(candidate.path) for candidate in candidates} == {
        "/a/report.pptx", "/b/report.pptx"}
    assert all(candidate.confidence == "low" for candidate in candidates)
