"""Corruption-pattern classification for .pptx files.

The verdicts correspond to the corruption taxonomy established by this
project's investigation of real OneDrive-corrupted presentations:

* ``HEAD_ZERO_FILL``     — leading chunks overwritten with zeros; the
  tail of the file (including the central directory) survives.
* ``HEAD_FOREIGN_DATA``  — same geometry, but the leading chunks were
  overwritten with unrelated non-zero data.
* ``VERSION_MIX``        — the file is a collage of chunks from two
  different versions: local file headers found by scanning do not match
  the entries indexed by the (surviving) central directory.
* ``TAIL_TRUNCATED``     — the file ends prematurely; the central
  directory and EOCD are gone but leading entries are intact.

Unknown damage deliberately falls back to ``OTHER_CORRUPT`` rather than
being forced into a known pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pptrepair.census import CensusResult
from pptrepair.scanner import ZipStructure, alignment_of

#: Minimum number of CRC-valid scanned entries at offsets unknown to the
#: central directory for a VERSION_MIX verdict.
VERSION_MIX_MIN_MISMATCHES = 5

#: Minimum length (bytes) of a leading zero run for HEAD_ZERO_FILL.
HEAD_ZERO_MIN_LENGTH = 65536

#: Zero-coverage ratio at or above which a file with no usable ZIP
#: structure is reported as entirely zeroed.
ALL_ZERO_RATIO = 0.99


class Verdict(Enum):
    """Classification outcome for one file."""

    NORMAL = "normal"
    HEAD_ZERO_FILL = "head_zero_fill"
    HEAD_FOREIGN_DATA = "head_foreign_data"
    VERSION_MIX = "version_mix"
    TAIL_TRUNCATED = "tail_truncated"
    OTHER_CORRUPT = "other_corrupt"
    NOT_A_ZIP = "not_a_zip"


@dataclass
class Diagnosis:
    """Full diagnosis of one file."""

    path: Path
    verdict: Verdict
    evidence: list[str] = field(default_factory=list)
    structure: ZipStructure | None = None
    cd_census: CensusResult | None = None
    lfh_census: CensusResult | None = None
    salvage_summary: dict = field(default_factory=dict)
    """Keys: ``entries_ok``, ``entries_total``, ``slides_ok``,
    ``slides_total`` (ints) and ``source`` (``"cd"`` | ``"lfh"``).
    Empty when nothing was salvageable/examined (e.g. NOT_A_ZIP)."""


def classify(path: Path, structure: ZipStructure,
             cd_census: CensusResult | None,
             lfh_census: CensusResult) -> Diagnosis:
    """Classify one file according to the project's corruption taxonomy.

    Decision procedure (first matching rule wins):

    1. Empty file, or no ZIP signatures at all and not mostly zeros
       -> NOT_A_ZIP.
    2. No EOCD:
       a. head is a local-file-header signature and at least one LFH
          exists -> TAIL_TRUNCATED;
       b. zero_ratio >= ALL_ZERO_RATIO -> OTHER_CORRUPT (entirely
          zeroed);
       c. otherwise -> OTHER_CORRUPT.
    3. EOCD present but the central directory census is unavailable
       (zipfile cannot open) -> OTHER_CORRUPT.
    4. Central directory census fully readable:
       a. essential pptx parts present -> NORMAL;
       b. otherwise -> OTHER_CORRUPT (valid ZIP, not a pptx package).
    5. Central directory census has failures:
       a. >= VERSION_MIX_MIN_MISMATCHES CRC-valid scanned entries whose
          header offsets are not listed in the central directory
          -> VERSION_MIX;
       b. leading zero run from offset 0 of >= HEAD_ZERO_MIN_LENGTH and
          every CD entry starting inside it unreadable
          -> HEAD_ZERO_FILL;
       c. head_kind == "other" and every CD entry starting before the
          first CRC-valid scanned entry unreadable -> HEAD_FOREIGN_DATA;
       d. otherwise -> OTHER_CORRUPT.

    Chunk-boundary alignment (256 KiB / 1 MiB) of damage borders is
    reported as *evidence* but is never a requirement, so unknown
    variants degrade gracefully.

    The salvage summary is computed from the LFH census for
    VERSION_MIX / TAIL_TRUNCATED verdicts and from the CD census
    (falling back to the LFH census) otherwise.
    """
    verdict, evidence = _decide(structure, cd_census, lfh_census)
    salvage_summary = _build_salvage_summary(verdict, cd_census, lfh_census)
    return Diagnosis(
        path=path,
        verdict=verdict,
        evidence=evidence,
        structure=structure,
        cd_census=cd_census,
        lfh_census=lfh_census,
        salvage_summary=salvage_summary,
    )


def _format_size(num_bytes: int) -> str:
    """Format a byte count as a short human-readable alignment label."""
    if num_bytes % (1024 * 1024) == 0:
        return f"{num_bytes // (1024 * 1024)} MiB"
    if num_bytes % 1024 == 0:
        return f"{num_bytes // 1024} KiB"
    return f"{num_bytes} bytes"


def _alignment_note(offset: int) -> str | None:
    """Return an "aligned to a N boundary" clause for *offset*, if any."""
    alignment = alignment_of(offset)
    if alignment is None:
        return None
    return f"aligned to a {_format_size(alignment)} boundary"


def _zero_ratio_evidence(structure: ZipStructure) -> list[str]:
    """Return evidence describing a file that is almost entirely zeros."""
    evidence = [
        f"{structure.zero_total()} of {structure.size} bytes are zero "
        f"({structure.zero_ratio():.1%})",
    ]
    if structure.zero_runs:
        first_run = structure.zero_runs[0]
        line = (
            f"largest inspected zero region: [{first_run.start}, "
            f"{first_run.end}) ({first_run.length()} bytes)"
        )
        note = _alignment_note(first_run.end)
        if note is not None:
            line += f", end {note}"
        evidence.append(line)
    return evidence


def _decide(structure: ZipStructure, cd_census: CensusResult | None,
            lfh_census: CensusResult) -> tuple[Verdict, list[str]]:
    """Apply the decision procedure and return (verdict, evidence)."""
    if structure.size == 0:
        return Verdict.NOT_A_ZIP, ["empty file"]

    no_signatures = not structure.lfh_offsets and structure.cd_sig_count == 0
    if no_signatures and structure.eocd is None:
        return _decide_no_signatures(structure)

    if structure.eocd is None:
        return _decide_no_eocd(structure)

    if cd_census is None:
        return Verdict.OTHER_CORRUPT, [
            f"EOCD record found at offset {structure.eocd.offset} but the "
            "archive could not be opened (no valid central directory)",
        ]

    if cd_census.total() > 0 and cd_census.ok_count() == cd_census.total():
        return _decide_fully_readable(cd_census)

    return _decide_partial_cd(structure, cd_census, lfh_census)


#: Evidence line attached to files whose head bears the OLE compound
#: file signature: those are encrypted Office documents or legacy binary
#: formats rather than OneDrive chunk corruption.
CFB_EVIDENCE = (
    "file is an OLE compound document (likely an encrypted Office file "
    "or a legacy binary Office format, not OneDrive corruption)"
)


def _decide_no_signatures(structure: ZipStructure) -> tuple[Verdict, list[str]]:
    """Rule 1's second branch: no ZIP signatures found anywhere."""
    if structure.head_kind == "cfb":
        return Verdict.NOT_A_ZIP, [CFB_EVIDENCE]
    if structure.zero_ratio() >= ALL_ZERO_RATIO:
        return Verdict.OTHER_CORRUPT, _zero_ratio_evidence(structure)
    return Verdict.NOT_A_ZIP, ["no ZIP signatures found"]


def _decide_no_eocd(structure: ZipStructure) -> tuple[Verdict, list[str]]:
    """Rule 2: no end-of-central-directory record was found."""
    if structure.head_kind == "zip" and structure.lfh_offsets:
        return Verdict.TAIL_TRUNCATED, [
            "no end-of-central-directory record found",
            "file starts with a local file header signature",
            f"{len(structure.lfh_offsets)} local file header(s) found",
        ]
    if structure.head_kind == "cfb":
        # Stray ZIP signatures inside an OLE container (e.g. embedded
        # archives) do not make the file a damaged pptx package.
        return Verdict.NOT_A_ZIP, [CFB_EVIDENCE]
    if structure.zero_ratio() >= ALL_ZERO_RATIO:
        return Verdict.OTHER_CORRUPT, _zero_ratio_evidence(structure)
    return Verdict.OTHER_CORRUPT, ["no end-of-central-directory record"]


def _decide_fully_readable(cd_census: CensusResult) -> tuple[Verdict, list[str]]:
    """Rule 4: every central directory entry read back intact."""
    summary = f"all {cd_census.total()} central directory entries are readable"
    if cd_census.has_pptx_core():
        return Verdict.NORMAL, [summary, "essential pptx parts present"]
    return Verdict.OTHER_CORRUPT, [
        summary, "valid ZIP archive but not a pptx package",
    ]


def _decide_partial_cd(structure: ZipStructure, cd_census: CensusResult,
                        lfh_census: CensusResult) -> tuple[Verdict, list[str]]:
    """Rule 5: the central directory census has failures (or is empty)."""
    version_mix = _check_version_mix(cd_census, lfh_census)
    if version_mix is not None:
        return version_mix

    head_zero_fill = _check_head_zero_fill(structure, cd_census)
    if head_zero_fill is not None:
        return head_zero_fill

    head_foreign_data = _check_head_foreign_data(structure, cd_census, lfh_census)
    if head_foreign_data is not None:
        return head_foreign_data

    return Verdict.OTHER_CORRUPT, [
        f"{cd_census.ok_count()} of {cd_census.total()} central directory "
        "entries are readable",
    ]


def _check_version_mix(cd_census: CensusResult,
                        lfh_census: CensusResult) -> tuple[Verdict, list[str]] | None:
    """Rule 5a: many CRC-valid LFH entries are absent from the CD."""
    cd_offsets = {entry.header_offset for entry in cd_census.entries}
    mismatches = [
        entry for entry in lfh_census.ok_entries()
        if entry.header_offset not in cd_offsets
    ]
    if len(mismatches) < VERSION_MIX_MIN_MISMATCHES:
        return None
    evidence = [
        f"{len(mismatches)} CRC-valid scanned entries have header offsets "
        f"not listed in the central directory (>= {VERSION_MIX_MIN_MISMATCHES} "
        "required)",
        f"central directory lists {cd_census.total()} entries, "
        f"{cd_census.ok_count()} readable",
    ]
    return Verdict.VERSION_MIX, evidence


def _check_head_zero_fill(structure: ZipStructure,
                           cd_census: CensusResult) -> tuple[Verdict, list[str]] | None:
    """Rule 5b: the file starts with a large zeroed-out region."""
    if not structure.zero_runs:
        return None
    first_run = structure.zero_runs[0]
    if first_run.start != 0 or first_run.length() < HEAD_ZERO_MIN_LENGTH:
        return None
    affected = [
        entry for entry in cd_census.entries
        if entry.header_offset < first_run.end
    ]
    if not affected or any(entry.ok for entry in affected):
        return None
    evidence = [
        f"leading zero region [0, {first_run.end}) covers "
        f"{first_run.length()} bytes",
        f"{len(affected)} central directory entries starting inside the "
        "zero region are all unreadable",
    ]
    note = _alignment_note(first_run.end)
    if note is not None:
        evidence.append(f"zero region end is {note}")
    return Verdict.HEAD_ZERO_FILL, evidence


def _check_head_foreign_data(
        structure: ZipStructure, cd_census: CensusResult,
        lfh_census: CensusResult) -> tuple[Verdict, list[str]] | None:
    """Rule 5c: the file starts with unrelated non-zero data."""
    if structure.head_kind != "other":
        return None
    ok_entries = lfh_census.ok_entries()
    if not ok_entries:
        return None
    first_ok = min(entry.header_offset for entry in ok_entries)
    affected = [
        entry for entry in cd_census.entries
        if entry.header_offset < first_ok
    ]
    if not affected or any(entry.ok for entry in affected):
        return None
    evidence = [
        "file does not start with a ZIP local file header signature",
        f"first CRC-valid scanned entry starts at offset {first_ok}",
        f"{len(affected)} central directory entries before that offset "
        "are all unreadable",
    ]
    return Verdict.HEAD_FOREIGN_DATA, evidence


def _build_salvage_summary(
        verdict: Verdict, cd_census: CensusResult | None,
        lfh_census: CensusResult) -> dict:
    """Build the salvage summary dict for the chosen census source."""
    if verdict in (Verdict.VERSION_MIX, Verdict.TAIL_TRUNCATED):
        source_census: CensusResult | None = lfh_census
        source_label = "lfh"
    elif cd_census is not None:
        source_census = cd_census
        source_label = "cd"
    else:
        source_census = lfh_census
        source_label = "lfh"

    if source_census is None or source_census.total() == 0:
        return {}
    return {
        "entries_ok": source_census.ok_count(),
        "entries_total": source_census.total(),
        "slides_ok": len(source_census.ok_slide_numbers()),
        "slides_total": source_census.total_slide_count(),
        "source": source_label,
    }
