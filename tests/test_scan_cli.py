"""Tests for the ``pptrepair scan`` CLI subcommand.

Exercises :func:`pptrepair.cli.main` end to end (walker -> diagnose ->
report) through small synthetic trees written under ``tmp_path``, the
same way :mod:`test_cli` covers ``check``. The real ``broken_ppt/`` /
``normal_ppt/`` sample directories are never touched; the local corpus
integration test lives in ``test_integration_scan_local.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, zero_prefix

from pptrepair import scan as scan_module
from pptrepair import walker as walker_module
from pptrepair.cancel import OperationCancelled
from pptrepair.cli import EXIT_CORRUPT, EXIT_ERROR, EXIT_OK, main
from pptrepair.diagnostics import file_id
from pptrepair.report import ISSUE_URL

#: Small media payload so fixtures stay fast to build and scan.
_MEDIA_BYTES = 600_000

#: Shorthand for the capsys fixture type, to keep signatures short.
CaptureFixture = pytest.CaptureFixture[str]

#: A plain-text ``.pptx`` is not a ZIP at all and its head is not CFB,
#: so it always classifies as NOT_A_ZIP and qualifies as an unknown
#: fingerprint target -- the simplest way to get a target without
#: reproducing OTHER_CORRUPT's byte-level surgery.
_NOT_A_ZIP_BODY = b"This is a plain text file, not a ZIP archive at all. " * 20


def _write(dir_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``dir_path / name`` and return the resulting path."""
    path = dir_path / name
    path.write_bytes(data)
    return path


def _mkroot(tmp_path: Path, name: str = "root") -> Path:
    """Create and return an empty scan-root directory under *tmp_path*."""
    root = tmp_path / name
    root.mkdir()
    return root


# --- basic outcomes -----------------------------------------------------


def test_all_normal_tree_exits_ok(tmp_path: Path, capsys: CaptureFixture) -> None:
    """An all-intact tree yields exit 0 and a Scanned count in the summary."""
    root = _mkroot(tmp_path)
    (root / "sub").mkdir()
    _write(root, "a.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=1))
    _write(root / "sub", "b.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=2))

    exit_code = main(["scan", str(root)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Scanned: 2 file(s)" in out


def test_corrupted_tree_streams_progress_and_all_flag(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A corrupted file always streams; a normal one only with --all."""
    root = _mkroot(tmp_path)
    normal_path = _write(
        root, "normal.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    broken_data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    broken_path = _write(root, "broken.pptx", broken_data)

    exit_code = main(["scan", str(root)])
    out = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    assert f"{broken_path}: head_zero_fill" in out
    assert f"{normal_path}: normal" not in out

    exit_code_all = main(["scan", str(root), "--all"])
    out_all = capsys.readouterr().out
    assert exit_code_all == EXIT_CORRUPT
    assert f"{broken_path}: head_zero_fill" in out_all
    assert f"{normal_path}: normal" in out_all


def test_nonexistent_root_exits_error(tmp_path: Path, capsys: CaptureFixture) -> None:
    """A root that does not exist is a walk error and forces exit 2."""
    missing = tmp_path / "does_not_exist"

    exit_code = main(["scan", str(missing), "--json"])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ERROR
    payload = json.loads(captured.out)
    assert payload["summary"]["errors"] >= 1
    assert payload["errors"]
    # The error must also be visible, not only encoded in the payload.
    assert "pptrepair: error:" in captured.err
    assert "No such file or directory" in captured.err


def test_nonexistent_root_prints_error_in_text_mode(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Text mode: a missing root is announced on stderr instead of
    silently producing a clean-looking 0-file summary; other roots are
    still scanned."""
    missing = tmp_path / "does_not_exist"
    root = _mkroot(tmp_path)
    _write(root, "normal.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES))

    exit_code = main(["scan", str(missing), str(root)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ERROR
    assert "pptrepair: error:" in captured.err
    assert str(missing) in captured.err
    assert "Scanned: 1 file(s)" in captured.out


# --- --json schema --------------------------------------------------------


def test_json_output_schema_and_no_progress_lines_mixed_in(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """``--json`` prints one parseable object with the documented keys."""
    root = _mkroot(tmp_path)
    _write(root, "normal.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    broken_data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    _write(root, "broken.pptx", broken_data)

    exit_code = main(["scan", str(root), "--json"])

    out = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    # json.loads fails outright if a stray progress line leaked into stdout.
    payload = json.loads(out)

    summary = payload["summary"]
    assert set(summary) >= {
        "scanned", "verdicts", "cfb_files", "skipped", "errors",
        "unknown_pattern_files", "fingerprints_written",
        "fingerprints_skipped",
    }
    assert set(summary["skipped"]) == {
        "legacy", "office_temp", "cloud_placeholder", "oversize"}
    assert summary["scanned"] == 2
    assert summary["verdicts"] == {"normal": 1, "head_zero_fill": 1}


# --- empty-file classification -----------------------------------------------


def test_empty_file_counted_and_not_fingerprinted(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """An empty .pptx is tallied as ``empty_file`` and never fingerprinted,
    even when ``--report`` is given: EMPTY_FILE is a known pattern, so no
    ``diagnostics/`` directory is created at all."""
    root = _mkroot(tmp_path)
    _write(root, "empty.pptx", b"")
    report_dir = tmp_path / "report"

    exit_code = main(
        ["scan", str(root), "--report", str(report_dir), "--json"])

    out = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    payload = json.loads(out)
    assert payload["summary"]["verdicts"] == {"empty_file": 1}
    assert payload["summary"]["unknown_pattern_files"] == 0
    assert payload["summary"]["fingerprints_written"] == 0
    assert not (report_dir / "diagnostics").is_dir()


# --- --report ---------------------------------------------------------------


def test_report_dir_written_and_force_semantics(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """--report writes both reports plus a fingerprint; a clash needs --force."""
    root = _mkroot(tmp_path)
    unknown_path = _write(root, "unknown.pptx", _NOT_A_ZIP_BODY)
    report_dir = tmp_path / "report"

    exit_code = main(["scan", str(root), "--report", str(report_dir)])
    capsys.readouterr()

    assert exit_code == EXIT_CORRUPT
    assert (report_dir / "scan_report.txt").is_file()
    assert (report_dir / "scan_report.json").is_file()
    fingerprint_path = (
        report_dir / "diagnostics" / f"{file_id(unknown_path)}.diag.json")
    assert fingerprint_path.is_file()
    fingerprint = json.loads(fingerprint_path.read_text(encoding="utf-8"))
    assert fingerprint["verdict"] == "not_a_zip"

    exit_code_conflict = main(["scan", str(root), "--report", str(report_dir)])
    err = capsys.readouterr().err
    assert exit_code_conflict == EXIT_ERROR
    assert "already exists" in err
    assert "--force" in err

    exit_code_forced = main(
        ["scan", str(root), "--report", str(report_dir), "--force"])
    assert exit_code_forced == EXIT_CORRUPT


def test_unknown_pattern_hint_text_with_and_without_report(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """The unknown-pattern hint differs depending on --report."""
    root = _mkroot(tmp_path)
    _write(root, "unknown.pptx", _NOT_A_ZIP_BODY)

    main(["scan", str(root)])
    out_without_report = capsys.readouterr().out
    assert "Hint: re-run with --report DIR" in out_without_report

    report_dir = tmp_path / "report"
    main(["scan", str(root), "--report", str(report_dir)])
    out_with_report = capsys.readouterr().out
    assert str(report_dir / "diagnostics") in out_with_report
    assert ISSUE_URL in out_with_report


def test_max_fingerprints_cap_is_enforced_and_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Overflow past MAX_FINGERPRINTS is capped and surfaced in the summary."""
    monkeypatch.setattr(scan_module, "MAX_FINGERPRINTS", 1)
    root = _mkroot(tmp_path)
    _write(root, "unknown1.pptx", _NOT_A_ZIP_BODY)
    _write(root, "unknown2.pptx", _NOT_A_ZIP_BODY + b"more")
    report_dir = tmp_path / "report"

    exit_code = main(["scan", str(root), "--report", str(report_dir)])

    assert exit_code == EXIT_CORRUPT
    diag_files = list((report_dir / "diagnostics").glob("*.diag.json"))
    assert len(diag_files) == 1

    report_json = json.loads(
        (report_dir / "scan_report.json").read_text(encoding="utf-8"))
    assert report_json["summary"]["unknown_pattern_files"] == 2
    assert report_json["summary"]["fingerprints_written"] == 1
    assert report_json["summary"]["fingerprints_skipped"] == 1

    report_text = (report_dir / "scan_report.txt").read_text(encoding="utf-8")
    assert ("1 additional unknown-pattern file(s) were not fingerprinted"
            in report_text)


def test_include_filenames_flag_controls_fingerprint_name(tmp_path: Path) -> None:
    """diag.json's file.name is null by default, basename with --include-filenames."""
    root = _mkroot(tmp_path)
    unknown_path = _write(root, "SecretDeck.pptx", _NOT_A_ZIP_BODY)

    default_report = tmp_path / "report_default"
    main(["scan", str(root), "--report", str(default_report)])
    default_fp = json.loads(
        (default_report / "diagnostics" / f"{file_id(unknown_path)}.diag.json")
        .read_text(encoding="utf-8"))
    assert default_fp["file"]["name"] is None

    named_report = tmp_path / "report_named"
    main(["scan", str(root), "--report", str(named_report),
          "--include-filenames"])
    named_fp = json.loads(
        (named_report / "diagnostics" / f"{file_id(unknown_path)}.diag.json")
        .read_text(encoding="utf-8"))
    assert named_fp["file"]["name"] == "SecretDeck.pptx"


# --- cloud placeholder skipping ----------------------------------------------


def test_cloud_skip_note_always_shown_and_reported_in_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture
) -> None:
    """The cloud-skip note appears even on an all-normal scan, plus JSON."""
    root = _mkroot(tmp_path)
    _write(root, "normal.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    cloud_path = _write(root, "cloud.pptx", b"placeholder")
    cloud_ino = cloud_path.stat().st_ino
    monkeypatch.setattr(
        walker_module, "is_cloud_placeholder",
        lambda st: st.st_ino == cloud_ino)

    exit_code = main(["scan", str(root)])
    out = capsys.readouterr().out
    assert exit_code == EXIT_OK  # cloud-skips alone never change the exit code
    assert ("Not examined: 1 cloud-only file(s) were skipped without "
            "downloading." in out)
    assert "--allow-download" in out

    exit_code_json = main(["scan", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code_json == EXIT_OK
    assert str(cloud_path) in payload["skipped_cloud"]


def test_allow_download_announces_each_download_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: CaptureFixture
) -> None:
    """--allow-download prints a per-file download notice to stderr,
    in text and JSON mode alike; without the flag nothing is announced."""
    root = _mkroot(tmp_path)
    cloud_path = _write(root, "cloud.pptx",
                        build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    cloud_ino = cloud_path.stat().st_ino
    monkeypatch.setattr(
        walker_module, "is_cloud_placeholder",
        lambda st: st.st_ino == cloud_ino)

    exit_code = main(["scan", str(root), "--allow-download"])
    captured = capsys.readouterr()
    assert exit_code == EXIT_OK
    assert f"Downloading cloud-only file: {cloud_path}" in captured.err
    assert "Downloading" not in captured.out  # stderr only
    assert "Scanned: 1 file(s)" in captured.out

    exit_code_json = main(["scan", str(root), "--allow-download", "--json"])
    captured_json = capsys.readouterr()
    assert exit_code_json == EXIT_OK
    payload = json.loads(captured_json.out)  # stdout stays pure JSON
    assert payload["summary"]["scanned"] == 1
    assert f"Downloading cloud-only file: {cloud_path}" in captured_json.err

    exit_code_skip = main(["scan", str(root)])
    captured_skip = capsys.readouterr()
    assert exit_code_skip == EXIT_OK
    assert "Downloading" not in captured_skip.err


def test_no_cloud_skip_message_absent_when_nothing_skipped(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """With no cloud placeholders, the note is entirely omitted."""
    root = _mkroot(tmp_path)
    _write(root, "normal.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES))

    exit_code = main(["scan", str(root)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Not examined" not in out


# --- exit code priority -------------------------------------------------------


def test_errors_take_priority_over_corrupted_in_exit_code(tmp_path: Path) -> None:
    """A walk error forces exit 2 even when a corrupted file is also present."""
    root = _mkroot(tmp_path)
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "inner.pptx").write_bytes(b"data")
    broken_data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    _write(root, "broken.pptx", broken_data)

    blocked.chmod(0o000)
    try:
        exit_code = main(["scan", str(root), "--json"])
    finally:
        # Restore permissions so tmp_path cleanup can remove the tree.
        blocked.chmod(0o755)

    assert exit_code == EXIT_ERROR


# --- scan_paths() exclude ------------------------------------------------
#
# These call scan_module.scan_paths() directly: exclude is not yet wired
# into the scan CLI (it exists for repair-all's aggregate-output
# self-exclusion), so there is no ``pptrepair scan`` flag to drive it
# through main() with.


def test_scan_paths_exclude_removes_subtree_from_every_bucket(
    tmp_path: Path,
) -> None:
    """exclude=[out] drops every out/ entry -- targets, temp and legacy
    skips alike -- while the rest of the tree still scans normally."""
    root = _mkroot(tmp_path)
    _write(root, "kept.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    out_dir = root / "out"
    out_dir.mkdir()
    _write(out_dir, "excluded.pptx",
          build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=2))
    _write(out_dir, "~$excluded.pptx", b"office lock file")
    _write(out_dir, "legacy.ppt", b"legacy binary format")

    result = scan_module.scan_paths([root], exclude=[out_dir])

    scanned_names = {outcome.path.name for outcome in result.outcomes}
    assert "kept.pptx" in scanned_names
    assert "excluded.pptx" not in scanned_names
    assert result.walk.skipped_temp == []
    assert result.walk.skipped_legacy == []


def test_scan_paths_no_exclude_scans_the_full_tree(tmp_path: Path) -> None:
    """Omitting exclude (the default ``()``) leaves behaviour unchanged:
    every file under the root, including one under a subdirectory named
    like a batch output folder, is still diagnosed."""
    root = _mkroot(tmp_path)
    _write(root, "kept.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    out_dir = root / "out"
    out_dir.mkdir()
    _write(out_dir, "not_excluded.pptx",
          build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=2))

    result = scan_module.scan_paths([root])

    scanned_names = {outcome.path.name for outcome in result.outcomes}
    assert scanned_names == {"kept.pptx", "not_excluded.pptx"}


def test_scan_paths_exclude_resolves_relative_and_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative exclude path still matches an absolute scan root:
    both sides are normalised through ``Path.resolve()`` before
    comparison."""
    root = _mkroot(tmp_path)
    out_dir = root / "out"
    out_dir.mkdir()
    _write(out_dir, "excluded.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    _write(root, "kept.pptx",
          build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=2))

    monkeypatch.chdir(tmp_path)
    result = scan_module.scan_paths(
        [root.resolve()], exclude=[Path("root/out")])

    scanned_names = {outcome.path.name for outcome in result.outcomes}
    assert "kept.pptx" in scanned_names
    assert "excluded.pptx" not in scanned_names


# --- scan_paths() coordinated cancellation --------------------------------


def test_scan_paths_progress_cancellation_propagates_after_one_call(
    tmp_path: Path,
) -> None:
    """A progress callback that raises OperationCancelled aborts the scan
    immediately: the exception propagates uncaught, and no further file
    is diagnosed after the one that triggered it."""
    root = _mkroot(tmp_path)
    for index in range(3):
        _write(root, f"a{index}.pptx",
              build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=index))

    calls: list[scan_module.FileOutcome] = []

    def _cancel_on_first(outcome: scan_module.FileOutcome) -> None:
        calls.append(outcome)
        raise OperationCancelled("user requested cancellation")

    with pytest.raises(OperationCancelled):
        scan_module.scan_paths([root], progress=_cancel_on_first)

    assert len(calls) == 1


def test_scan_paths_progress_cancellation_leaves_no_report_files(
    tmp_path: Path,
) -> None:
    """Cancelling via progress before scan_paths returns leaves the
    report directory created (it is made up front) but never produces
    scan_report.txt/.json, since those are rendered by the caller only
    after scan_paths returns successfully."""
    root = _mkroot(tmp_path)
    for index in range(3):
        _write(root, f"a{index}.pptx",
              build_minimal_pptx(media_bytes=_MEDIA_BYTES, seed=index))
    report_dir = tmp_path / "report"

    def _cancel_on_first(outcome: scan_module.FileOutcome) -> None:
        raise OperationCancelled("user requested cancellation")

    with pytest.raises(OperationCancelled):
        scan_module.scan_paths([root], report_dir=report_dir,
                               progress=_cancel_on_first)

    assert report_dir.exists()
    assert not (report_dir / "scan_report.txt").exists()
    assert not (report_dir / "scan_report.json").exists()


# --- scan_paths() on_directory ---------------------------------------------


def test_scan_paths_on_directory_passes_through_to_walker(
    tmp_path: Path,
) -> None:
    """scan_paths forwards on_directory to discover_targets unchanged: the
    walked root and its subdirectory both reach the caller's callback."""
    root = _mkroot(tmp_path)
    sub_dir = root / "sub"
    sub_dir.mkdir()
    _write(sub_dir, "deck.pptx",
          build_minimal_pptx(media_bytes=_MEDIA_BYTES))

    visited: list[Path] = []
    result = scan_module.scan_paths([root], on_directory=visited.append)

    assert visited == [root, sub_dir]
    assert len(result.outcomes) == 1


def test_scan_paths_on_directory_cancellation_propagates(
    tmp_path: Path,
) -> None:
    """An on_directory callback that raises OperationCancelled aborts the
    scan before any file is diagnosed."""
    root = _mkroot(tmp_path)
    _write(root, "deck.pptx",
          build_minimal_pptx(media_bytes=_MEDIA_BYTES))

    def _cancel_on_first(_path: Path) -> None:
        raise OperationCancelled("user requested cancellation")

    with pytest.raises(OperationCancelled):
        scan_module.scan_paths([root], on_directory=_cancel_on_first)


# --- --max-file-size -----------------------------------------------------


def test_max_file_size_skips_oversize_file_text_and_json(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """--max-file-size keeps a small file and skips an oversize one --
    the oversize file is neither diagnosed nor listed as corrupted."""
    root = _mkroot(tmp_path)
    small_path = _write(
        root, "small.pptx", build_minimal_pptx(media_bytes=1000))
    big_path = _write(root, "big.pptx", b"x" * 20000)

    exit_code = main(["scan", str(root), "--max-file-size", "10000"])
    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Skipped: 1 file(s) over the size limit" in out
    assert "Scanned: 1 file(s)" in out

    exit_code_json = main(
        ["scan", str(root), "--max-file-size", "10000", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code_json == EXIT_OK
    assert payload["summary"]["skipped"]["oversize"] == 1
    assert payload["skipped_oversize"] == [str(big_path)]
    assert payload["summary"]["scanned"] == 1
    assert all(entry["path"] != str(big_path) for entry in payload["files"])
    assert str(small_path) in {entry["path"] for entry in payload["files"]}


def test_max_file_size_default_no_limit_scans_every_file(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Without --max-file-size, no file is ever skipped_oversize."""
    root = _mkroot(tmp_path)
    _write(root, "small.pptx", build_minimal_pptx(media_bytes=1000))
    _write(root, "big.pptx", b"x" * 20000)

    exit_code = main(["scan", str(root), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == EXIT_CORRUPT  # big.pptx is NOT_A_ZIP, not skipped
    assert payload["summary"]["skipped"]["oversize"] == 0
    assert payload["skipped_oversize"] == []
    assert payload["summary"]["scanned"] == 2


@pytest.mark.parametrize("bad_value", ["abc", "-1", "0"])
def test_max_file_size_rejects_invalid_argument(
    tmp_path: Path, bad_value: str
) -> None:
    """An invalid --max-file-size value is a usage error (exit 2)."""
    root = _mkroot(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["scan", str(root), "--max-file-size", bad_value])
    assert exc_info.value.code == 2
