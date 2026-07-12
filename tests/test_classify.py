"""Tests for :mod:`pptrepair.classify`.

All fixtures are built directly from the dataclasses defined in
:mod:`pptrepair.scanner` and :mod:`pptrepair.census`; no file I/O and
no calls to ``scan_structure`` / ``from_central_directory`` /
``from_lfh_scan`` are involved.
"""

from __future__ import annotations

from pathlib import Path

from pptrepair.census import CensusResult, EntryResult
from pptrepair.classify import HEAD_ZERO_MIN_LENGTH, Verdict, classify
from pptrepair.scanner import EocdInfo, ZeroRun, ZipStructure

DUMMY_PATH = Path("dummy.pptx")


def _structure(size: int, head_kind: str = "zip",
               zero_runs: list[ZeroRun] | None = None,
               lfh_offsets: list[int] | None = None,
               cd_sig_count: int = 0,
               eocd: EocdInfo | None = None) -> ZipStructure:
    """Build a :class:`ZipStructure` without touching the filesystem."""
    return ZipStructure(
        size=size,
        head_kind=head_kind,
        zero_runs=zero_runs or [],
        lfh_offsets=lfh_offsets or [],
        cd_sig_count=cd_sig_count,
        eocd=eocd,
    )


def _entry(name: str, header_offset: int, ok: bool,
           category: str = "other", file_size: int = 100,
           error: str | None = None) -> EntryResult:
    """Build a single :class:`EntryResult`."""
    return EntryResult(name=name, category=category,
                        header_offset=header_offset, file_size=file_size,
                        ok=ok, error=error)


def _census(method: str, entries: list[EntryResult]) -> CensusResult:
    """Build a :class:`CensusResult` from prebuilt entries."""
    return CensusResult(method=method, entries=entries)


def _core_entries() -> list[EntryResult]:
    """Return the three essential pptx core parts, all readable."""
    return [
        _entry("[Content_Types].xml", 0, True, category="core_parts"),
        _entry("_rels/.rels", 200, True, category="core_parts"),
        _entry("ppt/presentation.xml", 400, True, category="core_parts"),
    ]


def _eocd(offset: int, total_entries: int, cd_offset: int,
          cd_size: int) -> EocdInfo:
    """Build a consistent :class:`EocdInfo`."""
    return EocdInfo(offset=offset, total_entries=total_entries,
                     cd_offset=cd_offset, cd_size=cd_size,
                     is_consistent=(cd_offset + cd_size == offset))


def test_normal_all_ok_with_core_parts() -> None:
    """All entries readable and pptx core parts present -> NORMAL."""
    entries = _core_entries() + [
        _entry("ppt/slides/slide1.xml", 600, True, category="slide_xml"),
    ]
    cd = _census("central_directory", entries)
    lfh = _census("lfh_scan", entries)
    structure = _structure(
        size=1000, lfh_offsets=[0, 200, 400, 600], cd_sig_count=4,
        eocd=_eocd(offset=900, total_entries=4, cd_offset=700, cd_size=200),
    )

    diag = classify(DUMMY_PATH, structure, cd, lfh)

    assert diag.verdict == Verdict.NORMAL
    assert diag.evidence
    assert diag.salvage_summary == {
        "entries_ok": 4, "entries_total": 4,
        "slides_ok": 1, "slides_total": 1, "source": "cd",
    }


def test_fully_readable_but_missing_core_parts_is_other_corrupt() -> None:
    """All entries readable but core pptx parts missing -> OTHER_CORRUPT."""
    entries = [_entry("ppt/slides/slide1.xml", 0, True, category="slide_xml")]
    cd = _census("central_directory", entries)
    lfh = _census("lfh_scan", entries)
    structure = _structure(
        size=600, lfh_offsets=[0], cd_sig_count=1,
        eocd=_eocd(offset=500, total_entries=1, cd_offset=300, cd_size=100),
    )

    diag = classify(DUMMY_PATH, structure, cd, lfh)

    assert diag.verdict == Verdict.OTHER_CORRUPT
    assert diag.evidence


def test_head_zero_fill() -> None:
    """Large leading zero run killing every covered CD entry -> HEAD_ZERO_FILL."""
    two_mib = 2 * 1024 * 1024
    tail_offset = two_mib + 1000
    cd_entries = [
        _entry("ppt/media/image1.png", 1000, False,
               category="media", error="BadZipFile"),
        _entry("ppt/media/image2.png", 500_000, False,
               category="media", error="BadZipFile"),
        _entry("[Content_Types].xml", tail_offset, True,
               category="core_parts"),
    ]
    cd = _census("central_directory", cd_entries)
    lfh = _census("lfh_scan", [
        _entry("[Content_Types].xml", tail_offset, True,
               category="core_parts"),
    ])
    structure = _structure(
        size=tail_offset + 6000,
        zero_runs=[ZeroRun(start=0, end=two_mib)],
        lfh_offsets=[tail_offset],
        cd_sig_count=3,
        eocd=_eocd(offset=tail_offset + 5000, total_entries=3,
                    cd_offset=tail_offset + 4000, cd_size=900),
    )

    diag = classify(DUMMY_PATH, structure, cd, lfh)

    assert diag.verdict == Verdict.HEAD_ZERO_FILL
    assert diag.evidence
    assert diag.salvage_summary["source"] == "cd"
    assert diag.salvage_summary["entries_ok"] == 1
    assert diag.salvage_summary["entries_total"] == 3


def test_head_zero_fill_below_min_length_does_not_trigger() -> None:
    """A leading zero run below HEAD_ZERO_MIN_LENGTH must not trigger 5b."""
    short_len = HEAD_ZERO_MIN_LENGTH - 1
    tail_offset = short_len + 1000
    cd_entries = [
        _entry("ppt/media/image1.png", 1000, False,
               category="media", error="BadZipFile"),
        _entry("[Content_Types].xml", tail_offset, True,
               category="core_parts"),
    ]
    cd = _census("central_directory", cd_entries)
    lfh = _census("lfh_scan", [
        _entry("[Content_Types].xml", tail_offset, True,
               category="core_parts"),
    ])
    structure = _structure(
        size=tail_offset + 4000,
        zero_runs=[ZeroRun(start=0, end=short_len)],
        lfh_offsets=[tail_offset],
        cd_sig_count=2,
        eocd=_eocd(offset=tail_offset + 3000, total_entries=2,
                    cd_offset=tail_offset + 2000, cd_size=900),
    )

    diag = classify(DUMMY_PATH, structure, cd, lfh)

    assert diag.verdict != Verdict.HEAD_ZERO_FILL
    assert diag.verdict == Verdict.OTHER_CORRUPT


def test_head_foreign_data() -> None:
    """Foreign head data killing every leading CD entry -> HEAD_FOREIGN_DATA."""
    tail_offset = 20000
    cd_entries = [
        _entry("junk1", 0, False, category="other", error="BadZipFile"),
        _entry("junk2", 5000, False, category="other", error="BadZipFile"),
        _entry("[Content_Types].xml", tail_offset, True,
               category="core_parts"),
    ]
    cd = _census("central_directory", cd_entries)
    lfh = _census("lfh_scan", [
        _entry("[Content_Types].xml", tail_offset, True,
               category="core_parts"),
    ])
    structure = _structure(
        size=31000, head_kind="other", lfh_offsets=[tail_offset],
        cd_sig_count=3,
        eocd=_eocd(offset=30000, total_entries=3, cd_offset=25000,
                    cd_size=900),
    )

    diag = classify(DUMMY_PATH, structure, cd, lfh)

    assert diag.verdict == Verdict.HEAD_FOREIGN_DATA
    assert diag.evidence


def test_version_mix_five_mismatches() -> None:
    """Five or more CRC-valid entries unknown to the CD -> VERSION_MIX."""
    cd_entries = [
        _entry("[Content_Types].xml", 0, True, category="core_parts"),
        _entry("ppt/presentation.xml", 50, False,
               category="core_parts", error="BadZipFile"),
    ]
    cd = _census("central_directory", cd_entries)
    lfh_entries = [_entry("[Content_Types].xml", 0, True,
                           category="core_parts")]
    lfh_entries += [
        _entry(f"ppt/slides/slide{i}.xml", 1000 + i * 100, True,
               category="slide_xml")
        for i in range(5)
    ]
    lfh = _census("lfh_scan", lfh_entries)
    structure = _structure(
        size=2100, lfh_offsets=[e.header_offset for e in lfh_entries],
        cd_sig_count=2,
        eocd=_eocd(offset=2000, total_entries=2, cd_offset=1900, cd_size=90),
    )

    diag = classify(DUMMY_PATH, structure, cd, lfh)

    assert diag.verdict == Verdict.VERSION_MIX
    assert diag.salvage_summary["source"] == "lfh"
    assert diag.salvage_summary["entries_total"] == 6
    assert diag.salvage_summary["entries_ok"] == 6
    assert diag.salvage_summary["slides_ok"] == 5
    assert diag.salvage_summary["slides_total"] == 5


def test_version_mix_four_mismatches_is_not_version_mix() -> None:
    """Four mismatches stay below the VERSION_MIX threshold (boundary value)."""
    cd_entries = [
        _entry("[Content_Types].xml", 0, True, category="core_parts"),
        _entry("ppt/presentation.xml", 50, False,
               category="core_parts", error="BadZipFile"),
    ]
    cd = _census("central_directory", cd_entries)
    lfh_entries = [_entry("[Content_Types].xml", 0, True,
                           category="core_parts")]
    lfh_entries += [
        _entry(f"ppt/slides/slide{i}.xml", 1000 + i * 100, True,
               category="slide_xml")
        for i in range(4)
    ]
    lfh = _census("lfh_scan", lfh_entries)
    structure = _structure(
        size=1800, head_kind="zip",
        lfh_offsets=[e.header_offset for e in lfh_entries],
        cd_sig_count=2,
        eocd=_eocd(offset=1700, total_entries=2, cd_offset=1600, cd_size=90),
    )

    diag = classify(DUMMY_PATH, structure, cd, lfh)

    assert diag.verdict == Verdict.OTHER_CORRUPT


def test_tail_truncated() -> None:
    """No EOCD but the head is an LFH signature -> TAIL_TRUNCATED."""
    lfh_entries = [
        _entry("[Content_Types].xml", 0, True, category="core_parts"),
        _entry("_rels/.rels", 200, True, category="core_parts"),
    ]
    lfh = _census("lfh_scan", lfh_entries)
    structure = _structure(size=500, head_kind="zip", lfh_offsets=[0, 200],
                            cd_sig_count=0, eocd=None)

    diag = classify(DUMMY_PATH, structure, None, lfh)

    assert diag.verdict == Verdict.TAIL_TRUNCATED
    assert diag.salvage_summary["source"] == "lfh"
    assert diag.salvage_summary["entries_total"] == 2
    assert diag.salvage_summary["entries_ok"] == 2


def test_all_zero_file_is_other_corrupt() -> None:
    """An all-zero file (no EOCD, zero_ratio >= 0.99) -> OTHER_CORRUPT."""
    size = 100_000
    structure = _structure(
        size=size, head_kind="zeros",
        zero_runs=[ZeroRun(start=0, end=size)],
        lfh_offsets=[], cd_sig_count=0, eocd=None,
    )
    lfh = _census("lfh_scan", [])

    diag = classify(DUMMY_PATH, structure, None, lfh)

    assert diag.verdict == Verdict.OTHER_CORRUPT
    assert diag.evidence


def test_empty_file_is_not_a_zip() -> None:
    """A zero-byte file -> NOT_A_ZIP."""
    structure = _structure(size=0, head_kind="other", eocd=None)
    lfh = _census("lfh_scan", [])

    diag = classify(DUMMY_PATH, structure, None, lfh)

    assert diag.verdict == Verdict.NOT_A_ZIP


def test_text_like_file_is_not_a_zip() -> None:
    """No ZIP signatures at all and not mostly zeros -> NOT_A_ZIP."""
    structure = _structure(size=1000, head_kind="other", zero_runs=[],
                            lfh_offsets=[], cd_sig_count=0, eocd=None)
    lfh = _census("lfh_scan", [])

    diag = classify(DUMMY_PATH, structure, None, lfh)

    assert diag.verdict == Verdict.NOT_A_ZIP


def test_eocd_present_but_cd_census_none_is_other_corrupt() -> None:
    """EOCD present but no CD census (zipfile cannot open) -> OTHER_CORRUPT."""
    structure = _structure(
        size=530, lfh_offsets=[0, 100], cd_sig_count=2,
        eocd=_eocd(offset=500, total_entries=2, cd_offset=300, cd_size=190),
    )
    lfh = _census("lfh_scan", [
        _entry("a", 0, True), _entry("b", 100, True),
    ])

    diag = classify(DUMMY_PATH, structure, None, lfh)

    assert diag.verdict == Verdict.OTHER_CORRUPT
