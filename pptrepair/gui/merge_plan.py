"""Donor-planning logic for the desktop app's multi-source repair (Qt-free).

Multi-source repair reconstructs a corrupted ``.pptx`` by byte-splicing
it against other same-origin copies and lineage donors (see
:mod:`pptrepair.merge`). Before any of that runs, the UI has to decide,
for every corrupted file the last scan found, *which* other files could
serve as donors and how much each is trusted -- exactly the selection
:func:`pptrepair.merge.merge_restore` performs internally, surfaced here
so the user can review and approve it first.

This module is deliberately free of any Qt dependency: it turns a
:class:`~pptrepair.gui.worker.GuiScanResult` into plain
:class:`TargetPlan` records (:func:`build_target_plans`), which the
:class:`~pptrepair.gui.donor_dialog.DonorApprovalDialog` renders and the
:class:`~pptrepair.gui.repair_workers.MultiRepairWorker` executes.
Diagnosis is never repeated here -- every score is computed from the
:class:`~pptrepair.classify.Diagnosis` the scan already produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pptrepair.classify import Verdict
from pptrepair.origin import score_origin
from pptrepair.scan import ArchiveMaterial

if TYPE_CHECKING:  # avoid a runtime import cycle with worker.py
    from pptrepair.gui.worker import GuiScanResult

#: Same-origin tiers a donor may carry into the merge, matching the tiers
#: :func:`pptrepair.merge.merge_restore` (via
#: :func:`pptrepair.origin.score_origin`) will itself accept: ``"auto"``
#: unconditionally, ``"candidate"``/``"lineage"`` only on explicit
#: approval. The ``"rejected"`` tier (a different origin) is never usable.
_USABLE_TIERS = ("auto", "candidate", "lineage")

#: Sort key per tier, so a target's donors list strongest first
#: (auto -> candidate -> lineage).
_TIER_ORDER = {"auto": 0, "candidate": 1, "lineage": 2}


@dataclass(frozen=True)
class DonorRef:
    """One donor a corrupted target could be reconstructed from.

    Exactly one of :attr:`path` / :attr:`material` is set: a plain file
    already on disk, or a member still inside a backup archive that has
    to be materialized before use.

    :ivar display: the donor's user-facing name (an
        ``"<archive>::<member>"`` label for archive material, the plain
        path otherwise); never a temporary extraction path.
    :ivar tier: the :func:`pptrepair.origin.score_origin` tier
        (``"auto"`` / ``"candidate"`` / ``"lineage"``).
    :ivar path: the on-disk donor file, or None for archive material.
    :ivar material: the archive donor, or None for an on-disk file.
    """

    display: str
    tier: str
    path: Path | None
    material: ArchiveMaterial | None


@dataclass(frozen=True)
class TargetPlan:
    """One corrupted target together with every usable donor for it.

    :ivar target: the corrupted file to reconstruct.
    :ivar donors: the usable donors, ordered strongest tier first
        (auto -> candidate -> lineage); empty when no other source shares
        this target's origin (the file then falls back to single-file
        repair).
    """

    target: Path
    donors: tuple[DonorRef, ...]


@dataclass(frozen=True)
class ApprovedMerge:
    """One target's user-approved donor selection.

    Produced by :meth:`~pptrepair.gui.donor_dialog.DonorApprovalDialog.approved_plans`
    and consumed by :class:`~pptrepair.gui.repair_workers.MultiRepairWorker`.

    :ivar target: the corrupted file to reconstruct.
    :ivar donors: the donors the user checked (may be empty, meaning the
        target falls back to single-file repair).
    :ivar allow_candidate: True when at least one checked donor is
        ``candidate`` tier, forwarded as
        :func:`pptrepair.merge.merge_restore`'s ``allow_candidate``.
    :ivar allow_lineage: True when at least one checked donor is
        ``lineage`` tier, forwarded as ``allow_lineage``.
    """

    target: Path
    donors: tuple[DonorRef, ...]
    allow_candidate: bool
    allow_lineage: bool


def _usable_donor(target_diagnosis, donor_diagnosis, display: str,
                  path: Path | None,
                  material: ArchiveMaterial | None) -> DonorRef | None:
    """Score one candidate donor and wrap it when its tier is usable.

    Returns a :class:`DonorRef` when
    :func:`pptrepair.origin.score_origin` places the pair in one of
    :data:`_USABLE_TIERS`, or None (a ``rejected``-tier or otherwise
    unusable candidate). No filesystem access happens here; both
    diagnoses come from the scan.
    """
    score = score_origin(target_diagnosis, donor_diagnosis)
    if score.tier not in _USABLE_TIERS:
        return None
    return DonorRef(display=display, tier=score.tier, path=path,
                    material=material)


def build_target_plans(result: GuiScanResult) -> list[TargetPlan]:
    """Build one :class:`TargetPlan` per corrupted file in *result*.

    Every on-disk outcome the scan diagnosed as corrupted (a non-NORMAL
    verdict) becomes a target. Its donor pool is every *other* diagnosed
    on-disk outcome -- intact or corrupted alike -- plus every diagnosed
    archive material, each scored against the target through
    :func:`pptrepair.origin.score_origin`; only ``auto`` / ``candidate``
    / ``lineage`` tiers survive as donors, ordered strongest first. A
    target with no usable donor is still returned (with an empty
    ``donors`` tuple), so the caller can route it to single-file repair.

    Targets appear in scan (walk) order; the returned list is empty when
    the scan found nothing corrupted or *result* carries no on-disk scan.

    :param result: the last scan's aggregate outcome.
    """
    outcomes = result.scan.outcomes if result.scan is not None else []
    plans: list[TargetPlan] = []
    for target in outcomes:
        target_diag = target.diagnosis
        if target_diag is None or target_diag.verdict == Verdict.NORMAL:
            continue  # only corrupted, successfully diagnosed files are targets

        donors: list[DonorRef] = []
        for other in outcomes:
            if other is target or other.diagnosis is None:
                continue
            donor = _usable_donor(target_diag, other.diagnosis,
                                  str(other.path), other.path, None)
            if donor is not None:
                donors.append(donor)
        for material in result.materials:
            if material.diagnosis is None:
                continue
            donor = _usable_donor(target_diag, material.diagnosis,
                                  material.display(), None, material)
            if donor is not None:
                donors.append(donor)

        # Stable sort by tier keeps encounter order within each tier.
        donors.sort(key=lambda ref: _TIER_ORDER[ref.tier])
        plans.append(TargetPlan(target=target.path, donors=tuple(donors)))
    return plans
