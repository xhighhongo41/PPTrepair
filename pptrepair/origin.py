"""Same-origin scoring between two already-diagnosed .pptx files.

Given two files that have already been through the
scan -> census -> classify pipeline, this module decides whether one
plausibly shares an origin with the other -- e.g. a stray copy, an
older export, or a OneDrive sync-conflict twin -- purely from the
facts recorded in their :class:`~pptrepair.classify.Diagnosis`. No
filesystem access is performed here: every input is already diagnosed.

Two complementary signals feed the decision:

* the recorded central-directory / local-file-header metadata (entry
  name, CRC-32, compressed size) survives corruption even when an
  entry's *data* does not, since it describes the archive *as
  indexed*, not the bytes actually readable back -- comparing these
  recorded triples across two files is therefore a reliable
  fingerprint of shared content regardless of damage on either side;
* exact file size: the OneDrive corruption patterns this project
  targets overwrite chunks in place and so preserve the original byte
  length, giving a fast, strong signal when both files are
  byte-for-byte the same length.

When sizes differ (a different save/version rather than a copy), a
weighted "lineage score" over media/whole-archive matches is used
instead, since media parts change least across ordinary edits of the
same presentation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pptrepair.census import CensusResult, EntryResult
from pptrepair.classify import Diagnosis

#: Minimum whole-archive triple-set match ratio for an automatic
#: same-origin call when both files' sizes match exactly.
AUTO_TRIPLE_RATIO = 0.95

#: Minimum whole-archive name-set match ratio for a size-matched
#: candidate call that falls short of :data:`AUTO_TRIPLE_RATIO`.
CANDIDATE_NAME_RATIO = 0.70

#: Minimum weighted lineage score for a same-origin call when the two
#: files' sizes differ (a different version rather than an exact copy).
LINEAGE_SCORE_THRESHOLD = 0.80

#: Weight of the media triple ratio in the lineage score.
W_MEDIA = 0.5
#: Weight of the whole-archive triple ratio in the lineage score.
W_TRIPLE = 0.3
#: Weight of the whole-archive name ratio in the lineage score.
W_NAME = 0.2

#: Prefix identifying media parts, which change least across ordinary
#: edits of the same presentation and so drive most of the lineage
#: score.
MEDIA_PREFIX = "ppt/media/"


@dataclass(frozen=True)
class OriginScore:
    """Outcome of comparing two diagnosed files for a shared origin."""

    size_match: bool
    cd_pair: bool
    triple_ratio: float
    name_ratio: float
    media_ratio: float
    lineage_score: float
    tier: str  # "auto" | "candidate" | "lineage" | "rejected"
    evidence: list[str] = field(default_factory=list)


def _file_size(diag: Diagnosis) -> int | None:
    """Return *diag*'s file size, or None when the structure is unknown."""
    if diag.structure is None:
        return None
    return diag.structure.size


def _select_census(diag: Diagnosis) -> tuple[CensusResult | None, str]:
    """Pick the census to compare *diag* by: central directory first,
    falling back to the local-file-header scan.

    Returns ``(census, label)``, where *label* is a short
    human-readable description of the source used, suitable for
    evidence (e.g. ``"central directory (12 entries)"``). *census* is
    None when neither source is available.
    """
    cd_census = diag.cd_census
    if cd_census is not None and cd_census.method == "central_directory":
        return cd_census, f"central directory ({cd_census.total()} entries)"
    lfh_census = diag.lfh_census
    if lfh_census is not None:
        return lfh_census, f"LFH scan ({lfh_census.total()} entries)"
    return None, "no census available"


def _triples(entries: list[EntryResult]) -> set[tuple[str, int, int]]:
    """Return the recorded ``{(name, crc, comp_size)}`` set of *entries*.

    Entries whose recorded crc or comp_size is unknown (None) are
    excluded, since they cannot participate in a triple match. An
    entry's ``ok`` flag is deliberately ignored: the recorded values
    describe the archive as indexed, not whether the entry's data is
    still intact.
    """
    return {
        (entry.name, entry.crc, entry.comp_size) for entry in entries
        if entry.crc is not None and entry.comp_size is not None
    }


def _names(entries: list[EntryResult]) -> set[str]:
    """Return the set of entry names, regardless of recorded crc/size."""
    return {entry.name for entry in entries}


def _ratio(intersection_size: int, size_a: int, size_b: int) -> float:
    """Return ``intersection_size / max(size_a, size_b)``.

    Returns 0.0 when both sides are empty, matching the convention
    that an empty comparison never counts as a match.
    """
    denom = max(size_a, size_b)
    if denom == 0:
        return 0.0
    return intersection_size / denom


def _size_evidence(size_a: int | None, size_b: int | None,
                   size_match: bool) -> str:
    """Format the size-comparison evidence line."""
    if size_a is None or size_b is None:
        return "size: unavailable on at least one side"
    verb = "match" if size_match else "differ"
    return f"size: A={size_a} bytes, B={size_b} bytes ({verb})"


def score_origin(diag_a: Diagnosis, diag_b: Diagnosis) -> OriginScore:
    """Score two diagnosed files for a shared origin.

    Compares recorded per-entry metadata (name / CRC-32 / compressed
    size, taken from whichever census each file has -- central
    directory preferred, local-file-header scan as fallback) and,
    when available, exact file size:

    * ``size_match`` and a very high whole-archive triple-set match
      ratio (:data:`AUTO_TRIPLE_RATIO`) call it an automatic same-origin
      copy (``tier="auto"``);
    * ``size_match`` with a lower triple ratio but a still-high
      name-set match ratio (:data:`CANDIDATE_NAME_RATIO`) is a weaker
      ``"candidate"`` call;
    * when sizes differ, a weighted "lineage score" over the media and
      whole-archive ratios (:data:`LINEAGE_SCORE_THRESHOLD`) recognises
      a different version of the same presentation (``"lineage"``);
    * anything else is ``"rejected"``.

    Never raises: an empty or missing census on either side degrades
    gracefully to a "rejected" tier rather than an exception.
    """
    evidence: list[str] = []

    size_a = _file_size(diag_a)
    size_b = _file_size(diag_b)
    size_match = (
        size_a is not None and size_b is not None and size_a == size_b
    )
    evidence.append(_size_evidence(size_a, size_b, size_match))

    census_a, label_a = _select_census(diag_a)
    census_b, label_b = _select_census(diag_b)
    cd_pair = (
        census_a is not None and census_a.method == "central_directory"
        and census_b is not None and census_b.method == "central_directory"
    )
    evidence.append(f"A: {label_a}, B: {label_b}")

    entries_a = census_a.entries if census_a is not None else []
    entries_b = census_b.entries if census_b is not None else []
    if not entries_a or not entries_b:
        evidence.append("no comparable entries on at least one side")
        return OriginScore(
            size_match=size_match, cd_pair=cd_pair, triple_ratio=0.0,
            name_ratio=0.0, media_ratio=0.0, lineage_score=0.0,
            tier="rejected", evidence=evidence,
        )

    triples_a, triples_b = _triples(entries_a), _triples(entries_b)
    names_a, names_b = _names(entries_a), _names(entries_b)
    triple_ratio = _ratio(
        len(triples_a & triples_b), len(triples_a), len(triples_b))
    name_ratio = _ratio(
        len(names_a & names_b), len(names_a), len(names_b))
    evidence.append(
        f"triple_ratio={triple_ratio:.3f} "
        f"({len(triples_a & triples_b)}/{max(len(triples_a), len(triples_b))})")
    evidence.append(
        f"name_ratio={name_ratio:.3f} "
        f"({len(names_a & names_b)}/{max(len(names_a), len(names_b))})")

    media_names_a = {n for n in names_a if n.startswith(MEDIA_PREFIX)}
    media_names_b = {n for n in names_b if n.startswith(MEDIA_PREFIX)}
    media_triples_a = {t for t in triples_a if t[0].startswith(MEDIA_PREFIX)}
    media_triples_b = {t for t in triples_b if t[0].startswith(MEDIA_PREFIX)}
    media_ratio = _ratio(
        len(media_triples_a & media_triples_b),
        len(media_names_a), len(media_names_b))
    evidence.append(
        f"media_ratio={media_ratio:.3f} "
        f"({len(media_triples_a & media_triples_b)}/"
        f"{max(len(media_names_a), len(media_names_b))})")

    if media_names_a and media_names_b:
        lineage_score = (
            W_MEDIA * media_ratio + W_TRIPLE * triple_ratio
            + W_NAME * name_ratio)
    else:
        # No media entries to compare on at least one side; fall back to
        # a whole-archive-only blend.
        lineage_score = 0.6 * triple_ratio + 0.4 * name_ratio
        evidence.append(
            "no media entries on at least one side: lineage score uses "
            "triple_ratio/name_ratio only (weights 0.6/0.4)")
    evidence.append(f"lineage_score={lineage_score:.3f}")

    tier = _decide_tier(size_match, triple_ratio, name_ratio, lineage_score,
                        evidence)
    return OriginScore(
        size_match=size_match, cd_pair=cd_pair, triple_ratio=triple_ratio,
        name_ratio=name_ratio, media_ratio=media_ratio,
        lineage_score=lineage_score, tier=tier, evidence=evidence,
    )


def _decide_tier(size_match: bool, triple_ratio: float, name_ratio: float,
                 lineage_score: float, evidence: list[str]) -> str:
    """Apply the tier decision procedure, appending its reasoning.

    Rules are checked in order; the first match wins. See
    :func:`score_origin` for the tier semantics.
    """
    if size_match and triple_ratio >= AUTO_TRIPLE_RATIO:
        evidence.append(
            f"tier=auto: size matches and triple_ratio >= "
            f"{AUTO_TRIPLE_RATIO}")
        return "auto"
    if size_match and name_ratio >= CANDIDATE_NAME_RATIO:
        evidence.append(
            f"tier=candidate: size matches and name_ratio >= "
            f"{CANDIDATE_NAME_RATIO} (triple_ratio < {AUTO_TRIPLE_RATIO})")
        return "candidate"
    if not size_match and lineage_score >= LINEAGE_SCORE_THRESHOLD:
        evidence.append(
            f"tier=lineage: size differs but lineage_score >= "
            f"{LINEAGE_SCORE_THRESHOLD}")
        return "lineage"
    evidence.append(
        f"tier=rejected: no threshold met (size_match={size_match}, "
        f"triple_ratio={triple_ratio:.3f}, name_ratio={name_ratio:.3f}, "
        f"lineage_score={lineage_score:.3f})")
    return "rejected"
