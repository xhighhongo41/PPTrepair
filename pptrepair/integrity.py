"""Relationship-reference integrity inspection for OOXML packages.

Cross-checks every relationship-namespace ("r:") attribute value found in
a package's XML parts against the ``Id`` attributes actually defined in
the matching ``.rels`` part, surfacing "dangling" references left behind
by corruption or by an imperfect repair (see
``開発資料/v1.1.2実装計画.md`` §4.1 for the design rationale: rebuild's
relationship pruning can strip a ``.rels`` entry while the referencing
``r:embed``/``r:link``/etc. attribute in the slide XML survives,
prompting PowerPoint's "repair this file?" prompt on open).

This module also inspects a second, unrelated source of the same
"repair this file?" prompt: a slide's ``p:timing`` tree (the animation /
play sequence) references shapes by ``spid``, the value of the shape's
``p:cNvPr@id``. When rebuild neutralizes a video shape (e.g. dropping
its ``a:videoFile``) or removes a shape outright, the timing tree can be
left pointing at a shape id that no longer carries the media it expects,
or that no longer exists at all (see :func:`inspect_timing`).

A third, again unrelated source of the same prompt is a missing
*required* relationship: some parts are required by the OOXML schema to
carry a relationship of a specific ``Type`` (e.g. every slide master
must relate to a theme), yet that requirement is invisible to
:func:`inspect_references` -- the reference lives in the ``Type``
attribute of a ``.rels`` entry, not in an ``r:``-namespace attribute of
the referencing part's own XML. A prior rebuild pruning a lost theme
part's relationship out of a slide master's ``.rels`` is exactly this
case (see ``開発資料/v1.1.2実装計画.md`` §10, addendum item C, for the
design rationale); :func:`inspect_structure` surfaces it.

A fourth, still unrelated source is an *orphan* slide or notes slide: a
``ppt/slides/slideN.xml`` or ``ppt/notesSlides/notesSlideN.xml`` part that
no relationship anywhere in the package points at. A merge or rebuild that
keeps a part the surviving -- or borrowed -- presentation structure never
references leaves it unreachable, and PowerPoint again offers to repair
the file; :func:`inspect_orphans` surfaces these. Only slides and notes
slides are inspected: layouts, masters, themes and media are legitimately
reachable from a master or by extension convention even when no explicit
relationship names them, so flagging an unreferenced one would be a false
positive.

The inspection is read-only and uses only the standard library. Callers
are responsible for handling :class:`zipfile.BadZipFile`; this module
never catches it, so a malformed archive simply propagates the exception
(whose handling policy -- e.g. only inspecting archives already
classified as structurally normal -- belongs to the caller).
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

#: Namespace URI of relationship-reference attributes (``r:embed`` etc.).
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

#: Clark-notation prefix identifying an :data:`R_NS` attribute.
_R_NS_PREFIX = f"{{{R_NS}}}"

#: Namespace URI of presentationml elements (``p:sld``, ``p:cNvPr``, ...).
P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"

#: Namespace URI of drawingml elements (``a:videoFile``, ...).
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

_CNVPR_TAG = f"{{{P_NS}}}cNvPr"
_SPTGT_TAG = f"{{{P_NS}}}spTgt"
_VIDEO_TAG = f"{{{P_NS}}}video"
_AUDIO_TAG = f"{{{P_NS}}}audio"
_VIDEO_FILE_TAGS = (f"{{{A_NS}}}videoFile", f"{{{A_NS}}}quickTimeFile")
_AUDIO_FILE_TAGS = (f"{{{A_NS}}}audioFile", f"{{{A_NS}}}wavAudioFile")


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


@dataclass
class TimingRef:
    """One ``spid``-bearing attribute in a ``p:timing`` tree with no
    matching shape."""

    part: str
    """Package-relative name of the part carrying the reference, e.g.
    ``"ppt/slides/slide21.xml"``."""
    element: str
    """Local name of the element carrying the ``spid`` attribute, e.g.
    ``"spTgt"`` or ``"bldP"``."""
    spid: str
    """The unresolved shape id value the element references."""


@dataclass
class MediaMismatch:
    """A ``p:video``/``p:audio`` timing node targeting a shape that lacks
    the media file its kind requires."""

    part: str
    """Package-relative name of the part carrying the mismatch."""
    kind: str
    """Either ``"video"`` or ``"audio"``."""
    spid: str
    """Shape id targeted by the media node's ``p:spTgt``."""


@dataclass
class TimingIntegrityResult:
    """Outcome of one :func:`inspect_timing` run."""

    parts_scanned: int
    """Number of ``.xml`` (non-``.rels``) parts examined."""
    dangling: list[TimingRef]
    """Every ``spid`` reference with no matching shape id, ordered by
    part name and then by document order within the part."""
    media_mismatch: list[MediaMismatch]
    """Every ``p:video``/``p:audio`` node whose targeted shape exists but
    lacks the media file its kind requires, ordered the same way as
    :attr:`dangling`."""
    parse_errors: list[str]
    """Parts that could not be parsed as XML."""


@dataclass
class MissingStructure:
    """One required relationship ``Type`` absent from a part's ``.rels``."""

    part: str
    """Package-relative name of the part missing the relationship, e.g.
    ``"ppt/slideMasters/slideMaster2.xml"``."""
    required_type: str
    """Tail (last ``/``-separated segment) of the relationship ``Type``
    the part requires but does not have, e.g. ``"theme"``."""


@dataclass
class StructureIntegrityResult:
    """Outcome of one :func:`inspect_structure` run."""

    parts_checked: int
    """Number of parts matching one of the rules in
    :data:`_STRUCTURE_RULES`, regardless of outcome."""
    missing: list[MissingStructure]
    """Every required relationship found absent, ordered by part name
    and then in the rule's declared order for that part."""
    parse_errors: list[str]
    """``.rels`` parts that could not be parsed as XML (their part's
    required relationships are all counted as missing regardless)."""


@dataclass
class OrphanPart:
    """One slide or notes slide no relationship in the package references."""

    name: str
    """Package-relative name of the orphaned part, e.g.
    ``"ppt/slides/slide58.xml"``."""
    kind: str
    """Either ``"slide"`` (a ``ppt/slides/slideN.xml`` part) or
    ``"notes_slide"`` (a ``ppt/notesSlides/notesSlideN.xml`` part)."""


@dataclass
class OrphanIntegrityResult:
    """Outcome of one :func:`inspect_orphans` run."""

    orphans: list[OrphanPart]
    """Every unreferenced slide/notes-slide part, ordered by part name."""


#: Required-relationship rules: a part name pattern paired with the
#: relationship ``Type`` tail(s) its ``.rels`` must define at least one
#: of each, per ``開発資料/v1.1.2実装計画.md`` §10 (addendum item C).
#: Checked independently per required type, so a part naming two of
#: them (notes slides) can be reported missing only one.
_STRUCTURE_RULES: list[tuple[re.Pattern[str], tuple[str, ...]]] = [
    (re.compile(r"^ppt/slides/slide\d+\.xml$"), ("slideLayout",)),
    (re.compile(r"^ppt/slideLayouts/slideLayout\d+\.xml$"), ("slideMaster",)),
    (re.compile(r"^ppt/slideMasters/slideMaster\d+\.xml$"), ("theme",)),
    (re.compile(r"^ppt/notesMasters/notesMaster\d+\.xml$"), ("theme",)),
    (re.compile(r"^ppt/notesSlides/notesSlide\d+\.xml$"),
     ("slide", "notesMaster")),
    (re.compile(r"^ppt/presentation\.xml$"), ("slideMaster",)),
]

#: Part-name patterns paired with the :class:`OrphanPart` ``kind`` reported
#: for them; only these two part families are checked for orphaning, since
#: any other unreferenced part kind would be a false positive (see the
#: module docstring).
_ORPHAN_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^ppt/slides/slide\d+\.xml$"), "slide"),
    (re.compile(r"^ppt/notesSlides/notesSlide\d+\.xml$"), "notes_slide"),
]


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


def inspect_timing(path: Path) -> TimingIntegrityResult:
    """Cross-check ``p:timing`` shape-id references against actual shapes.

    Every entry whose name ends with ``.xml`` (excluding ``.rels`` parts
    themselves) is parsed, exactly as in :func:`inspect_references`. For
    each part, the set of live shape ids is built from every
    ``p:cNvPr@id`` found anywhere in the part, then two checks run:

    * **Dangling shape-id references.** Every attribute whose *local*
      name is ``spid`` -- regardless of namespace or of the local name
      of the element carrying it, so ``p:spTgt@spid`` and
      ``p:bldP@spid`` are both covered -- is looked up in that set. A
      value with no matching shape id is reported in
      :attr:`TimingIntegrityResult.dangling`. An attribute value equal
      to the empty string is skipped, mirroring the empty-``r:id``
      convention of :func:`inspect_references`.

    * **Media/shape mismatches.** Every ``p:video`` and ``p:audio``
      timing node that carries a descendant ``p:spTgt@spid`` resolving
      to an *existing* shape is checked for the media element its kind
      requires (``a:videoFile``/``a:quickTimeFile`` for video,
      ``a:audioFile``/``a:wavAudioFile`` for audio) anywhere within that
      shape's subtree (the ``p:cNvPr``'s grandparent, e.g. ``p:pic``). A
      node whose shape lacks the required element is reported in
      :attr:`TimingIntegrityResult.media_mismatch`. A node whose ``spid``
      does not resolve to any shape is left out of this check entirely,
      since it is already accounted for by the dangling check above --
      it would otherwise be double-counted. A ``p:video``/``p:audio``
      node without any ``p:spTgt`` descendant is skipped as well, having
      no shape to validate against.

    A part that fails to parse as XML is recorded in
    :attr:`TimingIntegrityResult.parse_errors`; inspection of the
    remaining parts continues regardless.

    This function opens *path* read-only and never modifies it.
    :class:`zipfile.BadZipFile` is not caught here and propagates to the
    caller.
    """
    dangling: list[TimingRef] = []
    media_mismatch: list[MediaMismatch] = []
    parse_errors: list[str] = []

    with zipfile.ZipFile(path) as archive:
        name_set = set(archive.namelist())
        # Same sort as inspect_references: keeps every output list
        # ordered deterministically by part name, then by document order.
        parts = sorted(
            name for name in name_set
            if name.endswith(".xml") and not name.endswith(".rels"))

        for part in parts:
            try:
                root = ET.fromstring(archive.read(part))
            except ET.ParseError:
                parse_errors.append(part)
                continue

            cnvpr_by_id: dict[str, ET.Element] = {}
            for cnvpr in root.iter(_CNVPR_TAG):
                shape_id = cnvpr.get("id")
                if shape_id:
                    cnvpr_by_id.setdefault(shape_id, cnvpr)
            shape_ids = set(cnvpr_by_id)

            # Maps every element to its parent so a shape's ancestor
            # chain can be walked from its p:cNvPr up to e.g. p:pic.
            parent_map = {
                child: parent for parent in root.iter() for child in parent
            }

            dangling.extend(_collect_dangling_spids(root, part, shape_ids))
            media_mismatch.extend(_collect_media_mismatches(
                root, part, cnvpr_by_id, parent_map))

    return TimingIntegrityResult(
        parts_scanned=len(parts),
        dangling=dangling,
        media_mismatch=media_mismatch,
        parse_errors=parse_errors,
    )


def inspect_structure(path: Path) -> StructureIntegrityResult:
    """Check every part in *path* against :data:`_STRUCTURE_RULES`.

    Each rule pairs a part-name pattern with the relationship ``Type``
    tail(s) that part's matching ``.rels`` (``X/_rels/Y.rels`` for a
    part ``X/Y``, as in :func:`inspect_references`) must define at
    least one relationship of. Every archive entry matching one of the
    patterns is a "target part" and counts towards
    :attr:`StructureIntegrityResult.parts_checked`; a part matching no
    rule (most parts) is ignored entirely.

    Required types are checked independently: a target part naming two
    of them (notes slides require both ``slide`` and ``notesMaster``)
    can be missing only one and still be reported for that one alone,
    rather than only when every required type is absent.

    When a target part's matching ``.rels`` is absent from the archive,
    every one of its required types is reported missing (there being no
    relationships to find any of them in). When the ``.rels`` exists but
    fails to parse as XML, its name is recorded in
    :attr:`StructureIntegrityResult.parse_errors` and every required
    type is likewise reported missing.

    Results are ordered deterministically: target parts in name order,
    then required types in the rule's declared order for that part.

    This function opens *path* read-only and never modifies it.
    :class:`zipfile.BadZipFile` is not caught here and propagates to the
    caller.
    """
    missing: list[MissingStructure] = []
    parse_errors: list[str] = []

    with zipfile.ZipFile(path) as archive:
        name_set = set(archive.namelist())
        # Sorting by part name up front keeps the missing-relationship
        # list ordered deterministically, matching inspect_references.
        targets = sorted(
            (name, required_types)
            for name in name_set
            for pattern, required_types in _STRUCTURE_RULES
            if pattern.match(name)
        )

        for part, required_types in targets:
            defined_types = _defined_types(
                archive, name_set, part, parse_errors)
            missing.extend(
                MissingStructure(part=part, required_type=required)
                for required in required_types
                if required not in defined_types
            )

    return StructureIntegrityResult(
        parts_checked=len(targets),
        missing=missing,
        parse_errors=parse_errors,
    )


def inspect_orphans(path: Path) -> OrphanIntegrityResult:
    """Report slides / notes slides no relationship in the package targets.

    The set of *referenced* parts is built by walking every ``.rels`` entry
    in the archive and resolving each ``Relationship``'s ``Target`` against
    that ``.rels`` part's own base directory (``X/`` for a
    ``X/_rels/Y.rels`` part, the package root for ``_rels/.rels``), exactly
    as OPC does. ``TargetMode="External"`` relationships are skipped, since
    they name resources outside the package rather than an internal part.
    A ``.rels`` that cannot be parsed as XML contributes no references but
    does not abort the scan.

    Every archive entry matching one of :data:`_ORPHAN_RULES`
    (``ppt/slides/slideN.xml`` or ``ppt/notesSlides/notesSlideN.xml``) that
    is absent from that referenced set is reported as an
    :class:`OrphanPart`, ordered by part name. No other part kind is
    inspected: an unreferenced layout, master, theme or media part is
    reachable by convention (or from a master) and would be a false
    positive.

    This function opens *path* read-only and never modifies it.
    :class:`zipfile.BadZipFile` is not caught here and propagates to the
    caller.
    """
    with zipfile.ZipFile(path) as archive:
        name_set = set(archive.namelist())
        referenced = _collect_referenced_parts(archive, name_set)

    orphans: list[OrphanPart] = []
    for name in sorted(name_set):
        kind = _orphan_kind(name)
        if kind is None or name in referenced:
            continue
        orphans.append(OrphanPart(name=name, kind=kind))

    return OrphanIntegrityResult(orphans=orphans)


def _orphan_kind(name: str) -> str | None:
    """Return *name*'s orphan ``kind``, or None when it is not checked."""
    for pattern, kind in _ORPHAN_RULES:
        if pattern.match(name):
            return kind
    return None


def _collect_referenced_parts(archive: zipfile.ZipFile,
                              name_set: set[str]) -> set[str]:
    """Return every internal part targeted by a relationship in the archive.

    Walks all ``.rels`` entries, resolving each internal (non-External)
    ``Relationship``'s ``Target`` against the ``.rels`` part's base
    directory. Unparsable ``.rels`` parts are skipped silently, since the
    orphan check only needs the references it can positively read.
    """
    referenced: set[str] = set()
    for rels_name in name_set:
        if not rels_name.endswith(".rels"):
            continue
        try:
            root = ET.fromstring(archive.read(rels_name))
        except ET.ParseError:
            continue
        base = _rels_base_dir(rels_name)
        for child in root:
            if not isinstance(child.tag, str):
                continue
            if _local_name(child.tag) != "Relationship":
                continue
            if child.get("TargetMode") == "External":
                continue
            target = child.get("Target")
            if not target:
                continue
            referenced.add(_resolve_rels_target(base, target))
    return referenced


def _rels_base_dir(rels_name: str) -> str:
    """Return the base directory a ``.rels`` part resolves its targets against.

    A relationships part ``X/_rels/Y.rels`` resolves relative targets
    against ``X/`` (kept with its trailing slash); the package-root
    ``_rels/.rels`` resolves against the empty string.
    """
    index = rels_name.rfind("_rels/")
    if index == -1:
        return ""
    return rels_name[:index]


def _resolve_rels_target(base: str, target: str) -> str:
    """Resolve a relationship *target* against its *base* directory.

    A leading slash makes the target package-root relative; otherwise it is
    joined onto *base*. The result is POSIX-normalised so ``../`` and
    ``./`` segments collapse to a canonical package-relative part name.
    """
    if target.startswith("/"):
        cleaned = target[1:]
    else:
        cleaned = posixpath.join(base, target)
    return posixpath.normpath(cleaned)


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


def _defined_types(archive: zipfile.ZipFile, name_set: set[str], part: str,
                   parse_errors: list[str]) -> set[str]:
    """Return the relationship ``Type`` tails defined for *part*.

    Unlike :func:`_defined_ids`, an absent or unparsable ``.rels`` both
    collapse to an empty set here: :func:`inspect_structure` treats
    "no matching relationship" the same way regardless of which of the
    two caused it, only distinguishing them for
    :attr:`StructureIntegrityResult.parse_errors`, which this function
    still appends to on an XML parse failure.
    """
    rels_name = _rels_name_for(part)
    if rels_name not in name_set:
        return set()
    try:
        rels_root = ET.fromstring(archive.read(rels_name))
    except ET.ParseError:
        parse_errors.append(rels_name)
        return set()

    types: set[str] = set()
    for child in rels_root:
        if not isinstance(child.tag, str):
            continue
        if _local_name(child.tag) != "Relationship":
            continue
        rtype = child.get("Type")
        if rtype:
            types.add(rtype.rsplit("/", 1)[-1])
    return types


def _collect_dangling_spids(
        root: ET.Element, part: str, shape_ids: set[str]) -> list[TimingRef]:
    """Return every non-empty ``spid`` attribute in *root* with no shape.

    Every attribute is considered regardless of its owning element's name
    or the attribute's own namespace, as long as its *local* name is
    ``spid``; only its value needs to resolve against *shape_ids*.
    Results are in document order, matching :func:`_collect_refs`.
    """
    refs: list[TimingRef] = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue  # Skip comments / processing instructions.
        element_name = _local_name(element.tag)
        for attr_name, value in element.attrib.items():
            if _local_name(attr_name) != "spid" or value == "":
                continue
            if value not in shape_ids:
                refs.append(TimingRef(
                    part=part, element=element_name, spid=value))
    return refs


def _collect_media_mismatches(
        root: ET.Element, part: str, cnvpr_by_id: dict[str, ET.Element],
        parent_map: dict[ET.Element, ET.Element]) -> list[MediaMismatch]:
    """Return every ``p:video``/``p:audio`` node missing its media file.

    Only nodes whose ``p:spTgt@spid`` resolves to a shape that actually
    exists in *cnvpr_by_id* are considered; a ``spid`` with no matching
    shape is already reported by :func:`_collect_dangling_spids` and
    must not be double-counted here.
    """
    mismatches: list[MediaMismatch] = []
    for element in root.iter():
        if element.tag == _VIDEO_TAG:
            kind, required_tags = "video", _VIDEO_FILE_TAGS
        elif element.tag == _AUDIO_TAG:
            kind, required_tags = "audio", _AUDIO_FILE_TAGS
        else:
            continue

        spid = _find_sptgt_spid(element)
        if not spid:
            continue
        cnvpr = cnvpr_by_id.get(spid)
        if cnvpr is None:
            continue  # Already reported as a dangling spid reference.

        shape_root = _shape_root(cnvpr, parent_map)
        if shape_root is None:
            continue  # Unexpected shape structure; nothing to check.
        has_required_media = any(
            shape_root.find(f".//{tag}") is not None
            for tag in required_tags)
        if not has_required_media:
            mismatches.append(MediaMismatch(part=part, kind=kind, spid=spid))
    return mismatches


def _find_sptgt_spid(media_element: ET.Element) -> str | None:
    """Return the first descendant ``p:spTgt@spid`` value, or None."""
    for sptgt in media_element.iter(_SPTGT_TAG):
        spid = sptgt.get("spid")
        if spid:
            return spid
    return None


def _shape_root(
        cnvpr: ET.Element,
        parent_map: dict[ET.Element, ET.Element]) -> ET.Element | None:
    """Return the shape's root element (e.g. ``p:pic``) for *cnvpr*.

    A shape's ``p:cNvPr`` sits inside a non-visual properties group
    (``p:nvPicPr``, ``p:nvSpPr``, ...), which is itself a direct child of
    the shape's root element. Either ancestor lookup returning nothing
    (e.g. *cnvpr* sitting outside a normal shape tree) yields None.
    """
    non_visual_group = parent_map.get(cnvpr)
    if non_visual_group is None:
        return None
    return parent_map.get(non_visual_group)


def _local_name(tag: str) -> str:
    """Return the local (non-namespaced) part of a Clark-notation *tag*."""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag
