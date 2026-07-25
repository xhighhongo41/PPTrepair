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
* ``EMPTY_FILE``         — the file is zero bytes long; nothing
  survives.
* ``FULL_ZERO_FILL``     — the file is (almost) entirely zero-filled
  and nothing is salvageable.
* ``INTERIOR_DAMAGE``    — head and central directory are intact;
  damage is confined to interior entry data.
* ``TAIL_FOREIGN_DATA``  — a complete archive is followed by a large
  region of unindexed foreign data hiding the EOCD from ordinary
  readers.
* ``FOREIGN_ZIP_OVERWRITE`` — part of the archive body was overwritten
  by fragments of an *unrelated* ZIP archive; the intruding fragments'
  own local file headers survive the scan as CRC-valid entries whose
  names the central directory never listed. Distinct from
  ``HEAD_FOREIGN_DATA`` (a head-confined super-variant of which does
  contain such fragments): this fallback catches foreign-ZIP damage that
  is *not* limited to the leading region.
* ``SCATTERED_OVERWRITE`` — the end-of-central-directory record and the
  central directory survive intact, yet almost none of the local entries
  do: the archive body was largely overwritten in place.

Unknown damage deliberately falls back to ``OTHER_CORRUPT`` rather than
being forced into a known pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pptrepair.census import CensusResult, EntryResult
from pptrepair.scanner import ZipStructure, alignment_of

#: Minimum number of CRC-valid scanned entries at offsets unknown to the
#: central directory for a VERSION_MIX verdict.
VERSION_MIX_MIN_MISMATCHES = 5

#: Minimum length (bytes) of a leading zero run for HEAD_ZERO_FILL
#: (4 KiB, matching the scanner's zero-run detection block size).
HEAD_ZERO_MIN_LENGTH = 4096

#: Zero-coverage ratio at or above which a file with no usable ZIP
#: structure is reported as entirely zeroed.
ALL_ZERO_RATIO = 0.99

#: Amount (bytes) of unindexed data following the EOCD beyond which
#: zipfile's EOCD search window (the fixed 22-byte record plus up to
#: 65535 bytes of comment) no longer reaches it, so an ordinary reader
#: cannot open the archive directly.
TAIL_JUNK_MIN = 65557

#: Minimum number of entries the central directory must index before a
#: SCATTERED_OVERWRITE verdict is considered, so a tiny archive with a
#: couple of damaged entries is never swept up as "largely overwritten".
SCATTERED_MIN_CD_ENTRIES = 20

#: Fraction of indexed entries whose local file header must *still*
#: survive, below which the body counts as largely overwritten in place
#: (SCATTERED_OVERWRITE).
SCATTERED_LFH_SURVIVAL_MAX = 0.10

#: Maximum number of foreign ZIP fragment names listed inline in
#: evidence before the remainder is summarised as a count.
_FOREIGN_FRAGMENT_SAMPLE = 5


class Verdict(Enum):
    """Classification outcome for one file."""

    NORMAL = "normal"
    HEAD_ZERO_FILL = "head_zero_fill"
    HEAD_FOREIGN_DATA = "head_foreign_data"
    VERSION_MIX = "version_mix"
    TAIL_TRUNCATED = "tail_truncated"
    OTHER_CORRUPT = "other_corrupt"
    NOT_A_ZIP = "not_a_zip"
    EMPTY_FILE = "empty_file"
    FULL_ZERO_FILL = "full_zero_fill"
    INTERIOR_DAMAGE = "interior_damage"
    TAIL_FOREIGN_DATA = "tail_foreign_data"
    FOREIGN_ZIP_OVERWRITE = "foreign_zip_overwrite"
    SCATTERED_OVERWRITE = "scattered_overwrite"


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

    1. Empty file (size == 0) -> EMPTY_FILE.
    2. No ZIP signatures anywhere and no EOCD:
       a. head is the OLE compound file signature -> NOT_A_ZIP (CFB
          evidence);
       b. zero_ratio >= ALL_ZERO_RATIO -> FULL_ZERO_FILL;
       c. otherwise -> NOT_A_ZIP.
    3. No EOCD, but some ZIP signature was found:
       a. head is a local-file-header signature and at least one LFH
          exists -> TAIL_TRUNCATED;
       b. head is the OLE compound file signature -> NOT_A_ZIP;
       c. zero_ratio >= ALL_ZERO_RATIO -> FULL_ZERO_FILL;
       d. otherwise -> OTHER_CORRUPT.
    4. EOCD present but the central directory census is unavailable
       (zipfile cannot open):
       a. zero_ratio >= ALL_ZERO_RATIO and no CRC-valid scanned entries
          exist -> FULL_ZERO_FILL (nothing salvageable);
       b. the EOCD is internally consistent, more than TAIL_JUNK_MIN
          bytes of unindexed data follow it and at least one CRC-valid
          scanned entry exists -> TAIL_FOREIGN_DATA (a complete archive
          hidden behind foreign trailing data);
       c. otherwise -> OTHER_CORRUPT.
    5. Central directory census fully readable:
       a. essential pptx parts present -> NORMAL;
       b. otherwise -> OTHER_CORRUPT (valid ZIP, not a pptx package).
    6. Central directory census has failures:
       a. >= VERSION_MIX_MIN_MISMATCHES CRC-valid scanned entries whose
          header offsets are not listed in the central directory
          -> VERSION_MIX;
       b. leading zero run from offset 0 of >= HEAD_ZERO_MIN_LENGTH and
          every CD entry starting inside it unreadable
          -> HEAD_ZERO_FILL;
       c. head_kind in ("other", "zeros"), every CD entry starting
          before the first CRC-valid scanned entry *the central
          directory also indexes* unreadable, and no unreadable CD entry
          starts at or after that offset (damage confined to the head)
          -> HEAD_FOREIGN_DATA;
       d. head_kind == "zip", the EOCD is internally consistent and at
          least one CD entry is readable -> INTERIOR_DAMAGE (head and
          central directory intact, only entry data damaged);
       e. at least one CRC-valid scanned entry whose name the central
          directory never listed (a fragment of an unrelated ZIP)
          -> FOREIGN_ZIP_OVERWRITE;
       f. a consistent EOCD, >= SCATTERED_MIN_CD_ENTRIES indexed entries
          and a surviving local-file-header fraction below
          SCATTERED_LFH_SURVIVAL_MAX -> SCATTERED_OVERWRITE;
       g. otherwise -> OTHER_CORRUPT.

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
        (f"{structure.zero_total()} of {structure.size} bytes are zero "
        f"({structure.zero_ratio():.1%})"),
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
        return Verdict.EMPTY_FILE, ["empty file"]

    no_signatures = not structure.lfh_offsets and structure.cd_sig_count == 0
    if no_signatures and structure.eocd is None:
        return _decide_no_signatures(structure)

    if structure.eocd is None:
        return _decide_no_eocd(structure)

    if cd_census is None:
        return _decide_unopenable(structure, lfh_census)

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
    """Rule 2: no ZIP signatures found anywhere, and no EOCD."""
    if structure.head_kind == "cfb":
        return Verdict.NOT_A_ZIP, [CFB_EVIDENCE]
    if structure.zero_ratio() >= ALL_ZERO_RATIO:
        return Verdict.FULL_ZERO_FILL, _zero_ratio_evidence(structure)
    return Verdict.NOT_A_ZIP, ["no ZIP signatures found"]


def _decide_no_eocd(structure: ZipStructure) -> tuple[Verdict, list[str]]:
    """Rule 3: no end-of-central-directory record was found."""
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
        return Verdict.FULL_ZERO_FILL, _zero_ratio_evidence(structure)
    return Verdict.OTHER_CORRUPT, ["no end-of-central-directory record"]


def _decide_unopenable(structure: ZipStructure,
                        lfh_census: CensusResult) -> tuple[Verdict, list[str]]:
    """Rule 4: an EOCD exists but the archive cannot be opened as a ZIP.

    Distinguishes a fully zero-filled file (nothing salvageable), a
    complete archive hidden behind a large block of unindexed trailing
    data (:data:`TAIL_JUNK_MIN`), and everything else, which stays
    OTHER_CORRUPT.
    """
    if (structure.zero_ratio() >= ALL_ZERO_RATIO
            and not lfh_census.ok_entries()):
        return Verdict.FULL_ZERO_FILL, _zero_ratio_evidence(structure) + [
            "central directory unreadable, nothing salvageable",
        ]

    eocd = structure.eocd
    eocd_end = eocd.offset + 22 + eocd.comment_length
    trailing = structure.size - eocd_end
    if (eocd.is_consistent and trailing > TAIL_JUNK_MIN
            and lfh_census.ok_entries()):
        return Verdict.TAIL_FOREIGN_DATA, [
            (f"EOCD record found at offset {eocd.offset}, but {trailing} "
            "bytes of unindexed data follow the archive"),
            f"leading archive [0, {eocd_end}) appears complete",
            (f"{len(lfh_census.ok_entries())} CRC-valid entries found by "
            "scanning"),
        ]

    return Verdict.OTHER_CORRUPT, [
        (f"EOCD record found at offset {eocd.offset} but the "
        "archive could not be opened (no valid central directory)"),
    ]


def _decide_fully_readable(cd_census: CensusResult) -> tuple[Verdict, list[str]]:
    """Rule 5: every central directory entry read back intact."""
    summary = f"all {cd_census.total()} central directory entries are readable"
    if cd_census.has_pptx_core():
        return Verdict.NORMAL, [summary, "essential pptx parts present"]
    return Verdict.OTHER_CORRUPT, [
        summary, "valid ZIP archive but not a pptx package",
    ]


def _decide_partial_cd(structure: ZipStructure, cd_census: CensusResult,
                        lfh_census: CensusResult) -> tuple[Verdict, list[str]]:
    """Rule 6: the central directory census has failures (or is empty)."""
    version_mix = _check_version_mix(cd_census, lfh_census)
    if version_mix is not None:
        return version_mix

    head_zero_fill = _check_head_zero_fill(structure, cd_census)
    if head_zero_fill is not None:
        return head_zero_fill

    head_foreign_data = _check_head_foreign_data(structure, cd_census, lfh_census)
    if head_foreign_data is not None:
        return head_foreign_data

    interior_damage = _check_interior_damage(structure, cd_census)
    if interior_damage is not None:
        return interior_damage

    foreign_zip = _check_foreign_zip_overwrite(structure, cd_census, lfh_census)
    if foreign_zip is not None:
        return foreign_zip

    scattered = _check_scattered_overwrite(structure, cd_census)
    if scattered is not None:
        return scattered

    return Verdict.OTHER_CORRUPT, [
        (f"{cd_census.ok_count()} of {cd_census.total()} central directory "
        "entries are readable"),
    ]


def _check_version_mix(cd_census: CensusResult,
                        lfh_census: CensusResult) -> tuple[Verdict, list[str]] | None:
    """Rule 6a: many CRC-valid LFH entries are absent from the CD."""
    cd_offsets = {entry.header_offset for entry in cd_census.entries}
    mismatches = [
        entry for entry in lfh_census.ok_entries()
        if entry.header_offset not in cd_offsets
    ]
    if len(mismatches) < VERSION_MIX_MIN_MISMATCHES:
        return None
    evidence = [
        (f"{len(mismatches)} CRC-valid scanned entries have header offsets "
        f"not listed in the central directory (>= {VERSION_MIX_MIN_MISMATCHES} "
        "required)"),
        (f"central directory lists {cd_census.total()} entries, "
        f"{cd_census.ok_count()} readable"),
    ]
    return Verdict.VERSION_MIX, evidence


def _check_head_zero_fill(structure: ZipStructure,
                           cd_census: CensusResult) -> tuple[Verdict, list[str]] | None:
    """Rule 6b: the file starts with a large zeroed-out region."""
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
        (f"leading zero region [0, {first_run.end}) covers "
        f"{first_run.length()} bytes"),
        (f"{len(affected)} central directory entries starting inside the "
        "zero region are all unreadable"),
    ]
    note = _alignment_note(first_run.end)
    if note is not None:
        evidence.append(f"zero region end is {note}")
    return Verdict.HEAD_ZERO_FILL, evidence


def cd_matched_ok_entries(cd_census: CensusResult | None,
                          lfh_census: CensusResult) -> list[EntryResult]:
    """Return CRC-valid LFH entries the central directory also indexes.

    An entry qualifies only when *both* its name and its header offset
    match some entry in *cd_census*, so a foreign ZIP fragment that
    happens to occupy the damaged file's byte range (its offset unknown
    to the central directory) — or a same-named part written at a
    different offset — can never be mistaken for one of the archive's
    own entries. Returns an empty list when *cd_census* is None (there is
    no central directory to corroborate the scan against).
    """
    if cd_census is None:
        return []
    cd_keys = {(entry.name, entry.header_offset)
               for entry in cd_census.entries}
    return [
        entry for entry in lfh_census.ok_entries()
        if (entry.name, entry.header_offset) in cd_keys
    ]


def _foreign_fragment_entries(cd_census: CensusResult | None,
                              lfh_census: CensusResult) -> list[EntryResult]:
    """Return CRC-valid LFH entries whose names the CD never listed.

    These are the fingerprint of an unrelated ZIP archive overwriting
    part of the file: genuine, CRC-valid local entries that belong to a
    *different* archive. Returns an empty list when *cd_census* is None
    (no central directory to tell own entries from foreign ones).
    """
    if cd_census is None:
        return []
    cd_names = {entry.name for entry in cd_census.entries}
    return [
        entry for entry in lfh_census.ok_entries()
        if entry.name not in cd_names
    ]


def _foreign_fragment_names_line(fragments: list[EntryResult]) -> str:
    """Format a one-line summary of foreign ZIP fragment names.

    Lists up to :data:`_FOREIGN_FRAGMENT_SAMPLE` names verbatim; any
    remainder is reported as a count.
    """
    shown = fragments[:_FOREIGN_FRAGMENT_SAMPLE]
    names = ", ".join(repr(entry.name) for entry in shown)
    line = f"foreign archive fragments found by scanning: {names}"
    extra = len(fragments) - _FOREIGN_FRAGMENT_SAMPLE
    if extra > 0:
        line += f" (+{extra} more)"
    return line


def _check_head_foreign_data(
        structure: ZipStructure, cd_census: CensusResult,
        lfh_census: CensusResult) -> tuple[Verdict, list[str]] | None:
    """Rule 6c: the file starts with unrelated non-zero data.

    Also covers a head whose first block reads as zeros
    (``head_kind == "zeros"``) followed by foreign data, since the
    scanner only inspects the first four bytes to tell zeros from a ZIP
    signature. Damage must be confined to the head region: every
    unreadable CD entry has to start before the first CRC-valid scanned
    entry, otherwise scattered corruption elsewhere in the file could be
    mistaken for this pattern.

    The "first CRC-valid scanned entry" is taken only from entries the
    central directory also indexes (:func:`cd_matched_ok_entries`), so a
    CRC-valid fragment of an *unrelated* ZIP archive embedded in the
    foreign head cannot pull the boundary earlier and defeat the
    head-confinement check (the real-world super-variant where a driver
    package's ZIP fragments overwrote the leading megabytes). When such
    foreign fragments are present, their names are added to the evidence
    as a corruption signature.
    """
    if structure.head_kind not in ("other", "zeros"):
        return None
    matched = cd_matched_ok_entries(cd_census, lfh_census)
    if not matched:
        return None
    first_ok = min(entry.header_offset for entry in matched)
    affected = [
        entry for entry in cd_census.entries
        if entry.header_offset < first_ok
    ]
    if not affected or any(entry.ok for entry in affected):
        return None
    if any(entry.header_offset >= first_ok
           for entry in cd_census.entries if not entry.ok):
        return None
    evidence = [
        "file does not start with a ZIP local file header signature",
        f"first CRC-valid scanned entry starts at offset {first_ok}",
        (f"{len(affected)} central directory entries before that offset "
        "are all unreadable"),
    ]
    fragments = _foreign_fragment_entries(cd_census, lfh_census)
    if fragments:
        evidence.append(_foreign_fragment_names_line(fragments))
    return Verdict.HEAD_FOREIGN_DATA, evidence


def _check_interior_damage(
        structure: ZipStructure,
        cd_census: CensusResult) -> tuple[Verdict, list[str]] | None:
    """Rule 6d: the head and central directory are intact; only some
    entries' data is damaged, with no recognisable geometry otherwise."""
    if structure.head_kind != "zip":
        return None
    if structure.eocd is None or not structure.eocd.is_consistent:
        return None
    if cd_census.ok_count() == 0:
        return None
    evidence = [
        (f"{cd_census.ok_count()} of {cd_census.total()} central directory "
        "entries are readable"),
        ("file head and central directory are intact; damage is confined "
        "to entry data"),
    ]
    if structure.zero_runs:
        largest_run = max(structure.zero_runs, key=lambda run: run.length())
        line = (
            f"largest zero region: [{largest_run.start}, "
            f"{largest_run.end}) ({largest_run.length()} bytes)"
        )
        note = _alignment_note(largest_run.end)
        if note is not None:
            line += f", end {note}"
        evidence.append(line)
    return Verdict.INTERIOR_DAMAGE, evidence


def _check_foreign_zip_overwrite(
        structure: ZipStructure, cd_census: CensusResult,
        lfh_census: CensusResult) -> tuple[Verdict, list[str]] | None:
    """Rule 6e: fragments of an unrelated ZIP overwrote part of the file.

    Fires when the local-file-header scan turns up at least one CRC-valid
    entry whose name the central directory never listed (see
    :func:`_foreign_fragment_entries`): a genuine entry that belongs to a
    *different* archive whose bytes landed inside this file. Only reached
    once the head-confined :func:`_check_head_foreign_data` pattern has
    been ruled out, so this catches foreign-ZIP damage that is *not*
    limited to the leading region.

    Requires the same damaged-head precondition as rule 6c
    (``head_kind in ("other", "zeros")``): an in-place foreign overwrite
    corrupts the leading region, so a file that still starts with a valid
    local file header is version-mix/interior territory and is left to
    the OTHER_CORRUPT fallback rather than being mislabelled a foreign
    overwrite (which also keeps a sub-threshold version mix — fewer than
    :data:`VERSION_MIX_MIN_MISMATCHES` CD-unknown CRC-valid entries — out
    of this bucket).
    """
    if structure.head_kind not in ("other", "zeros"):
        return None
    fragments = _foreign_fragment_entries(cd_census, lfh_census)
    if not fragments:
        return None
    evidence = [
        (f"{len(fragments)} CRC-valid scanned entries belong to an "
        "unrelated ZIP archive (names not listed in the central "
        "directory)"),
    ]
    for entry in fragments[:_FOREIGN_FRAGMENT_SAMPLE]:
        evidence.append(
            f"foreign fragment {entry.name!r} at offset "
            f"{entry.header_offset}")
    extra = len(fragments) - _FOREIGN_FRAGMENT_SAMPLE
    if extra > 0:
        evidence.append(f"... and {extra} more foreign fragment(s)")
    evidence.append(
        "part of the archive was overwritten with fragments of an "
        "unrelated ZIP archive")
    return Verdict.FOREIGN_ZIP_OVERWRITE, evidence


def _check_scattered_overwrite(
        structure: ZipStructure,
        cd_census: CensusResult) -> tuple[Verdict, list[str]] | None:
    """Rule 6f: the archive body was largely overwritten in place.

    The end-of-central-directory record and the central directory
    survive intact, yet almost none of the local file headers do: fewer
    than :data:`SCATTERED_LFH_SURVIVAL_MAX` of the indexed entries still
    have a recognisable local header. Requires a consistent EOCD and at
    least :data:`SCATTERED_MIN_CD_ENTRIES` indexed entries, so a small
    archive with a couple of damaged entries is not swept up.
    """
    eocd = structure.eocd
    if eocd is None or not eocd.is_consistent:
        return None
    total = cd_census.total()
    if total < SCATTERED_MIN_CD_ENTRIES:
        return None
    lfh_survivors = len(structure.lfh_offsets)
    survival = lfh_survivors / total
    if survival >= SCATTERED_LFH_SURVIVAL_MAX:
        return None
    evidence = [
        (f"only {lfh_survivors} local file header(s) survive for {total} "
        f"central directory entries ({survival:.1%})"),
        ("the central directory and EOCD are intact, but the archive body "
        "was overwritten in place"),
    ]
    return Verdict.SCATTERED_OVERWRITE, evidence


def _build_salvage_summary(
        verdict: Verdict, cd_census: CensusResult | None,
        lfh_census: CensusResult) -> dict:
    """Build the salvage summary dict for the chosen census source."""
    if verdict in (Verdict.VERSION_MIX, Verdict.TAIL_TRUNCATED,
                   Verdict.TAIL_FOREIGN_DATA):
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
