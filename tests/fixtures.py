"""Test fixture helpers: synthetic .pptx generation and corruption injection.

This module builds small, self-contained ZIP archives that mimic the
package layout of a real .pptx file, and provides a handful of pure
byte-level transforms that reproduce the corruption patterns observed
in real OneDrive-damaged presentations (see ``PROJECT.md``). All
functions are deterministic given their ``seed`` argument, so tests can
rely on reproducible fixtures without shipping any real (and
non-public) broken files in the repository.
"""

from __future__ import annotations

import io
import random
import struct
import zipfile

#: struct format of the fixed 22-byte EOCD record (including signature).
_EOCD_STRUCT = "<IHHHHIIH"
_EOCD_SIG = b"PK\x05\x06"

#: Marker bytes observed in real foreign-data corruption, re-embedded
#: here so fixtures can exercise detection logic that looks for it.
_FOREIGN_MARKER = b"\x01\x00\x00\x00"
_FOREIGN_MARKER_STEP = 1000

# OPC/OOXML namespace URIs, factored out to keep the XML template
# strings below under a reasonable line length.
_NS_PACKAGE = "http://schemas.openxmlformats.org/package/2006"
_NS_CONTENT_TYPES = f"{_NS_PACKAGE}/content-types"
_NS_PACKAGE_REL = f"{_NS_PACKAGE}/relationships"
_NS_CORE_PROPS = f"{_NS_PACKAGE}/metadata/core-properties"
_NS_OFFICE_DOC = "http://schemas.openxmlformats.org/officeDocument/2006"
_NS_OFFICE_REL = f"{_NS_OFFICE_DOC}/relationships"
_NS_PML = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_DML = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Content-type prefixes shared by several package parts.
_CT_OFFICE = "application/vnd.openxmlformats-officedocument"
_CT_PACKAGE = "application/vnd.openxmlformats-package"
_CT_PML = f"{_CT_OFFICE}.presentationml"


def _xml(body: str) -> bytes:
    """Wrap *body* with an XML declaration and encode it as UTF-8.

    A thin helper so every part written by :func:`build_minimal_pptx`
    shares the same declaration without repeating it everywhere.
    """
    declaration = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    return (declaration + body).encode("utf-8")


def _content_types_xml(num_slides: int) -> bytes:
    """Build a minimal ``[Content_Types].xml`` covering all package parts."""
    slide_overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{n}.xml" '
        f'ContentType="{_CT_PML}.slide+xml"/>'
        for n in range(1, num_slides + 1)
    )
    tail_parts = (
        ("/ppt/presentation.xml", f"{_CT_PML}.presentation.main+xml"),
        ("/ppt/slideMasters/slideMaster1.xml", f"{_CT_PML}.slideMaster+xml"),
        ("/ppt/slideLayouts/slideLayout1.xml", f"{_CT_PML}.slideLayout+xml"),
        ("/ppt/theme/theme1.xml", f"{_CT_OFFICE}.theme+xml"),
        ("/ppt/presProps.xml", f"{_CT_PML}.presProps+xml"),
        ("/ppt/viewProps.xml", f"{_CT_PML}.viewProps+xml"),
        ("/ppt/tableStyles.xml", f"{_CT_PML}.tableStyles+xml"),
        ("/docProps/core.xml", f"{_CT_PACKAGE}.core-properties+xml"),
        ("/docProps/app.xml", f"{_CT_OFFICE}.extended-properties+xml"),
    )
    overrides = slide_overrides + "".join(
        f'<Override PartName="{part_name}" ContentType="{content_type}"/>'
        for part_name, content_type in tail_parts
    )
    body = (
        f'<Types xmlns="{_NS_CONTENT_TYPES}">'
        '<Default Extension="rels" '
        f'ContentType="{_CT_PACKAGE}.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="png" ContentType="image/png"/>'
        f"{overrides}"
        "</Types>"
    )
    return _xml(body)


def _relationships_xml(relationships: list[tuple[str, str, str]]) -> bytes:
    """Build a ``Relationships`` document from ``(id, type, target)`` triples."""
    rel_items = "".join(
        f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>'
        for rid, rtype, target in relationships
    )
    body = f'<Relationships xmlns="{_NS_PACKAGE_REL}">{rel_items}</Relationships>'
    return _xml(body)


def _package_rels_xml() -> bytes:
    """Build the package-level ``_rels/.rels`` part."""
    return _relationships_xml(
        [
            ("rId1", f"{_NS_OFFICE_REL}/officeDocument", "ppt/presentation.xml"),
            ("rId2", _NS_CORE_PROPS, "docProps/core.xml"),
            ("rId3", f"{_NS_OFFICE_REL}/extended-properties", "docProps/app.xml"),
        ]
    )


def _presentation_xml(num_slides: int) -> bytes:
    """Build ``ppt/presentation.xml`` with *num_slides* entries in ``sldIdLst``."""
    slide_ids = "".join(
        f'<p:sldId id="{256 + n}" r:id="rId{n + 1}"/>' for n in range(num_slides)
    )
    body = (
        f'<p:presentation xmlns:p="{_NS_PML}" xmlns:r="{_NS_OFFICE_REL}">'
        '<p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>'
        f"<p:sldIdLst>{slide_ids}</p:sldIdLst>"
        "</p:presentation>"
    )
    return _xml(body)


def _presentation_rels_xml(num_slides: int) -> bytes:
    """Build ``ppt/_rels/presentation.xml.rels`` referencing slides, master and theme."""
    relationships = [
        ("rId1", f"{_NS_OFFICE_REL}/slideMaster", "slideMasters/slideMaster1.xml")
    ]
    relationships.extend(
        (f"rId{n + 1}", f"{_NS_OFFICE_REL}/slide", f"slides/slide{n + 1}.xml")
        for n in range(num_slides)
    )
    relationships.append(
        (f"rId{num_slides + 2}", f"{_NS_OFFICE_REL}/theme", "theme/theme1.xml")
    )
    return _relationships_xml(relationships)


def _slide_xml(n: int) -> bytes:
    """Build a minimal ``ppt/slides/slide{n}.xml`` part, tagged with its slide number."""
    body = (
        f'<p:sld xmlns:p="{_NS_PML}" xmlns:a="{_NS_DML}">'
        f'<p:cSld name="Slide{n}"><p:spTree><p:nvGrpSpPr/><p:grpSpPr/></p:spTree></p:cSld>'
        "</p:sld>"
    )
    return _xml(body)


def _slide_rels_xml() -> bytes:
    """Build ``ppt/slides/_rels/slide{n}.xml.rels`` referencing the shared layout."""
    return _relationships_xml(
        [
            (
                "rId1",
                f"{_NS_OFFICE_REL}/slideLayout",
                "../slideLayouts/slideLayout1.xml",
            )
        ]
    )


def _slide_master_rels_xml() -> bytes:
    """Build ``ppt/slideMasters/_rels/slideMaster1.xml.rels``."""
    return _relationships_xml(
        [
            (
                "rId1",
                f"{_NS_OFFICE_REL}/slideLayout",
                "../slideLayouts/slideLayout1.xml",
            )
        ]
    )


def _slide_layout_rels_xml() -> bytes:
    """Build ``ppt/slideLayouts/_rels/slideLayout1.xml.rels``."""
    return _relationships_xml(
        [
            (
                "rId1",
                f"{_NS_OFFICE_REL}/slideMaster",
                "../slideMasters/slideMaster1.xml",
            )
        ]
    )


def _simple_part(root_tag: str, xmlns: str) -> bytes:
    """Build a trivial single-element XML part such as themes or view props."""
    return _xml(f'<{root_tag} xmlns="{xmlns}"/>')


def build_minimal_pptx(
    num_slides: int = 3, media_bytes: int = 1_048_576, seed: int = 0
) -> bytes:
    """Build a minimal but structurally valid .pptx-like ZIP in memory.

    The archive mimics the part layout and *write order* of a real
    PowerPoint-produced .pptx file: content types and package
    relationships first, then the presentation part and its slides,
    then masters/layouts/theme, then the (large) media part, and
    finally the small "tail" parts that survive many real-world
    truncation and overwrite corruptions. The XML bodies are
    deliberately trivial; only ZIP-level well-formedness is
    guaranteed, not renderability in PowerPoint.

    :param num_slides: number of ``ppt/slides/slideN.xml`` parts to emit.
    :param media_bytes: size in bytes of the embedded ``image1.png``
        payload, generated as incompressible random data so the
        resulting archive size tracks this value closely.
    :param seed: seed for the deterministic random media payload.
    :return: the complete ZIP archive as bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Content types describing every part below.
        zf.writestr("[Content_Types].xml", _content_types_xml(num_slides))

        # 2. Package-level relationships.
        zf.writestr("_rels/.rels", _package_rels_xml())

        # 3. Presentation part and its relationships.
        zf.writestr("ppt/presentation.xml", _presentation_xml(num_slides))
        zf.writestr(
            "ppt/_rels/presentation.xml.rels", _presentation_rels_xml(num_slides)
        )

        # 4. One slide part (plus relationships) per requested slide.
        for n in range(1, num_slides + 1):
            zf.writestr(f"ppt/slides/slide{n}.xml", _slide_xml(n))
            zf.writestr(f"ppt/slides/_rels/slide{n}.xml.rels", _slide_rels_xml())

        # 5. Shared master/layout/theme parts.
        zf.writestr(
            "ppt/slideMasters/slideMaster1.xml", _simple_part("p:sldMaster", _NS_PML)
        )
        zf.writestr(
            "ppt/slideMasters/_rels/slideMaster1.xml.rels",
            _slide_master_rels_xml(),
        )
        zf.writestr(
            "ppt/slideLayouts/slideLayout1.xml", _simple_part("p:sldLayout", _NS_PML)
        )
        zf.writestr(
            "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
            _slide_layout_rels_xml(),
        )
        zf.writestr("ppt/theme/theme1.xml", _simple_part("a:theme", _NS_DML))

        # 6. Large, incompressible media part; drives the overall archive
        # size close to media_bytes regardless of ZIP_DEFLATED overhead.
        media = random.Random(seed).randbytes(media_bytes)
        zf.writestr("ppt/media/image1.png", media)

        # 7. Small tail parts, written last to mirror the parts that
        # survive real-world head corruption/truncation.
        zf.writestr("ppt/presProps.xml", _simple_part("p:presentationPr", _NS_PML))
        zf.writestr("ppt/viewProps.xml", _simple_part("p:viewPr", _NS_PML))
        zf.writestr("ppt/tableStyles.xml", _simple_part("a:tblStyleLst", _NS_DML))
        zf.writestr(
            "docProps/core.xml",
            _xml(
                f'<cp:coreProperties xmlns:cp="{_NS_CORE_PROPS}" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title/></cp:coreProperties>'
            ),
        )
        zf.writestr(
            "docProps/app.xml",
            _simple_part("Properties", f"{_NS_OFFICE_DOC}/extended-properties"),
        )

    return buffer.getvalue()


def zero_prefix(data: bytes, length: int) -> bytes:
    """Replace the first *length* bytes of *data* with zeros.

    Mimics the ``HEAD_ZERO_FILL`` corruption pattern where leading
    chunks are overwritten with zeros while the file length is
    unchanged.
    """
    return b"\x00" * length + data[length:]


def foreign_prefix(data: bytes, length: int, seed: int = 1) -> bytes:
    """Replace the first *length* bytes of *data* with unrelated data.

    Mimics the ``HEAD_FOREIGN_DATA`` corruption pattern: the leading
    chunks are overwritten with non-zero data from an unrelated source.
    Two features of real-world samples are reproduced here:

    * a ``\\x01\\x00\\x00\\x00`` marker re-appears roughly every 1000
      bytes within the replaced region;
    * the replaced region never contains the byte sequence ``PK`` by
      accident, so it cannot be mistaken for a ZIP signature.
    """
    rng = random.Random(seed)
    prefix = bytearray(rng.randbytes(length))

    # Embed the marker bytes observed in real foreign-data corruption
    # at a roughly 1000-byte cadence.
    marker_len = len(_FOREIGN_MARKER)
    pos = 0
    while pos + marker_len <= length:
        prefix[pos : pos + marker_len] = _FOREIGN_MARKER
        pos += _FOREIGN_MARKER_STEP

    # Ensure no accidental "PK" sequence survives in the replaced
    # region; zero out the first byte of any match. Zero is neither
    # 'P' (0x50) nor 'K' (0x4B), so this cannot introduce a new match.
    pk = b"PK"
    search_from = 0
    while True:
        idx = prefix.find(pk, search_from)
        if idx == -1:
            break
        prefix[idx] = 0x00
        search_from = idx + 1

    return bytes(prefix) + data[length:]


def truncate(data: bytes, length: int) -> bytes:
    """Cut *data* down to its first *length* bytes.

    Mimics the ``TAIL_TRUNCATED`` corruption pattern where the file
    ends prematurely and the central directory is lost.
    """
    return data[:length]


def zero_range(data: bytes, start: int, end: int) -> bytes:
    """Replace ``data[start:end]`` with zero bytes, keeping the length unchanged."""
    return data[:start] + b"\x00" * (end - start) + data[end:]


def find_eocd(data: bytes) -> tuple[int, int, int]:
    """Locate and parse the last end-of-central-directory record in *data*.

    :return: a ``(cd_offset, cd_size, eocd_offset)`` tuple.
    :raises ValueError: if no EOCD signature is found, or the 22-byte
        fixed record cannot be read in full.
    """
    eocd_offset = data.rfind(_EOCD_SIG)
    if eocd_offset == -1:
        raise ValueError("EOCD signature not found")

    record = data[eocd_offset : eocd_offset + 22]
    if len(record) < 22:
        raise ValueError("EOCD record is truncated")

    fields = struct.unpack(_EOCD_STRUCT, record)
    cd_size = fields[5]
    cd_offset = fields[6]
    return (cd_offset, cd_size, eocd_offset)


def version_mix(old: bytes, new: bytes) -> bytes:
    """Synthesize a ``VERSION_MIX`` corruption: old head, new tail.

    Splices the leading part of *old* (up to its own central
    directory) onto the surviving central directory and EOCD of *new*,
    zeroing the gap between them. This reproduces files that appear to
    be a collage of chunks from two different save versions.

    :raises ValueError: propagated from :func:`find_eocd` if either
        archive lacks a readable EOCD record.
    """
    old_cd_offset, _old_cd_size, _old_eocd_offset = find_eocd(old)
    new_cd_offset, _new_cd_size, _new_eocd_offset = find_eocd(new)

    head_len = old_cd_offset
    assert len(new) > len(old) and head_len < new_cd_offset

    result = bytearray(new)
    result[:head_len] = old[:head_len]
    result[head_len:new_cd_offset] = b"\x00" * (new_cd_offset - head_len)
    return bytes(result)


class _NonSeekableWriter(io.RawIOBase):
    """A write-only, non-seekable stream that forwards writes to a buffer.

    Passing an instance of this class to :class:`zipfile.ZipFile`
    forces it into streaming mode, where entries are written with the
    local file header's bit 3 (data descriptor present) set instead of
    pre-computed size/CRC fields.
    """

    def __init__(self, buffer: io.BytesIO) -> None:
        super().__init__()
        self._buffer = buffer

    def writable(self) -> bool:
        """Report that the stream accepts writes."""
        return True

    def seekable(self) -> bool:
        """Report that the stream does not support seeking."""
        return False

    def write(self, b: bytes) -> int:
        """Forward *b* to the underlying buffer and return its length."""
        self._buffer.write(b)
        return len(b)


def build_zip_with_data_descriptors(entries: dict[str, bytes]) -> bytes:
    """Build a ZIP archive whose entries use trailing data descriptors.

    Writes *entries* through a non-seekable stream wrapper so that
    :mod:`zipfile` falls back to the data-descriptor local file header
    variant (flag bit 3 set) instead of writing sizes/CRC up front.

    :param entries: mapping of archive member name to its contents,
        written in dict iteration order.
    :return: the complete ZIP archive as bytes.
    """
    buffer = io.BytesIO()
    writer = _NonSeekableWriter(buffer)
    with zipfile.ZipFile(writer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()
