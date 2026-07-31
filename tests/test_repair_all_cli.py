"""Tests for the ``pptrepair repair-all`` CLI subcommand.

Exercises :func:`pptrepair.cli.main` end to end (walker -> scan ->
:func:`pptrepair.batch.repair_paths` -> report) through small synthetic
trees written under ``tmp_path``, the same way :mod:`test_scan_cli`
covers ``scan``. The real ``broken_ppt/`` / ``normal_ppt/`` sample
directories are never touched.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, truncate

from pptrepair.cli import EXIT_CORRUPT, EXIT_ERROR, EXIT_OK, main
from pptrepair.repair import default_output_path

#: Shorthand for the capsys fixture type, to keep signatures short.
CaptureFixture = pytest.CaptureFixture[str]


def _write(dir_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``dir_path / name`` and return the resulting path."""
    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _mkroot(tmp_path: Path, name: str = "root") -> Path:
    """Create and return an empty scan-root directory under *tmp_path*."""
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def _header_offset(data: bytes, name: str) -> int:
    """Return the local-file-header offset of *name* inside *data*."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.getinfo(name).header_offset


def _rebuildable_truncated(num_slides: int = 3) -> bytes:
    """Build a TAIL_TRUNCATED fixture that rebuilds with every slide intact."""
    data = build_minimal_pptx(num_slides=num_slides, media_bytes=4096)
    cutoff = _header_offset(data, "ppt/media/image1.png")
    return truncate(data, cutoff)


# --- argument validation ------------------------------------------------


def test_output_dir_and_in_place_are_mutually_exclusive(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """``-o`` and ``--in-place`` together is a usage error (exit 2)."""
    root = _mkroot(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        main(["repair-all", str(root), "-o", str(tmp_path / "out"),
              "--in-place"])

    assert excinfo.value.code == 2
    assert "not allowed" in capsys.readouterr().err


def test_output_dir_or_in_place_is_required(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Neither ``-o`` nor ``--in-place`` given is a usage error (exit 2)."""
    root = _mkroot(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        main(["repair-all", str(root)])

    assert excinfo.value.code == 2
    assert capsys.readouterr().err  # argparse prints a usage message


# --- aggregate mode: mixed tree ------------------------------------------


def test_mixed_tree_aggregate_repair_exits_ok_and_writes_mirror(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A mixed tree repairs into OUTDIR, mirroring the input layout, and
    streams both the phase-1 and phase-2 per-file lines plus the summary."""
    root = _mkroot(tmp_path)
    _write(root, "healthy.pptx", build_minimal_pptx(num_slides=1, media_bytes=4096))
    broken = _write(root / "sub", "trunc.pptx", _rebuildable_truncated())
    out = tmp_path / "out"

    exit_code = main(["repair-all", str(root), "-o", str(out)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert f"{broken}: tail_truncated" in out_text          # phase 1
    assert f"{broken}: repaired (rebuild) -> " in out_text  # phase 2
    assert "=== Repair summary ===" in out_text
    assert "Repaired: 1 file(s)" in out_text
    artifact = out / "sub" / "trunc.repaired.pptx"
    assert artifact.is_file()


def test_all_normal_tree_exits_ok_with_zero_repaired(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """An all-intact tree yields exit 0 and ``Repaired: 0 file(s)``."""
    root = _mkroot(tmp_path)
    _write(root, "a.pptx", build_minimal_pptx(num_slides=1, media_bytes=4096))
    out = tmp_path / "out"

    exit_code = main(["repair-all", str(root), "-o", str(out)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Repaired: 0 file(s)" in out_text


def test_unrepairable_file_exits_corrupt(tmp_path: Path) -> None:
    """A tree with only an unrepairable (empty) file exits 1."""
    root = _mkroot(tmp_path)
    _write(root, "empty.pptx", b"")
    out = tmp_path / "out"

    exit_code = main(["repair-all", str(root), "-o", str(out)])

    assert exit_code == EXIT_CORRUPT


def test_nonexistent_root_exits_error(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A root that does not exist is a walk error and forces exit 2."""
    missing = tmp_path / "does_not_exist"
    out = tmp_path / "out"

    exit_code = main(["repair-all", str(missing), "-o", str(out)])

    err = capsys.readouterr().err
    assert exit_code == EXIT_ERROR
    assert "pptrepair: error:" in err


# --- --json ---------------------------------------------------------------


def test_json_output_schema_and_no_progress_lines_mixed_in(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """``--json`` prints one parseable object; no phase 1/2 progress lines
    leak into stdout."""
    root = _mkroot(tmp_path)
    _write(root, "healthy.pptx", build_minimal_pptx(num_slides=1, media_bytes=4096))
    _write(root, "trunc.pptx", _rebuildable_truncated())
    out = tmp_path / "out"

    exit_code = main(["repair-all", str(root), "-o", str(out), "--json"])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    payload = json.loads(out_text)  # fails outright if a stray line leaked in
    assert payload["schema_version"] == 3
    assert set(payload["counts"]) >= {
        "repaired", "repaired_rebuild", "repaired_trim", "repaired_extract",
        "unrepairable", "unrepairable_cfb", "skipped_existing", "failed",
        "planned",
    }
    assert payload["counts"]["repaired"] == 1
    assert len(payload["repairs"]) == 1


# --- --report ---------------------------------------------------------------


def test_report_dir_writes_four_files_and_force_semantics(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """--report writes the two scan and two repair report files; a clash
    without --force is a usage error with a --force hint."""
    root = _mkroot(tmp_path)
    _write(root, "trunc.pptx", _rebuildable_truncated())
    out = tmp_path / "out"
    report_dir = tmp_path / "report"

    exit_code = main(
        ["repair-all", str(root), "-o", str(out), "--report", str(report_dir)])
    capsys.readouterr()

    assert exit_code == EXIT_OK
    scan_text = (report_dir / "scan_report.txt").read_text(encoding="utf-8")
    assert scan_text.startswith("=== Scan summary ===")
    scan_json = json.loads(
        (report_dir / "scan_report.json").read_text(encoding="utf-8"))
    assert scan_json["summary"]["scanned"] == 1
    repair_text = (report_dir / "repair_report.txt").read_text(encoding="utf-8")
    assert repair_text.startswith("=== Scan summary ===")
    assert "=== Repair summary ===" in repair_text
    repair_json = json.loads(
        (report_dir / "repair_report.json").read_text(encoding="utf-8"))
    assert repair_json["schema_version"] == 3
    assert repair_json["counts"]["repaired"] == 1

    exit_code_conflict = main(
        ["repair-all", str(root), "-o", str(out), "--report", str(report_dir)])
    err = capsys.readouterr().err
    assert exit_code_conflict == EXIT_ERROR
    assert "already exists" in err
    assert "--force" in err


# --- --dry-run --------------------------------------------------------------


def test_dry_run_writes_nothing_and_reports_plan(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """--dry-run diagnoses and plans without writing OUTDIR or --report."""
    root = _mkroot(tmp_path)
    _write(root, "trunc.pptx", _rebuildable_truncated())
    out = tmp_path / "out"
    report_dir = tmp_path / "report"

    exit_code = main([
        "repair-all", str(root), "-o", str(out), "--report", str(report_dir),
        "--dry-run",
    ])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert not out.exists()
    assert not report_dir.exists()
    assert "Planned: 1 file(s)" in out_text
    assert "dry run: nothing was written." in out_text
    assert ": planned (" in out_text


# --- --in-place ---------------------------------------------------------------


def test_in_place_writes_next_to_source(tmp_path: Path) -> None:
    """--in-place writes the artifact next to its source (default output
    path), not into any aggregate directory."""
    root = _mkroot(tmp_path)
    src = _write(root / "nested", "trunc.pptx", _rebuildable_truncated())

    exit_code = main(["repair-all", str(root), "--in-place"])

    assert exit_code == EXIT_OK
    expected = default_output_path(src, "rebuild")
    assert expected == src.parent / "trunc.repaired.pptx"
    assert expected.is_file()


# --- OUTDIR is an existing file ------------------------------------------


def test_output_dir_as_existing_file_exits_error(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """An OUTDIR path that already exists as a regular file is a usage
    error (exit 2), checked before any scanning starts."""
    root = _mkroot(tmp_path)
    _write(root, "trunc.pptx", _rebuildable_truncated())
    out = tmp_path / "out"
    out.write_bytes(b"not a directory")

    exit_code = main(["repair-all", str(root), "-o", str(out)])

    err = capsys.readouterr().err
    assert exit_code == EXIT_ERROR
    assert "pptrepair: error:" in err


# --- skipped_existing ---------------------------------------------------


def test_skipped_existing_requires_force_to_overwrite(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A pre-existing artifact is skipped (exit 1) without --force and
    overwritten (exit 0) with it."""
    root = _mkroot(tmp_path)
    _write(root, "trunc.pptx", _rebuildable_truncated())
    out = tmp_path / "out"

    first = main(["repair-all", str(root), "-o", str(out)])
    capsys.readouterr()
    assert first == EXIT_OK
    artifact = out / "trunc.repaired.pptx"
    assert artifact.is_file()
    marker = artifact.read_bytes()

    skipped = main(["repair-all", str(root), "-o", str(out)])
    out_text = capsys.readouterr().out
    assert skipped == EXIT_CORRUPT
    assert "skipped (output exists)" in out_text
    assert artifact.read_bytes() == marker  # untouched

    forced = main(["repair-all", str(root), "-o", str(out), "--force"])
    capsys.readouterr()
    assert forced == EXIT_OK
    assert artifact.is_file()


# --- --max-file-size -----------------------------------------------------


def test_max_file_size_skips_oversize_file_no_artifact_written(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A corrupted file over --max-file-size is skipped before repair:
    no artifact is written for it, and it counts as neither corrupted
    nor repaired."""
    root = _mkroot(tmp_path)
    broken = _write(root, "trunc.pptx", _rebuildable_truncated())
    out = tmp_path / "out"
    small_limit = broken.stat().st_size // 2

    exit_code = main(
        ["repair-all", str(root), "-o", str(out),
         "--max-file-size", str(small_limit)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Skipped: 1 file(s) over the size limit" in out_text
    assert "Repaired: 0 file(s)" in out_text
    assert not out.exists()


# --- --include-hidden -----------------------------------------------------


def test_include_hidden_flag_is_accepted_and_repairs_a_hidden_file(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """--include-hidden is a recognised flag; with it, a hidden corrupted
    file is repaired instead of being silently skipped by default."""
    root = _mkroot(tmp_path)
    _write(root, ".trunc.pptx", _rebuildable_truncated())
    out = tmp_path / "out"

    exit_code = main(
        ["repair-all", str(root), "-o", str(out), "--include-hidden"])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Repaired: 1 file(s)" in out_text
