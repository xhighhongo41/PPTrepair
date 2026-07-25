"""Twin-/lineage-restoration and merge-candidate helpers for reports.

Split out of :mod:`pptrepair.report` to keep that facade's own module
small; see :mod:`pptrepair.report_scan` and :mod:`pptrepair.report_batch`
for the callers of these helpers.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.origin import OriginScore, score_origin
from pptrepair.report_common import (
    _LINEAGE_CANDIDATES_DISPLAY_LIMIT,
    _MERGE_GROUP_MIN_FILES,
    _TWIN_CANDIDATES_DISPLAY_LIMIT,
)
from pptrepair.twin import TwinCandidate, build_twin_index, find_twin_candidates

if TYPE_CHECKING:  # avoid runtime import cycles with scan
    from pptrepair.scan import ArchiveMaterial, FileOutcome


@dataclass(frozen=True)
class _LineageCandidate:
    """One lineage-version candidate for a corrupted file.

    *display* is the candidate's user-facing name (``str(path)`` for an
    on-disk file, its ``"<archive>::<member>"`` label for an archive
    member); *origin_archive* is the source archive's path for a member,
    None for an on-disk file.
    """

    display: str
    score: OriginScore
    origin_archive: str | None


@dataclass(frozen=True)
class _MergeFile:
    """One member of a same-size merge-candidate group.

    *display* is the user-facing name (``str(path)`` or a ``"::"`` label);
    *command* is a path usable as a ``pptrepair merge`` SRC argument
    (the file's own path, or, for an archive member, the archive path).
    """

    display: str
    command: str


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
    outcomes: Sequence[FileOutcome],
    materials: Sequence[ArchiveMaterial] = (),
) -> dict[Path, list[TwinCandidate]]:
    """Map each corrupted file's path to its twin-restoration candidates.

    Builds one :class:`~pptrepair.twin.TwinIndex` from *outcomes* plus
    the optional archive *materials* (see
    :func:`~pptrepair.twin.build_twin_index`) and queries it, via
    :func:`~pptrepair.twin.find_twin_candidates`, for every outcome
    whose verdict is not :attr:`Verdict.NORMAL` (an outcome with no
    ``diagnosis`` -- a failed pipeline -- is skipped). Only on-disk
    outcomes are queried; archive materials serve solely as donors,
    never as query targets. The queried size is
    ``diagnosis.structure.size``, or None when ``structure`` is None.
    Only paths with at least one candidate are kept in the returned
    mapping.
    """
    index = build_twin_index(outcomes, materials)
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
            path=_twin_display(candidate),
            reason=_twin_reason_label(candidate.confidence, tr))
        for candidate in shown
    ]
    remaining = len(candidates) - len(shown)
    if remaining > 0:
        lines.append(
            tr("  (+{n} more restore candidates)").format(n=remaining))
    return lines


def _twin_display(candidate: TwinCandidate) -> str:
    """Return *candidate*'s user-facing name (label for an archive member)."""
    return (candidate.member_label if candidate.member_label is not None
            else str(candidate.path))


def _twin_candidates_to_json(candidates: list[TwinCandidate]) -> list[dict]:
    """Render *candidates* as the JSON-schema list for a "twin_candidates" key.

    ``origin_archive`` is added only for a candidate materialized from an
    archive (its value the archive's path); an on-disk candidate carries
    no such key, keeping its object identical to the pre-archive schema.
    """
    result: list[dict] = []
    for candidate in candidates:
        entry: dict = {"path": _twin_display(candidate),
                       "confidence": candidate.confidence,
                       "size": candidate.size}
        if candidate.origin_archive is not None:
            entry["origin_archive"] = candidate.origin_archive
        result.append(entry)
    return result


def _lineage_candidates_map(
    outcomes: Sequence[FileOutcome],
    materials: Sequence[ArchiveMaterial] = (),
) -> dict[Path, list[_LineageCandidate]]:
    """Map each corrupted file's path to its lineage-version candidates.

    For every outcome whose verdict is not :attr:`Verdict.NORMAL`, scores
    it (:func:`pptrepair.origin.score_origin`) against every *other*
    diagnosed on-disk outcome (normal or corrupted alike) and against
    every diagnosed archive *material*, using only the already-recorded
    :class:`~pptrepair.classify.Diagnosis` objects (no file is re-read).
    Only ``tier == "lineage"`` results are kept, sorted by descending
    ``lineage_score`` and capped at
    :data:`_LINEAGE_CANDIDATES_DISPLAY_LIMIT`; a path with no such result
    is absent from the returned mapping. Archive materials are donors
    only -- they are never themselves a corrupted query target. The
    default empty *materials* reproduces the pre-archive behaviour.
    """
    # Donors: on-disk outcomes (with a self-path for exclusion) plus
    # archive materials (no self-path; never a query target).
    donors: list[tuple[Path | None, str, Diagnosis, str | None]] = []
    for other in outcomes:
        if other.diagnosis is None:
            continue
        donors.append((other.path, str(other.path), other.diagnosis, None))
    for material in materials:
        if material.diagnosis is None:
            continue
        donors.append((None, material.display(), material.diagnosis,
                       str(material.archive_path)))

    candidates_map: dict[Path, list[_LineageCandidate]] = {}
    for outcome in outcomes:
        diagnosis = outcome.diagnosis
        if diagnosis is None or diagnosis.verdict == Verdict.NORMAL:
            continue
        scored: list[_LineageCandidate] = []
        for self_path, display, donor_diag, origin_archive in donors:
            if self_path is not None and self_path == outcome.path:
                continue
            score = score_origin(diagnosis, donor_diag)
            if score.tier == "lineage":
                scored.append(_LineageCandidate(
                    display=display, score=score,
                    origin_archive=origin_archive))
        if scored:
            scored.sort(key=lambda c: c.score.lineage_score, reverse=True)
            candidates_map[outcome.path] = (
                scored[:_LINEAGE_CANDIDATES_DISPLAY_LIMIT])
    return candidates_map


def _lineage_candidate_text_lines(
    path: Path, lineage_map: dict[Path, list[_LineageCandidate]],
    tr: Callable[[str], str],
) -> list[str]:
    """Render the indented lineage-candidate lines that follow *path*'s own
    line in a text report, one translated ``lineage candidate: <path>
    (score <n.nn>)`` line per candidate (``<path>`` being the
    ``"::"`` label for an archive member). Empty when *path* has no entry
    in *lineage_map*.
    """
    candidates = lineage_map.get(path, [])
    return [
        tr("  lineage candidate: {path} (score {score})").format(
            path=candidate.display,
            score=f"{candidate.score.lineage_score:.2f}")
        for candidate in candidates
    ]


def _lineage_candidates_to_json(
    candidates: list[_LineageCandidate],
) -> list[dict]:
    """Render *candidates* as the JSON-schema list for a "lineage_candidates" key.

    ``origin_archive`` is added only for a candidate materialized from an
    archive, leaving an on-disk candidate's object unchanged from the
    pre-archive schema.
    """
    result: list[dict] = []
    for candidate in candidates:
        entry: dict = {"path": candidate.display,
                       "lineage_score": candidate.score.lineage_score,
                       "media_ratio": candidate.score.media_ratio}
        if candidate.origin_archive is not None:
            entry["origin_archive"] = candidate.origin_archive
        result.append(entry)
    return result


def _merge_group_map(
    outcomes: Sequence[FileOutcome],
    materials: Sequence[ArchiveMaterial] = (),
) -> list[dict]:
    """Group corrupted files sharing an exact byte size into merge candidates.

    Only corrupted (non-:attr:`Verdict.NORMAL`) donors with a known
    ``diagnosis.structure.size`` participate: on-disk *outcomes* and,
    when given, corrupted archive *materials* alike (a material joins a
    group only when it too is damaged, since merge splices same-save
    *corrupted* copies together). A size shared by fewer than
    :data:`_MERGE_GROUP_MIN_FILES` such donors is not a group. Each group
    is ``{"size": int, "files": [_MergeFile, ...]}``, on-disk files in
    scan order followed by any materials; groups themselves are returned
    in ascending size order. The default empty *materials* reproduces
    the pre-archive behaviour.
    """
    by_size: dict[int, list[_MergeFile]] = {}
    for outcome in outcomes:
        diagnosis = outcome.diagnosis
        if diagnosis is None or diagnosis.verdict == Verdict.NORMAL:
            continue
        if diagnosis.structure is None:
            continue
        by_size.setdefault(diagnosis.structure.size, []).append(
            _MergeFile(display=str(outcome.path), command=str(outcome.path)))
    for material in materials:
        diagnosis = material.diagnosis
        if diagnosis is None or diagnosis.verdict == Verdict.NORMAL:
            continue
        if diagnosis.structure is None:
            continue
        # A materialized member is merged from by passing its archive as
        # a SRC, so the command path is the archive's own path.
        by_size.setdefault(diagnosis.structure.size, []).append(
            _MergeFile(display=material.display(),
                       command=str(material.archive_path)))
    return [
        {"size": size, "files": files}
        for size, files in sorted(by_size.items())
        if len(files) >= _MERGE_GROUP_MIN_FILES
    ]


def _merge_group_text_lines(group: dict, tr: Callable[[str], str]) -> list[str]:
    """Render one merge-candidate group's text lines.

    One translated line listing the group's size and files (each shown
    by its display name, a ``"::"`` label for an archive member), one
    translated note pointing at ``pptrepair merge``, and one untranslated
    example command line (machine-facing). The example uses each file's
    SRC-usable *command* path, de-duplicated in order so several members
    of one archive collapse to a single archive argument.
    """
    files = group["files"]
    files_str = ", ".join(merge_file.display for merge_file in files)
    lines = [tr("  {size} bytes: {files}").format(
        size=group["size"], files=files_str)]
    lines.append(tr(
        "    These files may be the same saved version and could be "
        "repaired together into one file with pptrepair merge."))
    commands: list[str] = []
    for merge_file in files:
        if merge_file.command not in commands:
            commands.append(merge_file.command)
    example = " ".join(f'"{command}"' for command in commands)
    lines.append(f"    pptrepair merge {example}")
    return lines


def _merge_groups_to_json(groups: list[dict]) -> list[dict]:
    """Render *groups* (from :func:`_merge_group_map`) as the JSON-schema list.

    ``files`` stays a plain list of display strings (``str(path)`` for an
    on-disk file, a ``"<archive>::<member>"`` label for a member -- the
    label itself being the archive-origin marker), so an all-on-disk
    group's JSON is unchanged from the pre-archive schema.
    """
    return [
        {"size": group["size"],
         "files": [merge_file.display for merge_file in group["files"]]}
        for group in groups
    ]
