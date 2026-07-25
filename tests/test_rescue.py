"""Tests for :mod:`pptrepair.rescue` and the ``salvage`` CLI command.

Every fixture is a synthetic archive built in memory and written under
``tmp_path``; the real ``broken_ppt/`` / ``normal_ppt/`` sample
directories are never touched, and no rescue output is ever written
outside ``tmp_path``. The four rescue stages are exercised end-to-end
through the real diagnosis pipeline, and the CLI is driven in-process
via :func:`pptrepair.cli.main`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures import (
    build_minimal_jpeg,
    build_minimal_png,
    build_minimal_pptx,
    foreign_prefix,
    rebuild_with_entries,
    zero_entry_data_tail,
    zero_interior_entry,
)

from pptrepair import rescue
from pptrepair.classify import Verdict
from pptrepair.cli import EXIT_CORRUPT, EXIT_OK, main
from pptrepair.repair import OutputExistsError

#: Small media payload so fixtures stay fast to build and scan.
_MEDIA_BYTES = 40_000

#: DrawingML/PresentationML namespaces for hand-built slide parts.
_NS_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_NS_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the resulting path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _big_slide_xml(marker: str, runs: int = 150) -> bytes:
    """Return a slide part with *runs* text runs, each tagged with *marker*."""
    body = "".join(
        f"<a:p><a:r><a:t>{marker} line {i} lorem ipsum dolor sit</a:t>"
        "</a:r></a:p>"
        for i in range(runs)
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<p:sld xmlns:p="{_NS_P}" xmlns:a="{_NS_A}"><p:cSld><p:spTree>'
        f"<p:sp><p:txBody>{body}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    return xml.encode("utf-8")


def _files_under(directory: Path) -> list[Path]:
    """Return every regular file below *directory* (empty when absent)."""
    if not directory.exists():
        return []
    return [path for path in directory.rglob("*") if path.is_file()]


# --- stage 1: readable entry rescue -----------------------------------------


def test_readable_entries_saved_and_counted(tmp_path: Path) -> None:
    """Interior damage: readable entries land in entries/, count matches."""
    broken = zero_interior_entry(
        build_minimal_pptx(num_slides=3, media_bytes=_MEDIA_BYTES, seed=1),
        skip=2)
    path = _write(tmp_path, "interior.pptx", broken)

    result = rescue.rescue_file(path)

    assert result.verdict == Verdict.INTERIOR_DAMAGE
    entries = _files_under(result.output_dir / "entries")
    assert len(entries) == result.entries_saved
    assert result.entries_saved == result.report["counts"]["entries_saved"]
    assert result.entries_saved > 0
    # The report file mirrors the in-memory report exactly.
    on_disk = json.loads(
        (result.output_dir / "salvage_report.json").read_text("utf-8"))
    assert on_disk == result.report


# --- stage 2: image carving --------------------------------------------------


def _overlay_image_in_head(data: bytes, image: bytes,
                           insert_at: int = 256) -> bytes:
    """Overwrite *data*'s head with foreign bytes embedding *image*.

    The overwritten region is large enough to hold *image* at
    *insert_at* while leaving the archive's central directory and tail
    entries untouched, yielding a corrupted (non-NORMAL) file whose raw
    bytes contain the embedded image for the carver to find.
    """
    boundary = insert_at + len(image) + 512
    head = bytearray(foreign_prefix(data, boundary))
    head[insert_at:insert_at + len(image)] = image
    return bytes(head)


def test_carves_embedded_jpeg(tmp_path: Path) -> None:
    """A JPEG hidden in foreign head data is carved into carved/."""
    jpeg = build_minimal_jpeg()
    data = build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES, seed=2)
    path = _write(tmp_path, "carve_jpeg.pptx",
                  _overlay_image_in_head(data, jpeg))

    result = rescue.rescue_file(path)

    assert result.verdict != Verdict.NORMAL
    assert result.carved_images >= 1
    carved = _files_under(result.output_dir / "carved")
    assert any(path.read_bytes() == jpeg for path in carved)
    # Provenance of carved images is explicitly marked unknown.
    assert all(item["provenance"] == "unknown"
               for item in result.report["carved"])


def test_carves_embedded_png(tmp_path: Path) -> None:
    """A PNG hidden in foreign head data is carved into carved/."""
    png = build_minimal_png()
    data = build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES, seed=3)
    path = _write(tmp_path, "carve_png.pptx",
                  _overlay_image_in_head(data, png))

    result = rescue.rescue_file(path)

    assert result.verdict != Verdict.NORMAL
    assert result.carved_images >= 1
    carved = _files_under(result.output_dir / "carved")
    assert any(path.read_bytes() == png for path in carved)


def test_carved_images_deduplicated_against_entries(tmp_path: Path) -> None:
    """A stored media JPEG is rescued once, not duplicated by carving."""
    jpeg = build_minimal_jpeg()
    data = build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES, seed=5)
    # Store the JPEG uncompressed so its raw bytes appear in the archive
    # and the carver can find them, then damage an unrelated interior part.
    with_media = rebuild_with_entries(
        data, extra={"ppt/media/photo.jpg": jpeg},
        stored={"ppt/media/photo.jpg"})
    broken = zero_interior_entry(with_media, skip=2)
    path = _write(tmp_path, "dedup.pptx", broken)

    result = rescue.rescue_file(path)

    # The media part is rescued as a normal entry ...
    entries = _files_under(result.output_dir / "entries")
    assert any(path.read_bytes() == jpeg for path in entries)
    # ... and the identical carved bitstream is dropped as a duplicate.
    carved = _files_under(result.output_dir / "carved")
    assert all(path.read_bytes() != jpeg for path in carved)


# --- stage 3 + 4: partial XML decode and text extraction --------------------


def test_partial_xml_and_text_recovery(tmp_path: Path) -> None:
    """A slide with a zeroed data tail yields a partial part and text."""
    marker = "RESCUEMARK"
    base = build_minimal_pptx(num_slides=3, media_bytes=_MEDIA_BYTES, seed=4)
    with_big = rebuild_with_entries(
        base, extra={"ppt/slides/slide1.xml": _big_slide_xml(marker)})
    broken = zero_entry_data_tail(
        with_big, "ppt/slides/slide1.xml", keep_fraction=0.6)
    path = _write(tmp_path, "partial.pptx", broken)

    result = rescue.rescue_file(path)

    assert result.partial_xml >= 1
    partials = _files_under(result.output_dir / "partial_xml")
    assert partials
    assert all(p.stat().st_size >= rescue.MIN_PARTIAL_XML_BYTES
               for p in partials)

    text_file = result.output_dir / "rescued_text.txt"
    assert text_file.exists()
    text = text_file.read_text("utf-8")
    assert marker in text
    assert result.text_lines >= 1


# --- path safety ------------------------------------------------------------


def test_unsafe_entry_name_stays_inside_output(tmp_path: Path) -> None:
    """A ``../evil.txt`` entry is flattened and never escapes the folder."""
    payload = b"EVIL CONTENT"
    base = build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES, seed=7)
    with_evil = rebuild_with_entries(base, extra={"../evil.txt": payload})
    broken = zero_interior_entry(with_evil, skip=2)
    path = _write(tmp_path, "evil.pptx", broken)

    result = rescue.rescue_file(path)

    output_dir = result.output_dir
    # Nothing escaped one or two levels up out of entries/.
    assert not (output_dir / "evil.txt").exists()
    assert not (output_dir.parent / "evil.txt").exists()
    # The content was still rescued, under a flattened, contained name.
    flattened = [
        p for p in _files_under(output_dir / "entries")
        if p.name.startswith("_unsafe_") and p.name.endswith("evil.txt")
    ]
    assert flattened
    assert flattened[0].read_bytes() == payload
    assert any("evil.txt" in warning for warning in result.warnings)


# --- intact input and output-directory handling -----------------------------


def test_normal_file_creates_no_output(tmp_path: Path) -> None:
    """An intact file rescues nothing and creates no output folder."""
    good = build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES)
    path = _write(tmp_path, "good.pptx", good)

    result = rescue.rescue_file(path)

    assert result.verdict == Verdict.NORMAL
    assert result.output_dir is None
    assert not (tmp_path / "good.rescued").exists()
    assert result.rescued_count() == 0


def test_existing_output_requires_force(tmp_path: Path) -> None:
    """An existing output folder is refused unless --force reuses it."""
    broken = zero_interior_entry(
        build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES, seed=1),
        skip=2)
    path = _write(tmp_path, "reuse.pptx", broken)
    out_dir = tmp_path / "rescued_here"
    out_dir.mkdir()

    with pytest.raises(OutputExistsError):
        rescue.rescue_file(path, out_dir)

    result = rescue.rescue_file(path, out_dir, force=True)
    assert result.output_dir == out_dir
    assert (out_dir / "salvage_report.json").exists()


# --- CLI --------------------------------------------------------------------


def test_cli_json_smoke(tmp_path: Path,
                        capsys: pytest.CaptureFixture[str]) -> None:
    """The salvage CLI emits a coherent report and a matching exit code."""
    broken = zero_interior_entry(
        build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES, seed=1),
        skip=2)
    path = _write(tmp_path, "cli.pptx", broken)
    out_dir = tmp_path / "cli_out"

    exit_code = main(["salvage", "--json", "-o", str(out_dir), str(path)])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_OK
    assert payload["kind"] == "pptrepair-rescue-report"
    assert payload["schema_version"] == rescue.REPORT_SCHEMA_VERSION
    assert payload["counts"]["entries_saved"] > 0
    saved = len(_files_under(out_dir / "entries"))
    assert saved == payload["counts"]["entries_saved"]


def test_cli_normal_reports_nothing_to_salvage(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An intact file exits 0 with a plain-text notice and no output."""
    good = build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES)
    path = _write(tmp_path, "intact.pptx", good)

    exit_code = main(["salvage", str(path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Nothing to salvage" in out
    assert not (tmp_path / "intact.rescued").exists()


def test_cli_nothing_rescued_exits_corrupt(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A file with no recoverable content exits 1."""
    path = _write(tmp_path, "empty.pptx", b"")

    exit_code = main(["salvage", str(path)])

    assert exit_code == EXIT_CORRUPT
    assert "Salvage summary" in capsys.readouterr().out
