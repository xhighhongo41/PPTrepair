"""Selection and raw retrieval of salvageable archive entries.

This module bridges diagnosis (which *identifies* recoverable entries)
and repair (which needs their *contents*):

* :func:`select_salvageable` picks, per verdict, which census is the
  trustworthy source of entries and resolves duplicate names.
* :class:`SalvageReader` streams the payload of a salvaged entry out of
  the damaged file, either through :mod:`zipfile` (central-directory
  sourced entries) or by re-parsing the local file header at a known
  offset (scan-sourced entries).

The damaged input file is only ever opened read-only.
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from pptrepair.census import EntryResult
from pptrepair.classify import Diagnosis, Verdict, cd_matched_ok_entries

#: Chunk size for streaming entry payloads.
STREAM_CHUNK_SIZE = 1024 * 1024

#: Local file header signature (``PK\x03\x04``). Kept local to this
#: module (rather than imported from :mod:`pptrepair.census`) so the
#: reader carries its own copy of the on-disk header layout.
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

#: Header flag bit 3: sizes/CRC follow the data in a trailing data
#: descriptor rather than appearing in the header itself.
_FLAG_DATA_DESCRIPTOR = 0x08

#: Compression methods understood by the reader (store and deflate).
_SUPPORTED_METHODS = frozenset((0, 8))

#: Lowest and highest years representable by a DOS date field
#: (7-bit offset from 1980).
_DOS_MIN_YEAR = 1980
_DOS_MAX_YEAR = 2107


@dataclass
class SalvagedEntry:
    """One archive entry chosen for recovery."""

    name: str
    category: str
    source: str  # "cd" | "lfh"
    entry: EntryResult


def select_salvageable(
        diagnosis: Diagnosis) -> tuple[list[SalvagedEntry], list[str]]:
    """Choose the recoverable entries for *diagnosis*.

    Source selection per verdict:

    * ``TAIL_TRUNCATED`` / ``VERSION_MIX`` / ``TAIL_FOREIGN_DATA`` —
      CRC-valid entries of the LFH census only (for VERSION_MIX the
      central directory describes a different version and must not be
      trusted; for TAIL_FOREIGN_DATA the auto repair path tries a trim
      first, so this LFH-based fallback only matters if that fails).
    * ``HEAD_ZERO_FILL`` / ``HEAD_FOREIGN_DATA`` / ``INTERIOR_DAMAGE`` —
      readable entries of the central-directory census.
    * ``FOREIGN_ZIP_OVERWRITE`` / ``SCATTERED_OVERWRITE`` — union:
      readable CD entries first, then CRC-valid LFH entries the central
      directory also indexes (:func:`pptrepair.classify.cd_matched_ok_entries`),
      so a fragment of the *foreign* ZIP archive that overwrote part of
      the file can never be selected for recovery.
    * ``OTHER_CORRUPT`` — union: readable CD entries first, then
      CRC-valid LFH-only entries whose names are not already taken.
    * ``NORMAL`` / ``NOT_A_ZIP`` / ``EMPTY_FILE`` / ``FULL_ZERO_FILL`` —
      empty.

    Duplicate names are resolved in favour of (1) the CD-sourced entry,
    then (2) the LFH entry written last (largest header offset): when a
    part is stored twice — PowerPoint's incremental saves append the
    updated copy — the last one is what the (possibly lost) central
    directory would have pointed at. Each dropped duplicate is reported
    in the returned warnings list.

    :return: ``(entries, warnings)``.
    """
    verdict = diagnosis.verdict
    if verdict in (Verdict.NORMAL, Verdict.NOT_A_ZIP, Verdict.EMPTY_FILE,
                   Verdict.FULL_ZERO_FILL):
        return [], []
    if verdict in (Verdict.TAIL_TRUNCATED, Verdict.VERSION_MIX,
                   Verdict.TAIL_FOREIGN_DATA):
        candidates = _lfh_candidates(diagnosis.lfh_census)
    elif verdict in (Verdict.HEAD_ZERO_FILL, Verdict.HEAD_FOREIGN_DATA,
                     Verdict.INTERIOR_DAMAGE):
        candidates = _cd_candidates(diagnosis.cd_census)
    elif verdict in (Verdict.FOREIGN_ZIP_OVERWRITE,
                     Verdict.SCATTERED_OVERWRITE):
        # CD entries first, then only LFH entries the central directory
        # corroborates, so a foreign ZIP fragment is never recovered.
        candidates = (_cd_candidates(diagnosis.cd_census)
                      + _matched_lfh_candidates(diagnosis))
    else:  # OTHER_CORRUPT: CD first, then LFH-only names.
        candidates = (_cd_candidates(diagnosis.cd_census)
                      + _lfh_candidates(diagnosis.lfh_census))
    return _resolve_duplicates(candidates)


def _cd_candidates(census) -> list[SalvagedEntry]:
    """Return CD-sourced salvage candidates for a (possibly None) census."""
    if census is None:
        return []
    return [SalvagedEntry(e.name, e.category, "cd", e)
            for e in census.ok_entries()]


def _lfh_candidates(census) -> list[SalvagedEntry]:
    """Return LFH-sourced salvage candidates for a (possibly None) census."""
    if census is None:
        return []
    return [SalvagedEntry(e.name, e.category, "lfh", e)
            for e in census.ok_entries()]


def _matched_lfh_candidates(diagnosis: Diagnosis) -> list[SalvagedEntry]:
    """Return LFH candidates the central directory also indexes.

    Only CRC-valid local file header entries whose ``(name, header
    offset)`` the central directory lists are eligible (see
    :func:`pptrepair.classify.cd_matched_ok_entries`), so a foreign ZIP
    fragment sharing the damaged file's byte range is never a salvage
    candidate. Empty when the LFH census is unavailable.
    """
    if diagnosis.lfh_census is None:
        return []
    matched = cd_matched_ok_entries(diagnosis.cd_census, diagnosis.lfh_census)
    return [SalvagedEntry(e.name, e.category, "lfh", e) for e in matched]


def _rank(candidate: SalvagedEntry) -> tuple[int, int]:
    """Return a sort key ranking CD entries above later-offset LFH ones.

    Among duplicates sharing a name, the copy written later (larger
    header offset) supersedes earlier ones, mirroring ZIP semantics
    where the central directory points at the authoritative copy — the
    one appended last by an incremental save.
    """
    source_rank = 0 if candidate.source == "cd" else 1
    return (source_rank, -candidate.entry.header_offset)


def _pick_winner(current: SalvagedEntry,
                 rival: SalvagedEntry) -> tuple[SalvagedEntry, SalvagedEntry]:
    """Return ``(winner, loser)`` for two entries sharing a name.

    *current* was seen first; ties therefore keep it.
    """
    if _rank(current) <= _rank(rival):
        return current, rival
    return rival, current


def _resolve_duplicates(
        candidates: list[SalvagedEntry]
) -> tuple[list[SalvagedEntry], list[str]]:
    """Collapse duplicate names, preferring CD then the largest offset.

    First-seen order of the surviving names is preserved. Every dropped
    duplicate is reported as a warning naming the kept and dropped
    header offsets.
    """
    kept: dict[str, SalvagedEntry] = {}
    order: list[str] = []
    warnings: list[str] = []
    for candidate in candidates:
        name = candidate.name
        current = kept.get(name)
        if current is None:
            kept[name] = candidate
            order.append(name)
            continue
        winner, loser = _pick_winner(current, candidate)
        kept[name] = winner
        warnings.append(
            f"duplicate entry name {name!r}: kept offset "
            f"{winner.entry.header_offset}, dropped offset "
            f"{loser.entry.header_offset}")
    return [kept[name] for name in order], warnings


class SalvageReader:
    """Streams salvaged entry payloads out of one damaged file.

    Usable as a context manager. ``extract``/``rebuild`` accept any
    object with the same ``open``/``datetime_of`` surface, so tests may
    substitute an in-memory fake.
    """

    def __init__(self, path: Path) -> None:
        """Prepare a reader over *path* (opened lazily, read-only)."""
        self._path = path
        self._file: BinaryIO | None = None
        self._zip: zipfile.ZipFile | None = None
        self._size: int | None = None

    def __enter__(self) -> "SalvageReader":
        # Open the raw handle used for LFH re-parsing eagerly; the
        # zipfile object for CD entries is opened only when first needed.
        self._file = self._path.open("rb")
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def open(self, salvaged: SalvagedEntry) -> Iterator[bytes]:
        """Yield the decompressed payload of *salvaged* in chunks.

        CD-sourced entries are read through :mod:`zipfile`; LFH-sourced
        entries are read by re-parsing the 30-byte local file header at
        ``salvaged.entry.header_offset`` and inflating the raw stream
        (store and deflate methods, including flag-bit-3 entries whose
        sizes live in a trailing data descriptor). Memory use stays
        bounded regardless of entry size.

        For LFH-sourced entries the CRC-32 is verified only after the
        whole payload has been streamed, so this generator may still
        raise :class:`SalvageError` *after* yielding all of its chunks.

        :raises SalvageError: when the payload can no longer be read
            back intact (should not happen for entries that passed the
            census, but the file may have changed on disk).
        """
        if salvaged.source == "cd":
            yield from self._open_cd(salvaged)
        elif salvaged.source == "lfh":
            yield from self._open_lfh(salvaged)
        else:
            raise SalvageError(f"unknown salvage source {salvaged.source!r}")

    def datetime_of(self, salvaged: SalvagedEntry) -> tuple[
            int, int, int, int, int, int] | None:
        """Return the entry's original ZIP timestamp, if recoverable.

        CD-sourced entries use ``ZipInfo.date_time``; LFH-sourced
        entries decode the DOS date/time fields of the local header.
        Returns None when no plausible timestamp is available.
        """
        try:
            if salvaged.source == "cd":
                return self._datetime_cd(salvaged)
            if salvaged.source == "lfh":
                return self._datetime_lfh(salvaged)
        except Exception:
            # A timestamp is advisory only; any failure degrades to
            # "unknown" rather than aborting the caller.
            return None
        return None

    # -- central-directory (zipfile) path ---------------------------------

    def _ensure_zip(self) -> zipfile.ZipFile:
        """Open (once) and return the :class:`zipfile.ZipFile` handle."""
        if self._zip is None:
            try:
                self._zip = zipfile.ZipFile(self._path)
            except Exception as exc:
                raise SalvageError(
                    f"cannot open {self._path} as a ZIP archive: "
                    f"{exc}") from exc
        return self._zip

    @staticmethod
    def _find_zipinfo(zf: zipfile.ZipFile,
                      header_offset: int) -> zipfile.ZipInfo | None:
        """Return the entry whose local header sits at *header_offset*.

        Matching on the offset (not the name) disambiguates archives
        that list the same name more than once.
        """
        for info in zf.infolist():
            if info.header_offset == header_offset:
                return info
        return None

    def _open_cd(self, salvaged: SalvagedEntry) -> Iterator[bytes]:
        """Stream a CD-sourced entry through :mod:`zipfile`."""
        zf = self._ensure_zip()
        info = self._find_zipinfo(zf, salvaged.entry.header_offset)
        if info is None:
            raise SalvageError(
                f"no central-directory entry at offset "
                f"{salvaged.entry.header_offset} for {salvaged.name!r}")
        try:
            with zf.open(info) as stream:
                while True:
                    chunk = stream.read(STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk
        except SalvageError:
            raise
        except Exception as exc:
            # zipfile raises (e.g. BadZipFile) when the CRC fails on read.
            raise SalvageError(
                f"cannot read {salvaged.name!r} through the central "
                f"directory: {exc}") from exc

    def _datetime_cd(self, salvaged: SalvagedEntry) -> tuple[
            int, int, int, int, int, int] | None:
        """Return the CD timestamp of *salvaged*, or None if not found."""
        zf = self._ensure_zip()
        info = self._find_zipinfo(zf, salvaged.entry.header_offset)
        if info is None:
            return None
        year, month, day, hour, minute, second = info.date_time
        return (year, month, day, hour, minute, second)

    # -- local-file-header (raw re-parse) path ----------------------------

    def _require_file(self) -> BinaryIO:
        """Return the raw file handle, requiring the reader to be open."""
        if self._file is None:
            raise SalvageError(
                "reader is not open; use it as a context manager")
        return self._file

    def _file_size(self) -> int:
        """Return the size of the damaged file (cached)."""
        if self._size is None:
            self._size = self._path.stat().st_size
        return self._size

    def _parse_local_header(
            self, header_offset: int) -> tuple[int, int, int, int, int]:
        """Read and validate the local file header at *header_offset*.

        :return: ``(flags, method, crc, comp_size, data_start)``.
        :raises SalvageError: on a short read, a bad signature or an
            unsupported compression method.
        """
        f = self._require_file()
        header = _read_at(f, header_offset, _LFH_FIXED_SIZE)
        if len(header) < _LFH_FIXED_SIZE:
            raise SalvageError(
                f"truncated local file header at offset {header_offset}")
        if header[:len(_LFH_SIG)] != _LFH_SIG:
            raise SalvageError(
                f"no local file header signature at offset {header_offset}")
        (_sig, _ver, flags, method, _mtime, _mdate, crc, comp_size,
         _uncomp_size, name_len, extra_len) = struct.unpack(
            _LFH_STRUCT, header)
        if method not in _SUPPORTED_METHODS:
            raise SalvageError(
                f"unsupported compression method {method} at offset "
                f"{header_offset}")
        data_start = header_offset + _LFH_FIXED_SIZE + name_len + extra_len
        return flags, method, crc, comp_size, data_start

    def _open_lfh(self, salvaged: SalvagedEntry) -> Iterator[bytes]:
        """Stream an LFH-sourced entry by re-parsing its local header."""
        flags, method, crc, comp_size, data_start = self._parse_local_header(
            salvaged.entry.header_offset)
        if flags & _FLAG_DATA_DESCRIPTOR:
            yield from self._stream_data_descriptor(data_start, method)
        else:
            yield from self._stream_fixed(data_start, method, crc, comp_size)

    def _stream_fixed(self, data_start: int, method: int, expected_crc: int,
                      comp_size: int) -> Iterator[bytes]:
        """Stream an entry whose sizes are known from its header.

        The CRC-32 is accumulated over the decompressed output and only
        checked once every chunk has been yielded.
        """
        f = self._require_file()
        f.seek(data_start)
        remaining = comp_size
        crc = 0
        decompressor = zlib.decompressobj(-15) if method == 8 else None
        while remaining > 0:
            chunk = f.read(min(STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                raise SalvageError(
                    "unexpected end of file while reading entry data")
            remaining -= len(chunk)
            out = _inflate(decompressor, chunk)
            if out:
                crc = zlib.crc32(out, crc)
                yield out
        if decompressor is not None:
            tail = _flush(decompressor)
            if tail:
                crc = zlib.crc32(tail, crc)
                yield tail
        if crc != expected_crc:
            raise SalvageError("CRC mismatch while reading salvaged entry")

    def _stream_data_descriptor(self, data_start: int,
                                method: int) -> Iterator[bytes]:
        """Stream a flag-bit-3 entry whose sizes trail the data.

        The deflate stream is inflated to end-of-stream, then the
        trailing data descriptor is parsed for the authoritative CRC and
        compared once all chunks have been yielded.
        """
        if method != 8:
            # A stored entry with a data descriptor cannot be delimited.
            raise SalvageError(
                "stored entry with a data descriptor is unsupported")
        f = self._require_file()
        f.seek(data_start)
        decompressor = zlib.decompressobj(-15)
        crc = 0
        fed = 0
        pos = data_start
        file_size = self._file_size()
        while not decompressor.eof:
            if pos >= file_size:
                raise SalvageError("truncated deflate stream")
            chunk = f.read(min(STREAM_CHUNK_SIZE, file_size - pos))
            if not chunk:
                raise SalvageError("truncated deflate stream")
            pos += len(chunk)
            fed += len(chunk)
            out = _inflate(decompressor, chunk)
            if out:
                crc = zlib.crc32(out, crc)
                yield out
        tail = _flush(decompressor)
        if tail:
            crc = zlib.crc32(tail, crc)
            yield tail
        # After end-of-stream the tail of the last chunk lands in
        # unused_data, so the data end is what was actually consumed.
        consumed = fed - len(decompressor.unused_data)
        descriptor_crc = _read_data_descriptor(f, data_start + consumed)
        if descriptor_crc is None:
            raise SalvageError("missing data descriptor")
        if crc != descriptor_crc:
            raise SalvageError("CRC mismatch while reading salvaged entry")

    def _datetime_lfh(self, salvaged: SalvagedEntry) -> tuple[
            int, int, int, int, int, int] | None:
        """Decode the DOS timestamp of an LFH-sourced entry."""
        f = self._require_file()
        header = _read_at(f, salvaged.entry.header_offset, _LFH_FIXED_SIZE)
        if len(header) < _LFH_FIXED_SIZE:
            return None
        if header[:len(_LFH_SIG)] != _LFH_SIG:
            return None
        (_sig, _ver, _flags, _method, mtime, mdate, _crc, _comp,
         _uncomp, _name_len, _extra_len) = struct.unpack(_LFH_STRUCT, header)
        return _decode_dos_datetime(mdate, mtime)


class SalvageError(Exception):
    """Raised when a salvaged entry's payload cannot be read back."""


def _read_at(f: BinaryIO, offset: int, length: int) -> bytes:
    """Seek to *offset* and return up to *length* bytes."""
    f.seek(offset)
    return f.read(length)


def _inflate(decompressor, chunk: bytes) -> bytes:
    """Feed *chunk* to *decompressor* (or pass it through when None).

    Any low-level deflate failure is surfaced as :class:`SalvageError`.
    """
    if decompressor is None:
        return chunk
    try:
        return decompressor.decompress(chunk)
    except zlib.error as exc:
        raise SalvageError(f"deflate error: {exc}") from exc


def _flush(decompressor) -> bytes:
    """Flush any residual output from *decompressor*."""
    try:
        return decompressor.flush()
    except zlib.error as exc:
        raise SalvageError(f"deflate error: {exc}") from exc


def _read_data_descriptor(f: BinaryIO, deflate_end: int) -> int | None:
    """Return the CRC-32 stored in the data descriptor at *deflate_end*.

    The 12-byte descriptor (``crc``, ``comp_size``, ``uncomp_size``) may
    be preceded by the optional ``PK\\x07\\x08`` signature. Returns None
    when the record cannot be read in full.
    """
    head = _read_at(f, deflate_end, len(_DD_SIG) + 12)
    if head[:len(_DD_SIG)] == _DD_SIG:
        body = head[len(_DD_SIG):]
        if len(body) < 12:
            return None
        crc, _comp, _uncomp = struct.unpack("<III", body[:12])
        return crc
    if len(head) < 12:
        return None
    crc, _comp, _uncomp = struct.unpack("<III", head[:12])
    return crc


def _decode_dos_datetime(dos_date: int, dos_time: int) -> tuple[
        int, int, int, int, int, int] | None:
    """Decode a DOS date/time pair into ``(Y, M, D, h, m, s)``.

    Returns None when the decoded fields are out of range (an implausible
    timestamp that should not be trusted).
    """
    day = dos_date & 0x1F
    month = (dos_date >> 5) & 0x0F
    year = ((dos_date >> 9) & 0x7F) + 1980
    second = (dos_time & 0x1F) * 2
    minute = (dos_time >> 5) & 0x3F
    hour = (dos_time >> 11) & 0x1F
    if not (_DOS_MIN_YEAR <= year <= _DOS_MAX_YEAR):
        return None
    if not (1 <= month <= 12):
        return None
    if not (1 <= day <= 31):
        return None
    if hour > 23 or minute > 59 or second > 59:
        return None
    return (year, month, day, hour, minute, second)
