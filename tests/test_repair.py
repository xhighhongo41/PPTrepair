"""Tests for :mod:`pptrepair.repair`, :mod:`pptrepair.report` (repair
rendering) and the ``pptrepair repair`` CLI command.

Exercises the full diagnose -> mode-select -> rebuild/extract -> re-verify
pipeline through :func:`pptrepair.cli.main`, using small synthetic
archives written under ``tmp_path``. Nothing is ever written inside the
repository itself, and the real ``broken_ppt/`` / ``normal_ppt/`` sample
directories are never touched.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from pathlib import Path

import pytest
from fixtures import (append_foreign_tail, build_minimal_pptx, truncate,
                      zero_interior_entry, zero_prefix)

from pptrepair.census import from_central_directory, from_lfh_scan
from pptrepair.classify import Diagnosis, Verdict, classify
from pptrepair.cli import main
from pptrepair.repair import repair_file
from pptrepair.scanner import scan_structure

#: Shorthand for the capsys fixture type, to keep signatures short.
CaptureFixture = pytest.CaptureFixture[str]

#: Matches a slide XML archive member name.
_SLIDE_RE = re.compile(r"^ppt/slides/slide\d+\.xml$")


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the resulting path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _diagnose(path: Path) -> Diagnosis:
    """Run the scan -> census -> classify pipeline over *path*."""
    structure = scan_structure(path)
    cd_census = from_central_directory(path)
    lfh_census = from_lfh_scan(path)
    return classify(path, structure, cd_census, lfh_census)


def _header_offset(data: bytes, name: str) -> int:
    """Return the local-file-header offset of *name* inside *data*."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.getinfo(name).header_offset


def _rebuildable_truncated_pptx(num_slides: int = 3,
                                media_bytes: int = 4096) -> bytes:
    """Build a TAIL_TRUNCATED fixture that keeps every slide intact.

    Cuts the archive right where the (large) media part would start, so
    the content types, package relationships, presentation part and
    every slide (plus its relationships) survive fully intact, while
    the central directory and everything after it is lost.
    """
    data = build_minimal_pptx(num_slides=num_slides, media_bytes=media_bytes)
    cutoff = _header_offset(data, "ppt/media/image1.png")
    return truncate(data, cutoff)


def _extractable_head_zero_pptx(num_slides: int = 200,
                                media_bytes: int = 50_000) -> bytes:
    """Build a HEAD_ZERO_FILL fixture whose media part survives intact.

    Many small slides are used to push the media part's header offset
    past :data:`pptrepair.classify.HEAD_ZERO_MIN_LENGTH`, so the leading
    zero run can cover every small XML part (presentation, slides, ...)
    while leaving the media entry -- and the small tail parts written
    after it -- readable through the central directory.
    """
    data = build_minimal_pptx(num_slides=num_slides, media_bytes=media_bytes)
    cutoff = _header_offset(data, "ppt/media/image1.png")
    return zero_prefix(data, cutoff)


# --- 1. rebuild repairs a tail-truncated file --------------------------------


def test_truncate_rebuilds_intact_file(tmp_path: Path,
                                       capsys: CaptureFixture) -> None:
    """A tail-truncated file rebuilds into an intact package, slides kept."""
    broken = _rebuildable_truncated_pptx(num_slides=3)
    path = _write(tmp_path, "broken.pptx", broken)

    exit_code = main(["repair", str(path)])

    assert exit_code == 0
    output = tmp_path / "broken.repaired.pptx"
    assert output.exists()
    assert _diagnose(output).verdict == Verdict.NORMAL
    with zipfile.ZipFile(output) as zf:
        slide_names = [n for n in zf.namelist() if _SLIDE_RE.match(n)]
    assert len(slide_names) == 3
    capsys.readouterr()


# --- 2. extract salvages a head-zero-filled file -----------------------------


def test_zero_prefix_extracts_recovery_folder(tmp_path: Path) -> None:
    """A head-zero-filled file extracts a recovery folder with a report."""
    broken = _extractable_head_zero_pptx()
    path = _write(tmp_path, "broken.pptx", broken)

    exit_code = main(["repair", str(path)])

    assert exit_code == 0
    out_dir = tmp_path / "broken.salvaged"
    assert out_dir.is_dir()
    assert (out_dir / "images").is_dir()
    assert (out_dir / "texts" / "slide_titles.txt").is_file()
    report = (out_dir / "REPORT.txt").read_text(encoding="utf-8")
    assert "head_zero_fill" in report
    assert "Hint" in report


# --- 3. --lang ja works without a compiled catalog ---------------------------


def test_lang_ja_renders_japanese_report(tmp_path: Path) -> None:
    """``--lang ja`` renders the report through the shipped ja catalog.

    Verdict codes stay English by design; the surrounding sentences must
    come out in Japanese.
    """
    broken = _extractable_head_zero_pptx()
    path = _write(tmp_path, "broken.pptx", broken)

    exit_code = main(["repair", str(path), "--lang", "ja"])

    assert exit_code == 0
    out_dir = tmp_path / "broken.salvaged"
    report = (out_dir / "REPORT.txt").read_text(encoding="utf-8")
    assert "head_zero_fill" in report
    assert "ヒント" in report
    assert "判定" in report


# --- 4. an intact file is a no-op --------------------------------------------


def test_intact_file_is_a_no_op(tmp_path: Path,
                                capsys: CaptureFixture) -> None:
    """An already-intact file produces no artifact but reports success."""
    data = build_minimal_pptx(num_slides=2, media_bytes=4096)
    path = _write(tmp_path, "healthy.pptx", data)

    exit_code = main(["repair", str(path)])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert not (tmp_path / "healthy.repaired.pptx").exists()
    assert not (tmp_path / "healthy.salvaged").exists()
    assert out.strip() != ""
    assert "normal" in out


# --- 5. a non-ZIP file is unrepairable ---------------------------------------


def test_non_zip_input_is_unrepairable(tmp_path: Path) -> None:
    """A file with no ZIP structure at all yields exit code 1."""
    path = _write(tmp_path, "junk.pptx", b"not a zip file" * 100)

    exit_code = main(["repair", str(path)])

    assert exit_code == 1


# --- 6. an existing output requires --force ----------------------------------


def test_existing_output_requires_force(tmp_path: Path,
                                        capsys: CaptureFixture) -> None:
    """An existing output path fails without --force, succeeds with it."""
    broken = _rebuildable_truncated_pptx(num_slides=3)
    path = _write(tmp_path, "broken.pptx", broken)
    existing_output = tmp_path / "broken.repaired.pptx"
    existing_output.write_bytes(b"placeholder")

    exit_code = main(["repair", str(path)])
    err = capsys.readouterr().err
    assert exit_code == 2
    assert "force" in err.lower()
    assert existing_output.read_bytes() == b"placeholder"

    exit_code = main(["repair", str(path), "--force"])
    capsys.readouterr()
    assert exit_code == 0
    assert existing_output.read_bytes() != b"placeholder"


# --- 7. a forced rebuild without a presentation part is unrepairable --------


def test_forced_rebuild_without_presentation_fails(tmp_path: Path) -> None:
    """``--mode rebuild`` on a file lacking presentation.xml exits 1."""
    broken = _extractable_head_zero_pptx()
    path = _write(tmp_path, "broken.pptx", broken)

    exit_code = main(["repair", str(path), "--mode", "rebuild"])

    assert exit_code == 1
    assert not (tmp_path / "broken.repaired.pptx").exists()


# --- 8. --json emits the documented schema -----------------------------------


def test_json_output_schema(tmp_path: Path, capsys: CaptureFixture) -> None:
    """``--json`` emits an object with exactly the documented keys."""
    broken = _rebuildable_truncated_pptx(num_slides=3)
    path = _write(tmp_path, "broken.pptx", broken)

    exit_code = main(["repair", str(path), "--json"])

    out = capsys.readouterr().out
    assert exit_code == 0
    payload = json.loads(out)
    assert set(payload.keys()) == {
        "path", "verdict", "mode", "success", "output", "salvage",
        "lost_slide_numbers", "lost_entries_total", "trimmed_bytes",
        "recheck_verdict", "recheck_dangling_refs", "synthesized_parts",
        "pruned_relationships", "pruned_slide_ids", "cleaned_parts",
        "removed_elements", "written_files", "warnings",
    }
    assert payload["mode"] == "rebuild"
    assert payload["success"] is True
    assert payload["recheck_verdict"] == "normal"
    assert payload["recheck_dangling_refs"] == 0


# --- 9. a nonexistent input reports an error ---------------------------------


def test_nonexistent_input_reports_error(tmp_path: Path,
                                         capsys: CaptureFixture) -> None:
    """A missing input path yields exit code 2 with an stderr message."""
    missing = tmp_path / "does_not_exist.pptx"

    exit_code = main(["repair", str(missing)])

    err = capsys.readouterr().err
    assert exit_code == 2
    assert err.strip() != ""


# --- 10. -o selects an explicit output path ----------------------------------


def test_explicit_output_path(tmp_path: Path,
                              capsys: CaptureFixture) -> None:
    """``-o`` overrides the default output path."""
    broken = _rebuildable_truncated_pptx(num_slides=3)
    path = _write(tmp_path, "broken.pptx", broken)
    custom_output = tmp_path / "custom.pptx"

    exit_code = main(["repair", str(path), "-o", str(custom_output)])

    capsys.readouterr()
    assert exit_code == 0
    assert custom_output.exists()
    assert not (tmp_path / "broken.repaired.pptx").exists()


# --- 11. trim repairs a complete archive hidden behind foreign tail data -----


def test_trim_success_reproduces_the_leading_archive_exactly(
    tmp_path: Path,
) -> None:
    """A TAIL_FOREIGN_DATA file trims to a byte-exact copy of the
    leading archive, with no lost entries."""
    intact = build_minimal_pptx(num_slides=2, media_bytes=50_000)
    broken = append_foreign_tail(intact, 131072)
    path = _write(tmp_path, "broken.pptx", broken)
    assert _diagnose(path).verdict == Verdict.TAIL_FOREIGN_DATA

    outcome = repair_file(path)

    assert outcome.mode == "trim"
    assert outcome.success is True
    assert outcome.output_path is not None
    assert outcome.output_path.exists()
    assert outcome.trimmed_bytes == 131072
    assert outcome.recheck_verdict == "normal"
    assert outcome.lost_entries_total == 0
    assert outcome.output_path.read_bytes() == intact


# --- 12. trim falls back to rebuild when the leading archive is itself broken


def test_trim_falls_back_to_rebuild_when_leading_archive_is_damaged(
    tmp_path: Path,
) -> None:
    """A TAIL_FOREIGN_DATA file whose own leading archive is damaged
    (one non-essential entry destroyed) does not check out clean after
    trimming, so repair falls back to a salvage-based rebuild."""
    # skip=17 targets ppt/viewProps.xml for num_slides=3/no chart, a
    # non-essential part: presentation.xml, every slide and the media
    # part all stay salvageable for the rebuild fallback.
    data = build_minimal_pptx(num_slides=3, media_bytes=50_000)
    damaged = zero_interior_entry(data, skip=17)
    broken = append_foreign_tail(damaged, 131072)
    path = _write(tmp_path, "broken.pptx", broken)
    assert _diagnose(path).verdict == Verdict.TAIL_FOREIGN_DATA

    outcome = repair_file(path)

    assert outcome.mode == "rebuild"
    assert outcome.success is True
    assert outcome.output_path is not None
    assert outcome.output_path.exists()
    assert any("falling back to salvage-based repair" in warning
               for warning in outcome.warnings)


# --- 13. empty and fully zero-filled files are unrepairable no-ops ----------


def test_empty_file_repair_is_a_noop_failure(tmp_path: Path) -> None:
    """An empty file yields mode "none" and reports failure."""
    path = _write(tmp_path, "empty.pptx", b"")

    outcome = repair_file(path)

    assert outcome.diagnosis.verdict == Verdict.EMPTY_FILE
    assert outcome.mode == "none"
    assert outcome.success is False


def test_full_zero_fill_repair_is_a_noop_failure(tmp_path: Path) -> None:
    """A fully zero-filled file yields mode "none" and reports failure."""
    path = _write(tmp_path, "zerofilled.pptx", b"\x00" * (256 * 1024))

    outcome = repair_file(path)

    assert outcome.diagnosis.verdict == Verdict.FULL_ZERO_FILL
    assert outcome.mode == "none"
    assert outcome.success is False


# --- 14. interior damage rebuilds through the existing salvage path ---------


def test_interior_damage_rebuilds_successfully(tmp_path: Path) -> None:
    """A file with damage confined to one interior entry's data rebuilds
    into a clean package through the pre-existing CD-salvage path."""
    data = build_minimal_pptx(num_slides=3, media_bytes=50_000)
    damaged = zero_interior_entry(data, skip=17)  # ppt/viewProps.xml
    path = _write(tmp_path, "interior.pptx", damaged)
    assert _diagnose(path).verdict == Verdict.INTERIOR_DAMAGE

    outcome = repair_file(path)

    assert outcome.mode == "rebuild"
    assert outcome.success is True
    assert outcome.recheck_verdict == "normal"
    assert outcome.recheck_dangling_refs == 0
