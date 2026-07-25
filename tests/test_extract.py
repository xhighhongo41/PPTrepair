"""Tests for :mod:`pptrepair.extract`.

Uses a fake reader that exposes the same ``open``/``datetime_of``
surface as :class:`pptrepair.salvage.SalvageReader` without depending
on that (still under parallel development) implementation.
:class:`~pptrepair.salvage.SalvagedEntry` and
:class:`~pptrepair.census.EntryResult` are constructed directly.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

from fixtures import build_minimal_pptx

from pptrepair.census import EntryResult, categorize
from pptrepair.extract import ExtractResult, extract_salvage
from pptrepair.salvage import SalvagedEntry


class FakeSalvageReader:
    """In-memory stand-in for :class:`pptrepair.salvage.SalvageReader`.

    Holds each entry's full payload and yields it back in one or more
    chunks, matching the ``open``/``datetime_of`` interface that
    :func:`pptrepair.extract.extract_salvage` relies on.
    """

    def __init__(self, payloads: dict[str, bytes],
                chunk_size: int = 8192) -> None:
        """Store *payloads* (entry name -> raw bytes) for later replay."""
        self._payloads = payloads
        self._chunk_size = chunk_size

    def open(self, salvaged: SalvagedEntry) -> Iterator[bytes]:
        """Yield the payload of *salvaged* in fixed-size chunks."""
        data = self._payloads[salvaged.name]
        step = self._chunk_size
        for start in range(0, len(data), step) if data else [0]:
            yield data[start:start + step]

    def datetime_of(self, salvaged: SalvagedEntry) -> None:
        """Always report no recoverable timestamp."""
        return


def _entry(name: str) -> SalvagedEntry:
    """Build a SalvagedEntry for *name* with a plausible EntryResult."""
    return SalvagedEntry(
        name=name,
        category=categorize(name),
        source="cd",
        entry=EntryResult(
            name=name, category=categorize(name), header_offset=0,
            file_size=0, ok=True,
        ),
    )


def _pptx_parts(include_chart: bool = True, num_slides: int = 3,
                media_bytes: int = 2000) -> dict[str, bytes]:
    """Return {name: raw bytes} for every part of a synthetic .pptx."""
    data = build_minimal_pptx(
        num_slides=num_slides, media_bytes=media_bytes,
        include_chart=include_chart,
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _read(path: Path) -> str:
    """Read a text file written by extract_salvage as UTF-8."""
    return path.read_text(encoding="utf-8")


class TestRouting:
    """Every entry's raw payload lands in exactly one place."""

    def test_image_media_routed_to_images(self, tmp_path: Path) -> None:
        """A ppt/media/*.png entry is written under images/."""
        parts = _pptx_parts()
        reader = FakeSalvageReader(parts)
        salvaged = [_entry("ppt/media/image1.png")]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        dest = tmp_path / "images" / "image1.png"
        assert dest.read_bytes() == parts["ppt/media/image1.png"]
        assert "images/image1.png" in result.written_files
        assert not (tmp_path / "parts").exists()

    def test_chart_routed_to_charts_raw_and_csv(self, tmp_path: Path) -> None:
        """A chart entry is written raw plus a derived CSV under charts/."""
        parts = _pptx_parts()
        reader = FakeSalvageReader(parts)
        salvaged = [_entry("ppt/charts/chart1.xml")]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        raw = tmp_path / "charts" / "chart1.xml"
        assert raw.read_bytes() == parts["ppt/charts/chart1.xml"]
        assert (tmp_path / "charts" / "chart1_data.csv").exists()
        assert "charts/chart1.xml" in result.written_files
        assert "charts/chart1_data.csv" in result.written_files
        assert not (tmp_path / "parts").exists()

    def test_other_part_routed_under_parts_with_original_path(
        self, tmp_path: Path
    ) -> None:
        """A misc part (presProps.xml) lands at parts/<original path>."""
        parts = _pptx_parts()
        reader = FakeSalvageReader(parts)
        salvaged = [_entry("ppt/presProps.xml")]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        dest = tmp_path / "parts" / "ppt" / "presProps.xml"
        assert dest.read_bytes() == parts["ppt/presProps.xml"]
        assert "parts/ppt/presProps.xml" in result.written_files


class TestSlideTitles:
    """docProps/app.xml -> texts/slide_titles.txt."""

    def test_slice_via_heading_pairs(self, tmp_path: Path) -> None:
        """A HeadingPairs "Slide Titles" section slices TitlesOfParts."""
        parts = _pptx_parts(num_slides=3)
        reader = FakeSalvageReader(parts)
        salvaged = [_entry("docProps/app.xml")]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        text = _read(tmp_path / "texts" / "slide_titles.txt")
        assert "Slide Title 2" in text
        assert "texts/slide_titles.txt" in result.extracted_texts


class TestDocumentInfo:
    """docProps/core.xml -> texts/document_info.txt."""

    def test_contains_creator(self, tmp_path: Path) -> None:
        """The recovered creator name appears in document_info.txt."""
        parts = _pptx_parts()
        reader = FakeSalvageReader(parts)
        salvaged = [_entry("docProps/core.xml")]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        text = _read(tmp_path / "texts" / "document_info.txt")
        assert "Fixture Author" in text
        assert "texts/document_info.txt" in result.extracted_texts


class TestSlideBodyText:
    """ppt/slides/slideN.xml -> texts/slideN.txt."""

    def test_body_text_recovered(self, tmp_path: Path) -> None:
        """The slide's <a:t> runs are joined into texts/slide1.txt."""
        parts = _pptx_parts()
        reader = FakeSalvageReader(parts)
        salvaged = [_entry("ppt/slides/slide1.xml")]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        text = _read(tmp_path / "texts" / "slide1.txt")
        assert "Slide 1 body text" in text
        assert "texts/slide1.txt" in result.extracted_texts

    def test_notes_slide_body_text(self, tmp_path: Path) -> None:
        """A notes-slide part recovers the same way, under notesSlideN.txt."""
        name = "ppt/notesSlides/notesSlide1.xml"
        payload = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<p:notes xmlns:p="http://schemas.openxmlformats.org/'
            b'presentationml/2006/main" '
            b'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            b'<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r>'
            b'<a:t>Speaker note text</a:t>'
            b'</a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:notes>'
        )
        reader = FakeSalvageReader({name: payload})
        salvaged = [_entry(name)]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        text = _read(tmp_path / "texts" / "notesSlide1.txt")
        assert "Speaker note text" in text
        assert "texts/notesSlide1.txt" in result.extracted_texts


class TestChartCsv:
    """Chart CSV data recovery."""

    def test_csv_contains_categories_and_values(self, tmp_path: Path) -> None:
        """The recovered CSV holds both cached categories and values."""
        parts = _pptx_parts()
        reader = FakeSalvageReader(parts)
        salvaged = [_entry("ppt/charts/chart1.xml")]

        extract_salvage(reader, salvaged, tmp_path, str)

        csv_path = tmp_path / "charts" / "chart1_data.csv"
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        flat = [cell for row in rows for cell in row]
        assert "Category A" in flat
        assert "Category B" in flat
        assert "10.5" in flat
        assert "20.5" in flat


class TestBrokenXml:
    """A broken app.xml downgrades to a warning, keeping the raw copy."""

    def test_broken_app_xml_warns_and_keeps_raw(self, tmp_path: Path) -> None:
        """Malformed XML yields a warning; parts/ keeps the raw bytes."""
        reader = FakeSalvageReader({"docProps/app.xml": b"<broken"})
        salvaged = [_entry("docProps/app.xml")]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        assert result.warnings
        raw = tmp_path / "parts" / "docProps" / "app.xml"
        assert raw.read_bytes() == b"<broken"
        assert not (tmp_path / "texts" / "slide_titles.txt").exists()


class TestZipSlip:
    """Hostile entry names are skipped, never written anywhere."""

    def test_traversal_names_are_skipped(self, tmp_path: Path) -> None:
        """"../evil.txt", "/abs.txt" and a drive-lettered path are rejected."""
        names = ["../evil.txt", "/abs.txt", "C:\\win.txt"]
        reader = FakeSalvageReader({name: b"payload" for name in names})
        salvaged = [_entry(name) for name in names]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        assert set(result.skipped) == set(names)
        assert result.written_files == []
        # Nothing must have been created under the output directory at all.
        assert list(tmp_path.iterdir()) == []


class TestTranslation:
    """The tr() callable is applied to every generated heading/label."""

    def test_tr_applied_to_headings(self, tmp_path: Path) -> None:
        """Headings in slide_titles.txt / document_info.txt carry the marker."""
        parts = _pptx_parts()
        reader = FakeSalvageReader(parts)
        salvaged = [_entry("docProps/app.xml"), _entry("docProps/core.xml")]

        def tr(message: str) -> str:
            return "@@" + message

        extract_salvage(reader, salvaged, tmp_path, tr)

        titles_text = _read(tmp_path / "texts" / "slide_titles.txt")
        assert titles_text.startswith("@@")

        info_text = _read(tmp_path / "texts" / "document_info.txt")
        assert "@@Creator" in info_text


class TestDuplicateBasenames:
    """Two media entries sharing a basename must both survive."""

    def test_duplicate_image_basenames_both_written(
        self, tmp_path: Path
    ) -> None:
        """Colliding basenames are disambiguated as "name (2).ext"."""
        first = b"\x89PNGfirst"
        second = b"\x89PNGsecond"
        reader = FakeSalvageReader({
            "ppt/media/image1.png": first,
            "ppt/embeddings/image1.png": second,
        })
        salvaged = [
            _entry("ppt/media/image1.png"),
            _entry("ppt/embeddings/image1.png"),
        ]

        result = extract_salvage(reader, salvaged, tmp_path, str)

        # Only ppt/media/* is routed to images/; the second entry, not
        # being under ppt/media/, falls through to parts/.
        assert (tmp_path / "images" / "image1.png").read_bytes() == first
        assert (
            tmp_path / "parts" / "ppt" / "embeddings" / "image1.png"
        ).read_bytes() == second
        assert isinstance(result, ExtractResult)

    def test_duplicate_basenames_within_media_folder_are_disambiguated(
        self, tmp_path: Path
    ) -> None:
        """Two ppt/media/ entries with the same basename both survive."""
        first = b"\x89PNGfirst"
        second = b"\x89PNGsecond"
        reader = FakeSalvageReader({
            "ppt/media/image1.png": first,
        })
        # Simulate a second, distinctly-pathed media entry that still
        # collapses to the same basename once routed to images/.
        reader._payloads["ppt/media/sub/image1.png"] = second
        salvaged = [
            _entry("ppt/media/image1.png"),
            _entry("ppt/media/sub/image1.png"),
        ]

        extract_salvage(reader, salvaged, tmp_path, str)

        images_dir = tmp_path / "images"
        written = {p.name: p.read_bytes() for p in images_dir.iterdir()}
        assert written == {
            "image1.png": first,
            "image1 (2).png": second,
        }
