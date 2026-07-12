"""Recovery-folder generation (extract mode).

For files whose slide bodies are unrecoverable (typically the
``head_zero_fill`` / ``head_foreign_data`` verdicts), this module turns
whatever survived into a folder of directly usable files:

* ``images/`` and ``media/`` — surviving pictures / audio / video,
  written as ordinary files under their original names;
* ``texts/`` — best-effort text recovery: the slide-title list from
  ``docProps/app.xml``, document metadata from ``docProps/core.xml``,
  and body text of any surviving slide / notes XML;
* ``charts/`` — surviving chart parts, raw XML plus a CSV of the
  cached data points;
* ``parts/`` — every salvaged entry as raw data, preserving the
  package-relative path (the authoritative copy; text extraction is
  derived, best-effort output);
* ``REPORT.txt`` — written by the caller (see :mod:`pptrepair.repair`).

All writes stream chunk-wise; entry names are sanitised so a hostile
archive cannot escape the output directory (zip-slip).
"""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from pptrepair.salvage import SalvagedEntry, SalvageReader

#: File extensions routed to ``images/`` (lower-case, no dot).
IMAGE_EXTENSIONS = frozenset(
    ["png", "jpg", "jpeg", "gif", "bmp", "tif", "tiff", "emf", "wmf",
     "svg", "ico"])

# --- OOXML namespace URIs used while parsing recovered XML parts. --------
_NS_EXT_PROPS = (
    "http://schemas.openxmlformats.org/officeDocument/2006"
    "/extended-properties")
_NS_VT = (
    "http://schemas.openxmlformats.org/officeDocument/2006"
    "/docPropsVTypes")
_NS_CP = (
    "http://schemas.openxmlformats.org/package/2006"
    "/metadata/core-properties")
_NS_DC = "http://purl.org/dc/elements/1.1/"
_NS_DCTERMS = "http://purl.org/dc/terms/"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_NS_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"

#: HeadingPairs labels (English and Japanese) that mark the slide-title
#: section of ``docProps/app.xml``'s ``TitlesOfParts`` vector.
_SLIDE_TITLE_HEADINGS = frozenset(["Slide Titles", "スライド タイトル"])

#: Entry name patterns routed to derived ``texts/`` output.
_SLIDE_XML_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_NOTES_XML_RE = re.compile(r"^ppt/notesSlides/notesSlide(\d+)\.xml$")
_CHART_XML_RE = re.compile(r"^ppt/charts/chart[^/]*\.xml$")
_APP_XML_NAME = "docProps/app.xml"
_CORE_XML_NAME = "docProps/core.xml"

#: Fields extracted from ``docProps/core.xml``, as (label, namespace, tag).
_CORE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("Title", _NS_DC, "title"),
    ("Creator", _NS_DC, "creator"),
    ("Last Modified By", _NS_CP, "lastModifiedBy"),
    ("Created", _NS_DCTERMS, "created"),
    ("Modified", _NS_DCTERMS, "modified"),
)


@dataclass
class ExtractResult:
    """Outcome of one recovery-folder generation."""

    output_dir: Path
    written_files: list[str] = field(default_factory=list)
    """Files written under ``output_dir`` (relative POSIX paths)."""
    extracted_texts: list[str] = field(default_factory=list)
    """Derived text files successfully produced under ``texts/``."""
    skipped: list[str] = field(default_factory=list)
    """Entry names skipped for safety (path traversal etc.)."""
    warnings: list[str] = field(default_factory=list)


def extract_salvage(reader: SalvageReader,
                    salvaged: list[SalvagedEntry],
                    output_dir: Path,
                    tr: Callable[[str], str]) -> ExtractResult:
    """Write the recovery folder for *salvaged* under *output_dir*.

    Routing rules (every entry additionally lands raw in ``parts/``
    unless routed to ``images/``/``media/``/``charts/``, which already
    hold the raw payload):

    * ``ppt/media/*`` with an image extension -> ``images/<basename>``;
      other media -> ``media/<basename>``;
    * ``ppt/charts/chart*.xml`` -> ``charts/<basename>`` plus a
      best-effort ``charts/<stem>_data.csv`` from the ``c:numCache`` /
      ``c:strCache`` values;
    * ``docProps/app.xml`` -> ``texts/slide_titles.txt`` (titles taken
      from ``TitlesOfParts``, sliced via ``HeadingPairs`` when a known
      slide-title heading is present, otherwise dumped in full with a
      caveat line);
    * ``docProps/core.xml`` -> ``texts/document_info.txt``;
    * surviving ``ppt/slides/slideN.xml`` / ``ppt/notesSlides/*`` ->
      ``texts/slideN.txt`` / ``texts/notesSlideN.txt`` built from the
      ``<a:t>`` runs;
    * everything else -> ``parts/<original path>``.

    Text extraction is best-effort: any XML parse failure downgrades to
    a warning and the raw copy in ``parts/`` remains the source of
    truth. Headers inside the generated text files go through *tr*.

    Entry names are sanitised before use: absolute paths, drive
    letters, and ``..`` components are rejected into ``skipped``.

    The caller owns ``REPORT.txt``; this function must not write it.
    """
    result = ExtractResult(output_dir=output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    image_names: set[str] = set()
    media_names: set[str] = set()
    chart_names: set[str] = set()

    for entry in salvaged:
        safe_path = _sanitize_name(entry.name)
        if safe_path is None:
            result.skipped.append(entry.name)
            continue

        if entry.name.startswith("ppt/media/"):
            _route_media(reader, entry, output_dir, image_names,
                        media_names, result)
            continue

        if _CHART_XML_RE.match(entry.name):
            _route_chart(reader, entry, output_dir, chart_names, tr, result)
            continue

        # Everything else lands raw under parts/, preserving the
        # original package-relative path.
        dest = output_dir / "parts" / Path(safe_path.as_posix())
        rel_written = f"parts/{safe_path.as_posix()}"
        text_kind = _text_kind(entry.name)
        if text_kind is None:
            _stream_to_file(reader, entry, dest)
            result.written_files.append(rel_written)
            continue

        # Text-derivable parts are small enough to hold fully in memory;
        # doing so lets the raw copy and the derived text share one read.
        data = _read_all(reader, entry)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        result.written_files.append(rel_written)
        _derive_text(text_kind, entry.name, data, output_dir, tr, result)

    return result


def _sanitize_name(name: str) -> PurePosixPath | None:
    """Validate *name* as a safe, package-relative POSIX path.

    Rejects empty names, absolute paths (POSIX-style or carrying a
    Windows drive letter), and any ``..`` path component, so a hostile
    archive cannot write outside the recovery folder (zip-slip).
    """
    if not name:
        return None
    normalized = name.replace("\\", "/")
    if re.match(r"^[A-Za-z]:", normalized):
        return None  # Windows drive letter, e.g. "C:/win.txt".
    path = PurePosixPath(normalized)
    if path.is_absolute():
        return None
    if not path.parts or any(part == ".." for part in path.parts):
        return None
    return path


def _text_kind(name: str) -> str | None:
    """Return the derived-text category for *name*, or None."""
    if name == _APP_XML_NAME:
        return "app"
    if name == _CORE_XML_NAME:
        return "core"
    if _SLIDE_XML_RE.match(name):
        return "slide"
    if _NOTES_XML_RE.match(name):
        return "notes"
    return None


def _route_media(reader: SalvageReader, entry: SalvagedEntry,
                 output_dir: Path, image_names: set[str],
                 media_names: set[str], result: ExtractResult) -> None:
    """Stream a ``ppt/media/*`` entry to ``images/`` or ``media/``."""
    basename = PurePosixPath(entry.name).name
    ext = Path(basename).suffix.lstrip(".").lower()
    if ext in IMAGE_EXTENSIONS:
        subdir, used = "images", image_names
    else:
        subdir, used = "media", media_names
    dest_name = _dedupe_basename(used, basename)
    dest = output_dir / subdir / dest_name
    _stream_to_file(reader, entry, dest)
    result.written_files.append(f"{subdir}/{dest_name}")


def _route_chart(reader: SalvageReader, entry: SalvagedEntry,
                 output_dir: Path, chart_names: set[str],
                 tr: Callable[[str], str], result: ExtractResult) -> None:
    """Write a chart part raw plus a best-effort cached-data CSV."""
    data = _read_all(reader, entry)
    basename = PurePosixPath(entry.name).name
    dest_name = _dedupe_basename(chart_names, basename)
    dest = output_dir / "charts" / dest_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    result.written_files.append(f"charts/{dest_name}")

    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        result.warnings.append(
            f"Failed to parse chart XML {entry.name}: {exc}")
        return
    rows = _parse_chart_rows(root, tr)
    if rows is None:
        return  # No cached data present; the raw XML is all we have.
    csv_name = f"{Path(dest_name).stem}_data.csv"
    csv_path = output_dir / "charts" / csv_name
    _write_chart_csv(csv_path, rows)
    result.written_files.append(f"charts/{csv_name}")


def _derive_text(kind: str, name: str, data: bytes, output_dir: Path,
                 tr: Callable[[str], str], result: ExtractResult) -> None:
    """Parse *data* as XML and write the derived text file for *kind*.

    A parse failure is recorded as a warning; the raw copy already
    written under ``parts/`` remains the authoritative source.
    """
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        result.warnings.append(f"Failed to parse {name}: {exc}")
        return
    if kind == "app":
        _write_slide_titles(root, output_dir, tr, result)
    elif kind == "core":
        _write_document_info(root, output_dir, tr, result)
    elif kind == "slide":
        n = _SLIDE_XML_RE.match(name).group(1)  # type: ignore[union-attr]
        _write_body_text(root, output_dir, f"slide{n}", result)
    elif kind == "notes":
        n = _NOTES_XML_RE.match(name).group(1)  # type: ignore[union-attr]
        _write_body_text(root, output_dir, f"notesSlide{n}", result)


# --- streaming / IO helpers -------------------------------------------------


def _read_all(reader: SalvageReader, entry: SalvagedEntry) -> bytes:
    """Read the full payload of *entry* into memory."""
    return b"".join(reader.open(entry))


def _stream_to_file(reader: SalvageReader, entry: SalvagedEntry,
                    dest: Path) -> None:
    """Stream *entry*'s payload straight to *dest* without buffering it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as f:
        for chunk in reader.open(entry):
            f.write(chunk)


def _dedupe_basename(used: set[str], basename: str) -> str:
    """Return a collision-free name for *basename*, recording it in *used*.

    Collisions are resolved as ``"name (2).ext"``, ``"name (3).ext"``,
    and so on.
    """
    if basename not in used:
        used.add(basename)
        return basename
    stem = Path(basename).stem
    suffix = Path(basename).suffix
    n = 2
    while True:
        candidate = f"{stem} ({n}){suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def _write_text_file(path: Path, lines: Iterable[str], output_dir: Path,
                     result: ExtractResult) -> None:
    """Write *lines* to *path* and record it in *result*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rel = path.relative_to(output_dir).as_posix()
    result.written_files.append(rel)
    result.extracted_texts.append(rel)


def _qn(namespace: str, tag: str) -> str:
    """Return the Clark-notation qualified name for *tag* in *namespace*."""
    return f"{{{namespace}}}{tag}"


# --- docProps/app.xml -> texts/slide_titles.txt -----------------------------


def _heading_pairs(root: ET.Element) -> list[tuple[str, int]]:
    """Return ``(heading, count)`` pairs from ``HeadingPairs``, if any."""
    heading_pairs_el = root.find(_qn(_NS_EXT_PROPS, "HeadingPairs"))
    if heading_pairs_el is None:
        return []
    vector_el = heading_pairs_el.find(_qn(_NS_VT, "vector"))
    if vector_el is None:
        return []
    variants = vector_el.findall(_qn(_NS_VT, "variant"))
    pairs: list[tuple[str, int]] = []
    for i in range(0, len(variants) - 1, 2):
        label_el = variants[i].find(_qn(_NS_VT, "lpstr"))
        count_el = variants[i + 1].find(_qn(_NS_VT, "i4"))
        if label_el is None or count_el is None:
            continue
        try:
            count = int(count_el.text or "0")
        except ValueError:
            count = 0
        pairs.append((label_el.text or "", count))
    return pairs


def _titles_of_parts(root: ET.Element) -> list[str]:
    """Return the ``TitlesOfParts`` string vector, if any."""
    titles_el = root.find(_qn(_NS_EXT_PROPS, "TitlesOfParts"))
    if titles_el is None:
        return []
    vector_el = titles_el.find(_qn(_NS_VT, "vector"))
    if vector_el is None:
        return []
    return [el.text or "" for el in vector_el.findall(_qn(_NS_VT, "lpstr"))]


def _slide_titles(root: ET.Element) -> tuple[list[str], bool]:
    """Return ``(titles, sliced)`` for the slide-title section.

    ``sliced`` is True when a known slide-title heading was found in
    ``HeadingPairs`` and *titles* was sliced accordingly; False means
    *titles* is the full, unsliced ``TitlesOfParts`` vector.
    """
    titles = _titles_of_parts(root)
    offset = 0
    for label, count in _heading_pairs(root):
        if label in _SLIDE_TITLE_HEADINGS:
            return titles[offset:offset + count], True
        offset += count
    return titles, False


def _write_slide_titles(root: ET.Element, output_dir: Path,
                        tr: Callable[[str], str],
                        result: ExtractResult) -> None:
    """Derive ``texts/slide_titles.txt`` from ``docProps/app.xml``."""
    titles, sliced = _slide_titles(root)
    if sliced:
        lines = [tr("Slide titles recovered from document properties:")]
    else:
        lines = [tr(
            "No slide-title heading found; listing all recovered "
            "title strings:")]
    lines.extend(f"{i}. {title}" for i, title in enumerate(titles, start=1))
    _write_text_file(
        output_dir / "texts" / "slide_titles.txt", lines, output_dir, result)


# --- docProps/core.xml -> texts/document_info.txt ---------------------------


def _write_document_info(root: ET.Element, output_dir: Path,
                         tr: Callable[[str], str],
                         result: ExtractResult) -> None:
    """Derive ``texts/document_info.txt`` from ``docProps/core.xml``."""
    lines = []
    for label, namespace, tag in _CORE_FIELDS:
        el = root.find(_qn(namespace, tag))
        if el is not None and el.text:
            lines.append(f"{tr(label)}: {el.text}")
    _write_text_file(
        output_dir / "texts" / "document_info.txt", lines, output_dir,
        result)


# --- ppt/slides|notesSlides -> texts/<stem>.txt ------------------------------


def _slide_body_text(root: ET.Element) -> str:
    """Join every ``<a:t>`` run under *root*, one paragraph per line."""
    paragraphs = []
    for p in root.iter(_qn(_NS_A, "p")):
        text = "".join(t.text or "" for t in p.iter(_qn(_NS_A, "t")))
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _write_body_text(root: ET.Element, output_dir: Path, stem: str,
                     result: ExtractResult) -> None:
    """Derive ``texts/<stem>.txt`` from a slide or notes-slide part.

    Nothing is written when the recovered body text is empty.
    """
    body = _slide_body_text(root)
    if not body:
        return
    path = output_dir / "texts" / f"{stem}.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body + "\n", encoding="utf-8")
    rel = path.relative_to(output_dir).as_posix()
    result.written_files.append(rel)
    result.extracted_texts.append(rel)


# --- ppt/charts/chart*.xml -> charts/<stem>_data.csv -------------------------


def _cache_points(parent: ET.Element, child_tag: str) -> dict[int, str]:
    """Return ``{idx: value}`` from the ``strCache``/``numCache`` under
    *parent*/<child_tag>, or an empty dict when no cache is present."""
    container = parent.find(_qn(_NS_C, child_tag))
    if container is None:
        return {}
    # The cache sits inside a c:strRef/c:numRef (or, for literal data,
    # directly under the container), so search all descendants rather
    # than assuming a fixed nesting depth.
    cache = container.find(f".//{_qn(_NS_C, 'strCache')}")
    if cache is None:
        cache = container.find(f".//{_qn(_NS_C, 'numCache')}")
    if cache is None:
        return {}
    points: dict[int, str] = {}
    for pt in cache.findall(_qn(_NS_C, "pt")):
        idx_attr = pt.get("idx")
        v_el = pt.find(_qn(_NS_C, "v"))
        if idx_attr is None or v_el is None:
            continue
        points[int(idx_attr)] = v_el.text or ""
    return points


def _series_name(ser: ET.Element) -> str | None:
    """Return the display name of *ser* (``c:tx``), if any."""
    tx = ser.find(_qn(_NS_C, "tx"))
    if tx is None:
        return None
    str_ref = tx.find(_qn(_NS_C, "strRef"))
    if str_ref is not None:
        cache = str_ref.find(_qn(_NS_C, "strCache"))
        if cache is not None:
            pt = cache.find(_qn(_NS_C, "pt"))
            if pt is not None:
                v = pt.find(_qn(_NS_C, "v"))
                if v is not None:
                    return v.text
    v = tx.find(_qn(_NS_C, "v"))
    return v.text if v is not None else None


def _parse_chart_rows(root: ET.Element,
                      tr: Callable[[str], str]) -> list[list[str]] | None:
    """Build a header + data-row CSV table from cached chart series data.

    Returns None when no series carry a category cache, meaning the
    chart has no recoverable cached data (only the raw XML survives).
    """
    series_list = root.findall(f".//{_qn(_NS_C, 'ser')}")
    if not series_list:
        return None

    categories: dict[int, str] = {}
    columns: list[tuple[str, dict[int, str]]] = []
    for i, ser in enumerate(series_list):
        cat_points = _cache_points(ser, "cat")
        if cat_points:
            categories.update(cat_points)
        val_points = _cache_points(ser, "val")
        name = _series_name(ser)
        if name is None:
            name = (tr("Value") if len(series_list) == 1
                    else f"{tr('Series')} {i + 1}")
        columns.append((name, val_points))
    if not categories:
        return None

    max_idx = max(categories)
    for _, vals in columns:
        if vals:
            max_idx = max(max_idx, max(vals))

    header = [tr("Category")] + [name for name, _ in columns]
    rows = [header]
    for idx in range(max_idx + 1):
        rows.append(
            [categories.get(idx, "")]
            + [vals.get(idx, "") for _, vals in columns])
    return rows


def _write_chart_csv(path: Path, rows: list[list[str]]) -> None:
    """Write *rows* (header first) to *path* as CSV."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
