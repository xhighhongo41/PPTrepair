"""Twin-/lineage-restoration and merge-candidate helpers for reports.

Split out of :mod:`pptrepair.report` to keep that facade's own module
small; see :mod:`pptrepair.report_scan` and :mod:`pptrepair.report_batch`
for the callers of these helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Sequence

from pptrepair.classify import Verdict
from pptrepair.origin import OriginScore, score_origin
from pptrepair.report_common import (_LINEAGE_CANDIDATES_DISPLAY_LIMIT,
                                     _MERGE_GROUP_MIN_FILES,
                                     _TWIN_CANDIDATES_DISPLAY_LIMIT)
from pptrepair.twin import TwinCandidate, build_twin_index, find_twin_candidates

if TYPE_CHECKING:  # avoid runtime import cycles with scan
    from pptrepair.scan import FileOutcome


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


def _lineage_candidates_map(
    outcomes: "Sequence[FileOutcome]",
) -> dict[Path, list[tuple[Path, OriginScore]]]:
    """Map each corrupted file's path to its lineage-version candidates.

    For every outcome whose verdict is not :attr:`Verdict.NORMAL`, scores
    it (:func:`pptrepair.origin.score_origin`) against every *other*
    diagnosed outcome, normal or corrupted alike, using only the
    :class:`~pptrepair.classify.Diagnosis` objects *outcomes* already
    carries (no file is re-read). Only ``tier == "lineage"`` results are
    kept, sorted by descending ``lineage_score`` and capped at
    :data:`_LINEAGE_CANDIDATES_DISPLAY_LIMIT`; a path with no such result
    is absent from the returned mapping.
    """
    candidates_map: dict[Path, list[tuple[Path, OriginScore]]] = {}
    for outcome in outcomes:
        diagnosis = outcome.diagnosis
        if diagnosis is None or diagnosis.verdict == Verdict.NORMAL:
            continue
        scored: list[tuple[Path, OriginScore]] = []
        for other in outcomes:
            if other.path == outcome.path or other.diagnosis is None:
                continue
            score = score_origin(diagnosis, other.diagnosis)
            if score.tier == "lineage":
                scored.append((other.path, score))
        if scored:
            scored.sort(key=lambda item: item[1].lineage_score, reverse=True)
            candidates_map[outcome.path] = (
                scored[:_LINEAGE_CANDIDATES_DISPLAY_LIMIT])
    return candidates_map


def _lineage_candidate_text_lines(
    path: Path, lineage_map: dict[Path, list[tuple[Path, OriginScore]]],
    tr: Callable[[str], str],
) -> list[str]:
    """Render the indented lineage-candidate lines that follow *path*'s own
    line in a text report, one translated ``lineage candidate: <path>
    (score <n.nn>)`` line per candidate. Empty when *path* has no entry
    in *lineage_map*.
    """
    candidates = lineage_map.get(path, [])
    return [
        tr("  lineage candidate: {path} (score {score})").format(
            path=candidate_path, score=f"{score.lineage_score:.2f}")
        for candidate_path, score in candidates
    ]


def _lineage_candidates_to_json(
    candidates: list[tuple[Path, OriginScore]],
) -> list[dict]:
    """Render *candidates* as the JSON-schema list for a "lineage_candidates" key."""
    return [
        {"path": str(path), "lineage_score": score.lineage_score,
         "media_ratio": score.media_ratio}
        for path, score in candidates
    ]


def _merge_group_map(outcomes: "Sequence[FileOutcome]") -> list[dict]:
    """Group corrupted files sharing an exact byte size into merge candidates.

    Only outcomes with a non-:attr:`Verdict.NORMAL` verdict and a known
    ``diagnosis.structure.size`` participate; a size shared by fewer than
    :data:`_MERGE_GROUP_MIN_FILES` such files is not a group. Each group
    is ``{"size": int, "files": [Path, ...]}``, files in scan order;
    groups themselves are returned in ascending size order.
    """
    by_size: dict[int, list[Path]] = {}
    for outcome in outcomes:
        diagnosis = outcome.diagnosis
        if diagnosis is None or diagnosis.verdict == Verdict.NORMAL:
            continue
        if diagnosis.structure is None:
            continue
        by_size.setdefault(diagnosis.structure.size, []).append(outcome.path)
    return [
        {"size": size, "files": paths}
        for size, paths in sorted(by_size.items())
        if len(paths) >= _MERGE_GROUP_MIN_FILES
    ]


def _merge_group_text_lines(group: dict, tr: Callable[[str], str]) -> list[str]:
    """Render one merge-candidate group's text lines.

    One translated line listing the group's size and files, one
    translated note pointing at ``pptrepair merge``, and one
    untranslated example command line (machine-facing, like the other
    path-bearing lines in this module).
    """
    files = group["files"]
    files_str = ", ".join(str(path) for path in files)
    lines = [tr("  {size} bytes: {files}").format(
        size=group["size"], files=files_str)]
    lines.append(tr(
        "    These files may be the same saved version and could be "
        "repaired together into one file with pptrepair merge."))
    example = " ".join(f'"{path}"' for path in files)
    lines.append(f"    pptrepair merge {example}")
    return lines


def _merge_groups_to_json(groups: list[dict]) -> list[dict]:
    """Render *groups* (from :func:`_merge_group_map`) as the JSON-schema list."""
    return [
        {"size": group["size"], "files": [str(path) for path in group["files"]]}
        for group in groups
    ]
