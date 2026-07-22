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
import zlib

#: struct format of the fixed 22-byte EOCD record (including signature).
_EOCD_STRUCT = "<IHHHHIIH"
_EOCD_SIG = b"PK\x05\x06"

#: Marker bytes observed in real foreign-data corruption, re-embedded
#: here so fixtures can exercise detection logic that looks for it.
_FOREIGN_MARKER = b"\x01\x00\x00\x00"
_FOREIGN_MARKER_STEP = 1000

#: Local file header signature (``PK\x03\x04``), used to locate entries
#: for :func:`zero_interior_entry`.
_LFH_SIG = b"PK\x03\x04"

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
_NS_EXT_PROPS = f"{_NS_OFFICE_DOC}/extended-properties"
_NS_VT = f"{_NS_OFFICE_DOC}/docPropsVTypes"
_NS_DC = "http://purl.org/dc/elements/1.1/"
_NS_DCTERMS = "http://purl.org/dc/terms/"
_NS_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_NS_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"

# Content-type prefixes shared by several package parts.
_CT_OFFICE = "application/vnd.openxmlformats-officedocument"
_CT_PACKAGE = "application/vnd.openxmlformats-package"
_CT_PML = f"{_CT_OFFICE}.presentationml"
_CT_CHART = f"{_CT_OFFICE}.drawingml.chart+xml"


def _xml(body: str) -> bytes:
    """Wrap *body* with an XML declaration and encode it as UTF-8.

    A thin helper so every part written by :func:`build_minimal_pptx`
    shares the same declaration without repeating it everywhere.
    """
    declaration = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    return (declaration + body).encode("utf-8")


def _content_types_xml(num_slides: int, include_chart: bool = False) -> bytes:
    """Build a minimal ``[Content_Types].xml`` covering all package parts.

    :param include_chart: when True, add the Override entry for
        ``ppt/charts/chart1.xml``.
    """
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
    if include_chart:
        tail_parts = tail_parts + (("/ppt/charts/chart1.xml", _CT_CHART),)
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
    shape = (
        "<p:sp><p:txBody><a:p><a:r>"
        f"<a:t>Slide {n} body text</a:t>"
        "</a:r></a:p></p:txBody></p:sp>"
    )
    body = (
        f'<p:sld xmlns:p="{_NS_PML}" xmlns:a="{_NS_DML}">'
        f'<p:cSld name="Slide{n}"><p:spTree><p:nvGrpSpPr/><p:grpSpPr/>'
        f"{shape}</p:spTree></p:cSld>"
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
    """Build ``ppt/slideMasters/_rels/slideMaster1.xml.rels``.

    Real PowerPoint masters always carry a theme relationship of their
    own (in addition to the presentation-level one); PowerPoint treats a
    themeless master as damage, so the fixture mirrors that shape.
    """
    return _relationships_xml(
        [
            (
                "rId1",
                f"{_NS_OFFICE_REL}/slideLayout",
                "../slideLayouts/slideLayout1.xml",
            ),
            (
                "rId2",
                f"{_NS_OFFICE_REL}/theme",
                "../theme/theme1.xml",
            ),
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


def _app_xml(num_slides: int) -> bytes:
    """Build ``docProps/app.xml`` with a slide-title heading pair.

    Emits Extended Properties with a ``HeadingPairs`` entry naming
    "Slide Titles" (paired with *num_slides*) and a matching
    ``TitlesOfParts`` vector of ``Slide Title {n}`` strings, so tests
    can exercise the HeadingPairs-sliced title recovery path.
    """
    titles = "".join(
        f"<vt:lpstr>Slide Title {n}</vt:lpstr>" for n in range(1, num_slides + 1)
    )
    body = (
        f'<Properties xmlns="{_NS_EXT_PROPS}" xmlns:vt="{_NS_VT}">'
        "<HeadingPairs>"
        '<vt:vector size="2" baseType="variant">'
        "<vt:variant><vt:lpstr>Slide Titles</vt:lpstr></vt:variant>"
        f'<vt:variant><vt:i4>{num_slides}</vt:i4></vt:variant>'
        "</vt:vector>"
        "</HeadingPairs>"
        "<TitlesOfParts>"
        f'<vt:vector size="{num_slides}" baseType="lpstr">{titles}</vt:vector>'
        "</TitlesOfParts>"
        "</Properties>"
    )
    return _xml(body)


def _core_xml() -> bytes:
    """Build ``docProps/core.xml`` with a creator and creation timestamp."""
    body = (
        f'<cp:coreProperties xmlns:cp="{_NS_CORE_PROPS}" xmlns:dc="{_NS_DC}" '
        f'xmlns:dcterms="{_NS_DCTERMS}" xmlns:xsi="{_NS_XSI}">'
        "<dc:title/>"
        "<dc:creator>Fixture Author</dc:creator>"
        '<dcterms:created xsi:type="dcterms:W3CDTF">'
        "2024-01-01T00:00:00Z</dcterms:created>"
        "</cp:coreProperties>"
    )
    return _xml(body)


def _chart_xml() -> bytes:
    """Build a minimal ``ppt/charts/chart1.xml`` with cached series data.

    One series with two categories and two numeric values, cached in
    ``c:strCache``/``c:numCache`` so chart-data recovery can be
    exercised without a companion embedded workbook.
    """
    body = (
        f'<c:chartSpace xmlns:c="{_NS_CHART}" xmlns:a="{_NS_DML}">'
        "<c:chart><c:plotArea><c:barChart><c:ser>"
        '<c:idx val="0"/><c:order val="0"/>'
        "<c:cat><c:strRef><c:f>Sheet1!$A$2:$A$3</c:f><c:strCache>"
        '<c:ptCount val="2"/>'
        '<c:pt idx="0"><c:v>Category A</c:v></c:pt>'
        '<c:pt idx="1"><c:v>Category B</c:v></c:pt>'
        "</c:strCache></c:strRef></c:cat>"
        "<c:val><c:numRef><c:f>Sheet1!$B$2:$B$3</c:f><c:numCache>"
        "<c:formatCode>General</c:formatCode>"
        '<c:ptCount val="2"/>'
        '<c:pt idx="0"><c:v>10.5</c:v></c:pt>'
        '<c:pt idx="1"><c:v>20.5</c:v></c:pt>'
        "</c:numCache></c:numRef></c:val>"
        "</c:ser></c:barChart></c:plotArea></c:chart>"
        "</c:chartSpace>"
    )
    return _xml(body)


def build_minimal_pptx(
    num_slides: int = 3, media_bytes: int = 1_048_576, seed: int = 0,
    include_chart: bool = False,
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
    :param include_chart: when True, also emit ``ppt/charts/chart1.xml``
        with cached series data (plus its Content_Types Override).
        Leaving this False (the default) reproduces the archive exactly
        as before this parameter was added.
    :return: the complete ZIP archive as bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        # 1. Content types describing every part below.
        zf.writestr(
            "[Content_Types].xml",
            _content_types_xml(num_slides, include_chart=include_chart),
        )

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

        # 6b. Optional chart part, with cached data for recovery tests.
        if include_chart:
            zf.writestr("ppt/charts/chart1.xml", _chart_xml())

        # 7. Small tail parts, written last to mirror the parts that
        # survive real-world head corruption/truncation.
        zf.writestr("ppt/presProps.xml", _simple_part("p:presentationPr", _NS_PML))
        zf.writestr("ppt/viewProps.xml", _simple_part("p:viewPr", _NS_PML))
        zf.writestr("ppt/tableStyles.xml", _simple_part("a:tblStyleLst", _NS_DML))
        zf.writestr("docProps/core.xml", _core_xml())
        zf.writestr("docProps/app.xml", _app_xml(num_slides))

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


def append_foreign_tail(data: bytes, length: int, seed: int = 1) -> bytes:
    """Append *length* bytes of unrelated data after *data*.

    Mimics the ``TAIL_FOREIGN_DATA`` corruption pattern: a complete
    archive followed by a large block of unindexed foreign data that
    hides its end-of-central-directory record from an ordinary ZIP
    reader. The appended block never contains the byte sequence ``PK``
    by accident, so it cannot be mistaken for a stray ZIP signature.
    """
    rng = random.Random(seed)
    tail = bytearray(rng.randbytes(length))

    # Ensure no accidental "PK" sequence survives in the appended
    # region; replace it with a two-byte sequence that cannot itself
    # form a new "PK" match ('Q' and 'x' are neither 'P' nor 'K').
    pk = b"PK"
    search_from = 0
    while True:
        idx = tail.find(pk, search_from)
        if idx == -1:
            break
        tail[idx : idx + 2] = b"Qx"
        search_from = idx + 2

    return data + bytes(tail)


def build_foreign_zip(entries: dict[str, bytes]) -> bytes:
    """Build a standalone ZIP that mimics an *unrelated* archive.

    Used to reproduce the real corruption in which fragments of a
    different ZIP (e.g. a Windows driver package) overwrote the head of
    a .pptx. Every member is deflate-compressed with a valid CRC, so a
    local file header scan of the resulting bytes accepts each as a
    genuine, CRC-valid entry whose name is unknown to the .pptx central
    directory.

    :param entries: mapping of member name to contents, written in dict
        iteration order.
    :return: the complete foreign ZIP archive as bytes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w",
                         compression=zipfile.ZIP_DEFLATED) as zf:
        for name, payload in entries.items():
            zf.writestr(name, payload)
    return buffer.getvalue()


def overlay_foreign_zip_head(data: bytes, boundary: int, foreign_zip: bytes,
                             insert_at: int = 4096, seed: int = 3) -> bytes:
    """Overwrite ``data[:boundary]`` with foreign, non-ZIP-headed bytes
    that embed *foreign_zip* verbatim starting at *insert_at*.

    Reproduces the ``HEAD_FOREIGN_DATA`` super-variant seen in real
    OneDrive corruption: the leading region is replaced by a synthetic
    stream whose first bytes are *not* a ZIP signature (so the scanner
    reports head kind ``"other"``), yet which contains, deeper inside,
    the intact local file headers of an unrelated ZIP archive. Those
    foreign headers survive a local-file-header scan as CRC-valid
    entries the .pptx central directory never listed. The bytes from
    *boundary* onward (the surviving tail entries plus the central
    directory and EOCD) are left untouched.

    :param boundary: end (exclusive) of the overwritten region; should
        be the header offset of a surviving tail entry so no entry
        straddles the seam.
    :param foreign_zip: the unrelated archive's bytes (see
        :func:`build_foreign_zip`).
    :param insert_at: offset within the overwritten region at which the
        foreign archive is embedded; must be strictly positive so the
        head itself stays non-ZIP filler.
    :param seed: seed for the deterministic non-ZIP filler bytes.
    :raises ValueError: when the foreign archive does not fit before
        *boundary*, or *insert_at* is not strictly positive.
    """
    if boundary > len(data):
        raise ValueError("boundary lies beyond the end of the data")
    if insert_at < 1 or insert_at + len(foreign_zip) > boundary:
        raise ValueError("foreign archive does not fit before the boundary")
    region = bytearray(random.Random(seed).randbytes(boundary))

    # Scrub every accidental "PK" so the only ZIP signatures left in the
    # head region are the foreign archive's own, inserted below.
    pk = b"PK"
    search_from = 0
    while True:
        idx = region.find(pk, search_from)
        if idx == -1:
            break
        region[idx] = 0x00
        search_from = idx + 1

    # Force a non-zero, non-ZIP head so the scanner reports head_kind
    # "other" rather than "zip" or "zeros".
    region[0:4] = b"\x01\x02\x03\x04"

    # Embed the intact foreign archive after some non-ZIP filler; done
    # after scrubbing so the foreign headers' own "PK" bytes survive.
    region[insert_at:insert_at + len(foreign_zip)] = foreign_zip
    return bytes(region) + data[boundary:]


def truncate(data: bytes, length: int) -> bytes:
    """Cut *data* down to its first *length* bytes.

    Mimics the ``TAIL_TRUNCATED`` corruption pattern where the file
    ends prematurely and the central directory is lost.
    """
    return data[:length]


def zero_range(data: bytes, start: int, end: int) -> bytes:
    """Replace ``data[start:end]`` with zero bytes, keeping the length unchanged."""
    return data[:start] + b"\x00" * (end - start) + data[end:]


def make_corrupted_copies(data: bytes, specs: list[list[tuple]]) -> list[bytes]:
    """Build several distinctly corrupted copies of *data*.

    Each element of *specs* describes one copy as an ordered list of
    corruption operations, applied left to right onto a fresh copy of
    *data* (an empty list leaves the copy untouched). Every operation is a
    tuple whose first element names an existing byte-level transform in
    this module:

    * ``("zero_range", start, end)`` -> :func:`zero_range`;
    * ``("foreign_prefix", length)`` -> :func:`foreign_prefix`;
    * ``("truncate", length)`` -> :func:`truncate`.

    Reproduces the real-world scenario in which several same-origin copies
    (a working file plus its sync-conflict twins) are each damaged in a
    different place, so merge restoration can splice the surviving byte
    ranges back together.

    :param specs: one operation list per copy to produce.
    :return: the corrupted copies, in the same order as *specs*.
    :raises ValueError: on an unknown operation name.
    """
    copies: list[bytes] = []
    for spec in specs:
        current = data
        for operation in spec:
            kind = operation[0]
            if kind == "zero_range":
                current = zero_range(current, operation[1], operation[2])
            elif kind == "foreign_prefix":
                current = foreign_prefix(current, operation[1])
            elif kind == "truncate":
                current = truncate(current, operation[1])
            else:
                raise ValueError(f"unknown corruption operation: {kind!r}")
        copies.append(current)
    return copies


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


def lfh_offsets(data: bytes) -> list[int]:
    """Return every local-file-header (``PK\\x03\\x04``) offset in *data*.

    Offsets are returned in ascending order; used by tests that need to
    target a specific surviving entry by position.
    """
    offsets: list[int] = []
    idx = data.find(_LFH_SIG)
    while idx != -1:
        offsets.append(idx)
        idx = data.find(_LFH_SIG, idx + 1)
    return offsets


def header_offset(data: bytes, name: str) -> int:
    """Return the local-file-header offset of member *name* in *data*."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.getinfo(name).header_offset


def zero_interior_entry(data: bytes, skip: int = 2) -> bytes:
    """Zero out one local file header entry deep inside *data*.

    Locates every ``PK\\x03\\x04`` (local file header) signature in
    *data*, then zeroes the span from the ``(skip + 1)``-th signature up
    to the next local file header signature -- or up to the central
    directory (located via :func:`find_eocd`) when it is the last one.
    The zeroed span swallows the entry's own signature, so it vanishes
    entirely from a raw local-file-header scan rather than surviving as
    a CRC-valid entry the central directory does not know about (which
    would risk a spurious ``VERSION_MIX`` verdict). The leading *skip*
    entries, the central directory and the EOCD record are left intact.

    Mimics the ``INTERIOR_DAMAGE`` corruption pattern: the file head and
    central directory survive, but one entry's data (and header) in the
    middle of the archive is destroyed.

    :param skip: number of leading local file header entries to leave
        untouched before picking the one to zero out.
    :raises ValueError: if fewer than ``skip + 1`` local file header
        signatures are found in *data*.
    """
    offsets = []
    search_from = 0
    while True:
        idx = data.find(_LFH_SIG, search_from)
        if idx == -1:
            break
        offsets.append(idx)
        search_from = idx + 1

    if len(offsets) <= skip:
        raise ValueError(
            f"expected more than {skip} local file header(s), "
            f"found {len(offsets)}"
        )

    start = offsets[skip]
    if skip + 1 < len(offsets):
        end = offsets[skip + 1]
    else:
        cd_offset, _cd_size, _eocd_offset = find_eocd(data)
        end = cd_offset

    return zero_range(data, start, end)


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


def build_minimal_jpeg(pad_to: int = 9000) -> bytes:
    """Build a syntactically valid, walkable JPEG bitstream.

    The marker sequence (SOI, a zero-filled COM used for padding, APP0,
    DQT, SOF0, SOS with a short 0xFF-free entropy stream, EOI) is
    complete enough for a structural JPEG walker to trace from start to
    end. The entropy data deliberately contains no ``0xFF`` byte so no
    byte-stuffing is needed, and the COM padding contains no ``PK``
    sequence, so the image can be embedded in foreign data without
    forging a stray ZIP signature.

    :param pad_to: minimum total size in bytes (padded via the COM
        segment); defaults above the carver's 8 KiB floor.
    :return: the complete JPEG as bytes.
    """
    soi = b"\xff\xd8"
    app0 = (b"\xff\xe0" + struct.pack(">H", 16)
            + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
    dqt = b"\xff\xdb" + struct.pack(">H", 67) + b"\x00" + bytes(range(1, 65))
    sof = (b"\xff\xc0" + struct.pack(">H", 17) + b"\x08"
           + struct.pack(">HH", 1, 1) + b"\x03"
           + b"\x01\x11\x00\x02\x11\x00\x03\x11\x00")
    sos = (b"\xff\xda" + struct.pack(">H", 12) + b"\x03"
           + b"\x01\x00\x02\x11\x03\x11" + b"\x00\x3f\x00")
    entropy = b"\x11\x22\x33\x44\x55\x66\x77\x00"
    eoi = b"\xff\xd9"
    tail = app0 + dqt + sof + sos + entropy + eoi

    pad_payload = max(0, pad_to - len(soi) - len(tail) - 4)
    com = b"\xff\xfe" + struct.pack(">H", pad_payload + 2) + b"\x00" * pad_payload
    return soi + com + tail


def build_minimal_png(pad_to: int = 9000) -> bytes:
    """Build a syntactically valid, walkable PNG bitstream.

    Emits the 8-byte signature followed by IHDR, a zero-filled tEXt
    chunk used for padding, IDAT and IEND, every chunk carrying a correct
    CRC so a structural PNG walker traces cleanly to IEND.

    :param pad_to: minimum total size in bytes (padded via the tEXt
        chunk); defaults above the carver's 8 KiB floor.
    :return: the complete PNG as bytes.
    """
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\xff\xff"))
    iend = _png_chunk(b"IEND", b"")

    fixed = len(sig) + len(ihdr) + len(idat) + len(iend)
    pad_payload = max(0, pad_to - fixed - 12)  # 12 = chunk length+type+CRC
    text = _png_chunk(b"tEXt", b"\x00" * pad_payload)
    return sig + ihdr + text + idat + iend


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    """Return one PNG chunk (length + type + data + CRC-32)."""
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(
        ">I", crc)


def rebuild_with_entries(data: bytes, extra: dict[str, bytes] | None = None,
                         stored: set[str] = frozenset()) -> bytes:
    """Rebuild the archive *data*, replacing/adding entries.

    Every original entry is re-emitted in order; a name present in
    *extra* replaces that entry's payload, and any remaining *extra*
    names are appended as new entries. Names listed in *stored* are
    written uncompressed (``ZIP_STORED``), so their raw payload appears
    verbatim in the resulting bytes -- handy for exercising the image
    carver against a stored media part.

    :param extra: mapping of member name to replacement/added payload.
    :param stored: names to write without compression.
    :return: the rebuilt ZIP archive as bytes.
    """
    pending = dict(extra or {})
    items: list[tuple[str, bytes]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            payload = pending.pop(name) if name in pending else zf.read(name)
            items.append((name, payload))
    items.extend(pending.items())

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as zf:
        for name, payload in items:
            compress = (zipfile.ZIP_STORED if name in stored
                        else zipfile.ZIP_DEFLATED)
            zf.writestr(name, payload, compress_type=compress)
    return buffer.getvalue()


def make_edited_version(data: bytes, *, replace: dict[str, bytes] | None = None,
                        add: dict[str, bytes] | None = None,
                        remove: list[str] | None = None) -> bytes:
    """Build an edited copy of an existing archive.

    Reads *data* as a ZIP and re-emits its entries in their original
    order and compression method, applying up to three edits: *replace*
    swaps in a new payload for an existing member, *remove* drops named
    members entirely, and *add* appends new members afterwards
    (``ZIP_DEFLATED``, except names under ``ppt/media/`` which are
    written ``ZIP_STORED`` to mimic PowerPoint's own uncompressed media
    parts). Used to synthesise a plausible "different version of the
    same presentation" -- most parts unchanged, a handful edited -- for
    same-origin scoring tests.

    :param replace: mapping of existing member name to its new payload.
    :param add: mapping of new member name to its payload.
    :param remove: member names to drop entirely.
    :return: the edited ZIP archive as bytes.
    """
    replace = dict(replace or {})
    remove_set = set(remove or [])
    items: list[tuple[str, bytes, int]] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            name = info.filename
            if name in remove_set:
                continue
            payload = replace.get(name, zf.read(name))
            items.append((name, payload, info.compress_type))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as zf:
        for name, payload, compress_type in items:
            zf.writestr(name, payload, compress_type=compress_type)
        for name, payload in (add or {}).items():
            compress_type = (zipfile.ZIP_STORED if name.startswith("ppt/media/")
                             else zipfile.ZIP_DEFLATED)
            zf.writestr(name, payload, compress_type=compress_type)
    return buffer.getvalue()


def zero_entry_data_tail(data: bytes, name: str,
                         keep_fraction: float = 0.6) -> bytes:
    """Zero the trailing part of one entry's compressed data.

    Keeps the local file header and the leading *keep_fraction* of the
    entry's compressed payload intact, zeroing the rest. The result
    mimics interior data damage whose local header survives, so the
    entry's deflate stream still decodes a readable prefix before
    breaking -- the exact case partial-XML recovery targets.

    :param name: member whose data tail is zeroed.
    :param keep_fraction: fraction of the compressed payload to preserve.
    :return: the damaged archive bytes (length unchanged).
    """
    offset = header_offset(data, name)
    (_sig, _ver, _flags, _method, _mtime, _mdate, _crc, comp_size,
     _uncomp, name_len, extra_len) = struct.unpack(
        "<IHHHHHIIIHH", data[offset:offset + 30])
    data_start = offset + 30 + name_len + extra_len
    keep = int(comp_size * keep_fraction)
    return zero_range(data, data_start + keep, data_start + comp_size)
