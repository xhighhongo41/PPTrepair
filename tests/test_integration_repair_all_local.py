"""Integration tests for ``pptrepair repair-all``.

Two independent halves, mirroring the split already used elsewhere in
this test suite:

* Part A builds a small synthetic tree (fixtures.py + the corruption
  injection helpers also used by ``test_batch.py`` /
  ``test_repair_all_cli.py``) covering every repair path (rebuild /
  trim / extract / unrepairable) plus two intact files, and drives it
  through :func:`pptrepair.cli.main`. It needs no external data and
  always runs.
* Part B, following the same conventions as
  ``test_integration_repair_local.py`` / ``test_integration_scan_local.py``,
  exercises the real ``broken_ppt/`` / ``normal_ppt/`` corpus and skips
  itself (per test, not at module level, since Part A must still run)
  when the corpus or its expectation files are absent. All repair
  artifacts and reports go under pytest's ``tmp_path``; ``broken_ppt/``
  and ``normal_ppt/`` are only ever read.
"""

from __future__ import annotations

import hashlib
import io
import json
import unicodedata
import zipfile
from collections import Counter
from pathlib import Path

import pytest
from fixtures import (append_foreign_tail, build_minimal_pptx, truncate,
                      zero_prefix)

from pptrepair.cli import EXIT_CORRUPT, EXIT_OK, main

CaptureFixture = pytest.CaptureFixture[str]

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Expectation files; kept in a git-ignored internal directory because
#: they reference real (non-public) file names. See
#: ``test_integration_scan_local.py`` / ``test_integration_repair_local.py``.
_EXPECTED_CLASSIFICATION_FILE = (
    _REPO_ROOT / "開発資料" / "調査データ" / "expected_classification.json")
_EXPECTED_REPAIR_FILE = (
    _REPO_ROOT / "開発資料" / "調査データ" / "expected_repair.json")


def _load_json_expected(path: Path) -> dict:
    """Load an ``{"expected": {...}}`` expectation file, or {} if absent."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["expected"]


_EXPECTED_CLASSIFICATION = _load_json_expected(_EXPECTED_CLASSIFICATION_FILE)
_EXPECTED_REPAIR = _load_json_expected(_EXPECTED_REPAIR_FILE)


# =========================================================================
# Part A: synthetic tree, always runs (no external data required)
# =========================================================================


def _write(dir_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``dir_path / name`` and return the resulting path."""
    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _header_offset(data: bytes, name: str) -> int:
    """Return the local-file-header offset of *name* inside *data*."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.getinfo(name).header_offset


def _rebuildable_truncated(num_slides: int = 3) -> bytes:
    """Build a TAIL_TRUNCATED fixture that rebuilds with every slide intact."""
    data = build_minimal_pptx(num_slides=num_slides, media_bytes=4096)
    cutoff = _header_offset(data, "ppt/media/image1.png")
    return truncate(data, cutoff)


def _trimmable_foreign_tail() -> bytes:
    """Build a TAIL_FOREIGN_DATA fixture that trims to a clean archive."""
    intact = build_minimal_pptx(num_slides=2, media_bytes=50_000)
    return append_foreign_tail(intact, 131072)


def _extractable_head_zero(num_slides: int = 200,
                           media_bytes: int = 50_000) -> bytes:
    """Build a large-scale HEAD_ZERO_FILL fixture whose media part survives.

    A large slide count keeps enough of the central directory past the
    zeroed head that the salvage pass has real content to recover,
    exercising the extract path end to end (matching the corruption
    scale seen in the real corpus's own HEAD_ZERO_FILL samples).
    """
    data = build_minimal_pptx(num_slides=num_slides, media_bytes=media_bytes)
    cutoff = _header_offset(data, "ppt/media/image1.png")
    return zero_prefix(data, cutoff)


def _sha256_snapshot(root: Path) -> dict[str, str]:
    """Return ``{relative_path: sha256_hex}`` for every file under *root*."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _build_mixed_tree(root: Path) -> None:
    """Populate *root* with two intact files and one of each repair path.

    Layout (several subdirectories, per the test plan):

    * ``intact_a/healthy1.pptx``, ``intact_b/nested/healthy2.pptx`` — NORMAL.
    * ``rebuild/trunc.pptx`` — TAIL_TRUNCATED, repairs via rebuild.
    * ``trim/tail.pptx`` — TAIL_FOREIGN_DATA, repairs via trim.
    * ``extract/deck/broken.pptx`` — large-scale HEAD_ZERO_FILL, repairs
      via extract.
    * ``unrepairable/empty.pptx`` — EMPTY_FILE, nothing survives.
    """
    _write(root / "intact_a", "healthy1.pptx",
          build_minimal_pptx(num_slides=1, media_bytes=4096))
    _write(root / "intact_b" / "nested", "healthy2.pptx",
          build_minimal_pptx(num_slides=2, media_bytes=4096, seed=2))
    _write(root / "rebuild", "trunc.pptx", _rebuildable_truncated())
    _write(root / "trim", "tail.pptx", _trimmable_foreign_tail())
    _write(root / "extract" / "deck", "broken.pptx", _extractable_head_zero())
    _write(root / "unrepairable", "empty.pptx", b"")


def test_synthetic_tree_repair_all_full_cycle(
    tmp_path: Path, capsys: CaptureFixture,
) -> None:
    """One ``repair-all`` run over a mixed tree, then a skip/force rerun.

    Exercises every check in the test plan's Part A in one place, since
    checks 1-5 all come from the same invocation and check 6 is
    necessarily a rerun of it:

    1. exit code 1 (the empty file stays unrepaired) and per-action counts.
    2. artifacts land at the mirrored location with the right suffix.
    3. rebuild/trim artifacts recheck ``normal`` with all three integrity
       counts at 0.
    4. the input tree is byte-for-byte unchanged (sha256 snapshot).
    5. the extract artifact contains a ``REPORT.txt``.
    6. a same-command rerun reports every artifact ``skipped_existing``,
       and ``--force`` overwrites them.
    """
    root = tmp_path / "input"
    root.mkdir()
    _build_mixed_tree(root)
    out = tmp_path / "out"

    before = _sha256_snapshot(root)
    exit_code = main(["repair-all", str(root), "-o", str(out), "--json"])
    payload = json.loads(capsys.readouterr().out)
    after = _sha256_snapshot(root)

    # --- 1. exit code + counts -------------------------------------------
    assert exit_code == EXIT_CORRUPT
    assert payload["counts"] == {
        "repaired": 3,
        "repaired_rebuild": 1,
        "repaired_trim": 1,
        "repaired_extract": 1,
        "unrepairable": 1,
        "unrepairable_cfb": 0,
        "skipped_existing": 0,
        "failed": 0,
        "planned": 0,
    }

    # --- 2. mirrored artifact locations + suffixes ------------------------
    rebuild_artifact = out / "rebuild" / "trunc.repaired.pptx"
    trim_artifact = out / "trim" / "tail.repaired.pptx"
    extract_artifact = out / "extract" / "deck" / "broken.salvaged"
    assert rebuild_artifact.is_file()
    assert trim_artifact.is_file()
    assert extract_artifact.is_dir()
    # No artifact (and no empty mirrored directory) for intact or
    # unrepairable inputs.
    assert not (out / "intact_a").exists()
    assert not (out / "intact_b").exists()
    assert not (out / "unrepairable").exists()

    # --- 3. recheck: rebuild/trim artifacts are clean ----------------------
    repairs_by_name = {
        Path(entry["path"]).name: entry for entry in payload["repairs"]
    }
    assert len(repairs_by_name) == 4  # the 3 repaired + the unrepairable
    for name in ("trunc.pptx", "tail.pptx"):
        entry = repairs_by_name[name]
        assert entry["action"] == "repaired"
        assert entry["recheck_verdict"] == "normal"
        assert entry["recheck_dangling_refs"] == 0
        assert entry["recheck_timing_issues"] == 0
        assert entry["recheck_structure_issues"] == 0

    # --- 4. input tree is byte-for-byte unchanged --------------------------
    assert before == after

    # --- 5. REPORT.txt inside the extract artifact --------------------------
    report_txt = extract_artifact / "REPORT.txt"
    assert report_txt.is_file()
    assert report_txt.read_text(encoding="utf-8").strip() != ""

    # --- 6. rerun: everything skipped, then --force overwrites -------------
    rerun_exit = main(["repair-all", str(root), "-o", str(out), "--json"])
    rerun_payload = json.loads(capsys.readouterr().out)
    assert rerun_exit == EXIT_CORRUPT
    assert rerun_payload["counts"]["skipped_existing"] == 3
    assert rerun_payload["counts"]["repaired"] == 0
    assert rerun_payload["counts"]["unrepairable"] == 1

    forced_exit = main(
        ["repair-all", str(root), "-o", str(out), "--force", "--json"])
    forced_payload = json.loads(capsys.readouterr().out)
    assert forced_exit == EXIT_CORRUPT  # the empty file is still unrepairable
    assert forced_payload["counts"]["repaired"] == 3
    assert forced_payload["counts"]["skipped_existing"] == 0
    # The overwritten artifacts are still valid outputs.
    assert rebuild_artifact.is_file()
    assert trim_artifact.is_file()
    assert extract_artifact.is_dir()


# =========================================================================
# Part B: real corpus (broken_ppt/ + normal_ppt/), skipped when absent
# =========================================================================


def _skip_unless_corpus_available() -> tuple[dict, dict]:
    """Skip the calling test unless the real corpus and expectations exist.

    Mirrors the module-level skip in ``test_integration_scan_local.py`` /
    ``test_integration_repair_local.py``, but as a per-test guard: Part A
    above must keep running even when the (non-public) local corpus is
    absent, so this file cannot use ``allow_module_level=True``.

    :return: ``(present_classification, present_repair)``, each an
        ``{relative_path: expectation}`` mapping restricted to corpus
        files that are actually present on disk (a partial local corpus
        does not fail the test, matching the existing convention).
    """
    if not _EXPECTED_CLASSIFICATION or not _EXPECTED_REPAIR:
        pytest.skip(
            "local corpus expectation files not present; skipping "
            "repair-all integration test against real files")
    for root in (_REPO_ROOT / "broken_ppt", _REPO_ROOT / "normal_ppt"):
        if not root.is_dir():
            pytest.skip(f"corpus directory not present: {root}")

    present_classification = {
        relative: verdict for relative, verdict in _EXPECTED_CLASSIFICATION.items()
        if (_REPO_ROOT / relative).is_file()
    }
    present_repair = {
        relative: expected for relative, expected in _EXPECTED_REPAIR.items()
        if (_REPO_ROOT / relative).is_file()
    }
    if not present_classification or not present_repair:
        pytest.skip("no corpus files from the expectation files are present")
    return present_classification, present_repair


def _size_mtime_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Return ``{relative_path: (size, mtime_ns)}`` for every file under *root*.

    Cheaper than hashing (Part A's ``_sha256_snapshot``) for the
    real-world corpus, whose files are far larger; still catches any
    accidental write (size or timestamp both change on write).
    """
    return {
        str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _normalize(path_str: str) -> str:
    """NFC-normalize *path_str* so NFD-decomposed macOS paths still match."""
    return unicodedata.normalize("NFC", path_str)


def test_real_corpus_repair_all_matches_ground_truth_and_leaves_corpus_untouched(
    tmp_path: Path, capsys: CaptureFixture,
) -> None:
    """``repair-all broken_ppt normal_ppt`` against the real corpus.

    1. the phase-1 verdict breakdown matches the recorded ground truth
       (normal / head_zero_fill / head_foreign_data / version_mix /
       tail_truncated).
    2. every corrupted file repairs (``action == "repaired"``) with the
       recorded mode, exit code 0; rebuild artifacts recheck ``normal``
       with all three integrity counts at 0.
    3. the aggregate output mirrors the multi-root layout
       (``out/broken_ppt/...``).
    4. neither ``broken_ppt/`` nor ``normal_ppt/`` is written to.
    5. ``--report`` writes the four scan/repair report files.
    """
    present_classification, present_repair = _skip_unless_corpus_available()
    broken_root = _REPO_ROOT / "broken_ppt"
    normal_root = _REPO_ROOT / "normal_ppt"

    before_broken = _size_mtime_snapshot(broken_root)
    before_normal = _size_mtime_snapshot(normal_root)

    out = tmp_path / "out"
    report_dir = tmp_path / "report"
    exit_code = main([
        "repair-all", str(broken_root), str(normal_root),
        "-o", str(out), "--report", str(report_dir), "--json",
    ])
    payload = json.loads(capsys.readouterr().out)

    # --- 4. neither corpus directory was touched ----------------------------
    assert _size_mtime_snapshot(broken_root) == before_broken
    assert _size_mtime_snapshot(normal_root) == before_normal

    # --- 1. scan verdict breakdown matches ground truth ---------------------
    scan_summary = payload["scan"]["summary"]
    assert scan_summary["scanned"] == len(present_classification)
    assert scan_summary["errors"] == 0
    expected_counts = dict(Counter(present_classification.values()))
    assert scan_summary["verdicts"] == expected_counts

    # --- 2. every corrupted file repaired, matching the recorded mode -------
    assert exit_code == EXIT_OK
    corrupted_present = {
        relative: verdict for relative, verdict in present_classification.items()
        if verdict != "normal"
    }
    assert set(corrupted_present) == set(present_repair)

    repairs_by_path = {
        _normalize(entry["path"]): entry for entry in payload["repairs"]
    }
    assert len(payload["repairs"]) == len(present_repair)
    for relative, expected in present_repair.items():
        key = _normalize(str(_REPO_ROOT / relative))
        entry = repairs_by_path.get(key)
        assert entry is not None, relative
        assert entry["action"] == "repaired", relative
        assert entry["mode"] == expected["mode"], relative
        if expected["mode"] == "rebuild":
            assert entry["recheck_verdict"] == "normal", relative
            assert entry["recheck_dangling_refs"] == 0, relative
            assert entry["recheck_timing_issues"] == 0, relative
            assert entry["recheck_structure_issues"] == 0, relative

    # --- 3. aggregate output mirrors the multi-root layout -------------------
    broken_out = out / "broken_ppt"
    assert broken_out.is_dir()
    assert len(list(broken_out.iterdir())) == len(present_repair)

    # --- 5. --report writes the four scan/repair report files ---------------
    assert (report_dir / "scan_report.txt").is_file()
    assert (report_dir / "scan_report.json").is_file()
    assert (report_dir / "repair_report.txt").is_file()
    assert (report_dir / "repair_report.json").is_file()


def test_real_corpus_dry_run_reports_all_repairable_without_writing(
    tmp_path: Path, capsys: CaptureFixture,
) -> None:
    """``--dry-run`` over the real corpus plans every repair and writes nothing.

    The corpus's 9 corrupted files are all salvageable (see
    ``expected_repair.json``), so a dry run should predict a repair for
    every one of them (``unrepaired_remaining() == 0``) and exit 0.
    """
    _skip_unless_corpus_available()
    broken_root = _REPO_ROOT / "broken_ppt"
    normal_root = _REPO_ROOT / "normal_ppt"

    before_broken = _size_mtime_snapshot(broken_root)
    before_normal = _size_mtime_snapshot(normal_root)

    out2 = tmp_path / "out2"
    exit_code = main([
        "repair-all", str(broken_root), str(normal_root),
        "-o", str(out2), "--dry-run",
    ])
    capsys.readouterr()  # drain the per-file plan lines

    assert exit_code == EXIT_OK
    assert not out2.exists()
    assert _size_mtime_snapshot(broken_root) == before_broken
    assert _size_mtime_snapshot(normal_root) == before_normal
