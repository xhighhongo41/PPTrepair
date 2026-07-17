"""Relationship-reference integrity inspection for OOXML packages.

Cross-checks every relationship-namespace ("r:") attribute value found in
a package's XML parts against the ``Id`` attributes actually defined in
the matching ``.rels`` part, surfacing "dangling" references left behind
by corruption or by an imperfect repair (see
``開発資料/v1.1.2実装計画.md`` §4.1 for the design rationale: rebuild's
relationship pruning can strip a ``.rels`` entry while the referencing
``r:embed``/``r:link``/etc. attribute in the slide XML survives,
prompting PowerPoint's "repair this file?" prompt on open).

The inspection is read-only and uses only the standard library. Callers
are responsible for handling :class:`zipfile.BadZipFile`; this module
never catches it, so a malformed archive simply propagates the exception
(whose handling policy -- e.g. only inspecting archives already
classified as structurally normal -- belongs to the caller).
"""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

#: Namespace URI of relationship-reference attributes (``r:embed`` etc.).
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: Clark-notation prefix identifying an :data:`R_NS` attribute.
_R_NS_PREFIX = f"{{{R_NS}}}"


@dataclass
class DanglingRef:
    """One relationship-namespace attribute value with no matching Id."""

    part: str
    """Package-relative name of the part carrying the reference, e.g.
    ``"ppt/slides/slide2.xml"``."""
    attribute: str
    """Local name of the referencing attribute, e.g. ``"embed"``."""
    rid: str
    """The unresolved relationship id, e.g. ``"rId3"``."""
    element: str
    """Local name of the element carrying the attribute, e.g. ``"blip"``."""


@dataclass
class RefIntegrityResult:
    """Outcome of one :func:`inspect_references` run."""

    parts_scanned: int
    """Number of ``.xml`` (non-``.rels``) parts examined."""
    dangling: list[DanglingRef]
    """Every reference whose id has no matching relationship, ordered by
    part name and then by document order within the part."""
    missing_rels_parts: list[str]
    """Parts that carry relationship-namespace attributes but whose
    matching ``.rels`` part does not exist in the archive."""
    parse_errors: list[str]
    """Parts that could not be parsed as XML (including ``.rels`` parts
    whose parse failure forced every reference of their source part to be
    treated as dangling)."""


def inspect_references(path: Path) -> RefIntegrityResult:
    """Cross-check r:-namespace references against relationship ids.

    Every entry whose name ends with ``.xml`` (excluding ``.rels`` parts
    themselves) is parsed and scanned for attributes in the :data:`R_NS`
    namespace, regardless of the attribute's local name (``embed``,
    ``link``, ``id``, ``pict``, ... are all covered). Each such
    attribute's value is looked up in the ``Id`` set of the part's
    matching relationships part (``X/_rels/Y.rels`` for a part ``X/Y``);
    values with no match are reported as dangling. An attribute value
    equal to the empty string is skipped entirely, since an empty
    ``r:id`` is a valid marker for an internal anchor rather than an
    unresolved external reference. ``TargetMode="External"`` relationship
    ids count as defined, since only their target resolution differs
    from internal ones, not whether the id itself exists.

    When a part carries at least one r:-namespace attribute but its
    matching relationships part is absent from the archive, the part is
    recorded in :attr:`RefIntegrityResult.missing_rels_parts` and *every*
    one of its references is reported as dangling, there being no
    relationship set to validate against. A part with no r:-namespace
    attributes at all is left out of both lists even when its matching
    ``.rels`` part is missing, since it has nothing to validate either.

    A part that fails to parse as XML is recorded in
    :attr:`RefIntegrityResult.parse_errors`; inspection of the remaining
    parts continues regardless.

    This function opens *path* read-only and never modifies it.
    :class:`zipfile.BadZipFile` is not caught here and propagates to the
    caller.
    """
    dangling: list[DanglingRef] = []
    missing_rels_parts: list[str] = []
    parse_errors: list[str] = []

    with zipfile.ZipFile(path) as archive:
        name_set = set(archive.namelist())
        # Sorting by part name up front keeps every output list ordered
        # deterministically by part name (ties within a part are already
        # in document order, since ElementTree iterates that way).
        parts = sorted(
            name for name in name_set
            if name.endswith(".xml") and not name.endswith(".rels"))

        for part in parts:
            try:
                root = ET.fromstring(archive.read(part))
            except ET.ParseError:
                parse_errors.append(part)
                continue

            refs = _collect_refs(root, part)
            if not refs:
                continue

            defined_ids = _defined_ids(archive, name_set, part, parse_errors)
            if defined_ids is None:
                missing_rels_parts.append(part)
                dangling.extend(refs)
                continue

            dangling.extend(ref for ref in refs if ref.rid not in defined_ids)

    return RefIntegrityResult(
        parts_scanned=len(parts),
        dangling=dangling,
        missing_rels_parts=missing_rels_parts,
        parse_errors=parse_errors,
    )


def _collect_refs(root: ET.Element, part: str) -> list[DanglingRef]:
    """Return every non-empty :data:`R_NS` attribute reference in *root*.

    References are returned in document order, which is what keeps
    :attr:`RefIntegrityResult.dangling` deterministic once parts are
    themselves processed in name order.
    """
    refs: list[DanglingRef] = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue  # Skip comments / processing instructions.
        element_name = _local_name(element.tag)
        for attr_name, value in element.attrib.items():
            if not attr_name.startswith(_R_NS_PREFIX) or value == "":
                continue
            refs.append(DanglingRef(
                part=part, attribute=_local_name(attr_name),
                rid=value, element=element_name))
    return refs


def _rels_name_for(part: str) -> str:
    """Return the ``.rels`` part name matching *part*.

    A part ``X/Y`` is described by ``X/_rels/Y.rels``; a package-root
    part (no directory component) is described by ``_rels/Y.rels``.
    """
    directory = posixpath.dirname(part)
    basename = posixpath.basename(part)
    rels_dir = f"{directory}/_rels" if directory else "_rels"
    return f"{rels_dir}/{basename}.rels"


def _defined_ids(archive: zipfile.ZipFile, name_set: set[str], part: str,
                 parse_errors: list[str]) -> set[str] | None:
    """Return the relationship ids defined for *part*, or None if absent.

    Returns None when the matching ``.rels`` part does not exist in the
    archive at all (the "missing rels part" case). When the ``.rels``
    part exists but fails to parse as XML, its name is appended to
    *parse_errors* and an empty set is returned instead: no id can be
    vouched for, but the part is not conflated with the "no such .rels
    part" case, since one genuinely exists.
    """
    rels_name = _rels_name_for(part)
    if rels_name not in name_set:
        return None
    try:
        rels_root = ET.fromstring(archive.read(rels_name))
    except ET.ParseError:
        parse_errors.append(rels_name)
        return set()

    ids: set[str] = set()
    for child in rels_root:
        if not isinstance(child.tag, str):
            continue
        if _local_name(child.tag) != "Relationship":
            continue
        rid = child.get("Id")
        if rid:
            ids.add(rid)
    return ids


def _local_name(tag: str) -> str:
    """Return the local (non-namespaced) part of a Clark-notation *tag*."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag
