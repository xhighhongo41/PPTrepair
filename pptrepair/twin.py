"""Search for intact "twin" files that might restore a corrupted pptx.

OneDrive chunk corruption preserves the original file size (only bytes
inside the file are overwritten in place, the file is never truncated
or extended). Consequently, another file elsewhere in the scanned tree
that shares the corrupted file's name and/or size is a plausible
starting point for manual restoration -- e.g. a stray copy, an older
export, or a sibling file left over from a sync conflict.

This module only builds and queries a lookup index over already
diagnosed files (:mod:`pptrepair.scan`); it performs no filesystem
access of its own (no re-stat, no hashing) and is not wired into the
scan report -- that integration is a separate task.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pptrepair.classify import Verdict
from pptrepair.scan import FileOutcome

#: Relative strength of each confidence tier, smallest is strongest.
#: Used both to keep only a candidate's best match and to order results.
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class TwinCandidate:
    """One candidate intact file that might restore a corrupted pptx."""

    path: Path
    confidence: str  # "high" | "medium" | "low"
    size: int


class TwinIndex:
    """Lookup index of NORMAL-verdict files, keyed by name and by size.

    Built once per scan (:func:`build_twin_index`) and queried per
    corrupted file (:func:`find_twin_candidates`).
    """

    def __init__(self) -> None:
        self._by_name: dict[str, list[Path]] = {}
        self._by_size: dict[int, list[Path]] = {}
        self._sizes: dict[Path, int] = {}

    def add(self, path: Path, size: int) -> None:
        """Register one NORMAL-verdict file under its basename and size."""
        self._by_name.setdefault(path.name, []).append(path)
        self._by_size.setdefault(size, []).append(path)
        self._sizes[path] = size

    def by_name(self, name: str) -> list[Path]:
        """Return every indexed path whose basename equals *name*."""
        return self._by_name.get(name, [])

    def by_size(self, size: int) -> list[Path]:
        """Return every indexed path whose size equals *size* bytes."""
        return self._by_size.get(size, [])

    def size_of(self, path: Path) -> int:
        """Return the indexed size of *path* (must already be added)."""
        return self._sizes[path]


def build_twin_index(outcomes: Iterable[FileOutcome]) -> TwinIndex:
    """Build a :class:`TwinIndex` from *outcomes*.

    Only outcomes whose verdict is :attr:`Verdict.NORMAL` are indexed;
    everything else (corrupted files, pipeline failures) is skipped.
    An outcome is also skipped when it has no ``diagnosis`` or no
    ``diagnosis.structure`` (should not happen for a NORMAL verdict in
    practice, but the pipeline's failure contract allows it). The size
    used is ``outcome.diagnosis.structure.size``, i.e. the size already
    observed while scanning -- no filesystem re-stat is performed here.
    """
    index = TwinIndex()
    for outcome in outcomes:
        diagnosis = outcome.diagnosis
        if diagnosis is None or diagnosis.verdict != Verdict.NORMAL:
            continue
        structure = diagnosis.structure
        if structure is None:
            continue
        index.add(outcome.path, structure.size)
    return index


def find_twin_candidates(path: Path, size: int | None, index: TwinIndex,
                          limit: int = 10) -> list[TwinCandidate]:
    """Search *index* for files that might be an intact twin of *path*.

    Confidence tiers, strongest to weakest:

    * ``"high"``   -- same basename and same size.
    * ``"medium"`` -- same size, different basename.
    * ``"low"``    -- same basename only (size unknown or different).

    *path* itself is never returned, even if it happens to be indexed.
    When *size* is None the size-based tiers are meaningless, so only
    name-based ``"low"`` matches are considered. Each candidate is
    returned once, at its single best (lowest-ranked) confidence.
    Results are ordered high -> medium -> low, ties broken by
    ``str(path)``, and truncated to *limit* entries.
    """
    best: dict[Path, str] = {}

    def _consider(candidate: Path, confidence: str) -> None:
        if candidate == path:
            return
        current = best.get(candidate)
        if current is None or _CONFIDENCE_RANK[confidence] < _CONFIDENCE_RANK[current]:
            best[candidate] = confidence

    for candidate in index.by_name(path.name):
        if size is not None and index.size_of(candidate) == size:
            _consider(candidate, "high")
        else:
            _consider(candidate, "low")

    if size is not None:
        for candidate in index.by_size(size):
            if candidate.name != path.name:
                _consider(candidate, "medium")

    ordered = sorted(
        best.items(), key=lambda item: (_CONFIDENCE_RANK[item[1]], str(item[0])))
    return [
        TwinCandidate(path=candidate, confidence=confidence,
                      size=index.size_of(candidate))
        for candidate, confidence in ordered[:limit]
    ]
