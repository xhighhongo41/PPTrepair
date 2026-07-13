"""Shareable diagnostic fingerprints for unknown corruption patterns.

When ``pptrepair scan`` meets a file whose damage does not match any
known OneDrive corruption pattern, it can (opt-in, ``--report``) write
a *diagnostic fingerprint*: a JSON document describing the file's
structure precisely enough to design a new repair strategy, while
containing **no document content**.

Privacy contract (enforced by tests):

* No byte ranges of the file and no decompressed text are included.
* The original file path/name is excluded by default; ``file.name``
  holds the basename only when the user passes ``--include-filenames``.
* ``file.id`` is a truncated hash of the absolute path, usable to match
  a fingerprint against the (local-only) scan report but not to recover
  the path.
* Archive entry names are included: they are standardized OOXML part
  names (``ppt/slides/slide1.xml`` ...), not document text.

The schema is versioned (:data:`DIAG_SCHEMA_VERSION`) so fingerprints
collected from different tool versions can be aggregated later.
"""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from pathlib import Path

from pptrepair import __version__
from pptrepair.census import CensusResult, EntryResult
from pptrepair.classify import Diagnosis, Verdict
from pptrepair.scanner import EocdInfo, ZeroRun, ZipStructure

#: Version of the fingerprint JSON schema.
DIAG_SCHEMA_VERSION = 1

#: Block size (bytes) for the entropy/content profile.
PROFILE_BLOCK_SIZE = 65536

#: Shannon entropy (bits/byte) at or above which a block counts as
#: ``high_entropy`` (compressed or encrypted data).
HIGH_ENTROPY_THRESHOLD = 7.0

#: Fraction of printable ASCII/whitespace bytes at or above which a
#: non-zero block counts as ``text_like``.
TEXT_LIKE_RATIO = 0.95

#: Byte values counted as "printable ASCII or whitespace" for the
#: ``text_like`` block classification: tab, newline, carriage return,
#: and the printable ASCII range 0x20-0x7E.
_TEXT_LIKE_BYTES = frozenset({0x09, 0x0A, 0x0D}) | frozenset(range(0x20, 0x7F))


def file_id(path: Path) -> str:
    """Return the anonymous file identifier for *path*.

    First 12 hex digits of ``sha256`` over the UTF-8 encoded absolute
    path. Deterministic within a machine, not reversible.
    """
    digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
    return digest[:12]


def is_fingerprint_target(diagnosis: Diagnosis) -> bool:
    """Return True when *diagnosis* represents an unknown pattern.

    Targets are ``OTHER_CORRUPT`` files, plus ``NOT_A_ZIP`` files that
    are *not* OLE compound documents (``structure.head_kind != "cfb"``;
    CFB files are encrypted/legacy Office documents, not corruption).
    Known corruption patterns and ``NORMAL`` are never targets.
    """
    if diagnosis.verdict == Verdict.OTHER_CORRUPT:
        return True
    if diagnosis.verdict == Verdict.NOT_A_ZIP:
        structure = diagnosis.structure
        if structure is not None and structure.head_kind == "cfb":
            return False
        return True
    return False


def chunk_profile(path: Path,
                  block_size: int = PROFILE_BLOCK_SIZE) -> list[dict]:
    """Profile the file content in *block_size* blocks, run-length merged.

    Each block is classified, in order of precedence:

    * ``"zeros"``        — every byte is zero;
    * ``"text_like"``    — printable ASCII + whitespace bytes make up at
      least :data:`TEXT_LIKE_RATIO` of the block;
    * ``"high_entropy"`` — Shannon entropy >= HIGH_ENTROPY_THRESHOLD
      bits/byte;
    * ``"other"``        — anything else.

    Consecutive blocks with the same class are merged into one run.
    Returns dicts ``{"offset": int, "length": int, "class": str,
    "mean_entropy": float}`` where ``mean_entropy`` is the byte-count
    weighted mean over the run's blocks, rounded to two decimals.

    Implementation requirements: stream the file (never load it whole);
    the final block may be short; an empty file returns ``[]``.
    """
    runs: list[dict] = []
    open_run: dict | None = None  # offset, length, class, weighted_entropy
    offset = 0
    with path.open("rb") as f:
        while True:
            block = f.read(block_size)
            if not block:
                break
            length = len(block)
            entropy = _block_entropy(block)
            block_class = _classify_block(block, entropy)
            if open_run is not None and open_run["class"] == block_class:
                open_run["length"] += length
                open_run["weighted_entropy"] += entropy * length
            else:
                if open_run is not None:
                    runs.append(_finalize_run(open_run))
                open_run = {
                    "offset": offset,
                    "length": length,
                    "class": block_class,
                    "weighted_entropy": entropy * length,
                }
            offset += length
    if open_run is not None:
        runs.append(_finalize_run(open_run))
    return runs


def _block_entropy(block: bytes) -> float:
    """Return the Shannon entropy of *block* in bits/byte.

    Built from a 256-bin byte-value histogram (via ``bytes.count``),
    computed as ``-sum(p * log2(p))`` over the non-empty bins.
    """
    length = len(block)
    if length == 0:
        return 0.0
    entropy = 0.0
    for value in range(256):
        count = block.count(value)
        if count == 0:
            continue
        probability = count / length
        entropy -= probability * math.log2(probability)
    return entropy


def _classify_block(block: bytes, entropy: float) -> str:
    """Classify one content block, in precedence order (see :func:`chunk_profile`)."""
    if block.count(0) == len(block):
        return "zeros"
    text_like_count = sum(1 for byte in block if byte in _TEXT_LIKE_BYTES)
    if text_like_count / len(block) >= TEXT_LIKE_RATIO:
        return "text_like"
    if entropy >= HIGH_ENTROPY_THRESHOLD:
        return "high_entropy"
    return "other"


def _finalize_run(run: dict) -> dict:
    """Convert an in-progress run accumulator to its final fingerprint shape."""
    mean_entropy = round(run["weighted_entropy"] / run["length"], 2)
    return {
        "offset": run["offset"],
        "length": run["length"],
        "class": run["class"],
        "mean_entropy": mean_entropy,
    }


def build_fingerprint(diagnosis: Diagnosis, *,
                      include_filename: bool = False) -> dict:
    """Build the fingerprint dict for *diagnosis* (see module docstring).

    Top-level keys::

        kind             "pptrepair-diagnostic-fingerprint"
        schema_version   DIAG_SCHEMA_VERSION
        tool_version     pptrepair.__version__
        file             {id, name (basename or None), extension
                          (lowercased suffix), size, mtime_utc
                          (ISO-8601 UTC, seconds precision, "Z")}
        verdict          diagnosis.verdict.value
        evidence         list[str]
        salvage_summary  diagnosis.salvage_summary or None
        zip_structure    see below; None when diagnosis.structure is None
        chunk_profile    chunk_profile(diagnosis.path)
        census           {"cd": ... , "lfh": ...} (each None when the
                          census is unavailable)
        entries          merged entry list, see below

    ``zip_structure``: ``head_kind``, ``size``, ``lfh_count``,
    ``cd_sig_count``, ``eocd`` (dict of the EocdInfo fields or None),
    ``zero_runs`` (``start``, ``end``, ``start_alignment``,
    ``end_alignment`` per run).

    Each census dict: ``total``, ``ok``, ``errors_by_type`` (error
    string -> count over failed entries) and ``categories``
    (``entry.category`` -> ``{"total": n, "ok": n}``).

    ``entries``: one dict per archive entry —
    ``{"name", "offset", "size", "ok", "error", "source"}`` — taken
    from the CD census when available (``source": "cd"``), then
    appended with LFH-census entries whose ``header_offset`` the CD did
    not list (``"source": "lfh"``). ``size`` is ``EntryResult.file_size``.

    The file itself is only read via :func:`chunk_profile`; the sizes,
    offsets and mtime come from *diagnosis* and ``os.stat``.
    """
    path = diagnosis.path
    stat = path.stat()
    mtime_utc = datetime.fromtimestamp(
        stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    file_info = {
        "id": file_id(path),
        "name": path.name if include_filename else None,
        "extension": path.suffix.lower(),
        "size": stat.st_size,
        "mtime_utc": mtime_utc,
    }
    return {
        "kind": "pptrepair-diagnostic-fingerprint",
        "schema_version": DIAG_SCHEMA_VERSION,
        "tool_version": __version__,
        "file": file_info,
        "verdict": diagnosis.verdict.value,
        "evidence": list(diagnosis.evidence),
        "salvage_summary": diagnosis.salvage_summary or None,
        "zip_structure": _zip_structure_summary(diagnosis.structure),
        "chunk_profile": chunk_profile(path),
        "census": {
            "cd": _census_summary(diagnosis.cd_census),
            "lfh": _census_summary(diagnosis.lfh_census),
        },
        "entries": _merge_entries(diagnosis.cd_census, diagnosis.lfh_census),
    }


def _eocd_summary(eocd: EocdInfo | None) -> dict | None:
    """Return the fingerprint dict for one EOCD record (None passthrough)."""
    if eocd is None:
        return None
    return {
        "offset": eocd.offset,
        "total_entries": eocd.total_entries,
        "cd_offset": eocd.cd_offset,
        "cd_size": eocd.cd_size,
        "is_consistent": eocd.is_consistent,
    }


def _zero_run_summary(run: ZeroRun) -> dict:
    """Return the fingerprint dict for one zero run."""
    return {
        "start": run.start,
        "end": run.end,
        "start_alignment": run.start_alignment(),
        "end_alignment": run.end_alignment(),
    }


def _zip_structure_summary(structure: ZipStructure | None) -> dict | None:
    """Return the ``zip_structure`` fingerprint dict (None passthrough)."""
    if structure is None:
        return None
    return {
        "head_kind": structure.head_kind,
        "size": structure.size,
        "lfh_count": len(structure.lfh_offsets),
        "cd_sig_count": structure.cd_sig_count,
        "eocd": _eocd_summary(structure.eocd),
        "zero_runs": [_zero_run_summary(run) for run in structure.zero_runs],
    }


def _entry_summary(entry: EntryResult, source: str) -> dict:
    """Return the fingerprint dict for one archive entry, tagged with *source*."""
    return {
        "name": entry.name,
        "offset": entry.header_offset,
        "size": entry.file_size,
        "ok": entry.ok,
        "error": entry.error,
        "source": source,
    }


def _merge_entries(cd_census: CensusResult | None,
                   lfh_census: CensusResult | None) -> list[dict]:
    """Merge CD and LFH census entries: CD first, then unlisted LFH ones."""
    entries: list[dict] = []
    cd_offsets: set[int] = set()
    if cd_census is not None:
        for entry in cd_census.entries:
            cd_offsets.add(entry.header_offset)
            entries.append(_entry_summary(entry, "cd"))
    if lfh_census is not None:
        for entry in lfh_census.entries:
            if entry.header_offset in cd_offsets:
                continue
            entries.append(_entry_summary(entry, "lfh"))
    return entries


def _census_summary(census: CensusResult | None) -> dict | None:
    """Summarize one census as its fingerprint dict (None passthrough)."""
    if census is None:
        return None
    errors_by_type: dict[str, int] = {}
    for entry in census.entries:
        if entry.ok:
            continue
        error_key = entry.error if entry.error is not None else "unknown"
        errors_by_type[error_key] = errors_by_type.get(error_key, 0) + 1
    categories = {
        category: {"total": total, "ok": ok}
        for category, (ok, total) in census.category_stats().items()
    }
    return {
        "total": census.total(),
        "ok": census.ok_count(),
        "errors_by_type": errors_by_type,
        "categories": categories,
    }
