"""Best-effort content rescue from unrepairable .pptx files.

Where :mod:`pptrepair.repair` produces a *usable* artifact (a rebuilt
package or a curated recovery folder), this module is the last resort:
it pulls out whatever is still readable from a badly damaged file, even
when the archive can no longer be made consistent. Four independent
stages, each writing into its own subdirectory of the output folder, so
partial success in one stage never hides the results of another:

1. **Readable entries** (``entries/``) — every entry
   :func:`pptrepair.salvage.select_salvageable` trusts, streamed out of
   the file and written under a path-sanitised name.
2. **Carved images** (``carved/``) — JPEG/PNG bitstreams recovered by
   walking their on-disk structure through the file's raw bytes,
   deduplicated against the rescued entries. Their provenance is
   *unknown*: a carved image may come from the foreign data that
   overwrote the file rather than from the presentation itself.
3. **Partial XML** (``partial_xml/``) — for central-directory entries
   whose data no longer decompresses cleanly but whose local file
   header survives, the deflate stream is inflated up to the first
   error, keeping the readable prefix.
4. **Rescued text** (``rescued_text.txt``) — ``<a:t>`` runs harvested
   from the recovered slide/notes XML (stages 1 and 3).

A machine-readable ``salvage_report.json`` records every stage's
result. The damaged input file is only ever opened read-only.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from pptrepair.classify import Diagnosis, Verdict
from pptrepair.repair import OutputExistsError
from pptrepair.salvage import (SalvageError, SalvagedEntry, SalvageReader,
                               select_salvageable)
from pptrepair.scan import diagnose_file

#: JSON report identity/versioning, mirrored by the scan/repair reports.
REPORT_KIND = "pptrepair-rescue-report"
REPORT_SCHEMA_VERSION = 1

#: Default output-directory suffix appended to the input's stem.
RESCUE_SUFFIX = ".rescued"

#: Names of the per-stage output locations inside the rescue folder.
ENTRIES_DIRNAME = "entries"
CARVED_DIRNAME = "carved"
PARTIAL_DIRNAME = "partial_xml"
TEXT_FILENAME = "rescued_text.txt"
REPORT_FILENAME = "salvage_report.json"

#: Minimum and maximum size (bytes) of a carved image; anything outside
#: this window is discarded as noise or an implausible/hostile length.
MIN_CARVE_BYTES = 8 * 1024
MAX_CARVE_BYTES = 30 * 1024 * 1024

#: Minimum length (bytes) of a recovered partial-XML prefix worth
#: keeping, and a hard cap on its output to bound a decompression bomb.
MIN_PARTIAL_XML_BYTES = 256
MAX_PARTIAL_XML_BYTES = 30 * 1024 * 1024

#: JPEG start-of-image plus the first marker's introducer (``FF D8 FF``).
_JPEG_SOI = b"\xff\xd8\xff"

#: PNG 8-byte file signature.
_PNG_SIG = b"\x89PNG\r\n\x1a\n"

#: Local file header signature and fixed-part layout (kept local so the
#: rescuer carries its own copy of the on-disk header format).
_LFH_SIG = b"PK\x03\x04"
_LFH_STRUCT = "<IHHHHHIIIHH"
_LFH_FIXED_SIZE = 30

#: Compression method for raw deflate (the only one partial decode reads).
_METHOD_DEFLATE = 8

#: Content between ``<a:t>`` DrawingML text runs, spanning newlines.
_A_T_RE = re.compile(r"<a:t[^>]*>(.*?)</a:t>", re.DOTALL)

#: Archive-name prefixes whose parts carry slide/notes body text.
_TEXT_PREFIXES = ("ppt/slides/", "ppt/notesSlides/")


@dataclass
class RescueResult:
    """Language-neutral outcome of one :func:`rescue_file` run."""

    src: Path
    output_dir: Path | None
    verdict: Verdict
    entries_saved: int
    carved_images: int
    partial_xml: int
    text_lines: int
    warnings: list[str]
    report: dict

    def rescued_count(self) -> int:
        """Return the total number of rescued items across every stage."""
        return (self.entries_saved + self.carved_images
                + self.partial_xml + self.text_lines)


class RescueError(Exception):
    """Raised when the file cannot be diagnosed for rescue at all."""


def rescue_file(src: Path, output: Path | None = None, *, force: bool = False,
                lang: str = "en",
                diagnosis: Diagnosis | None = None) -> RescueResult:
    """Rescue whatever content survives inside *src*.

    Behaviour:

    * The input is opened read-only; every artifact is written under the
      output directory, which defaults to ``<stem>.rescued/`` next to
      *src*.
    * *diagnosis*, when omitted, is produced by
      :func:`pptrepair.scan.diagnose_file`; a diagnosis failure raises
      :class:`RescueError`.
    * A ``NORMAL`` verdict rescues nothing: no output directory is
      created and the returned result carries ``output_dir=None`` and a
      zero-count report, so the caller can report "nothing to salvage".
    * For any other verdict the output directory is created (an existing
      one raises :class:`pptrepair.repair.OutputExistsError` unless
      *force*, and is only ever reused, never cleared), the four rescue
      stages run, and ``salvage_report.json`` is written into it.

    *lang* is accepted for signature parity with
    :func:`pptrepair.repair.repair_file`; this function itself emits only
    language-neutral data (the CLI renders the translated summary).

    :raises RescueError: when *src* cannot be diagnosed.
    :raises pptrepair.repair.OutputExistsError: when the output directory
        exists and *force* is false.
    """
    if diagnosis is None:
        diagnosis, error = diagnose_file(src)
        if diagnosis is None:
            raise RescueError(error or f"could not diagnose {src}")

    if diagnosis.verdict == Verdict.NORMAL:
        return _normal_result(src, diagnosis)

    output_dir = output if output is not None else _default_output_dir(src)
    if output_dir.exists() and not force:
        raise OutputExistsError(
            f"output directory already exists: {output_dir}")
    # exist_ok=True: with --force an existing folder is reused in place,
    # never cleared (the input's rescued content must never be lost).
    output_dir.mkdir(parents=True, exist_ok=True)

    session = _RescueSession(src, output_dir, diagnosis)
    session.run()
    report = session.build_report()
    (output_dir / REPORT_FILENAME).write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

    return RescueResult(
        src=src,
        output_dir=output_dir,
        verdict=diagnosis.verdict,
        entries_saved=session.entries_saved,
        carved_images=session.carved_images,
        partial_xml=session.partial_xml,
        text_lines=session.text_lines,
        warnings=session.warnings,
        report=report,
    )


def _default_output_dir(src: Path) -> Path:
    """Return the default rescue folder path next to *src*."""
    return src.with_name(src.stem + RESCUE_SUFFIX)


def _normal_result(src: Path, diagnosis: Diagnosis) -> RescueResult:
    """Build the zero-content result for an intact (``NORMAL``) file."""
    report = {
        "kind": REPORT_KIND,
        "schema_version": REPORT_SCHEMA_VERSION,
        "source": {"path": str(src), "size": src.stat().st_size},
        "verdict": diagnosis.verdict.value,
        "counts": {"entries_saved": 0, "carved_images": 0,
                   "partial_xml": 0, "text_lines": 0},
        "entries": [],
        "carved": [],
        "partial": [],
        "warnings": [],
    }
    return RescueResult(
        src=src, output_dir=None, verdict=diagnosis.verdict,
        entries_saved=0, carved_images=0, partial_xml=0, text_lines=0,
        warnings=[], report=report)


@dataclass
class _CarvedImage:
    """One image bitstream located in the file's raw bytes."""

    kind: str  # "jpeg" | "png"
    ext: str
    offset: int
    length: int
    data: bytes


class _RescueSession:
    """Mutable state and per-stage logic for one rescue run."""

    def __init__(self, src: Path, output_dir: Path,
                 diagnosis: Diagnosis) -> None:
        """Prepare the session over *src* writing into *output_dir*."""
        self.src = src
        self.output_dir = output_dir
        self.diagnosis = diagnosis
        self.src_size = 0

        self.entries_saved = 0
        self.carved_images = 0
        self.partial_xml = 0
        self.text_lines = 0

        self.warnings: list[str] = []
        self.entry_status: list[dict] = []
        self.carved_report: list[dict] = []
        self.partial_report: list[dict] = []

        # Cross-stage state: hashes of rescued entries feed carving's
        # dedup; saved slide/notes files and partial XML feed text.
        self.entry_hashes: set[str] = set()
        self.saved_entries: list[tuple[str, Path, str]] = []
        self.partial_sources: list[tuple[str, bytes]] = []

    def run(self) -> None:
        """Run all four rescue stages in order."""
        self.src_size = self.src.stat().st_size
        raw = self.src.read_bytes()
        with SalvageReader(self.src) as reader:
            self._run_entries(reader)
        self._run_carving(raw)
        self._run_partial_xml(raw)
        self._run_text()

    # -- stage 1: readable entries ----------------------------------------

    def _run_entries(self, reader: SalvageReader) -> None:
        """Stream every salvageable entry into ``entries/``."""
        salvaged, warnings = select_salvageable(self.diagnosis)
        self.warnings.extend(warnings)
        entries_dir = self.output_dir / ENTRIES_DIRNAME
        for index, entry in enumerate(salvaged):
            self._save_one_entry(reader, entry, index, entries_dir)

    def _save_one_entry(self, reader: SalvageReader, entry: SalvagedEntry,
                        index: int, entries_dir: Path) -> None:
        """Stream one entry to disk, sanitising its name and recording it."""
        rel = self._safe_entry_target(entry.name, index, entries_dir)
        dest = entries_dir / rel
        saved_as = _rel_to(dest, self.output_dir)
        try:
            digest = _save_entry_payload(reader, entry, dest)
        except SalvageError as exc:
            if dest.exists():
                dest.unlink()
            self.entry_status.append(
                {"name": entry.name, "saved_as": None,
                 "status": "failed", "error": str(exc)})
            self.warnings.append(
                f"could not stream entry {entry.name!r}: {exc}")
            return
        self.entries_saved += 1
        self.entry_hashes.add(digest)
        self.saved_entries.append((entry.name, dest, digest))
        self.entry_status.append(
            {"name": entry.name, "saved_as": saved_as,
             "status": "saved", "sha256": digest})

    def _safe_entry_target(self, name: str, index: int,
                           entries_dir: Path) -> Path:
        """Return a relative, escape-proof target path for *name*.

        Unsafe names (absolute, drive-lettered, or containing ``..``)
        collapse to a flat ``_unsafe_<index>_<basename>`` name inside
        ``entries/`` and record a warning; the result is verified to
        resolve within *entries_dir* as a final guard.
        """
        if _is_unsafe_name(name):
            flat = _flat_unsafe_name(name, index)
            self.warnings.append(
                f"unsafe entry name {name!r}: saved as {flat}")
            return Path(flat)
        rel = _safe_relative(name)
        if not _resolves_within(entries_dir, rel):
            flat = _flat_unsafe_name(name, index)
            self.warnings.append(
                f"unsafe entry name {name!r}: saved as {flat}")
            return Path(flat)
        return rel

    # -- stage 2: image carving -------------------------------------------

    def _run_carving(self, raw: bytes) -> None:
        """Carve JPEG/PNG bitstreams out of *raw* into ``carved/``."""
        candidates = (
            _carve_signature(raw, _JPEG_SOI, _walk_jpeg, "jpeg", ".jpg")
            + _carve_signature(raw, _PNG_SIG, _walk_png, "png", ".png"))
        candidates.sort(key=lambda c: c.offset)
        carved_dir = self.output_dir / CARVED_DIRNAME
        seen = set(self.entry_hashes)  # dedup against rescued entries too
        number = 0
        for cand in candidates:
            digest = hashlib.sha256(cand.data).hexdigest()
            if digest in seen:
                continue
            seen.add(digest)
            number += 1
            filename = f"carved_{number:04d}{cand.ext}"
            dest = carved_dir / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(cand.data)
            self.carved_images += 1
            self.carved_report.append(
                {"saved_as": f"{CARVED_DIRNAME}/{filename}",
                 "offset": cand.offset, "length": cand.length,
                 "type": cand.kind, "sha256": digest,
                 "provenance": "unknown"})

    # -- stage 3: partial XML decode --------------------------------------

    def _run_partial_xml(self, raw: bytes) -> None:
        """Inflate the readable prefix of damaged XML/rels CD entries."""
        census = self.diagnosis.cd_census
        if census is None:
            return
        partial_dir = self.output_dir / PARTIAL_DIRNAME
        already_saved = {name for name, _p, _h in self.saved_entries}
        for entry in census.entries:
            if entry.ok or entry.name in already_saved:
                continue
            if not (entry.name.endswith(".xml")
                    or entry.name.endswith(".rels")):
                continue
            data = _partial_inflate_entry(raw, entry.header_offset)
            if data is None or len(data) < MIN_PARTIAL_XML_BYTES:
                continue
            filename = _flatten_xml_name(entry.name)
            dest = partial_dir / filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            self.partial_xml += 1
            self.partial_report.append(
                {"name": entry.name,
                 "saved_as": f"{PARTIAL_DIRNAME}/{filename}",
                 "bytes": len(data)})
            self.partial_sources.append((entry.name, data))

    # -- stage 4: text extraction -----------------------------------------

    def _run_text(self) -> None:
        """Harvest ``<a:t>`` runs into ``rescued_text.txt``."""
        blocks: list[str] = []
        total = 0
        for label, data in self._text_sources():
            texts = _extract_at_texts(data)
            if not texts:
                continue
            blocks.append("\n".join([f"=== {label} ==="] + texts))
            total += len(texts)
        if total == 0:
            return
        (self.output_dir / TEXT_FILENAME).write_text(
            "\n\n".join(blocks) + "\n", encoding="utf-8")
        self.text_lines = total

    def _text_sources(self) -> list[tuple[str, bytes]]:
        """Return ``(label, xml_bytes)`` slide/notes sources, name-ordered."""
        sources: list[tuple[str, bytes]] = []
        for name, path, _h in sorted(self.saved_entries, key=lambda r: r[0]):
            if name.startswith(_TEXT_PREFIXES):
                try:
                    sources.append((name, path.read_bytes()))
                except OSError:
                    continue
        sources.extend(sorted(self.partial_sources, key=lambda r: r[0]))
        return sources

    # -- report -----------------------------------------------------------

    def build_report(self) -> dict:
        """Return the ``salvage_report.json`` payload for this run."""
        return {
            "kind": REPORT_KIND,
            "schema_version": REPORT_SCHEMA_VERSION,
            "source": {"path": str(self.src), "size": self.src_size},
            "verdict": self.diagnosis.verdict.value,
            "counts": {
                "entries_saved": self.entries_saved,
                "carved_images": self.carved_images,
                "partial_xml": self.partial_xml,
                "text_lines": self.text_lines,
            },
            "entries": self.entry_status,
            "carved": self.carved_report,
            "partial": self.partial_report,
            "warnings": self.warnings,
        }


# --- entry streaming --------------------------------------------------------


def _save_entry_payload(reader: SalvageReader, entry: SalvagedEntry,
                        dest: Path) -> str:
    """Stream *entry* to *dest* and return the SHA-256 hex of its payload.

    :raises pptrepair.salvage.SalvageError: propagated from the reader
        (possibly after some chunks were already written; the caller
        removes the partial file).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    with dest.open("wb") as handle:
        for chunk in reader.open(entry):
            handle.write(chunk)
            hasher.update(chunk)
    return hasher.hexdigest()


# --- path safety ------------------------------------------------------------


def _is_unsafe_name(name: str) -> bool:
    """Return True when *name* must not be used as a relative path.

    Unsafe means anything that could escape the output directory: an
    absolute path, a Windows drive letter or alternate data stream (any
    ``:``), a ``..`` component, or a name with no usable components.
    """
    normalized = name.replace("\\", "/")
    if not name or normalized.startswith("/"):
        return True
    if ":" in name:
        return True
    parts = normalized.split("/")
    if ".." in parts:
        return True
    if not [part for part in parts if part not in ("", ".")]:
        return True
    return False


def _safe_relative(name: str) -> Path:
    """Return *name* as a clean relative path (assumes it is safe)."""
    parts = [part for part in name.replace("\\", "/").split("/")
             if part not in ("", ".")]
    return Path(*parts)


def _flat_unsafe_name(name: str, index: int) -> str:
    """Return a flat, collision-free name for an unsafe entry *name*."""
    base = name.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    base = base.replace(":", "_").strip()
    if not base or base in (".", ".."):
        base = "entry"
    return f"_unsafe_{index:04d}_{base}"


def _resolves_within(base: Path, rel: Path) -> bool:
    """Return True when ``base / rel`` stays inside *base* once resolved."""
    try:
        base_resolved = base.resolve()
        target_resolved = (base / rel).resolve()
    except OSError:
        return False
    return (target_resolved == base_resolved
            or base_resolved in target_resolved.parents)


def _rel_to(path: Path, base: Path) -> str:
    """Return *path* relative to *base* as a POSIX string, or its name."""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.name


# --- image carving ----------------------------------------------------------


def _carve_signature(raw: bytes, signature: bytes, walker, kind: str,
                     ext: str) -> list[_CarvedImage]:
    """Collect every image of one kind that *walker* accepts in *raw*.

    *walker* takes ``(raw, start)`` and returns the exclusive end offset
    of a syntactically complete image, or None when the structure at
    *start* cannot be walked. The scan resumes past a completed image and
    one byte past a rejected or undersized candidate.
    """
    out: list[_CarvedImage] = []
    pos = 0
    while True:
        start = raw.find(signature, pos)
        if start == -1:
            break
        end = walker(raw, start)
        if end is None:
            pos = start + 1
            continue
        length = end - start
        if length < MIN_CARVE_BYTES:
            # A syntactically valid but too-small image: skip past it.
            pos = end
            continue
        out.append(_CarvedImage(kind, ext, start, length, raw[start:end]))
        pos = end
    return out


def _walk_jpeg(raw: bytes, start: int) -> int | None:
    """Walk the JPEG marker structure at *start*, returning its end.

    Marker segments are skipped by their length field; at ``SOS`` the
    entropy-coded data is scanned (byte-stuffing and restart markers
    honoured) up to the next real marker, which handles multi-scan
    progressive JPEGs. Returns the offset just past the ``EOI`` (``FF
    D9``) marker, or None when the structure is malformed or exceeds
    :data:`MAX_CARVE_BYTES`.
    """
    n = len(raw)
    pos = start + 2  # past the SOI marker (FF D8); an FF marker follows
    while pos < n:
        if pos - start > MAX_CARVE_BYTES:
            return None
        if raw[pos] != 0xFF:
            return None
        while pos < n and raw[pos] == 0xFF:  # skip 0xFF fill bytes
            pos += 1
        if pos >= n:
            return None
        marker = raw[pos]
        pos += 1
        if marker == 0xD9:  # EOI
            return pos
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue  # standalone marker, no length field
        if pos + 1 >= n:
            return None
        seg_len = (raw[pos] << 8) | raw[pos + 1]
        if seg_len < 2:
            return None
        seg_end = pos + seg_len
        if seg_end > n:
            return None
        if marker == 0xDA:  # SOS: entropy-coded data follows the header
            entropy_pos = _scan_entropy(raw, seg_end, n)
            if entropy_pos is None:
                return None
            pos = entropy_pos
        else:
            pos = seg_end
    return None


def _scan_entropy(raw: bytes, pos: int, n: int) -> int | None:
    """Return the offset of the next real marker after entropy data.

    Byte-stuffed ``FF 00`` sequences and restart markers (``FF D0``-``FF
    D7``) are part of the stream; any other ``FF xx`` marks the boundary.
    Returns None when the data ends without one.
    """
    while pos < n:
        if raw[pos] != 0xFF:
            pos += 1
            continue
        if pos + 1 >= n:
            return None
        nxt = raw[pos + 1]
        if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
            pos += 2  # byte stuffing or restart marker: still entropy data
            continue
        if nxt == 0xFF:
            pos += 1  # fill byte; re-examine the following byte
            continue
        return pos
    return None


def _walk_png(raw: bytes, start: int) -> int | None:
    """Walk the PNG chunk structure at *start*, returning its end.

    Each chunk is ``length(4) + type(4) + data + crc(4)``; the walk
    follows the length fields until the ``IEND`` chunk. Returns the
    offset just past ``IEND``, or None when a chunk type is not four
    ASCII letters, a chunk runs past end-of-file, or the image exceeds
    :data:`MAX_CARVE_BYTES`.
    """
    n = len(raw)
    pos = start + len(_PNG_SIG)
    while pos + 8 <= n:
        length = int.from_bytes(raw[pos:pos + 4], "big")
        ctype = raw[pos + 4:pos + 8]
        if not _is_png_type(ctype):
            return None
        chunk_end = pos + 12 + length  # length + type + data + CRC
        if chunk_end > n or chunk_end - start > MAX_CARVE_BYTES:
            return None
        if ctype == b"IEND":
            return chunk_end
        pos = chunk_end
    return None


def _is_png_type(chunk_type: bytes) -> bool:
    """Return True when *chunk_type* is four ASCII letters."""
    if len(chunk_type) != 4:
        return False
    return all(0x41 <= b <= 0x5A or 0x61 <= b <= 0x7A for b in chunk_type)


# --- partial XML decode -----------------------------------------------------


def _partial_inflate_entry(raw: bytes, header_offset: int) -> bytes | None:
    """Inflate the readable prefix of the entry at *header_offset*.

    Returns None when the local file header is destroyed or the entry is
    not raw-deflate (both correctly excluded from partial decode), else
    the bytes recovered before the deflate stream first errors out.
    """
    if header_offset < 0 or header_offset + _LFH_FIXED_SIZE > len(raw):
        return None
    header = raw[header_offset:header_offset + _LFH_FIXED_SIZE]
    if header[:len(_LFH_SIG)] != _LFH_SIG:
        return None
    (_sig, _ver, _flags, method, _mtime, _mdate, _crc, _comp, _uncomp,
     name_len, extra_len) = struct.unpack(_LFH_STRUCT, header)
    if method != _METHOD_DEFLATE:
        return None
    data_start = header_offset + _LFH_FIXED_SIZE + name_len + extra_len
    if data_start > len(raw):
        return None
    return _inflate_best_effort(raw, data_start)


def _inflate_best_effort(raw: bytes, data_start: int) -> bytes:
    """Inflate a raw-deflate stream from *data_start*, keeping the prefix.

    The compressor is fed one byte at a time so that every byte decoded
    before the first error is retained (a single wide ``decompress`` call
    spanning the corruption would discard its own good output). Output is
    capped at :data:`MAX_PARTIAL_XML_BYTES` to bound a decompression bomb.
    """
    decompressor = zlib.decompressobj(-15)
    out = bytearray()
    pos = data_start
    n = len(raw)
    try:
        while pos < n and not decompressor.eof:
            out.extend(decompressor.decompress(raw[pos:pos + 1]))
            pos += 1
            if len(out) >= MAX_PARTIAL_XML_BYTES:
                break
        if decompressor.eof:
            out.extend(decompressor.flush())
    except zlib.error:
        pass  # keep whatever was produced before the stream broke
    return bytes(out)


def _flatten_xml_name(name: str) -> str:
    """Return a flat, safe filename for a partial-XML entry *name*."""
    flat = name.replace("\\", "/").replace("/", "__").replace(":", "_")
    flat = flat.lstrip(".")
    if not flat:
        flat = "part"
    return flat + ".partial.xml"


# --- text extraction --------------------------------------------------------


def _extract_at_texts(data: bytes) -> list[str]:
    """Return the unescaped content of every ``<a:t>`` run in *data*."""
    text = data.decode("utf-8", errors="replace")
    results: list[str] = []
    for match in _A_T_RE.finditer(text):
        content = html.unescape(match.group(1))
        if content:
            results.append(content)
    return results
