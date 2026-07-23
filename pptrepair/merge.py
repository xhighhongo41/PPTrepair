"""Merge restoration from several same-origin copies (lineage S).

OneDrive corruption often leaves more than one copy of the *same saved
version* behind -- the working file plus a sync-conflict twin, a stray
duplicate, and so on. Those copies share an identical byte layout (their
file sizes match exactly), so where one copy's bytes are destroyed the
other's frequently survive. This module reconstructs the original file
by *byte-splicing*: for every archive entry it copies the entry's byte
range from whichever copy still reproduces the CRC-32 the central
directory recorded for it.

The pipeline is:

1. diagnose every source through the ordinary
   scan -> census -> classify pipeline (:func:`pptrepair.scan.diagnose_file`);
2. score each non-target source against the target for a shared origin
   (:func:`pptrepair.origin.score_origin`) and keep only the copies safe
   to splice from (``auto`` tier, plus ``candidate`` on request), while
   collecting ``lineage``-tier sources as entry-level donors on request;
3. pick a reference central directory (the readable-CD copy with the
   highest local-file-header survival) and splice each entry's byte range
   out of the first copy that reproduces its recorded CRC-32; when no
   single copy survives an entry whole, try to piece it together across
   the 64 KiB corruption boundaries from several copies (*crossover*);
4. when every entry is recovered and the ranges tile the whole file, the
   output is byte-identical to the original (``guarantee="full"``);
   otherwise the gaps are filled from the target and the result is
   ``"partial"``. When some entry survives in no copy, the recovered
   entries are handed to the ordinary rebuild pipeline
   (:func:`pptrepair.rebuild.rebuild_package`) instead.

When no copy carries a usable central directory a *degraded* mode takes
over: the union of every CRC-valid local-file-header entry across the
copies is rebuilt into a fresh package, capped at ``"partial"`` since no
reference layout exists to guarantee byte identity.

A ``lineage``-tier source -- a *different saved version* of the same
document rather than a copy of the same save -- cannot be spliced (its
byte layout differs), but is used, on request (*allow_lineage*), as an
entry-level *donor* for any entry no copy could supply. Donor rescue
runs in two passes. First, when the reference central directory records
an entry's CRC-32 (the oracle), a donor's same-named entry is adopted as
verified (``method="donor"``) only when its recorded
``(CRC-32, uncompressed size)`` matches and its bytes reproduce that
CRC-32, so the content is provably the original; a donor's edited entry
is never adopted this verified way. Second, an entry still missing after
that pass -- typically plumbing XML (``ppt/presentation.xml``,
``[Content_Types].xml``, ``*.rels``) whose CRC-32 necessarily changes
between saved versions, so no oracle match was possible -- is adopted
from a donor on the donor's own CRC-32 alone
(``method="donor_unverified"``). Degraded mode, lacking any oracle,
adopts every donor entry this same unverified way. Adopting even one
``donor_unverified`` entry caps the run at the ``"hybrid"`` guarantee --
structurally rebuilt and part-verified, but not proven byte-identical to
the original.

Same-origin scoring only *selects* candidates and donors; the safety of
every adopted byte range is decided by its per-entry CRC-32, so
loosening the score thresholds never risks adopting foreign bytes. Every
source file is opened read-only.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import struct
import tempfile
import zipfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pptrepair.census import EntryResult, categorize, from_central_directory
from pptrepair.classify import Diagnosis
from pptrepair.integrity import (inspect_references, inspect_structure,
                                 inspect_timing)
from pptrepair.origin import OriginScore, score_origin
from pptrepair.rebuild import RebuildError, rebuild_package
from pptrepair.repair import OutputExistsError
from pptrepair.salvage import SalvagedEntry, SalvageReader
from pptrepair.scan import diagnose_file

#: Default output-file suffix appended to the target's stem.
MERGE_SUFFIX = ".merged.pptx"

#: Local file header signature (``PK\x03\x04``) and its fixed 30-byte
#: layout (signature, version-needed, flags, method, mod-time, mod-date,
#: CRC-32, compressed size, uncompressed size, name length, extra length).
_LFH_SIG = b"PK\x03\x04"
_LFH_STRUCT = "<IHHHHHIIIHH"
_LFH_FIXED_SIZE = 30

#: End-of-central-directory signature (``PK\x05\x06``) and its fixed
#: 22-byte layout; the central-directory start offset is field index 6.
_EOCD_SIG = b"PK\x05\x06"
_EOCD_STRUCT = "<IHHHHIIH"
_EOCD_FIXED_SIZE = 22

#: Compression methods the splice understands (store and raw deflate).
_METHOD_STORED = 0
_METHOD_DEFLATE = 8

#: 32-bit sentinel a standard EOCD uses to defer the real value to a
#: Zip64 record; treated here as an unusable (untrustworthy) offset.
_ZIP64_SENTINEL = 0xFFFFFFFF

#: 64 KiB: the empirically observed granularity of OneDrive corruption,
#: used as the file-absolute alignment at which a crossover splice may
#: switch which copy an entry's bytes are taken from.
CROSSOVER_ALIGN = 65536

#: Per-entry cap on the number of copy-switch combinations a crossover
#: search will try before giving up.
MAX_CROSSOVER_ATTEMPTS = 4096


@dataclass
class EntryProvenance:
    """Where one archive entry's recovered bytes came from."""

    name: str
    source: Path | None
    """The copy or donor the entry was adopted from, or None when it
    could not be recovered from any source. For a ``crossover`` adoption
    this is the copy that supplied the first segment (``sources[0]``); for
    a ``donor`` / ``donor_unverified`` adoption it is the donor file."""
    method: str
    """How the entry was recovered: ``"direct"`` (one copy's byte range),
    ``"crossover"`` (a range spliced across copies), ``"donor"`` (a
    lineage donor entry proven original against the reference oracle),
    ``"donor_unverified"`` (a lineage donor entry trusted on the donor's
    own CRC-32 alone -- in splice mode for a still-missing entry the
    oracle could not match, such as plumbing XML, and always in degraded
    mode), or ``"missing"`` (recovered from no source)."""
    sources: tuple[Path, ...] | None = None
    """For a ``crossover`` adoption, the copy each spliced segment was
    taken from, in order; None for every other method (a ``donor`` entry
    comes whole from ``source``)."""


@dataclass
class MergeOutcome:
    """Language-neutral result of one :func:`merge_restore` run."""

    output_path: Path | None
    guarantee: str  # "full" | "partial" | "hybrid" | "failed"
    """``"hybrid"`` marks a rebuilt output into which at least one
    ``donor_unverified`` entry was adopted -- part-verified against the
    donor's own CRC-32 but not proven byte-identical to the original,
    whether because the reference oracle's CRC-32 could not be matched
    (plumbing XML that always changes between versions) or because no
    reference oracle survived at all (degraded mode)."""
    provenances: list[EntryProvenance]
    scores: list[OriginScore]
    """Same-origin score of each scored non-target source, in source
    order (sources that could not be diagnosed carry no score)."""
    notes: list[str]
    """Factual English notes: excluded sources, gaps, degradations, etc."""


def merge_restore(
        sources: list[Path], output: Path | None = None, *,
        force: bool = False, allow_candidate: bool = False,
        allow_lineage: bool = False, lang: str = "en") -> MergeOutcome:
    """Reconstruct the original file from several same-origin *sources*.

    The first source is the *target*; every other is compared against it
    for a shared origin and kept only when the comparison trusts it as a
    byte-identical copy (``auto`` tier, plus ``candidate`` when
    *allow_candidate*). Recovery then byte-splices each archive entry from
    the first copy whose bytes reproduce the entry's recorded CRC-32; see
    the module docstring for the full pipeline and the ``full`` /
    ``partial`` / ``failed`` guarantee levels.

    When *allow_lineage* is set, ``lineage``-tier sources (different saved
    versions of the same document) are used as entry-level donors for any
    entry no copy could supply, in two passes: with the reference central
    directory as an oracle a donor entry is first adopted as verified
    (``method="donor"``, guarantee unchanged) only when it reproduces the
    recorded CRC-32; an entry still missing afterwards -- plumbing XML
    whose CRC-32 always differs between versions -- is then adopted on the
    donor's own CRC-32 alone (``method="donor_unverified"``), which
    downgrades the run to ``"hybrid"``. Degraded mode adopts every donor
    entry this second, unverified way. Without the flag a ``lineage``-tier
    source is only noted as unused.

    *output* defaults to ``<target-stem>.merged.pptx`` next to the target;
    an existing path raises :class:`pptrepair.repair.OutputExistsError`
    unless *force*. A ``failed`` run leaves no file behind. *lang* is
    accepted for signature parity with the CLI stage and otherwise unused.

    :raises ValueError: when fewer than two sources are given, or any
        source is not an existing file.
    :raises pptrepair.repair.OutputExistsError: when the output path
        exists and *force* is false.
    """
    if len(sources) < 2:
        raise ValueError("merge_restore requires at least two source files")
    for src in sources:
        if not src.is_file():
            raise ValueError(f"source is not an existing file: {src}")

    target = sources[0]
    output_path = (output if output is not None
                   else target.with_name(target.stem + MERGE_SUFFIX))
    if output_path.exists() and not force:
        raise OutputExistsError(f"output already exists: {output_path}")

    notes: list[str] = []
    scores: list[OriginScore] = []
    target_diag, target_error = diagnose_file(target)
    if target_diag is None:
        notes.append(
            f"target {target.name} could not be diagnosed: {target_error}")

    usable, donor_specs = _select_usable_sources(
        sources[1:], target, target_diag, allow_candidate, allow_lineage,
        scores, notes)

    copies: list[tuple[Path, Diagnosis]] = []
    if target_diag is not None:
        copies.append((target, target_diag))
    copies.extend(usable)
    if not copies:
        notes.append("no usable copy remained after diagnosis and scoring")
        return MergeOutcome(None, "failed", [], scores, notes)

    # Every copy is read whole into memory: the splice writes an output
    # of the common file size and reads entry ranges out of each copy.
    copy_bytes = {path: path.read_bytes() for path, _diag in copies}
    _note_identical_copies(copies, copy_bytes, notes)

    copy_paths = [path for path, _diag in copies]
    cd_copies = [(path, diag) for path, diag in copies
                 if diag.cd_census is not None
                 and diag.cd_census.method == "central_directory"]

    result: tuple[Path | None, str, list[EntryProvenance]] | None = None
    if cd_copies:
        # Prefer the readable-CD copy whose local file headers survived
        # best, since its recorded layout is the most trustworthy blueprint.
        ref_path, _ref_diag = max(
            cd_copies, key=lambda item: _lfh_survival(item[1]))
        result = _run_splice(
            ref_path, copy_bytes[ref_path], copy_paths, copy_bytes,
            target, output_path, donor_specs, notes)
    if result is None:
        result = _run_degraded(
            copies, copy_bytes, donor_specs, output_path, notes)

    out_path, guarantee, provenances = result
    return MergeOutcome(out_path, guarantee, provenances, scores, notes)


def _select_usable_sources(
        others: list[Path], target: Path, target_diag: Diagnosis | None,
        allow_candidate: bool, allow_lineage: bool, scores: list[OriginScore],
        notes: list[str]
) -> tuple[list[tuple[Path, Diagnosis]],
           list[tuple[Path, Diagnosis, OriginScore]]]:
    """Diagnose and score each non-target source, sorting them by role.

    Appends one score per successfully diagnosed source to *scores* (in
    source order) and one explanatory line to *notes* for every source
    excluded or held back. Returns ``(copies, donors)``:

    * *copies* are the ``(path, diagnosis)`` pairs cleared for byte
      splicing -- ``auto`` tier always, ``candidate`` only when
      *allow_candidate*;
    * *donors* are the ``(path, diagnosis, score)`` triples of
      ``lineage``-tier sources kept as entry-level donors, only when
      *allow_lineage* is set (otherwise each is noted as unused).
    """
    copies: list[tuple[Path, Diagnosis]] = []
    donors: list[tuple[Path, Diagnosis, OriginScore]] = []
    for src in others:
        diag, error = diagnose_file(src)
        if diag is None:
            notes.append(
                f"source {src.name} excluded: could not be diagnosed "
                f"({error})")
            continue
        if target_diag is None:
            notes.append(
                f"source {src.name} not scored: target diagnosis unavailable")
            continue
        score = score_origin(target_diag, diag)
        scores.append(score)
        if score.tier == "auto":
            copies.append((src, diag))
        elif score.tier == "candidate":
            if allow_candidate:
                copies.append((src, diag))
            else:
                notes.append(
                    f"candidate-tier source {src.name} not used "
                    "(pass allow_candidate to include it)")
        elif score.tier == "lineage":
            if allow_lineage:
                donors.append((src, diag, score))
            else:
                notes.append(
                    f"lineage-tier source {src.name} not used "
                    "(pass allow_lineage to include it)")
        else:
            notes.append(
                f"rejected-tier source {src.name} not used (not same origin)")
    return copies, donors


def _lfh_survival(diag: Diagnosis) -> float:
    """Return the fraction of *diag*'s local-file-header entries intact.

    Zero when no local-file-header census exists or it saw no entries.
    """
    lfh = diag.lfh_census
    if lfh is None or lfh.total() == 0:
        return 0.0
    return lfh.ok_count() / lfh.total()


def _note_identical_copies(copies: list[tuple[Path, Diagnosis]],
                           copy_bytes: dict[Path, bytes],
                           notes: list[str]) -> None:
    """Record copies that are byte-for-byte identical (no merge gain).

    Copies are grouped by size first (a cheap disqualifier), and only the
    same-size ones are hashed, so a matching size is confirmed before any
    SHA-256 work is done.
    """
    by_size: dict[int, list[Path]] = {}
    for path, _diag in copies:
        by_size.setdefault(len(copy_bytes[path]), []).append(path)
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_hash: dict[str, list[Path]] = {}
        for path in same_size:
            digest = hashlib.sha256(copy_bytes[path]).hexdigest()
            by_hash.setdefault(digest, []).append(path)
        for paths in by_hash.values():
            if len(paths) > 1:
                names = ", ".join(path.name for path in paths)
                notes.append(f"identical copies: no merge gain from {names}")


class _DonorSource:
    """Enumerate and read the verified entries of one lineage donor.

    A lineage donor is a *different saved version* of the same document:
    its byte layout differs, so it cannot be spliced, but an entry it
    still shares unchanged with the original is a usable part supply. This
    thin wrapper exposes only what the donor-rescue stages need -- the
    donor's recorded per-entry ``(CRC-32, uncompressed size)`` and a
    CRC-verified read of one entry's payload -- reading from a readable
    central directory when the donor has one and from its CRC-valid
    local-file-header scan otherwise. The two-method surface is
    deliberately minimal so a later in-archive donor (a version kept
    inside a ``.zip``/``.tar``) can be dropped in behind it unchanged.
    """

    def __init__(self, path: Path, diagnosis: Diagnosis, data: bytes) -> None:
        self.path = path
        self._data = data
        # Prefer the central directory: it records every indexed entry's
        # CRC-32 and uncompressed size directly. Fall back to the
        # CRC-valid local-file-header scan when no central directory
        # survives (a degraded donor).
        self._use_cd = (
            diagnosis.cd_census is not None
            and diagnosis.cd_census.method == "central_directory")
        self._lfh_by_name: dict[str, EntryResult] = {}
        if not self._use_cd and diagnosis.lfh_census is not None:
            for entry in diagnosis.lfh_census.entries:
                if (entry.ok and entry.crc is not None
                        and entry.comp_size is not None
                        and entry.name not in self._lfh_by_name):
                    self._lfh_by_name[entry.name] = entry

    def recorded(self) -> dict[str, tuple[int | None, int | None]]:
        """Return ``{name: (recorded crc, recorded uncompressed size)}``.

        Taken from the central directory when the donor has a readable
        one, otherwise from its CRC-valid local-file-header entries. These
        are the values the donor *records*, not proof its data is intact
        -- that is confirmed by :meth:`read`.
        """
        recorded: dict[str, tuple[int | None, int | None]] = {}
        if self._use_cd:
            try:
                with zipfile.ZipFile(io.BytesIO(self._data)) as archive:
                    for info in archive.infolist():
                        recorded[info.filename] = (
                            info.CRC & 0xFFFFFFFF, info.file_size)
            except Exception:
                return {}
            return recorded
        for name, entry in self._lfh_by_name.items():
            recorded[name] = (entry.crc, entry.file_size)
        return recorded

    def read(self, name: str) -> bytes | None:
        """Return *name*'s payload when it passes the donor's own CRC-32.

        None when the entry is absent, unreadable, or fails its recorded
        CRC-32. The central-directory path leans on :mod:`zipfile`, which
        raises on a CRC mismatch; the scan path re-reads the local file
        header at its recorded offset via :func:`_read_lfh_entry`.
        """
        if self._use_cd:
            try:
                with zipfile.ZipFile(io.BytesIO(self._data)) as archive:
                    return archive.read(name)
            except Exception:
                return None
        entry = self._lfh_by_name.get(name)
        if entry is None or entry.comp_size is None or entry.crc is None:
            return None
        recovered = _read_lfh_entry(
            self._data, entry.header_offset, entry.comp_size, entry.crc)
        if recovered is None:
            return None
        return recovered[1]


def _build_donors(
        donor_specs: list[tuple[Path, Diagnosis, OriginScore]]
) -> list[_DonorSource]:
    """Read each lineage donor and wrap it, in priority order.

    Donors are ordered by descending lineage score, ties broken by
    descending media ratio, so the closest version is tried first. Each
    donor's bytes are read here rather than up front, so a full splice
    (which never reaches a rescue stage) pays no donor read cost.
    """
    ordered = sorted(
        donor_specs,
        key=lambda spec: (spec[2].lineage_score, spec[2].media_ratio),
        reverse=True)
    return [_DonorSource(path, diag, path.read_bytes())
            for path, diag, _score in ordered]


@dataclass
class _RefEntry:
    """One entry as recorded by the reference central directory."""

    name: str
    header_offset: int
    comp_size: int
    crc: int
    compress_type: int
    file_size: int


def _run_splice(
        ref_path: Path, ref_bytes: bytes, copy_paths: list[Path],
        copy_bytes: dict[Path, bytes], target: Path, output_path: Path,
        donor_specs: list[tuple[Path, Diagnosis, OriginScore]],
        notes: list[str]
) -> tuple[Path | None, str, list[EntryProvenance]] | None:
    """Splice each entry's byte range from the first CRC-valid copy.

    Any entry no copy can supply is offered to the lineage donors in
    *donor_specs* before the rebuild fallback (see
    :func:`_rescue_missing_via_donors`). Returns
    ``(output_path, guarantee, provenances)`` on success, or None to
    signal that the reference central directory is untrustworthy and the
    caller should fall back to the degraded (LFH-union) mode.
    """
    layout = _reference_layout(ref_path, ref_bytes)
    if layout is None:
        notes.append(
            f"reference central directory in {ref_path.name} is not "
            "usable; falling back to degraded mode")
        return None
    entries, intervals, cd_start = layout

    provenances: list[EntryProvenance] = []
    # name -> (interval, raw interval bytes, decompressed payload)
    adopted: dict[str, tuple[tuple[int, int], bytes, bytes]] = {}
    for entry, interval in zip(entries, intervals):
        picked = _adopt_entry(entry, interval, copy_paths, copy_bytes)
        if picked is not None:
            source, segment, raw = picked
            adopted[entry.name] = (interval, segment, raw)
            provenances.append(EntryProvenance(entry.name, source, "direct"))
            continue
        # No single copy survives the entry whole: try switching copies
        # at the 64 KiB corruption boundaries inside its range.
        crossed = _crossover_entry(entry, interval, copy_paths, copy_bytes)
        if crossed is not None:
            sources, segment, raw = crossed
            adopted[entry.name] = (interval, segment, raw)
            provenances.append(
                EntryProvenance(entry.name, sources[0], "crossover",
                                sources=sources))
            continue
        if _crossover_capped(interval, copy_paths, copy_bytes):
            notes.append(
                f"crossover search for {entry.name} hit the attempt cap "
                f"({MAX_CROSSOVER_ATTEMPTS})")
        provenances.append(EntryProvenance(entry.name, None, "missing"))

    if any(prov.method == "missing" for prov in provenances):
        # Some entry survived in no copy. Before the rebuild fallback, try
        # to rescue the still-missing entries from lineage donors in two
        # passes. First pass (oracle-checked): the reference central
        # directory records each entry's CRC-32, so a donor's same-named
        # entry is adopted as verified only when it reproduces that exact
        # value (a donor-side edit is never adopted this way).
        donor_payloads = _rescue_missing_via_donors(
            entries, provenances, donor_specs)
        # Second pass (unverified): an entry still missing -- typically
        # plumbing XML whose CRC-32 necessarily differs between saved
        # versions, so no oracle match was possible -- is adopted from a
        # donor on the donor's own CRC-32 alone, which caps the run at the
        # ``"hybrid"`` guarantee below.
        donor_payloads.update(
            _rescue_missing_unverified(entries, provenances, donor_specs))
        # Hand every recovered entry -- spliced or donor-supplied -- to the
        # ordinary rebuild pipeline, which prunes the dangling references.
        items: list[tuple[str, bytes, int]] = []
        for entry in entries:
            if entry.name in adopted:
                items.append(
                    (entry.name, adopted[entry.name][2], entry.compress_type))
            elif entry.name in donor_payloads:
                items.append(
                    (entry.name, donor_payloads[entry.name],
                     entry.compress_type))
        guarantee, out = _rebuild_from_entries(items, output_path, notes)
        if guarantee == "partial" and any(
                prov.method == "donor_unverified" for prov in provenances):
            # An unverified donor adoption mirrors the degraded path:
            # structurally rebuilt and part-verified, but not proven
            # byte-identical to the original.
            guarantee = "hybrid"
        return out, guarantee, provenances

    return _write_full_output(
        ref_bytes, cd_start, entries, intervals, adopted, target,
        copy_bytes, output_path, provenances, notes)


def _rescue_missing_via_donors(
        entries: list[_RefEntry], provenances: list[EntryProvenance],
        donor_specs: list[tuple[Path, Diagnosis, OriginScore]]
) -> dict[str, bytes]:
    """Adopt still-missing entries from lineage donors, oracle-checked.

    For every entry the splice left missing, each donor (in priority
    order) is asked for a same-named entry whose recorded
    ``(CRC-32, uncompressed size)`` equals the reference central
    directory's; its payload is then read and re-verified against that
    CRC-32 before being accepted (``method="donor"``). A name-only match
    with a different CRC-32 is a donor-side edit and is never adopted here
    as ``"donor"``; the second pass (:func:`_rescue_missing_unverified`)
    may still adopt it, explicitly, as ``"donor_unverified"``. Mutates the
    matching provenances in place (first donor wins) and returns
    ``{name: payload}`` for each rescued entry.
    """
    missing_names = {prov.name for prov in provenances
                     if prov.method == "missing"}
    if not missing_names or not donor_specs:
        return {}
    # Donors are read only now that a rescue is actually needed.
    donors = [(donor, donor.recorded())
              for donor in _build_donors(donor_specs)]
    prov_by_name = {prov.name: prov for prov in provenances}
    rescued: dict[str, bytes] = {}
    for entry in entries:
        if entry.name not in missing_names:
            continue
        for donor, recorded in donors:
            pair = recorded.get(entry.name)
            if pair is None or pair != (entry.crc, entry.file_size):
                continue
            payload = donor.read(entry.name)
            if payload is None:
                continue
            # Belt-and-braces: confirm the read bytes against the oracle
            # CRC-32 even though the donor already CRC-checked them.
            if (zlib.crc32(payload) & 0xFFFFFFFF) != entry.crc:
                continue
            prov = prov_by_name[entry.name]
            prov.method = "donor"
            prov.source = donor.path
            rescued[entry.name] = payload
            break
    return rescued


def _rescue_missing_unverified(
        entries: list[_RefEntry], provenances: list[EntryProvenance],
        donor_specs: list[tuple[Path, Diagnosis, OriginScore]]
) -> dict[str, bytes]:
    """Adopt still-missing entries from donors on the donor's CRC alone.

    Runs after :func:`_rescue_missing_via_donors`, over the entries the
    oracle-checked first pass still left missing -- typically the plumbing
    XML (``ppt/presentation.xml``, ``[Content_Types].xml``, ``*.rels``)
    whose recorded CRC-32 necessarily changes between saved versions, so
    no ``(CRC-32, uncompressed size)`` match with the reference central
    directory was possible. For each, the donors (in priority order) are
    asked for a same-named entry, adopted on the donor's own CRC-32 alone
    -- the same verification level as the degraded path's
    :func:`_rescue_union_via_donors` -- as ``method="donor_unverified"``
    (first donor wins). The content is explicitly *not* proven identical
    to the original, so adopting even one such entry caps the run at the
    ``"hybrid"`` guarantee; a donor that lacks the entry leaves it
    missing. Mutates the matching provenances in place and returns
    ``{name: payload}`` for each rescued entry.
    """
    missing_names = {prov.name for prov in provenances
                     if prov.method == "missing"}
    if not missing_names or not donor_specs:
        return {}
    donors = _build_donors(donor_specs)
    prov_by_name = {prov.name: prov for prov in provenances}
    rescued: dict[str, bytes] = {}
    for entry in entries:
        if entry.name not in missing_names:
            continue
        for donor in donors:
            payload = donor.read(entry.name)
            if payload is None:
                continue
            prov = prov_by_name[entry.name]
            prov.method = "donor_unverified"
            prov.source = donor.path
            rescued[entry.name] = payload
            break
    return rescued


def _reference_layout(
        ref_path: Path,
        ref_bytes: bytes) -> tuple[list[_RefEntry], list[tuple[int, int]],
                                   int] | None:
    """Read the reference CD entries, their byte ranges and the CD start.

    Entries are ordered by header offset and each range is
    ``[offset_i, offset_{i+1})`` (the last ending at the central
    directory). Returns None when the central directory cannot be read or
    its offsets are inconsistent (duplicated, out of order, or reaching
    past the central directory), which makes byte-splicing unsafe.
    """
    try:
        with zipfile.ZipFile(ref_path) as archive:
            infos = list(archive.infolist())
    except Exception:
        return None
    if not infos:
        return None
    entries = [
        _RefEntry(info.filename, info.header_offset, info.compress_size,
                  info.CRC & 0xFFFFFFFF, info.compress_type, info.file_size)
        for info in infos
    ]
    entries.sort(key=lambda entry: entry.header_offset)

    cd_start = _find_cd_start(ref_bytes)
    if cd_start is None:
        return None

    intervals: list[tuple[int, int]] = []
    for index, entry in enumerate(entries):
        start = entry.header_offset
        end = (entries[index + 1].header_offset if index + 1 < len(entries)
               else cd_start)
        if start < 0 or start >= cd_start or end <= start or end > cd_start:
            return None
        intervals.append((start, end))
    return entries, intervals, cd_start


def _find_cd_start(data: bytes) -> int | None:
    """Return the central-directory start offset from *data*'s EOCD.

    The record is located by searching for ``PK\\x05\\x06`` from the end.
    Returns None when no readable EOCD is present or it defers to a Zip64
    record (a ``0xFFFFFFFF`` offset), both of which make the layout
    unusable for splicing.
    """
    index = data.rfind(_EOCD_SIG)
    if index == -1 or index + _EOCD_FIXED_SIZE > len(data):
        return None
    fields = struct.unpack(
        _EOCD_STRUCT, data[index:index + _EOCD_FIXED_SIZE])
    cd_offset = fields[6]
    if cd_offset == _ZIP64_SENTINEL or cd_offset > len(data):
        return None
    return cd_offset


def _adopt_entry(entry: _RefEntry, interval: tuple[int, int],
                 copy_paths: list[Path],
                 copy_bytes: dict[Path, bytes]
                 ) -> tuple[Path, bytes, bytes] | None:
    """Return the first copy whose bytes reproduce *entry*'s CRC-32.

    Tries the copies in the given order (target first). For each, the
    entry's byte range is validated as a local file header naming
    *entry* whose compressed payload decompresses to the recorded CRC-32.
    Returns ``(source, range bytes, decompressed payload)`` on success --
    the whole range is adopted verbatim, since same-origin copies share
    an identical layout -- or None when no copy qualifies.
    """
    start, end = interval
    for path in copy_paths:
        data = copy_bytes[path]
        if end > len(data):
            continue
        segment = data[start:end]
        raw = _validate_entry_segment(segment, entry)
        if raw is not None:
            return path, segment, raw
    return None


def _validate_entry_segment(segment: bytes,
                            entry: _RefEntry) -> bytes | None:
    """Return *segment*'s payload when it is a whole, valid copy of *entry*.

    *segment* must begin with a local file header naming *entry* whose
    compressed payload decompresses to the CRC-32 the reference central
    directory recorded. Returns the decompressed payload on success, or
    None when the header, the compressed length, the decompression or the
    CRC-32 does not match. Shared by :func:`_adopt_entry` (a single-copy
    range) and :func:`_crossover_entry` (a range spliced across copies).
    """
    parsed = _parse_lfh(segment, 0)
    if parsed is None:
        return None
    _method, name, name_len, extra_len = parsed
    if name != entry.name:
        return None
    data_offset = _LFH_FIXED_SIZE + name_len + extra_len
    comp = segment[data_offset:data_offset + entry.comp_size]
    if len(comp) < entry.comp_size:
        return None
    raw = _safe_decompress(comp, entry.compress_type)
    if raw is None:
        return None
    if (zlib.crc32(raw) & 0xFFFFFFFF) != entry.crc:
        return None
    return raw


def _crossover_entry(
        entry: _RefEntry, interval: tuple[int, int],
        copy_paths: list[Path], copy_bytes: dict[Path, bytes]
) -> tuple[tuple[Path, ...], bytes, bytes] | None:
    """Recover *entry* by switching copies at 64 KiB corruption boundaries.

    Used when :func:`_adopt_entry` found no single copy that reproduces
    *entry* verbatim. The range is split at the file-absolute 64 KiB
    boundaries inside it (corruption is aligned to absolute file
    positions), and the copies are switched at those boundaries -- at most
    twice, i.e. up to three consecutive runs -- until a spliced
    reconstruction reproduces the recorded CRC-32. Candidates are tried
    deterministically (one switch before two; switch positions ascending;
    each run's copy in *copy_paths* order; adjacent runs always from
    different copies) and the search stops after
    :data:`MAX_CROSSOVER_ATTEMPTS` combinations.

    Returns ``(sources, spliced range bytes, decompressed payload)`` on
    success, where *sources* names the copy each run was taken from in
    order; None when no combination qualifies, the attempt cap was
    reached, or the range cannot be crossed over (fewer than two copies
    reach its end, or it holds no interior 64 KiB boundary).
    """
    start, end = interval
    candidates = [path for path in copy_paths
                  if len(copy_bytes[path]) >= end]
    if len(candidates) < 2:
        return None
    boundaries = _crossover_boundaries(start, end)
    if not boundaries:
        return None
    split_points = [start, *boundaries, end]

    attempts = 0
    for cuts, run_copies in _iter_crossover_assignments(
            len(boundaries), candidates):
        if attempts >= MAX_CROSSOVER_ATTEMPTS:
            return None
        attempts += 1
        # Concatenate each run's byte range from the copy assigned to it.
        segment = b"".join(
            copy_bytes[run_copies[run]][
                split_points[cuts[run]]:split_points[cuts[run + 1]]]
            for run in range(len(run_copies)))
        raw = _validate_entry_segment(segment, entry)
        if raw is not None:
            return tuple(run_copies), segment, raw
    return None


def _crossover_boundaries(start: int, end: int) -> list[int]:
    """Return the 64 KiB-aligned offsets strictly inside ``[start, end)``.

    The split points are multiples of :data:`CROSSOVER_ALIGN`
    (``k * 65536``) that fall strictly between *start* and *end*. An empty
    list means the range cannot be crossed over.
    """
    first = (start // CROSSOVER_ALIGN + 1) * CROSSOVER_ALIGN
    return list(range(first, end, CROSSOVER_ALIGN))


def _iter_crossover_assignments(
        num_boundaries: int, candidates: list[Path]
) -> Iterator[tuple[tuple[int, ...], tuple[Path, ...]]]:
    """Yield every crossover run assignment in deterministic order.

    An assignment is ``(cuts, run_copies)``: *cuts* are the split-point
    indices bounding each run (``0`` and ``num_boundaries + 1`` always the
    outer ends, the switch positions in between), and *run_copies* names
    the copy supplying each run. Assignments are produced with one switch
    before two, switch positions ascending, run copies in *candidates*
    order, and adjacent runs always from different copies.
    """
    for num_switches in (1, 2):
        for positions in itertools.combinations(
                range(1, num_boundaries + 1), num_switches):
            cuts = (0, *positions, num_boundaries + 1)
            for run_copies in itertools.product(
                    candidates, repeat=num_switches + 1):
                if all(run_copies[i] != run_copies[i + 1]
                       for i in range(num_switches)):
                    yield cuts, run_copies


def _crossover_capped(interval: tuple[int, int], copy_paths: list[Path],
                      copy_bytes: dict[Path, bytes]) -> bool:
    """Return True when *interval*'s crossover search space exceeds the cap.

    The combinations counted here are exactly the ones
    :func:`_crossover_entry` enumerates, so a True result means that
    function stopped early at :data:`MAX_CROSSOVER_ATTEMPTS` rather than
    ruling every combination out. The generator only builds cheap index
    tuples, so counting past the cap costs no byte copying.
    """
    start, end = interval
    candidates = [path for path in copy_paths
                  if len(copy_bytes[path]) >= end]
    boundaries = _crossover_boundaries(start, end)
    if len(candidates) < 2 or not boundaries:
        return False
    count = 0
    for _assignment in _iter_crossover_assignments(len(boundaries),
                                                   candidates):
        count += 1
        if count > MAX_CROSSOVER_ATTEMPTS:
            return True
    return False


def _parse_lfh(data: bytes, offset: int) -> tuple[int, str, int, int] | None:
    """Parse the local file header at *offset* in *data*.

    Returns ``(method, name, name_length, extra_length)``, or None when
    the header is truncated, lacks the ``PK\\x03\\x04`` signature or names
    a non-UTF-8 entry.
    """
    if offset < 0 or offset + _LFH_FIXED_SIZE > len(data):
        return None
    header = data[offset:offset + _LFH_FIXED_SIZE]
    if header[:len(_LFH_SIG)] != _LFH_SIG:
        return None
    fields = struct.unpack(_LFH_STRUCT, header)
    method = fields[3]
    name_len = fields[9]
    extra_len = fields[10]
    name_bytes = data[offset + _LFH_FIXED_SIZE:
                      offset + _LFH_FIXED_SIZE + name_len]
    if len(name_bytes) < name_len:
        return None
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return method, name, name_len, extra_len


def _safe_decompress(comp: bytes, method: int) -> bytes | None:
    """Decompress *comp* under *method* (store/deflate), or None on error.

    An unsupported method or a broken deflate stream both yield None so
    the caller simply moves on to the next copy.
    """
    if method == _METHOD_STORED:
        return comp
    if method == _METHOD_DEFLATE:
        try:
            decompressor = zlib.decompressobj(-15)
            out = decompressor.decompress(comp)
            out += decompressor.flush()
            return out
        except zlib.error:
            return None
    return None


def _write_full_output(
        ref_bytes: bytes, cd_start: int, entries: list[_RefEntry],
        intervals: list[tuple[int, int]],
        adopted: dict[str, tuple[tuple[int, int], bytes, bytes]],
        target: Path, copy_bytes: dict[Path, bytes], output_path: Path,
        provenances: list[EntryProvenance],
        notes: list[str]) -> tuple[Path, str, list[EntryProvenance]]:
    """Assemble the spliced output when every entry was recovered.

    Each adopted byte range is written at its offset, the central
    directory is copied from the reference, and any byte not covered by a
    range or the CD is filled from the target (downgrading the guarantee
    to ``partial``). A final :meth:`zipfile.ZipFile.testzip` self-check
    downgrades to ``partial`` too if it does not pass.
    """
    size = len(ref_bytes)
    buffer = bytearray(size)
    covered: list[tuple[int, int]] = []
    for entry, interval in zip(entries, intervals):
        start, end = interval
        _iv, segment, _raw = adopted[entry.name]
        buffer[start:end] = segment
        covered.append((start, end))
    buffer[cd_start:size] = ref_bytes[cd_start:size]
    covered.append((cd_start, size))

    guarantee = "full"
    gaps = _find_gaps(covered, size)
    if gaps:
        filler = copy_bytes.get(target, ref_bytes)
        for gap_start, gap_end in gaps:
            chunk = filler[gap_start:gap_end]
            buffer[gap_start:gap_start + len(chunk)] = chunk
            notes.append(
                f"gap [{gap_start}, {gap_end}) filled from {target.name}")
        guarantee = "partial"

    output_path.write_bytes(bytes(buffer))
    if not _selfcheck_zip(output_path):
        notes.append(
            "self-check failed: merged archive did not pass testzip(); "
            "downgraded to partial")
        guarantee = "partial"
    return output_path, guarantee, provenances


def _find_gaps(covered: list[tuple[int, int]], size: int
               ) -> list[tuple[int, int]]:
    """Return the ``[0, size)`` sub-ranges left uncovered by *covered*."""
    gaps: list[tuple[int, int]] = []
    position = 0
    for start, end in sorted(covered):
        if start > position:
            gaps.append((position, start))
        position = max(position, end)
    if position < size:
        gaps.append((position, size))
    return gaps


def _selfcheck_zip(path: Path) -> bool:
    """Return True when *path* opens and every entry passes ``testzip``."""
    try:
        with zipfile.ZipFile(path) as archive:
            return archive.testzip() is None
    except Exception:
        return False


def _run_degraded(copies: list[tuple[Path, Diagnosis]],
                  copy_bytes: dict[Path, bytes],
                  donor_specs: list[tuple[Path, Diagnosis, OriginScore]],
                  output_path: Path, notes: list[str]
                  ) -> tuple[Path | None, str, list[EntryProvenance]]:
    """Rebuild from the union of CRC-valid LFH entries across the copies.

    Used when no copy carries a readable central directory. The first
    CRC-valid copy of each entry name wins (target first). Lineage donors
    then supply any name the union lacks; with no reference layout there
    is no oracle to prove a donor entry equals the original, so each is
    adopted on the donor's own CRC-32 alone (``method="donor_unverified"``)
    and lifts the guarantee from ``partial`` to ``hybrid``. The union is
    capped at ``partial`` for the same lack of a reference layout.
    """
    notes.append(
        "no copy has a usable central directory; using degraded "
        "(local-file-header union) mode")
    union: dict[str, tuple[bytes, int]] = {}
    order: list[str] = []
    provenances: list[EntryProvenance] = []
    for path, diag in copies:
        lfh = diag.lfh_census
        if lfh is None:
            continue
        data = copy_bytes[path]
        for entry in lfh.entries:
            if not entry.ok or entry.name in union:
                continue
            if entry.comp_size is None or entry.crc is None:
                continue
            recovered = _read_lfh_entry(
                data, entry.header_offset, entry.comp_size, entry.crc)
            if recovered is None:
                continue
            method, raw = recovered
            union[entry.name] = (raw, method)
            order.append(entry.name)
            provenances.append(EntryProvenance(entry.name, path, "direct"))

    unverified = _rescue_union_via_donors(
        union, order, provenances, donor_specs)

    items = [(name, union[name][0], union[name][1]) for name in order]
    guarantee, out = _rebuild_from_entries(items, output_path, notes)
    if guarantee == "partial" and unverified:
        guarantee = "hybrid"
    return out, guarantee, provenances


def _rescue_union_via_donors(
        union: dict[str, tuple[bytes, int]], order: list[str],
        provenances: list[EntryProvenance],
        donor_specs: list[tuple[Path, Diagnosis, OriginScore]]) -> bool:
    """Add donor entries absent from the degraded *union*, unverified.

    Without a reference central directory the set of *lost* names is
    itself unknown, so every donor entry not already in the union is
    tried (first donor wins); the payload is trusted on the donor's own
    CRC-32 via :meth:`_DonorSource.read`. Any resulting over-inclusion is
    harmless -- the rebuild's dangling-reference cleanup prunes parts the
    package does not reference. Mutates *union*, *order* and *provenances*
    in place and returns True when at least one donor entry was adopted.
    """
    if not donor_specs:
        return False
    unverified = False
    for donor in _build_donors(donor_specs):
        for name in donor.recorded():
            if name in union:
                continue
            payload = donor.read(name)
            if payload is None:
                continue
            union[name] = (payload, _METHOD_DEFLATE)
            order.append(name)
            provenances.append(
                EntryProvenance(name, donor.path, "donor_unverified"))
            unverified = True
    return unverified


def _read_lfh_entry(data: bytes, header_offset: int, comp_size: int,
                    expected_crc: int) -> tuple[int, bytes] | None:
    """Read and CRC-verify the entry at *header_offset* in *data*.

    Returns ``(method, decompressed payload)`` when the *comp_size* bytes
    of compressed data decompress to *expected_crc*, else None.
    """
    parsed = _parse_lfh(data, header_offset)
    if parsed is None:
        return None
    method, _name, name_len, extra_len = parsed
    data_start = header_offset + _LFH_FIXED_SIZE + name_len + extra_len
    comp = data[data_start:data_start + comp_size]
    if len(comp) < comp_size:
        return None
    raw = _safe_decompress(comp, method)
    if raw is None:
        return None
    if (zlib.crc32(raw) & 0xFFFFFFFF) != (expected_crc & 0xFFFFFFFF):
        return None
    return method, raw


def _rebuild_from_entries(items: list[tuple[str, bytes, int]],
                          output_path: Path,
                          notes: list[str]) -> tuple[str, Path | None]:
    """Rebuild a consistent package at *output_path* from recovered *items*.

    Each item is ``(name, decompressed payload, compress type)``. The
    payloads are written to a throwaway intermediate archive, re-indexed,
    and handed to :func:`pptrepair.rebuild.rebuild_package`, which
    synthesises the missing plumbing and prunes dangling references. The
    rebuilt output is self-checked with the integrity inspectors. Returns
    ``("partial", output_path)`` on success, or ``("failed", None)`` when
    nothing was recovered or the salvage set lacks ``ppt/presentation.xml``.
    """
    if not items:
        notes.append("no entries could be recovered from any copy")
        return "failed", None

    with tempfile.TemporaryDirectory() as tmp_dir:
        intermediate = Path(tmp_dir) / "merge_intermediate.zip"
        with zipfile.ZipFile(intermediate, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, raw, compress_type in items:
                info = zipfile.ZipInfo(name)
                info.compress_type = compress_type
                zf.writestr(info, raw)

        census = from_central_directory(intermediate)
        if census is None:
            notes.append("intermediate archive could not be re-indexed")
            return "failed", None
        salvaged = [SalvagedEntry(entry.name, categorize(entry.name), "cd",
                                  entry) for entry in census.entries]
        try:
            with SalvageReader(intermediate) as reader:
                result = rebuild_package(reader, salvaged, output_path)
        except RebuildError as exc:
            notes.append(f"rebuild fallback failed: {exc}")
            return "failed", None

        for warning in result.warnings:
            notes.append(f"rebuild: {warning}")
        _append_integrity_notes(output_path, notes)
    return "partial", output_path


def _append_integrity_notes(output_path: Path, notes: list[str]) -> None:
    """Self-check the rebuilt *output_path* and note any inconsistency.

    Mirrors :func:`pptrepair.repair._run_rebuild`: a positive count from
    any of the three integrity inspectors is recorded as a note.
    """
    references = inspect_references(output_path)
    if references.dangling:
        notes.append(
            f"integrity: {len(references.dangling)} dangling relationship "
            "reference(s) in output")
    timing = inspect_timing(output_path)
    timing_issues = len(timing.dangling) + len(timing.media_mismatch)
    if timing_issues:
        notes.append(
            f"integrity: {timing_issues} inconsistent timing reference(s) "
            "in output")
    structure = inspect_structure(output_path)
    if structure.missing:
        notes.append(
            f"integrity: {len(structure.missing)} missing structural "
            "relationship(s) in output")
