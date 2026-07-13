"""Low-level structural scanner for ZIP-based (.pptx) files.

A single streaming pass over the file collects the structural facts
needed for corruption diagnosis:

* runs of zero bytes (detected at :data:`BLOCK_SIZE` granularity, then
  refined to exact byte boundaries),
* offsets of ZIP signatures (local file headers, central directory
  headers, end-of-central-directory records),
* a parsed end-of-central-directory (EOCD) record, if present.

Files are opened read-only and never modified. The scanner must not
load the whole file into memory; corrupted presentations of several
hundred MiB are a supported input.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

#: Granularity (bytes) for zero-run detection.
BLOCK_SIZE = 4096

#: Streaming read size (bytes). Tests may monkeypatch this to a small
#: value to exercise chunk-boundary handling, so implementations must
#: read it at call time (``scanner.CHUNK_SIZE``), not capture it at
#: import time. Must stay a multiple of :data:`BLOCK_SIZE`.
CHUNK_SIZE = 8 * 1024 * 1024

#: Alignments reported as evidence, smallest to largest.
ALIGNMENTS = (4096, 65536, 262144, 1048576)

LFH_SIG = b"PK\x03\x04"
CD_SIG = b"PK\x01\x02"
EOCD_SIG = b"PK\x05\x06"

#: OLE compound file (CFB) signature — encrypted Office documents and
#: legacy binary formats (.doc/.xls/.ppt) start with these eight bytes.
CFB_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

#: struct format of the fixed 22-byte EOCD record (including signature).
EOCD_STRUCT = "<IHHHHIIH"


def alignment_of(value: int) -> int | None:
    """Return the largest alignment from :data:`ALIGNMENTS` dividing *value*.

    Returns None when *value* is not a multiple of any listed alignment.
    Note that 0 is a multiple of every alignment.
    """
    best: int | None = None
    for candidate in ALIGNMENTS:
        if value % candidate == 0:
            best = candidate
    return best


@dataclass
class ZeroRun:
    """A contiguous run of zero bytes, refined to exact byte boundaries."""

    start: int
    end: int  # exclusive

    def length(self) -> int:
        """Return the number of zero bytes in the run."""
        return self.end - self.start

    def start_alignment(self) -> int | None:
        """Return the largest known alignment of the run's start offset."""
        return alignment_of(self.start)

    def end_alignment(self) -> int | None:
        """Return the largest known alignment of the run's end offset."""
        return alignment_of(self.end)


@dataclass
class EocdInfo:
    """Parsed end-of-central-directory record."""

    offset: int
    total_entries: int
    cd_offset: int
    cd_size: int
    is_consistent: bool
    """True when ``cd_offset + cd_size == offset``, i.e. the EOCD's own
    position matches where it claims the central directory ends."""


@dataclass
class ZipStructure:
    """Structural facts about one file, produced by :func:`scan_structure`."""

    size: int
    head_kind: str
    """Kind of the file head: ``"zip"`` (``PK\\x03\\x04``), ``"cfb"``
    (OLE compound file signature, eight bytes), ``"zeros"`` (first four
    bytes all zero), or ``"other"`` (anything else, including files
    shorter than four bytes)."""
    zero_runs: list[ZeroRun]
    lfh_offsets: list[int]
    cd_sig_count: int
    eocd: EocdInfo | None
    """The last EOCD record in the file, or None when no EOCD signature
    exists or its fixed 22-byte record cannot be read in full."""

    def zero_total(self) -> int:
        """Return the total number of bytes covered by zero runs."""
        return sum(run.length() for run in self.zero_runs)

    def zero_ratio(self) -> float:
        """Return the fraction of the file covered by zero runs (0.0-1.0)."""
        if self.size == 0:
            return 0.0
        return self.zero_total() / self.size


def _detect_zero_blocks(chunk: bytes, chunk_offset: int,
                         zero_blocks: list[list[int]]) -> None:
    """Scan one chunk for all-zero :data:`BLOCK_SIZE` blocks.

    Detected blocks are appended to *zero_blocks* (a list of mutable
    ``[start, end)`` pairs), merging with the previous entry when it is
    directly adjacent. *chunk_offset* is assumed to be block-aligned,
    which holds as long as ``CHUNK_SIZE`` is a multiple of
    :data:`BLOCK_SIZE`.
    """
    for block_start in range(0, len(chunk), BLOCK_SIZE):
        block = chunk[block_start:block_start + BLOCK_SIZE]
        # The final block may be shorter than BLOCK_SIZE, so compare the
        # zero count against the actual block length.
        if block.count(0) != len(block):
            continue
        abs_start = chunk_offset + block_start
        abs_end = abs_start + len(block)
        if zero_blocks and zero_blocks[-1][1] == abs_start:
            zero_blocks[-1][1] = abs_end
        else:
            zero_blocks.append([abs_start, abs_end])


def _find_signatures(search_buf: bytes, base: int, carry_len: int,
                      lfh_offsets: list[int],
                      eocd_offsets: list[int]) -> int:
    """Search *search_buf* for ZIP signatures and record their positions.

    *search_buf* is the carried-over tail of the previous chunk
    (*carry_len* bytes) followed by the current chunk; *base* is the
    absolute file offset of ``search_buf[0]``. A match found entirely
    within the carried-over tail (i.e. not touching any byte of the
    current chunk) is skipped, since it was already reported while
    processing the previous chunk; callers must pass a *carry_len* equal
    to the number of bytes carried over (at most ``len(LFH_SIG) - 1``).
    ``PK\\x03\\x04`` offsets are appended to *lfh_offsets*,
    ``PK\\x05\\x06`` offsets to *eocd_offsets*; ``PK\\x01\\x02``
    matches are only counted, and the count is returned.
    """
    cd_count = 0
    pos = search_buf.find(b"PK")
    while pos != -1:
        # A match completing inside the carried-over tail was already
        # reported by the previous call; skip it to avoid double-counting.
        if pos + 4 <= carry_len:
            pos = search_buf.find(b"PK", pos + 1)
            continue
        sig = search_buf[pos:pos + 4]
        if len(sig) == 4:
            offset = base + pos
            if sig == LFH_SIG:
                lfh_offsets.append(offset)
            elif sig == CD_SIG:
                cd_count += 1
            elif sig == EOCD_SIG:
                eocd_offsets.append(offset)
        pos = search_buf.find(b"PK", pos + 1)
    return cd_count


def _refine_zero_run_edges(path: Path, zero_blocks: list[list[int]],
                            size: int) -> list[ZeroRun]:
    """Refine block-granular zero runs to exact byte boundaries.

    For each ``[start, end)`` block-aligned run, the bytes immediately
    outside the run are inspected and the boundary is extended outward
    while adjacent bytes are also zero.
    """
    refined: list[ZeroRun] = []
    with path.open("rb") as f:
        for start, end in zero_blocks:
            exact_start = start
            if start > 0:
                back = max(0, start - BLOCK_SIZE)
                f.seek(back)
                buf = f.read(start - back)
                i = len(buf)
                while i > 0 and buf[i - 1] == 0:
                    i -= 1
                exact_start = back + i
            exact_end = end
            if end < size:
                f.seek(end)
                buf = f.read(BLOCK_SIZE)
                i = 0
                while i < len(buf) and buf[i] == 0:
                    i += 1
                exact_end = end + i
            refined.append(ZeroRun(start=exact_start, end=exact_end))
    return refined


def _parse_eocd(path: Path, offset: int) -> EocdInfo | None:
    """Read and parse the fixed 22-byte EOCD record at *offset*.

    Returns None when fewer than 22 bytes are available (truncated
    file).
    """
    with path.open("rb") as f:
        f.seek(offset)
        data = f.read(22)
    if len(data) < 22:
        return None
    (_, _disk_no, _cd_disk, _n_disk, n_total, cd_size, cd_offset,
     _comment_len) = struct.unpack(EOCD_STRUCT, data)
    return EocdInfo(
        offset=offset,
        total_entries=n_total,
        cd_offset=cd_offset,
        cd_size=cd_size,
        is_consistent=(cd_offset + cd_size == offset),
    )


def _detect_head_kind(head: bytes) -> str:
    """Classify the head bytes of the file (up to eight are examined)."""
    if head[:4] == LFH_SIG:
        return "zip"
    if head[:8] == CFB_SIG:
        return "cfb"
    if len(head) >= 4 and head[:4].count(0) == 4:
        return "zeros"
    return "other"


def scan_structure(path: Path) -> ZipStructure:
    """Scan *path* in one streaming pass and return its structure.

    Implementation requirements:

    * Read the file in ``CHUNK_SIZE`` chunks; never load it whole.
    * Zero runs are detected as maximal sequences of all-zero
      ``BLOCK_SIZE`` blocks, then refined outward to exact byte
      boundaries by inspecting the bytes adjacent to each run.
    * Signature search must not miss matches that straddle a chunk
      boundary (carry the last three bytes of each chunk over).
    * ``eocd`` is parsed from the *last* EOCD signature found; when the
      fixed 22-byte record cannot be read completely (e.g. truncated
      file), ``eocd`` is None.
    """
    size = path.stat().st_size
    zero_blocks: list[list[int]] = []
    lfh_offsets: list[int] = []
    eocd_offsets: list[int] = []
    cd_sig_count = 0
    head: bytes = b""

    prev_tail = b""  # carry-over for signatures straddling chunk boundaries
    offset = 0

    with path.open("rb") as f:
        while True:
            # Look up CHUNK_SIZE on every iteration so that tests can
            # monkeypatch the module global.
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            if offset == 0:
                head = chunk[:8]

            _detect_zero_blocks(chunk, offset, zero_blocks)

            search_buf = prev_tail + chunk
            base = offset - len(prev_tail)
            cd_sig_count += _find_signatures(
                search_buf, base, len(prev_tail), lfh_offsets, eocd_offsets)
            prev_tail = chunk[-3:]
            offset += len(chunk)

    zero_runs = _refine_zero_run_edges(path, zero_blocks, size)
    eocd = _parse_eocd(path, eocd_offsets[-1]) if eocd_offsets else None

    return ZipStructure(
        size=size,
        head_kind=_detect_head_kind(head),
        zero_runs=zero_runs,
        lfh_offsets=lfh_offsets,
        cd_sig_count=cd_sig_count,
        eocd=eocd,
    )
