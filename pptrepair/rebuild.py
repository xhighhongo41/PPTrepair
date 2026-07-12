"""Package reconstruction (rebuild mode).

Rewrites salvaged entries into a fresh, consistent .pptx:

1. every salvaged entry is streamed into a new ZIP archive;
2. missing-but-required package plumbing is synthesised
   (``[Content_Types].xml`` regenerated from the actual entry set,
   ``_rels/.rels`` from a static template);
3. references are reconciled so the surviving package is
   self-consistent: relationship entries whose targets are gone are
   pruned (external targets are kept), and ``<p:sldIdLst>`` /
   ``<p:sldMasterIdLst>`` etc. lose the ids whose parts vanished.

Slide XML *content* is never touched in v1.0: parts that survived are
written byte-identical, and only the package-plumbing XML listed above
is re-serialised (namespace prefixes preserved via
``ET.register_namespace``).
"""

from __future__ import annotations

import io
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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

    # 1. Prune every surviving relationship part, remembering which
    #    relationship ids the presentation part may still reference.
    surviving_presentation_rids: set[str] | None = None
    for entry in salvaged:
        if not entry.name.endswith(".rels"):
            continue
        data = _read_entry(reader, entry)
        new_bytes, surviving = _prune_rels(
            entry.name, data, final_names, result.pruned_relationships)
        plumbing[entry.name] = new_bytes
        plumbing_date_time[entry.name] = reader.datetime_of(entry)
        if entry.name == _PRESENTATION_RELS_NAME:
            surviving_presentation_rids = surviving

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
            part = child.get("PartName", "")
            if part.startswith("/"):
                part = part[1:]
            if part not in final_names:
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

    return _reserialize(data, edit)


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
