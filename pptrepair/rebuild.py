"""Package reconstruction (rebuild mode).

Rewrites salvaged entries into a fresh, consistent .pptx:

1. every salvaged entry is streamed into a new ZIP archive;
2. missing-but-required package plumbing is synthesised
   (``[Content_Types].xml`` regenerated from the actual entry set,
   ``_rels/.rels`` from a static template, and a default Office theme
   for any theme a surviving master still references but that the
   damage carried away);
3. references are reconciled so the surviving package is
   self-consistent: relationship entries whose targets are gone are
   pruned (external targets are kept), and ``<p:sldIdLst>`` /
   ``<p:sldMasterIdLst>`` etc. lose the ids whose parts vanished;
4. dangling relationship references left inside salvaged XML parts (an
   ``r:``-namespace attribute pointing at a relationship id that no
   longer survives) are cleaned up, so PowerPoint no longer offers to
   repair the rebuilt file;
5. the ``p:timing`` tree of each cleaned slide is reconciled with the
   shapes step 4 neutralised, so a ``p:video``/``p:audio`` node or an
   animation never points at a shape that lost its media or vanished.

Non-plumbing XML content is kept byte-identical whenever possible: a
part is only re-serialised when step 4 finds a dangling reference in
it, in which case the minimal enclosing element is removed (or, for
shared-fill blips and references no rule covers, only the offending
attribute) and the timing tree is reconciled in the same pass. Every
other salvaged part is streamed through unchanged, and package-plumbing
XML (``[Content_Types].xml``, every ``*.rels``, ``ppt/presentation.xml``)
is re-serialised with namespace prefixes preserved via
``ET.register_namespace``.
"""

from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pptrepair.salvage import SalvagedEntry, SalvageReader

#: Content types for [Content_Types].xml Default entries, by extension.
DEFAULT_CONTENT_TYPES: dict[str, str] = {
    "rels": "application/vnd.openxmlformats-package.relationships+xml",
    "xml": "application/xml",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
    "emf": "image/x-emf",
    "wmf": "image/x-wmf",
    "svg": "image/svg+xml",
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mov": "video/quicktime",
    "avi": "video/avi",
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "bin": "application/vnd.openxmlformats-officedocument.oleObject",
    "thmx": "application/vnd.openxmlformats-officedocument.themeManager+xml",
}

#: XML declaration prepended to every re-serialised or synthesised part.
_XML_DECLARATION = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'

#: Package parts that are always present in a rebuilt archive; when they
#: are missing from the salvage set they are synthesised from scratch.
_CONTENT_TYPES_NAME = "[Content_Types].xml"
_ROOT_RELS_NAME = "_rels/.rels"
_PRESENTATION_NAME = "ppt/presentation.xml"
_PRESENTATION_RELS_NAME = "ppt/_rels/presentation.xml.rels"

#: Namespace URIs needed to inspect presentation and relationship parts.
_PML_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_R_ID = f"{{{_R_NS}}}id"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

#: DrawingML main and PowerPoint 2010 namespaces, consulted only by the
#: dangling-reference cleanup step.
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P14_NS = "http://schemas.microsoft.com/office/powerpoint/2010/main"

#: Relationship type URIs used by the synthesised ``_rels/.rels``.
_REL_OFFICE_DOCUMENT = f"{_R_NS}/officeDocument"
_REL_EXTENDED = f"{_R_NS}/extended-properties"
_REL_CORE_PROPERTIES = (
    "http://schemas.openxmlformats.org/package/2006/"
    "relationships/metadata/core-properties"
)

#: Content-type prefixes shared by several package parts.
_CT_OFFICE = "application/vnd.openxmlformats-officedocument"
_CT_PACKAGE = "application/vnd.openxmlformats-package"
_CT_PML = f"{_CT_OFFICE}.presentationml"
_CT_DML = f"{_CT_OFFICE}.drawingml"

#: Ordered ``(part-name pattern, content type)`` rules for Override
#: synthesis; the first matching rule wins.
_OVERRIDE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^ppt/presentation\.xml$"),
     f"{_CT_PML}.presentation.main+xml"),
    (re.compile(r"^ppt/slides/slide\d+\.xml$"), f"{_CT_PML}.slide+xml"),
    (re.compile(r"^ppt/slideMasters/[^/]+\.xml$"),
     f"{_CT_PML}.slideMaster+xml"),
    (re.compile(r"^ppt/slideLayouts/[^/]+\.xml$"),
     f"{_CT_PML}.slideLayout+xml"),
    (re.compile(r"^ppt/notesSlides/[^/]+\.xml$"), f"{_CT_PML}.notesSlide+xml"),
    (re.compile(r"^ppt/notesMasters/[^/]+\.xml$"),
     f"{_CT_PML}.notesMaster+xml"),
    (re.compile(r"^ppt/theme/[^/]+\.xml$"), f"{_CT_OFFICE}.theme+xml"),
    (re.compile(r"^ppt/presProps\.xml$"), f"{_CT_PML}.presProps+xml"),
    (re.compile(r"^ppt/viewProps\.xml$"), f"{_CT_PML}.viewProps+xml"),
    (re.compile(r"^ppt/tableStyles\.xml$"), f"{_CT_PML}.tableStyles+xml"),
    (re.compile(r"^docProps/core\.xml$"), f"{_CT_PACKAGE}.core-properties+xml"),
    (re.compile(r"^docProps/app\.xml$"), f"{_CT_OFFICE}.extended-properties+xml"),
    (re.compile(r"^ppt/charts/chart\d+\.xml$"), f"{_CT_DML}.chart+xml"),
]

#: Presentation id lists whose children reference relationship ids.
_PRESENTATION_LISTS = (
    "sldIdLst", "sldMasterIdLst", "notesMasterIdLst", "handoutMasterIdLst")

#: Presentation id lists whose emptiness after reconciliation warrants a
#: warning (a valid presentation normally keeps at least one of each).
_EMPTY_WARNING_LISTS = ("sldIdLst", "sldMasterIdLst")

#: Matches ``xmlns`` / ``xmlns:prefix`` declarations in serialised XML.
_XMLNS_RE = re.compile(r"xmlns(?::([A-Za-z_][\w.\-]*))?\s*=")

#: Fallback ZIP timestamp when the original one is unavailable or invalid.
_DEFAULT_DATE_TIME = (1980, 1, 1, 0, 0, 0)

#: Fully-qualified element tags the reference-cleanup rules test against.
_P_PIC = f"{{{_PML_NS}}}pic"
_P_BLIPFILL = f"{{{_PML_NS}}}blipFill"
_P_EXT = f"{{{_PML_NS}}}ext"
_P_EXTLST = f"{{{_PML_NS}}}extLst"
_P_GRAPHICFRAME = f"{{{_PML_NS}}}graphicFrame"
_P_CUSTSHOW = f"{{{_PML_NS}}}custShow"
_P_SLD = f"{{{_PML_NS}}}sld"
_A_BLIP = f"{{{_A_NS}}}blip"
_P14_MEDIA = f"{{{_P14_NS}}}media"

#: PresentationML timing tags consulted by the timing-consistency pass
#: that follows reference cleanup (see :func:`_apply_timing_cleanup`).
_P_VIDEO = f"{{{_PML_NS}}}video"
_P_AUDIO = f"{{{_PML_NS}}}audio"
_P_PAR = f"{{{_PML_NS}}}par"
_P_CHILDTNLST = f"{{{_PML_NS}}}childTnLst"
_P_BLDLST = f"{{{_PML_NS}}}bldLst"
_P_SPTGT = f"{{{_PML_NS}}}spTgt"
_P_CNVPR = f"{{{_PML_NS}}}cNvPr"

#: DrawingML local names that carry a media *link* relationship (the
#: paired picture keeps its poster image once the link is dropped).
_A_MEDIA_LINK_LOCALS = frozenset(
    {"videoFile", "audioFile", "quickTimeFile", "wavAudioFile"})
#: DrawingML local names that carry a hyperlink relationship.
_A_HLINK_LOCALS = frozenset({"hlinkClick", "hlinkHover"})

#: Relationship types (matched by URI suffix) that bind a master part to
#: its theme; a master left without one makes PowerPoint offer to repair.
_REL_THEME_SUFFIX = "/theme"

#: Relationships parts whose lost ``/theme`` targets are re-synthesised.
_MASTER_RELS_PREFIXES = (
    "ppt/slideMasters/_rels/",
    "ppt/notesMasters/_rels/",
    "ppt/handoutMasters/_rels/",
)

#: A complete, schema-minimal default Office theme, used to replace a
#: theme part that the damage carried away while a master still
#: references it. The body is prefixed with :data:`_XML_DECLARATION`
#: before it is written, mirroring every other synthesised part.
_DEFAULT_THEME_XML = (
    f'<a:theme xmlns:a="{_A_NS}" name="Office Theme">'
    '<a:themeElements>'
    '<a:clrScheme name="Office">'
    '<a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>'
    '<a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>'
    '<a:dk2><a:srgbClr val="44546A"/></a:dk2>'
    '<a:lt2><a:srgbClr val="E7E6E6"/></a:lt2>'
    '<a:accent1><a:srgbClr val="4472C4"/></a:accent1>'
    '<a:accent2><a:srgbClr val="ED7D31"/></a:accent2>'
    '<a:accent3><a:srgbClr val="A5A5A5"/></a:accent3>'
    '<a:accent4><a:srgbClr val="FFC000"/></a:accent4>'
    '<a:accent5><a:srgbClr val="5B9BD5"/></a:accent5>'
    '<a:accent6><a:srgbClr val="70AD47"/></a:accent6>'
    '<a:hlink><a:srgbClr val="0563C1"/></a:hlink>'
    '<a:folHlink><a:srgbClr val="954F72"/></a:folHlink>'
    '</a:clrScheme>'
    '<a:fontScheme name="Office">'
    '<a:majorFont>'
    '<a:latin typeface="Calibri Light"/><a:ea typeface=""/>'
    '<a:cs typeface=""/>'
    '</a:majorFont>'
    '<a:minorFont>'
    '<a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/>'
    '</a:minorFont>'
    '</a:fontScheme>'
    '<a:fmtScheme name="Office">'
    '<a:fillStyleLst>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '</a:fillStyleLst>'
    '<a:lnStyleLst>'
    '<a:ln w="6350" cap="flat" cmpd="sng" algn="ctr">'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:prstDash val="solid"/></a:ln>'
    '<a:ln w="12700" cap="flat" cmpd="sng" algn="ctr">'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:prstDash val="solid"/></a:ln>'
    '<a:ln w="19050" cap="flat" cmpd="sng" algn="ctr">'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:prstDash val="solid"/></a:ln>'
    '</a:lnStyleLst>'
    '<a:effectStyleLst>'
    '<a:effectStyle><a:effectLst/></a:effectStyle>'
    '<a:effectStyle><a:effectLst/></a:effectStyle>'
    '<a:effectStyle><a:effectLst/></a:effectStyle>'
    '</a:effectStyleLst>'
    '<a:bgFillStyleLst>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    '</a:bgFillStyleLst>'
    '</a:fmtScheme>'
    '</a:themeElements>'
    '<a:objectDefaults/>'
    '<a:extraClrSchemeLst/>'
    '</a:theme>'
).encode()

#: One dangling reference: ``(attribute key, local name, relationship id)``.
_DanglingAttr = tuple[str, str, str]
#: An element together with the dangling references it carries.
_Carrier = tuple[ET.Element, list[_DanglingAttr]]


@dataclass
class RebuildResult:
    """Outcome of one package reconstruction."""

    output_path: Path
    written_entries: list[str] = field(default_factory=list)
    synthesized_parts: list[str] = field(default_factory=list)
    """Names of parts that had to be generated from scratch."""
    pruned_relationships: list[str] = field(default_factory=list)
    """Removed relationships as ``"<rels file>: <rId> -> <target>"``."""
    pruned_slide_ids: list[str] = field(default_factory=list)
    """Removed presentation list ids as ``"<list>: <rId>"``."""
    cleaned_parts: list[str] = field(default_factory=list)
    """Names of parts whose dangling references were cleaned up."""
    removed_elements: list[str] = field(default_factory=list)
    """Removed carriers as ``"<part>: <element> (<rIds>)"`` and
    attribute-only removals as ``"<part>: @<attr> on <element> (<rId>)"``."""
    warnings: list[str] = field(default_factory=list)


def rebuild_package(reader: SalvageReader,
                    salvaged: list[SalvagedEntry],
                    output_path: Path) -> RebuildResult:
    """Rebuild a consistent .pptx at *output_path* from *salvaged*.

    Implementation requirements:

    * ``ppt/presentation.xml`` must be present among *salvaged*; raise
      :class:`RebuildError` otherwise (mode selection guarantees this
      for the automatic path).
    * Package-plumbing XML ([Content_Types].xml, every ``*.rels``,
      ``ppt/presentation.xml``) is buffered, transformed against the
      final entry set and re-serialised with preserved namespace
      prefixes and an XML declaration; every other entry is streamed
      through unmodified (byte-identical), carrying over the original
      timestamp via ``reader.datetime_of`` when available.
    * ``[Content_Types].xml``: when salvaged, prune Overrides pointing
      at missing parts and ensure Defaults cover every present
      extension; when lost, synthesise it entirely from
      :data:`DEFAULT_CONTENT_TYPES` plus Overrides for the known part
      kinds.
    * ``_rels/.rels``: synthesise from a static template when lost
      (presentation part plus docProps parts actually present).
    * Relationship pruning happens before slide-list pruning so that
      ``<p:sldId>`` entries are dropped exactly when their relationship
      disappeared. ``TargetMode="External"`` relationships are never
      pruned.
    * After reference reconciliation, every salvaged XML part (other than
      ``*.rels`` and ``[Content_Types].xml``) is scanned for dangling
      relationship references and re-serialised only when at least one is
      found; otherwise it is streamed byte-identical.
      ``ppt/presentation.xml`` is scanned in its already-pruned form and
      appears in ``cleaned_parts`` only when that scan changes it (an
      id-list prune alone does not count).
    * The output must not exist beforehand (caller handles --force).
    """
    salvaged_by_name = {entry.name: entry for entry in salvaged}
    if _PRESENTATION_NAME not in salvaged_by_name:
        raise RebuildError(
            "cannot rebuild without a salvaged ppt/presentation.xml")

    result = RebuildResult(output_path=output_path)

    # The final entry set is everything salvaged plus the two plumbing
    # parts that always end up in the output (synthesised if lost). It
    # is fixed here so reference reconciliation can test targets against
    # the exact set of parts the archive will contain.
    final_names = set(salvaged_by_name)
    final_names.add(_CONTENT_TYPES_NAME)
    final_names.add(_ROOT_RELS_NAME)

    plumbing: dict[str, bytes] = {}
    plumbing_date_time: dict[str, tuple[int, ...] | None] = {}

    # 0. Synthesise any theme part a master still references but that the
    #    damage carried away, so no master is left themeless (which makes
    #    PowerPoint offer to repair the file). Done before step 1 so the
    #    relationship pruning below keeps the master's theme relationship.
    _synthesize_missing_themes(
        reader, salvaged, final_names, plumbing, plumbing_date_time, result)

    # 1. Prune every surviving relationship part, remembering the set of
    #    relationship ids that survived in each so later steps can tell
    #    which references still resolve.
    surviving_by_rels: dict[str, set[str]] = {}
    for entry in salvaged:
        if not entry.name.endswith(".rels"):
            continue
        data = _read_entry(reader, entry)
        new_bytes, surviving = _prune_rels(
            entry.name, data, final_names, result.pruned_relationships)
        plumbing[entry.name] = new_bytes
        plumbing_date_time[entry.name] = reader.datetime_of(entry)
        surviving_by_rels[entry.name] = surviving
    surviving_presentation_rids = surviving_by_rels.get(
        _PRESENTATION_RELS_NAME)

    # 2. Prune the presentation part's id lists against the surviving
    #    relationships (skipped when the presentation rels part is gone,
    #    since then no trustworthy id set exists).
    presentation_entry = salvaged_by_name[_PRESENTATION_NAME]
    presentation_data = _read_entry(reader, presentation_entry)
    plumbing[_PRESENTATION_NAME] = _prune_presentation(
        presentation_data, surviving_presentation_rids,
        result.pruned_slide_ids, result.warnings)
    plumbing_date_time[_PRESENTATION_NAME] = reader.datetime_of(
        presentation_entry)

    # 3. Content types: prune Overrides / top up Defaults, or synthesise.
    if _CONTENT_TYPES_NAME in salvaged_by_name:
        entry = salvaged_by_name[_CONTENT_TYPES_NAME]
        plumbing[_CONTENT_TYPES_NAME] = _prune_content_types(
            _read_entry(reader, entry), final_names, result.warnings)
        plumbing_date_time[_CONTENT_TYPES_NAME] = reader.datetime_of(entry)
    else:
        plumbing[_CONTENT_TYPES_NAME] = _synthesize_content_types(
            final_names, result.warnings)
        plumbing_date_time[_CONTENT_TYPES_NAME] = None
        result.synthesized_parts.append(_CONTENT_TYPES_NAME)

    # 4. Package relationships: synthesise only when lost (a surviving
    #    ``_rels/.rels`` was already pruned by the loop in step 1).
    if _ROOT_RELS_NAME not in salvaged_by_name:
        plumbing[_ROOT_RELS_NAME] = _synthesize_root_rels(final_names)
        plumbing_date_time[_ROOT_RELS_NAME] = None
        result.synthesized_parts.append(_ROOT_RELS_NAME)

    # 4b. Reference cleanup: strip dangling relationship references left
    #     inside salvaged XML parts so PowerPoint accepts the rebuilt
    #     file without offering to repair it. Parts without any dangling
    #     reference are left untouched (streamed byte-identical below).
    for entry in salvaged:
        name = entry.name
        if not name.endswith(".xml") or name.endswith(".rels"):
            continue
        if name == _CONTENT_TYPES_NAME:
            continue
        rels_name = _rels_part_name(name)
        surviving_ids = surviving_by_rels.get(rels_name)
        rels_missing = surviving_ids is None
        # ``ppt/presentation.xml`` is inspected in its already-pruned
        # form; every other part is read straight from the salvage set.
        source = plumbing.get(name)
        if source is None:
            source = _read_entry(reader, entry)
        cleaned, changed = _clean_dangling_refs(
            name, source, surviving_ids if surviving_ids is not None else set(),
            rels_missing, rels_name, result.removed_elements, result.warnings)
        if changed:
            plumbing[name] = cleaned
            plumbing_date_time[name] = reader.datetime_of(entry)
            result.cleaned_parts.append(name)

    # 5. Write the new archive: buffered plumbing first, then every other
    #    salvaged entry streamed through unchanged.
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in _plumbing_order(plumbing):
            info = _zip_info(name, plumbing_date_time.get(name))
            zf.writestr(info, plumbing[name])
            result.written_entries.append(name)
        for entry in salvaged:
            if entry.name in plumbing:
                continue
            info = _zip_info(entry.name, reader.datetime_of(entry))
            with zf.open(info, "w") as dest:
                for chunk in reader.open(entry):
                    dest.write(chunk)
            result.written_entries.append(entry.name)

    return result


class RebuildError(Exception):
    """Raised when a package cannot be rebuilt from the salvage set."""


def _read_entry(reader: SalvageReader, entry: SalvagedEntry) -> bytes:
    """Read the whole payload of *entry* into memory.

    Only used for small package-plumbing parts, which comfortably fit in
    memory; large parts are streamed instead.
    """
    return b"".join(reader.open(entry))


def _plumbing_order(plumbing: dict[str, bytes]) -> list[str]:
    """Return plumbing part names in a stable, conventional write order."""
    preferred = [_CONTENT_TYPES_NAME, _ROOT_RELS_NAME, _PRESENTATION_NAME]
    ordered = [name for name in preferred if name in plumbing]
    ordered += sorted(name for name in plumbing if name not in preferred)
    return ordered


def _safe_date_time(date_time: tuple[int, ...] | None) -> tuple[int, ...]:
    """Return a ZIP-storable timestamp, falling back to 1980-01-01.

    ZIP timestamps cannot represent years before 1980, so an unusable
    value is replaced by the canonical minimum.
    """
    if date_time is None or date_time[0] < 1980:
        return _DEFAULT_DATE_TIME
    return tuple(date_time)


def _zip_info(name: str, date_time: tuple[int, ...] | None) -> zipfile.ZipInfo:
    """Build a deflated :class:`zipfile.ZipInfo` for *name*."""
    info = zipfile.ZipInfo(name, date_time=_safe_date_time(date_time))
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def _reserialize(data: bytes, edit: Callable[[ET.Element], None]) -> bytes:
    """Parse *data*, apply *edit* to its root, and re-serialise it.

    Namespace prefixes declared in the original document are preserved,
    including declarations that :mod:`xml.etree.ElementTree` would drop
    because no element or attribute actually uses them (for example a
    ``p14`` prefix referenced only from an ``mc:Ignorable`` value). Those
    otherwise-lost declarations are re-attached to the root element so
    consumers that honour them (PowerPoint) keep accepting the file.
    """
    declarations = _collect_namespaces(data)
    for prefix, uri in declarations:
        try:
            ET.register_namespace(prefix, uri)
        except ValueError:
            # Reserved prefixes (e.g. ns\d+) cannot be registered; the
            # serializer picks its own prefix for those URIs instead.
            pass

    root = ET.fromstring(data)
    edit(root)

    # First pass: discover the declarations the serializer emits on its
    # own, so only the ones it would otherwise lose are re-attached.
    emitted = _declared_prefixes(ET.tostring(root, encoding="unicode"))
    for prefix, uri in declarations:
        if prefix in emitted:
            continue
        attribute = "xmlns" if prefix == "" else f"xmlns:{prefix}"
        # A literal ``xmlns[:prefix]`` attribute round-trips verbatim
        # through the serializer, restoring the lost declaration.
        if attribute not in root.attrib:
            root.set(attribute, uri)

    body = ET.tostring(root, encoding="unicode")
    return _XML_DECLARATION + body.encode("utf-8")


def _collect_namespaces(data: bytes) -> list[tuple[str, str]]:
    """Return the ``(prefix, uri)`` namespace declarations of *data*.

    Order is preserved and duplicates are dropped, so the caller can
    re-register and re-attach declarations deterministically.
    """
    declarations: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for _event, node in ET.iterparse(io.BytesIO(data), events=("start-ns",)):
        prefix, uri = node
        if (prefix, uri) not in seen:
            seen.add((prefix, uri))
            declarations.append((prefix, uri))
    return declarations


def _declared_prefixes(xml_text: str) -> set[str]:
    """Return the namespace prefixes declared in *xml_text*.

    The empty string represents the default (``xmlns=...``) declaration.
    """
    return {match.group(1) or "" for match in _XMLNS_RE.finditer(xml_text)}


def _rels_base(rels_name: str) -> str:
    """Return the package-relative base directory of a ``*.rels`` part.

    A relationships part ``X/_rels/Y.rels`` resolves its targets against
    ``X/`` (the empty string for the package root ``_rels/.rels``).
    """
    index = rels_name.rfind("_rels/")
    if index == -1:
        return ""
    return rels_name[:index]


def _resolve_target(base: str, target: str) -> str:
    """Resolve a relationship *target* against its *base* directory.

    A leading slash makes the target package-root relative; otherwise it
    is joined onto *base*. The result is POSIX-normalised so ``../`` and
    ``./`` segments are collapsed.
    """
    if target.startswith("/"):
        cleaned = target[1:]
    else:
        cleaned = posixpath.join(base, target)
    return posixpath.normpath(cleaned)


def _prune_rels(rels_name: str, data: bytes, final_names: set[str],
                pruned: list[str]) -> tuple[bytes, set[str]]:
    """Drop relationships whose targets vanished; keep external ones.

    Returns the re-serialised part together with the set of relationship
    ids that survived (needed to reconcile the presentation id lists).
    """
    base = _rels_base(rels_name)
    surviving: set[str] = set()

    def edit(root: ET.Element) -> None:
        for child in list(root):
            if not child.tag.endswith("}Relationship") \
                    and child.tag != "Relationship":
                continue
            rid = child.get("Id")
            if child.get("TargetMode") == "External":
                # External targets live outside the package and are never
                # pruned.
                if rid is not None:
                    surviving.add(rid)
                continue
            resolved = _resolve_target(base, child.get("Target", ""))
            if resolved in final_names:
                if rid is not None:
                    surviving.add(rid)
            else:
                root.remove(child)
                pruned.append(f"{rels_name}: {rid} -> {resolved}")

    return _reserialize(data, edit), surviving


def _prune_presentation(data: bytes, surviving_rids: set[str] | None,
                        pruned: list[str], warnings: list[str]) -> bytes:
    """Drop presentation id-list children whose relationship is gone.

    When *surviving_rids* is None (the presentation rels part did not
    survive), the id lists are left untouched, since no trustworthy id
    set exists to reconcile against.
    """
    def edit(root: ET.Element) -> None:
        for list_local in _PRESENTATION_LISTS:
            list_elem = root.find(f"{{{_PML_NS}}}{list_local}")
            if list_elem is None:
                continue
            if surviving_rids is not None:
                for child in list(list_elem):
                    rid = child.get(_R_ID)
                    if rid is not None and rid not in surviving_rids:
                        list_elem.remove(child)
                        pruned.append(f"{list_local}: {rid}")
            if list_local in _EMPTY_WARNING_LISTS and len(list_elem) == 0:
                warnings.append(
                    f"ppt/presentation.xml: {list_local} is empty after "
                    "reconciliation")

    return _reserialize(data, edit)


def _extension(name: str) -> str | None:
    """Return the lower-cased extension of a part name, or None."""
    base = name.rsplit("/", 1)[-1]
    if "." not in base:
        return None
    return base.rsplit(".", 1)[-1].lower()


def _present_extensions(names: set[str]) -> set[str]:
    """Return the set of lower-cased extensions used by *names*."""
    extensions: set[str] = set()
    for name in names:
        extension = _extension(name)
        if extension is not None:
            extensions.add(extension)
    return extensions


def _prune_content_types(data: bytes, final_names: set[str],
                         warnings: list[str]) -> bytes:
    """Prune stale Overrides and top up missing Default extensions."""
    default_tag = f"{{{_CT_NS}}}Default"
    override_tag = f"{{{_CT_NS}}}Override"

    def edit(root: ET.Element) -> None:
        # Drop Overrides whose part is no longer in the archive.
        for child in list(root):
            if child.tag != override_tag:
                continue
            if _override_part(child) not in final_names:
                root.remove(child)
        # Ensure every surviving extension has a Default entry.
        existing = {
            child.get("Extension", "").lower()
            for child in root if child.tag == default_tag
        }
        for extension in sorted(_present_extensions(final_names)):
            if extension in existing:
                continue
            content_type = _default_content_type(extension, warnings)
            default = ET.Element(default_tag)
            default.set("Extension", extension)
            default.set("ContentType", content_type)
            root.insert(0, default)
            existing.add(extension)
        # Ensure every part that needs an Override actually has one; this
        # backfills Overrides for parts synthesised after the fact (e.g. a
        # replacement theme), which the salvaged Content_Types could not
        # have listed.
        overridden = {
            _override_part(child)
            for child in root if child.tag == override_tag
        }
        for part in sorted(final_names):
            content_type = _override_content_type(part)
            if content_type is None or part in overridden:
                continue
            override = ET.Element(override_tag)
            override.set("PartName", f"/{part}")
            override.set("ContentType", content_type)
            root.append(override)
            overridden.add(part)

    return _reserialize(data, edit)


def _override_part(child: ET.Element) -> str:
    """Return the package-relative PartName of an Override *child*."""
    part = child.get("PartName", "")
    return part.removeprefix("/")


def _default_content_type(extension: str, warnings: list[str]) -> str:
    """Return the Default content type for *extension*.

    Unknown extensions fall back to ``application/octet-stream`` and add
    a warning so the caller can flag the guess.
    """
    content_type = DEFAULT_CONTENT_TYPES.get(extension)
    if content_type is None:
        warnings.append(
            f"[Content_Types].xml: no known content type for extension "
            f"'{extension}', using application/octet-stream")
        return "application/octet-stream"
    return content_type


def _override_content_type(name: str) -> str | None:
    """Return the Override content type for *name*, or None."""
    for pattern, content_type in _OVERRIDE_RULES:
        if pattern.match(name):
            return content_type
    return None


def _synthesize_content_types(final_names: set[str],
                              warnings: list[str]) -> bytes:
    """Build a complete ``[Content_Types].xml`` from the entry set."""
    defaults: list[tuple[str, str]] = []
    seen: set[str] = set()
    # ``rels`` and ``xml`` are mandatory; the remaining extensions come
    # from the parts actually present.
    for extension in ["rels", "xml", *sorted(_present_extensions(final_names))]:
        if extension in seen:
            continue
        seen.add(extension)
        defaults.append((extension, _default_content_type(extension, warnings)))

    overrides: list[tuple[str, str]] = []
    for name in sorted(final_names):
        content_type = _override_content_type(name)
        if content_type is not None:
            overrides.append((name, content_type))

    parts = [f'<Types xmlns="{_CT_NS}">']
    for extension, content_type in defaults:
        parts.append(
            f'<Default Extension="{extension}" ContentType="{content_type}"/>')
    for name, content_type in overrides:
        parts.append(
            f'<Override PartName="/{name}" ContentType="{content_type}"/>')
    parts.append("</Types>")
    return _XML_DECLARATION + "".join(parts).encode("utf-8")


def _synthesize_root_rels(final_names: set[str]) -> bytes:
    """Build a minimal ``_rels/.rels`` for the surviving core parts."""
    relationships = [("rId1", _REL_OFFICE_DOCUMENT, _PRESENTATION_NAME)]
    next_id = 2
    if "docProps/core.xml" in final_names:
        relationships.append(
            (f"rId{next_id}", _REL_CORE_PROPERTIES, "docProps/core.xml"))
        next_id += 1
    if "docProps/app.xml" in final_names:
        relationships.append(
            (f"rId{next_id}", _REL_EXTENDED, "docProps/app.xml"))
        next_id += 1

    parts = [f'<Relationships xmlns="{_REL_NS}">']
    for rid, rel_type, target in relationships:
        parts.append(
            f'<Relationship Id="{rid}" Type="{rel_type}" Target="{target}"/>')
    parts.append("</Relationships>")
    return _XML_DECLARATION + "".join(parts).encode("utf-8")


def _synthesize_missing_themes(
        reader: SalvageReader, salvaged: list[SalvagedEntry],
        final_names: set[str], plumbing: dict[str, bytes],
        plumbing_date_time: dict[str, tuple[int, ...] | None],
        result: RebuildResult) -> None:
    """Replace themes a master still references but the damage removed.

    Scans the salvaged ``slideMasters`` / ``notesMasters`` /
    ``handoutMasters`` relationships parts for ``/theme`` relationships
    whose (internal) target is absent from *final_names*. Each such target
    is registered in *plumbing* under its original name with a default
    Office theme and added to *final_names*, so the later relationship
    pruning keeps the master's theme relationship instead of orphaning the
    master. Mutates *final_names*, *plumbing*, *plumbing_date_time* and
    *result* in place.
    """
    for entry in salvaged:
        name = entry.name
        if not name.endswith(".rels"):
            continue
        if not any(name.startswith(prefix)
                   for prefix in _MASTER_RELS_PREFIXES):
            continue
        try:
            root = ET.fromstring(_read_entry(reader, entry))
        except ET.ParseError:
            continue
        base = _rels_base(name)
        for child in root:
            if not isinstance(child.tag, str):
                continue
            if _split_qname(child.tag)[1] != "Relationship":
                continue
            if not child.get("Type", "").endswith(_REL_THEME_SUFFIX):
                continue
            if child.get("TargetMode") == "External":
                continue
            target = _resolve_target(base, child.get("Target", ""))
            if target in final_names:
                continue
            plumbing[target] = _XML_DECLARATION + _DEFAULT_THEME_XML
            plumbing_date_time[target] = None
            final_names.add(target)
            result.synthesized_parts.append(target)
            result.warnings.append(
                f"{target}: theme was lost with the damage; a default "
                "theme was synthesized")


def _rels_part_name(name: str) -> str:
    """Return the ``.rels`` part that declares *name*'s relationships.

    A part ``X/Y`` resolves its relationship ids from ``X/_rels/Y.rels``.
    """
    directory = posixpath.dirname(name)
    base = posixpath.basename(name)
    return posixpath.join(directory, "_rels", f"{base}.rels")


def _split_qname(tag: str) -> tuple[str, str]:
    """Split a ``{uri}local`` tag into its namespace URI and local name.

    Unqualified names return an empty namespace URI.
    """
    if tag.startswith("{"):
        uri, _closing, local = tag[1:].partition("}")
        return uri, local
    return "", tag


def _collect_carriers(root: ET.Element,
                      surviving: set[str]) -> tuple[list[_Carrier], bool]:
    """Collect elements carrying a dangling relationship reference.

    Returns the ``(element, dangling)`` pairs -- where *dangling* lists
    the ``(attribute key, local name, relationship id)`` triples whose id
    is not in *surviving* -- together with a flag telling whether the
    part uses the relationships namespace at all. Empty values are
    ignored, since an empty ``r:id`` is a legitimate internal anchor
    rather than a dangling reference.
    """
    carriers: list[_Carrier] = []
    has_r_attr = False
    for elem in root.iter():
        dangling: list[_DanglingAttr] = []
        for key, value in elem.attrib.items():
            uri, local = _split_qname(key)
            if uri != _R_NS:
                continue
            has_r_attr = True
            if value and value not in surviving:
                dangling.append((key, local, value))
        if dangling:
            carriers.append((elem, dangling))
    return carriers, has_r_attr


def _clean_dangling_refs(name: str, data: bytes, surviving: set[str],
                         rels_missing: bool, rels_name: str,
                         removed_elements: list[str],
                         warnings: list[str]) -> tuple[bytes, bool]:
    """Strip dangling relationship references from one XML part.

    Returns ``(bytes, changed)``. When the part has no dangling reference
    (or cannot be parsed) the original bytes are returned with
    ``changed=False`` so the caller keeps the part byte-identical.
    """
    try:
        probe = ET.fromstring(data)
    except ET.ParseError:
        warnings.append(
            f"{name}: could not be parsed for reference cleanup; "
            "left unchanged")
        return data, False

    carriers, has_r_attr = _collect_carriers(probe, surviving)
    if rels_missing and has_r_attr:
        warnings.append(
            f"{name}: relationships part {rels_name} is missing; "
            "treating all references as dangling")
    if not carriers:
        return data, False

    def edit(root: ET.Element) -> None:
        _apply_cleanup(name, root, surviving, removed_elements, warnings)

    return _reserialize(data, edit), True


def _apply_cleanup(name: str, root: ET.Element, surviving: set[str],
                   removed_elements: list[str],
                   warnings: list[str]) -> None:
    """Resolve and apply the removal rules for one part's carriers."""
    parent_map = {child: parent for parent in root.iter() for child in parent}
    carriers, _has_r_attr = _collect_carriers(root, surviving)

    # Shape ids the cleanup neutralises, tracked so the timing tree can be
    # reconciled afterwards: ``stilled_ids`` are shapes that survive as a
    # still image (their media link was dropped), ``removed_ids`` are
    # shapes removed outright with their subtree.
    stilled_ids: set[str] = set()
    removed_ids: set[str] = set()

    # Bucket every carrier into a whole-element removal (keyed by the
    # anchor element it resolves to) or an attribute-only removal.
    element_removals: dict[ET.Element, list] = {}
    attr_removals: list[tuple[ET.Element, list[_DanglingAttr], bool]] = []
    for elem, dangling in carriers:
        kind, target = _resolve_rule(elem, parent_map)
        # A dropped media link leaves its host picture behind as a still
        # image; remember that shape so its timing node can be removed.
        uri, local = _split_qname(elem.tag)
        if uri == _A_NS and local in _A_MEDIA_LINK_LOCALS:
            host_id = _host_shape_cnvpr_id(elem, parent_map)
            if host_id is not None:
                stilled_ids.add(host_id)
        if kind in ("element", "ext"):
            slot = element_removals.get(target)
            if slot is None:
                element_removals[target] = [kind, [(elem, dangling)]]
            else:
                slot[1].append((elem, dangling))
        else:
            attr_removals.append((elem, dangling, kind == "attrs_warn"))

    # Drop anchors nested inside another anchor's subtree: the outer
    # removal already discards them.
    anchor_set = set(element_removals)
    kept_anchors = [anchor for anchor in element_removals
                    if not _has_ancestor_in(anchor, anchor_set, parent_map)]
    kept_set = set(kept_anchors)

    # Remove whole elements and record what went (aggregating every
    # dangling id that pointed into each anchor).
    extlst_cascade: list[ET.Element] = []
    for anchor in kept_anchors:
        kind, entries = element_removals[anchor]
        rids = _unique([value for _carrier, dangling in entries
                        for _key, _local, value in dangling])
        removed_elements.append(
            f"{name}: {_split_qname(anchor.tag)[1]} ({', '.join(rids)})")
        # Every shape vanishing with this subtree is a timing target that
        # no longer resolves.
        for shape_id in _descendant_cnvpr_ids(anchor):
            removed_ids.add(shape_id)
        parent = parent_map.get(anchor)
        if parent is None:
            continue
        parent.remove(anchor)
        if kind == "ext" and parent.tag == _P_EXTLST:
            extlst_cascade.append(parent)

    # A ``p:extLst`` emptied by media removal is itself dropped.
    for extlst in _unique(extlst_cascade):
        if len(extlst) == 0:
            grandparent = parent_map.get(extlst)
            if grandparent is not None:
                grandparent.remove(extlst)

    # Remove offending attributes on carriers not already discarded.
    for elem, dangling, warn in attr_removals:
        if _has_ancestor_in(elem, kept_set, parent_map):
            continue
        local_tag = _split_qname(elem.tag)[1]
        for key, local, value in dangling:
            if key in elem.attrib:
                del elem.attrib[key]
            removed_elements.append(
                f"{name}: @{local} on {local_tag} ({value})")
        if warn:
            warnings.append(
                f"{name}: dangling reference on <{local_tag}> is not "
                "covered by a cleanup rule; removed the attribute only")

    # Reconcile the timing tree with the shapes just neutralised.
    _apply_timing_cleanup(name, root, stilled_ids, removed_ids,
                          removed_elements)


def _apply_timing_cleanup(name: str, root: ET.Element, stilled_ids: set[str],
                          removed_ids: set[str],
                          removed_elements: list[str]) -> None:
    """Reconcile a slide's ``p:timing`` tree with neutralised shapes.

    Runs after the reference-cleanup removals so it operates on the final
    shape set. A ``p:video``/``p:audio`` node targeting a shape that is
    now a still image (*stilled_ids*) or gone (*removed_ids*) is removed;
    any other ``spid`` carrier pointing at a removed shape loses its
    enclosing ``p:par``; ``p:bldLst`` build entries for removed shapes are
    dropped, and both an emptied ``p:bldLst`` and an emptied
    ``p:childTnLst`` go with them. Shapes that are merely stilled keep
    their animations, since the shape itself still exists.
    """
    target_ids = stilled_ids | removed_ids
    if not target_ids:
        return

    # ``p:childTnLst`` elements that lose a child, checked for emptiness
    # once every removal below has run.
    touched: list[ET.Element] = []

    # Step 1: a media node whose shape is stilled or removed is dropped;
    # the still image (when the shape survives) stays behind.
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for media in list(root.iter()):
        if media.tag == _P_VIDEO:
            kind = "video"
        elif media.tag == _P_AUDIO:
            kind = "audio"
        else:
            continue
        spid = _sptgt_spid(media)
        if spid is None or spid not in target_ids:
            continue
        parent = parent_map.get(media)
        if parent is None:
            continue
        childtnlst = _nearest_ancestor(media, _P_CHILDTNLST, parent_map)
        parent.remove(media)
        removed_elements.append(f"{name}: {kind} (spid={spid})")
        if childtnlst is not None:
            touched.append(childtnlst)

    # Step 2: any other timing carrier pointing at a *removed* shape loses
    # its nearest enclosing ``p:par`` (or itself when none encloses it).
    # ``p:bldLst`` carriers are handled separately in step 3.
    parent_map = {child: parent for parent in root.iter() for child in parent}
    anchors: list[tuple[ET.Element, str]] = []
    for elem in root.iter():
        if _nearest_ancestor(elem, _P_BLDLST, parent_map) is not None:
            continue
        spid = _spid_attr(elem)
        if spid is None or spid not in removed_ids:
            continue
        par = _nearest_ancestor(elem, _P_PAR, parent_map)
        anchors.append((par if par is not None else elem, spid))

    anchor_set = {anchor for anchor, _spid in anchors}
    seen: set[ET.Element] = set()
    for anchor, spid in anchors:
        if anchor in seen:
            continue
        seen.add(anchor)
        if _has_ancestor_in(anchor, anchor_set, parent_map):
            continue
        parent = parent_map.get(anchor)
        if parent is None:
            continue
        childtnlst = _nearest_ancestor(anchor, _P_CHILDTNLST, parent_map)
        parent.remove(anchor)
        removed_elements.append(
            f"{name}: {_split_qname(anchor.tag)[1]} (spid={spid})")
        if childtnlst is not None:
            touched.append(childtnlst)

    # Step 3: a ``p:bldLst`` build entry for a removed shape is dropped,
    # and an emptied ``p:bldLst`` goes with it.
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for bldlst in list(root.iter(_P_BLDLST)):
        for child in list(bldlst):
            spid = child.get("spid")
            if spid and spid in removed_ids:
                bldlst.remove(child)
                removed_elements.append(
                    f"{name}: {_split_qname(child.tag)[1]} (spid={spid})")
        if len(bldlst) == 0:
            parent = parent_map.get(bldlst)
            if parent is not None:
                parent.remove(bldlst)

    # Step 4: a ``p:childTnLst`` emptied by the removals above is dropped;
    # the cascade stops here, since a childless ``p:cTn``/``p:par`` is
    # legal.
    parent_map = {child: parent for parent in root.iter() for child in parent}
    for childtnlst in _unique(touched):
        if len(childtnlst) == 0:
            parent = parent_map.get(childtnlst)
            if parent is not None:
                parent.remove(childtnlst)


def _host_shape_cnvpr_id(elem: ET.Element,
                         parent_map: dict[ET.Element, ET.Element]
                         ) -> str | None:
    """Return the ``p:cNvPr@id`` of the shape hosting *elem*, or None.

    Prefers the nearest enclosing ``p:pic``; failing that, the nearest
    ancestor that has a ``p:cNvPr`` descendant. The first ``p:cNvPr`` id
    found within that host is returned.
    """
    pic = _nearest_ancestor(elem, _P_PIC, parent_map)
    if pic is not None:
        return _first_cnvpr_id(pic)
    current = parent_map.get(elem)
    while current is not None:
        cnvpr_id = _first_cnvpr_id(current)
        if cnvpr_id is not None:
            return cnvpr_id
        current = parent_map.get(current)
    return None


def _first_cnvpr_id(elem: ET.Element) -> str | None:
    """Return the id of the first ``p:cNvPr`` in *elem*'s subtree, or None."""
    for cnvpr in elem.iter(_P_CNVPR):
        cnvpr_id = cnvpr.get("id")
        if cnvpr_id:
            return cnvpr_id
    return None


def _descendant_cnvpr_ids(elem: ET.Element) -> list[str]:
    """Return the ids of every ``p:cNvPr`` in *elem*'s subtree."""
    ids: list[str] = []
    for cnvpr in elem.iter(_P_CNVPR):
        cnvpr_id = cnvpr.get("id")
        if cnvpr_id:
            ids.append(cnvpr_id)
    return ids


def _sptgt_spid(media: ET.Element) -> str | None:
    """Return the first descendant ``p:spTgt@spid`` value of *media*."""
    for sptgt in media.iter(_P_SPTGT):
        spid = sptgt.get("spid")
        if spid:
            return spid
    return None


def _spid_attr(elem: ET.Element) -> str | None:
    """Return *elem*'s ``spid`` attribute value regardless of namespace."""
    for key, value in elem.attrib.items():
        if _split_qname(key)[1] == "spid" and value:
            return value
    return None


def _resolve_rule(elem: ET.Element,
                  parent_map: dict[ET.Element, ET.Element]
                  ) -> tuple[str, ET.Element]:
    """Decide how to discard the dangling reference carried by *elem*.

    Returns ``(kind, target)`` where *kind* is one of ``"element"`` (drop
    *target* wholesale), ``"ext"`` (drop *target*, then its ``p:extLst``
    once emptied), ``"attrs"`` (drop only the offending attributes) or
    ``"attrs_warn"`` (same, but flag it as an uncovered reference). The
    design table's rules are tried in order and the first match wins.
    """
    uri, local = _split_qname(elem.tag)

    # 1. Media *links* leave the poster picture behind as a still image.
    if uri == _A_NS and local in _A_MEDIA_LINK_LOCALS:
        return "element", elem
    # 2. The paired ``p14:media`` extension is dropped with its ext host.
    if elem.tag == _P14_MEDIA:
        ext = _nearest_ancestor(elem, _P_EXT, parent_map)
        if ext is not None:
            return "ext", ext
        parent = parent_map.get(elem)
        if parent is not None:
            return "ext", parent
        return "attrs", elem
    # 3/4. A picture blip means the image is gone -> drop the picture; a
    #      fill/background blip keeps its shape and loses the attribute.
    if elem.tag == _A_BLIP:
        parent = parent_map.get(elem)
        if parent is not None and parent.tag == _P_BLIPFILL:
            pic = _nearest_ancestor(elem, _P_PIC, parent_map)
            if pic is not None:
                return "element", pic
        return "attrs", elem
    # 5. Hyperlinks vanish but their text run stays.
    if uri == _A_NS and local in _A_HLINK_LOCALS:
        return "element", elem
    # 6. Charts / diagrams / OLE objects lose their whole graphic frame.
    graphic_frame = _nearest_ancestor(elem, _P_GRAPHICFRAME, parent_map)
    if graphic_frame is not None:
        return "element", graphic_frame
    # 7. A custom-show slide reference drops the reference element.
    if elem.tag == _P_SLD \
            and _nearest_ancestor(elem, _P_CUSTSHOW, parent_map) is not None:
        return "element", elem
    # 8. Anything else: minimal intervention, flagged for the caller.
    return "attrs_warn", elem


def _nearest_ancestor(elem: ET.Element, tag: str,
                      parent_map: dict[ET.Element, ET.Element]
                      ) -> ET.Element | None:
    """Return the nearest strict ancestor of *elem* tagged *tag*, or None."""
    current = parent_map.get(elem)
    while current is not None:
        if current.tag == tag:
            return current
        current = parent_map.get(current)
    return None


def _has_ancestor_in(elem: ET.Element, anchors: set[ET.Element],
                     parent_map: dict[ET.Element, ET.Element]) -> bool:
    """Return True when any strict ancestor of *elem* is in *anchors*."""
    current = parent_map.get(elem)
    while current is not None:
        if current in anchors:
            return True
        current = parent_map.get(current)
    return False


def _unique[T](items: list[T]) -> list[T]:
    """Return *items* de-duplicated while preserving first-seen order."""
    seen: set[T] = set()
    result: list[T] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
