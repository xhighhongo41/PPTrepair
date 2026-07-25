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

import posixpath
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pptrepair.classify import Verdict
from pptrepair.scan import FileOutcome

if TYPE_CHECKING:  # only needed for the optional archive-material argument
    from pptrepair.scan import ArchiveMaterial

#: Relative strength of each confidence tier, smallest is strongest.
#: Used both to keep only a candidate's best match and to order results.
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


@dataclass(frozen=True)
class TwinCandidate:
    """One candidate intact file that might restore a corrupted pptx.

    ``origin_archive`` / ``member_label`` are both None for an ordinary
    on-disk twin and both set for one materialized from inside a backup
    archive: ``member_label`` is its ``"<archive>::<member>"`` display
    (used everywhere in place of ``path``, which then holds only a
    synthetic identity) and ``origin_archive`` is the archive's own path.
    """

    path: Path
    confidence: str  # "high" | "medium" | "low"
    size: int
    origin_archive: str | None = None
    member_label: str | None = None


@dataclass(frozen=True)
class _IndexEntry:
    """One indexed NORMAL-verdict donor: an on-disk file or archive member.

    *name* is the basename used for name-tier matching; *path* is a real
    file path for an on-disk donor or a synthetic identity for a member
    (its label as a path), used only for self-exclusion and dedup. The
    optional archive fields carry through to :class:`TwinCandidate`.
    """

    path: Path
    name: str
    size: int
    origin_archive: str | None = None
    member_label: str | None = None

    def sort_key(self) -> str:
        """Return the deterministic tie-break key for ordering results."""
        return self.member_label if self.member_label is not None \
            else str(self.path)


class TwinIndex:
    """Lookup index of NORMAL-verdict donors, keyed by name and by size.

    Built once per scan (:func:`build_twin_index`) and queried per
    corrupted file (:func:`find_twin_candidates`). Donors are either
    on-disk files (:meth:`add`) or members materialized from a backup
    archive (:meth:`add_material`).
    """

    def __init__(self) -> None:
        self._by_name: dict[str, list[_IndexEntry]] = {}
        self._by_size: dict[int, list[_IndexEntry]] = {}

    def _register(self, entry: _IndexEntry) -> None:
        """Index *entry* under both its basename and its size."""
        self._by_name.setdefault(entry.name, []).append(entry)
        self._by_size.setdefault(entry.size, []).append(entry)

    def add(self, path: Path, size: int) -> None:
        """Register one NORMAL-verdict on-disk file by basename and size."""
        self._register(_IndexEntry(path=path, name=path.name, size=size))

    def add_material(self, name: str, size: int, label: str,
                     origin_archive: str) -> None:
        """Register one NORMAL-verdict archive member.

        *name* is the member's basename (for name-tier matching), *label*
        its ``"<archive>::<member>"`` display, and *origin_archive* the
        archive file's own path. The synthetic identity ``Path(label)``
        can never equal an on-disk corrupted path, so a member is never
        mistaken for the query file itself.
        """
        self._register(_IndexEntry(
            path=Path(label), name=name, size=size,
            origin_archive=origin_archive, member_label=label))

    def entries_by_name(self, name: str) -> list[_IndexEntry]:
        """Return every entry whose basename equals *name*."""
        return self._by_name.get(name, [])

    def entries_by_size(self, size: int) -> list[_IndexEntry]:
        """Return every entry whose size equals *size* bytes."""
        return self._by_size.get(size, [])


def build_twin_index(
    outcomes: Iterable[FileOutcome],
    materials: Sequence[ArchiveMaterial] = (),
) -> TwinIndex:
    """Build a :class:`TwinIndex` from *outcomes* and optional *materials*.

    Only donors whose verdict is :attr:`Verdict.NORMAL` are indexed;
    everything else (corrupted files, pipeline failures) is skipped. A
    donor is also skipped when it has no ``diagnosis`` or no
    ``diagnosis.structure`` (should not happen for a NORMAL verdict in
    practice, but the pipeline's failure contract allows it). The size
    used is the size already observed while scanning -- no filesystem
    re-stat is performed here.

    *materials* (archive members mined only under ``--search-archives``)
    are indexed the same way but keyed by their in-archive basename and
    carry their ``"<archive>::<member>"`` label and origin archive
    through to any resulting :class:`TwinCandidate`. The default empty
    *materials* leaves this function's behaviour identical to before the
    argument existed.
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
    for material in materials:
        diagnosis = material.diagnosis
        if diagnosis is None or diagnosis.verdict != Verdict.NORMAL:
            continue
        structure = diagnosis.structure
        if structure is None:
            continue
        index.add_material(
            name=posixpath.basename(material.member.member_name),
            size=structure.size, label=material.display(),
            origin_archive=str(material.archive_path))
    return index


def find_twin_candidates(path: Path, size: int | None, index: TwinIndex,
                          limit: int = 10) -> list[TwinCandidate]:
    """Search *index* for donors that might be an intact twin of *path*.

    Confidence tiers, strongest to weakest:

    * ``"high"``   -- same basename and same size.
    * ``"medium"`` -- same size, different basename.
    * ``"low"``    -- same basename only (size unknown or different).

    *path* itself is never returned, even if it happens to be indexed.
    When *size* is None the size-based tiers are meaningless, so only
    name-based ``"low"`` matches are considered. Each donor is returned
    once, at its single best (lowest-ranked) confidence. Results are
    ordered high -> medium -> low, ties broken by the donor's display
    (``str(path)`` for a file, its ``"::"`` label for a member), and
    truncated to *limit* entries.
    """
    best: dict[_IndexEntry, str] = {}

    def _consider(entry: _IndexEntry, confidence: str) -> None:
        if entry.path == path:
            return
        current = best.get(entry)
        if current is None or _CONFIDENCE_RANK[confidence] < _CONFIDENCE_RANK[current]:
            best[entry] = confidence

    for entry in index.entries_by_name(path.name):
        if size is not None and entry.size == size:
            _consider(entry, "high")
        else:
            _consider(entry, "low")

    if size is not None:
        for entry in index.entries_by_size(size):
            if entry.name != path.name:
                _consider(entry, "medium")

    ordered = sorted(
        best.items(),
        key=lambda item: (_CONFIDENCE_RANK[item[1]], item[0].sort_key()))
    return [
        TwinCandidate(path=entry.path, confidence=confidence, size=entry.size,
                      origin_archive=entry.origin_archive,
                      member_label=entry.member_label)
        for entry, confidence in ordered[:limit]
    ]
