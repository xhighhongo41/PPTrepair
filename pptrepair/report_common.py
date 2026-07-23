"""Shared constants for the ``pptrepair.report`` family of modules."""

from __future__ import annotations

from pptrepair.classify import Verdict

#: URL offered to users who hit an unknown corruption pattern.
ISSUE_URL = "https://github.com/xhighhongo41/PPTrepair/issues/new/choose"

#: One-line human description per verdict.
VERDICT_LABELS: dict[Verdict, str] = {
    Verdict.NORMAL: "intact PowerPoint package",
    Verdict.HEAD_ZERO_FILL: "corrupted: leading region overwritten with zeros",
    Verdict.HEAD_FOREIGN_DATA:
        "corrupted: leading region overwritten with foreign data",
    Verdict.VERSION_MIX: "corrupted: mixture of two file versions",
    Verdict.TAIL_TRUNCATED: "corrupted: file tail truncated",
    Verdict.OTHER_CORRUPT: "corrupted: unrecognized damage pattern",
    Verdict.NOT_A_ZIP: "not a ZIP-based file",
    Verdict.EMPTY_FILE: "corrupted: file is empty (all content lost)",
    Verdict.FULL_ZERO_FILL:
        "corrupted: file is (almost) entirely zero-filled",
    Verdict.INTERIOR_DAMAGE:
        "corrupted: interior region damaged, archive index intact",
    Verdict.TAIL_FOREIGN_DATA:
        "corrupted: intact archive followed by foreign data",
    Verdict.FOREIGN_ZIP_OVERWRITE:
        "corrupted: overwritten with fragments of another ZIP archive",
    Verdict.SCATTERED_OVERWRITE:
        "corrupted: archive body largely overwritten in place",
}

#: Number of twin-restoration candidates listed individually per file in
#: a text report before the remainder collapses into one "+n more" line.
_TWIN_CANDIDATES_DISPLAY_LIMIT = 3

#: Maximum number of lineage-version candidates listed per corrupted file
#: (highest :attr:`~pptrepair.origin.OriginScore.lineage_score` first).
_LINEAGE_CANDIDATES_DISPLAY_LIMIT = 5

#: Minimum number of same-size corrupted files to report as one merge
#: candidate group.
_MERGE_GROUP_MIN_FILES = 2
