"""Per-entry salvage census for ZIP-based (.pptx) files.

Two complementary strategies are provided:

* :func:`from_central_directory` trusts the central directory (CD) and
  attempts to read every listed entry through :mod:`zipfile`. It shows
  how much of the file *as indexed* is actually readable.
* :func:`from_lfh_scan` ignores the central directory and scans the raw
  byte stream for local file headers (``PK\\x03\\x04``), decompressing
  and CRC-checking each candidate entry. It recovers entries even when
  the central directory is missing (truncated files) or refers to a
  different version of the file's content.

Files are opened read-only and never modified.
"""

from __future__ import annotations

import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

#: Report categories, matched in order; first match wins.
CATEGORY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("slide_xml", re.compile(r"^ppt/slides/slide\d+\.xml$")),
    ("slide_rels", re.compile(r"^ppt/slides/_rels/")),
    ("notes", re.compile(r"^ppt/notesSlides/")),
    ("media", re.compile(r"^ppt/media/")),
    ("layout_master", re.compile(r"^ppt/(slideLayouts|slideMasters|theme)/")),
    ("core_parts", re.compile(
        r"^(\[Content_Types\]\.xml|_rels/\.rels|ppt/presentation\.xml"
        r"|ppt/_rels/presentation\.xml\.rels)$")),
    ("docProps", re.compile(r"^docProps/")),
]

_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")

#: Part names that must be present and readable for a file to count as
#: a well-formed .pptx package.
PPTX_CORE_NAMES = frozenset(
    ["[Content_Types].xml", "_rels/.rels", "ppt/presentation.xml"]
)

#: Local file header signature (``PK\x03\x04``).
_LFH_SIG = b"PK\x03\x04"

#: Optional data-descriptor signature (``PK\x07\x08``).
_DD_SIG = b"PK\x07\x08"

#: struct format of the fixed 30-byte local file header (signature
#: included): signature, version-needed, flags, method, mod-time,
#: mod-date, CRC-32, compressed size, uncompressed size, name length
#: and extra-field length.
_LFH_STRUCT = "<IHHHHHIIIHH"

#: Size in bytes of the fixed part of a local file header.
_LFH_FIXED_SIZE = 30

#: Header flag bit 3: sizes/CRC follow the data in a trailing
#: data descriptor rather than appearing in the header itself.
_FLAG_DATA_DESCRIPTOR = 0x08

#: Compression methods understood by the scanner (store and deflate).
_SUPPORTED_METHODS = frozenset((0, 8))

#: Accepted range for an entry's file-name length; anything outside is
#: treated as a bogus signature rather than a real header.
_MIN_NAME_LEN = 1
_MAX_NAME_LEN = 512

#: Streaming read size for the signature search. Kept small enough to
#: bound memory yet large enough to amortise ``seek``/``read`` calls;
#: read at call time so tests may monkeypatch it.
_SCAN_CHUNK_SIZE = 8 * 1024 * 1024

#: Streaming read size for per-entry data (decompression / CRC).
_READ_CHUNK_SIZE = 1024 * 1024


def categorize(name: str) -> str:
    """Return the report category for an archive entry name."""
    for category, pattern in CATEGORY_PATTERNS:
        if pattern.match(name):
            return category
    return "other"


@dataclass
class EntryResult:
    """Outcome of trying to recover a single archive entry."""

    name: str
    category: str
    header_offset: int
    file_size: int
    ok: bool
    error: str | None = None


@dataclass
class CensusResult:
    """Aggregate outcome of a salvage census."""

    method: str  # "central_directory" | "lfh_scan"
    entries: list[EntryResult] = field(default_factory=list)

    def total(self) -> int:
        """Return the number of entries examined."""
        return len(self.entries)

    def ok_count(self) -> int:
        """Return the number of entries recovered intact."""
        return sum(1 for e in self.entries if e.ok)

    def ok_entries(self) -> list[EntryResult]:
        """Return the entries recovered intact."""
        return [e for e in self.entries if e.ok]

    def ok_slide_numbers(self) -> list[int]:
        """Return sorted slide numbers whose XML was recovered intact."""
        numbers = []
        for e in self.entries:
            m = _SLIDE_RE.match(e.name)
            if m and e.ok:
                numbers.append(int(m.group(1)))
        return sorted(numbers)

    def total_slide_count(self) -> int:
        """Return the number of slide XML entries seen (intact or not)."""
        return sum(1 for e in self.entries if e.category == "slide_xml")

    def category_stats(self) -> dict[str, tuple[int, int]]:
        """Return ``{category: (ok, total)}`` over all entries."""
        stats: dict[str, list[int]] = {}
        for e in self.entries:
            ok, total = stats.setdefault(e.category, [0, 0])
            stats[e.category] = [ok + (1 if e.ok else 0), total + 1]
        return {k: (v[0], v[1]) for k, v in stats.items()}

    def has_pptx_core(self) -> bool:
        """Return True when all essential pptx parts are recovered intact."""
        ok_names = {e.name for e in self.entries if e.ok}
        return PPTX_CORE_NAMES <= ok_names


def from_central_directory(path: Path) -> CensusResult | None:
    """Census every entry listed in the central directory.

    Returns None when :mod:`zipfile` cannot open the file at all (no
    usable end-of-central-directory record).

    Implementation requirements:

    * Iterate ``ZipFile.infolist()`` and attempt a full read of each
      entry (``zipfile`` verifies the CRC on read).
    * A failing entry must not abort the census: record the exception
      type name in ``error`` and continue.
    * Entries must never be extracted to disk.
    """
    try:
        archive = zipfile.ZipFile(path)
    except Exception:
        # Any failure while opening (broken EOCD, non-ZIP data, ...)
        # means the central directory is unusable; signal it with None
        # so callers can fall back to the LFH scan.
        return None

    entries: list[EntryResult] = []
    with archive as zf:
        for info in zf.infolist():
            entries.append(_census_cd_entry(zf, info))
    return CensusResult(method="central_directory", entries=entries)


def _census_cd_entry(zf: zipfile.ZipFile,
                     info: zipfile.ZipInfo) -> EntryResult:
    """Read one central-directory entry in bounded chunks and record it.

    ``zipfile`` verifies the CRC as the stream is consumed, so a full
    read is enough to detect corruption. The data is streamed in fixed
    chunks and discarded; it is never held whole in memory or written
    to disk.
    """
    ok = True
    error: str | None = None
    try:
        with zf.open(info) as stream:
            while stream.read(_READ_CHUNK_SIZE):
                pass
    except Exception as exc:
        ok = False
        error = type(exc).__name__
    return EntryResult(
        name=info.filename,
        category=categorize(info.filename),
        header_offset=info.header_offset,
        file_size=info.file_size,
        ok=ok,
        error=error,
    )


def from_lfh_scan(path: Path) -> CensusResult:
    """Census entries by scanning for local file headers.

    Works without a central directory. Must stream the file (no whole-
    file reads); corrupted inputs of several hundred MiB are supported.

    Implementation requirements:

    * Find ``PK\\x03\\x04`` signatures; parse the 30-byte header.
    * Sanity-check candidate headers (UTF-8 decodable name of sane
      length, compression method 0 or 8); resume searching just past a
      bogus signature.
    * Method 8 data is inflated with raw deflate and CRC-checked;
      method 0 data is CRC-checked as-is.
    * Entries whose header flag bit 3 is set carry no sizes in the
      header: inflate until end-of-stream, then verify the CRC.
    * After a plausible entry, resume searching at the end of its data;
      an entry whose data extends past EOF is recorded as failed
      (``error="Truncated"``).
    """
    file_size = path.stat().st_size
    entries: list[EntryResult] = []
    with path.open("rb") as f:
        search_pos = 0
        while search_pos < file_size:
            sig_pos = _find_next_lfh(f, search_pos)
            if sig_pos is None:
                break
            entry, search_pos = _process_candidate(f, sig_pos, file_size)
            if entry is not None:
                entries.append(entry)
    return CensusResult(method="lfh_scan", entries=entries)


def _read_at(f: BinaryIO, offset: int, length: int) -> bytes:
    """Seek to *offset* and return up to *length* bytes.

    Fewer bytes are returned when the file ends before *length* is
    reached; callers treat a short read as end-of-file.
    """
    f.seek(offset)
    return f.read(length)


def _find_next_lfh(f: BinaryIO, start: int) -> int | None:
    """Return the offset of the next ``PK\\x03\\x04`` at or after *start*.

    The file is streamed in :data:`_SCAN_CHUNK_SIZE` chunks so that
    several-hundred-MiB inputs never load whole. The trailing three
    bytes of each chunk are carried over so a signature straddling a
    chunk boundary is still found. Returns None when no signature
    remains.
    """
    f.seek(start)
    carry = b""
    base = start  # absolute offset of buf[0]
    while True:
        # Read the chunk size at call time so tests may monkeypatch it.
        chunk = f.read(_SCAN_CHUNK_SIZE)
        if not chunk:
            return None
        buf = carry + chunk
        idx = buf.find(_LFH_SIG)
        if idx != -1:
            return base + idx
        # Carry the last three bytes (signature length minus one) so a
        # signature straddling the chunk boundary is not missed.
        tail = buf[-3:] if len(buf) >= 3 else buf
        base += len(buf) - len(tail)
        carry = tail


def _process_candidate(f: BinaryIO, sig_pos: int,
                       file_size: int) -> tuple[EntryResult | None, int]:
    """Validate and census one candidate local file header.

    Returns ``(entry, next_search)``. ``entry`` is None when the
    candidate fails the sanity checks (a bogus signature that is never
    recorded); ``next_search`` is the offset at which to resume the
    signature search.
    """
    header = _read_at(f, sig_pos, _LFH_FIXED_SIZE)
    if len(header) < _LFH_FIXED_SIZE:
        return None, sig_pos + 4
    (_sig, _ver, flags, method, _mtime, _mdate, crc,
     comp_size, uncomp_size, name_len, extra_len) = struct.unpack(
        _LFH_STRUCT, header)

    # Sanity checks: guard against mistaking a bogus signature for a
    # real header by validating name length, extra-field length,
    # compression method and UTF-8 decodability of the name.
    if not (_MIN_NAME_LEN <= name_len <= _MAX_NAME_LEN):
        return None, sig_pos + 4
    if extra_len > 0xFFFF:
        return None, sig_pos + 4
    if method not in _SUPPORTED_METHODS:
        return None, sig_pos + 4
    name_bytes = _read_at(f, sig_pos + _LFH_FIXED_SIZE, name_len)
    if len(name_bytes) < name_len:
        return None, sig_pos + 4
    try:
        name = name_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, sig_pos + 4

    data_start = sig_pos + _LFH_FIXED_SIZE + name_len + extra_len
    category = categorize(name)
    if flags & _FLAG_DATA_DESCRIPTOR:
        return _census_streamed_entry(
            f, sig_pos, name, category, method, data_start, file_size)
    return _census_fixed_entry(
        f, sig_pos, name, category, method, crc, comp_size, uncomp_size,
        data_start, file_size)


def _census_fixed_entry(f: BinaryIO, sig_pos: int, name: str, category: str,
                        method: int, header_crc: int, comp_size: int,
                        uncomp_size: int, data_start: int,
                        file_size: int) -> tuple[EntryResult, int]:
    """Census an entry whose sizes are known from its header.

    The compressed data is streamed and CRC-checked without ever
    materialising the decompressed output. Returns the entry together
    with the offset at which to resume searching.
    """
    if data_start + comp_size > file_size:
        # The data runs past EOF: a truncated entry. Its data end lies
        # outside the file, so stop scanning here.
        entry = EntryResult(
            name=name, category=category, header_offset=sig_pos,
            file_size=uncomp_size, ok=False, error="Truncated")
        return entry, file_size
    try:
        computed_crc = _crc_of_fixed_data(f, data_start, comp_size, method)
    except Exception as exc:
        # On a decompression failure the boundaries are untrustworthy,
        # so resume searching just past the candidate signature.
        entry = EntryResult(
            name=name, category=category, header_offset=sig_pos,
            file_size=uncomp_size, ok=False, error=type(exc).__name__)
        return entry, sig_pos + 4
    ok = computed_crc == header_crc
    entry = EntryResult(
        name=name, category=category, header_offset=sig_pos,
        file_size=uncomp_size, ok=ok, error=None if ok else "BadCRC")
    return entry, data_start + comp_size


def _crc_of_fixed_data(f: BinaryIO, data_start: int, comp_size: int,
                       method: int) -> int:
    """Stream *comp_size* bytes from *data_start* and return their CRC-32.

    Method 8 data is inflated incrementally with a raw-deflate
    decompressor; method 0 data is CRC-checked verbatim. The
    decompressed output is consumed chunk by chunk and never retained,
    keeping memory bounded regardless of the entry's size.
    """
    f.seek(data_start)
    remaining = comp_size
    crc = 0
    decompressor = zlib.decompressobj(-15) if method == 8 else None
    while remaining > 0:
        chunk = f.read(min(_READ_CHUNK_SIZE, remaining))
        if not chunk:
            break
        remaining -= len(chunk)
        out = decompressor.decompress(chunk) if decompressor else chunk
        crc = zlib.crc32(out, crc)
    if decompressor is not None:
        crc = zlib.crc32(decompressor.flush(), crc)
    return crc


def _census_streamed_entry(f: BinaryIO, sig_pos: int, name: str,
                           category: str, method: int, data_start: int,
                           file_size: int) -> tuple[EntryResult, int]:
    """Census a data-descriptor entry (header flag bit 3 set).

    No sizes are present in the header, so method 8 data is inflated to
    end-of-stream and the trailing data descriptor is parsed to recover
    the CRC and sizes. Method 0 cannot be delimited this way and is
    reported as unsupported. Returns the entry and the resume offset.
    """
    if method != 8:
        # A stored (method 0) entry with a data descriptor cannot be
        # delimited, since its length is unknown up front.
        entry = EntryResult(
            name=name, category=category, header_offset=sig_pos,
            file_size=0, ok=False, error="Unsupported")
        return entry, sig_pos + 4
    try:
        result = _inflate_stream(f, data_start, file_size)
    except Exception as exc:
        entry = EntryResult(
            name=name, category=category, header_offset=sig_pos,
            file_size=0, ok=False, error=type(exc).__name__)
        return entry, sig_pos + 4
    if result is None:
        # Reaching EOF without the stream ending means truncated data.
        entry = EntryResult(
            name=name, category=category, header_offset=sig_pos,
            file_size=0, ok=False, error="Truncated")
        return entry, file_size
    computed_crc, out_len, deflate_end = result

    descriptor = _read_data_descriptor(f, deflate_end)
    if descriptor is None:
        entry = EntryResult(
            name=name, category=category, header_offset=sig_pos,
            file_size=out_len, ok=False, error="MissingDescriptor")
        return entry, file_size
    dd_crc, _dd_comp, dd_uncomp, descriptor_end = descriptor
    ok = computed_crc == dd_crc
    entry = EntryResult(
        name=name, category=category, header_offset=sig_pos,
        file_size=dd_uncomp, ok=ok, error=None if ok else "BadCRC")
    return entry, descriptor_end


def _inflate_stream(f: BinaryIO, data_start: int,
                    file_size: int) -> tuple[int, int, int] | None:
    """Inflate a raw-deflate stream of unknown length from *data_start*.

    Feeds the compressor fixed-size chunks until it reaches
    end-of-stream, computing the CRC-32 and length of the output
    incrementally. Returns ``(crc, output_length, deflate_end)`` where
    ``deflate_end`` is the offset just past the compressed data, or None
    when EOF is hit before the stream ends (truncated data).
    """
    f.seek(data_start)
    decompressor = zlib.decompressobj(-15)
    crc = 0
    out_len = 0
    fed = 0
    pos = data_start
    while not decompressor.eof:
        if pos >= file_size:
            return None
        chunk = f.read(min(_READ_CHUNK_SIZE, file_size - pos))
        if not chunk:
            return None
        pos += len(chunk)
        fed += len(chunk)
        out = decompressor.decompress(chunk)
        crc = zlib.crc32(out, crc)
        out_len += len(out)
    crc = zlib.crc32(decompressor.flush(), crc)
    # After end-of-stream the tail of the last chunk lands in
    # unused_data, so derive the data end from the bytes actually
    # consumed as compressed input.
    consumed = fed - len(decompressor.unused_data)
    return crc, out_len, data_start + consumed


def _read_data_descriptor(
        f: BinaryIO, deflate_end: int) -> tuple[int, int, int, int] | None:
    """Parse the data descriptor located at *deflate_end*.

    The descriptor is 12 bytes (``crc``, ``comp_size``, ``uncomp_size``),
    optionally preceded by the ``PK\\x07\\x08`` signature. Returns
    ``(crc, comp_size, uncomp_size, descriptor_end)`` or None when the
    12-byte record cannot be read in full.
    """
    head = _read_at(f, deflate_end, len(_DD_SIG) + 12)
    if head[:len(_DD_SIG)] == _DD_SIG:
        body = head[len(_DD_SIG):]
        if len(body) < 12:
            return None
        crc, comp, uncomp = struct.unpack("<III", body[:12])
        return crc, comp, uncomp, deflate_end + len(_DD_SIG) + 12
    if len(head) < 12:
        return None
    crc, comp, uncomp = struct.unpack("<III", head[:12])
    return crc, comp, uncomp, deflate_end + 12
