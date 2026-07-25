"""Tests for :mod:`pptrepair.batch` (the ``repair-all`` driver core).

Covers the pure output-path planning (:func:`plan_output_base` /
:func:`plan_output_bases`) and the two-phase :func:`repair_paths`
driver, using small synthetic archives written under ``tmp_path``.
Nothing is ever written inside the repository, and the real
``broken_ppt/`` / ``normal_ppt/`` sample directories are never touched.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from fixtures import append_foreign_tail, build_minimal_pptx, truncate, zero_prefix

from pptrepair import batch as batch_module
from pptrepair.batch import (
    BatchItem,
    assign_root_labels,
    plan_output_base,
    plan_output_bases,
    repair_paths,
)
from pptrepair.cancel import OperationCancelled
from pptrepair.repair import default_output_path

#: Small media payload so fixtures stay fast to build and diagnose.
_MEDIA_BYTES = 50_000

#: CFB (OLE compound file) signature: encrypted / legacy Office documents.
_CFB_SIG = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


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


def _trimmable_foreign_tail() -> bytes:
    """Build a TAIL_FOREIGN_DATA fixture that trims to a clean archive."""
    intact = build_minimal_pptx(num_slides=2, media_bytes=_MEDIA_BYTES)
    return append_foreign_tail(intact, 131072)


def _extractable_head_zero(num_slides: int = 200,
                           media_bytes: int = 50_000) -> bytes:
    """Build a HEAD_ZERO_FILL fixture whose media part survives intact."""
    data = build_minimal_pptx(num_slides=num_slides, media_bytes=media_bytes)
    cutoff = _header_offset(data, "ppt/media/image1.png")
    return zero_prefix(data, cutoff)


def _snapshot(root: Path) -> dict[Path, bytes]:
    """Return ``{path: bytes}`` for every regular file under *root*."""
    return {
        path: path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    }


# --- plan_output_base / plan_output_bases ------------------------------------


def test_plan_single_root_mirrors_tree(tmp_path: Path) -> None:
    """A lone directory root mirrors the input tree under the output dir."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    path = root / "sub" / "deep.pptx"

    base = plan_output_base(path, [root], out)

    assert base == out / "sub" / "deep"


def test_plan_multiple_roots_use_root_name_subdir(tmp_path: Path) -> None:
    """Several roots each get a subdirectory named after the root."""
    alpha = _mkroot(tmp_path, "alpha")
    beta = _mkroot(tmp_path, "beta")
    out = tmp_path / "out"

    base_a = plan_output_base(alpha / "x.pptx", [alpha, beta], out)
    base_b = plan_output_base(beta / "sub" / "y.pptx", [alpha, beta], out)

    assert base_a == out / "alpha" / "x"
    assert base_b == out / "beta" / "sub" / "y"


def test_plan_same_named_roots_numbered_in_cli_order(tmp_path: Path) -> None:
    """Roots sharing a name are numbered name, name-2 in CLI order."""
    first = _mkroot(tmp_path / "one", "data")
    second = _mkroot(tmp_path / "two", "data")
    out = tmp_path / "out"
    roots = [first, second]

    assert assign_root_labels(roots) == ["data", "data-2"]
    assert plan_output_base(first / "a.pptx", roots, out) == out / "data" / "a"
    assert (plan_output_base(second / "b.pptx", roots, out)
            == out / "data-2" / "b")


def test_plan_triple_same_named_file_roots_stay_distinct(
    tmp_path: Path,
) -> None:
    """Three same-named file roots resolve to three distinct bases: the
    full-name fallback itself can collide and must keep numbering."""
    paths = [
        _write(tmp_path / label, "x.pptx", b"stub")
        for label in ("a", "b", "c")
    ]
    out = tmp_path / "out"

    bases, warnings = plan_output_bases(paths, paths, out)

    assert bases == [out / "x", out / "x.pptx", out / "x.pptx-2"]
    assert len(set(bases)) == 3
    assert len(warnings) == 2


def test_unrepairable_file_leaves_no_empty_output_dir(tmp_path: Path) -> None:
    """A corrupted file with nothing salvageable (mode "none") must not
    leave an empty mirrored directory behind in the output tree."""
    root = _mkroot(tmp_path)
    _write(root, "sub/void.pptx", b"")
    out = tmp_path / "out"

    result = repair_paths([root], output_dir=out)

    assert result.counts()["unrepairable"] == 1
    assert not (out / "sub").exists()


def test_plan_file_root_goes_directly_under_outdir(tmp_path: Path) -> None:
    """A file root drops its artifact directly under the output dir."""
    solo = _write(_mkroot(tmp_path), "solo.pptx", _rebuildable_truncated())
    out = tmp_path / "out"

    # A file root, whether alone or beside a directory root, never gets a
    # mirrored subtree (the artifact lands directly in OUTDIR).
    assert plan_output_base(solo, [solo], out) == out / "solo"
    other = _mkroot(tmp_path, "tree")
    assert plan_output_base(solo, [other, solo], out) == out / "solo"


def test_plan_stem_collision_falls_back_to_full_name_with_warning(
    tmp_path: Path,
) -> None:
    """Two inputs whose stems collide -> the later one uses its full name."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    first = root / "A.pptx"
    second = root / "A.pptm"

    bases, warnings = plan_output_bases([first, second], [root], out)

    assert bases[0] == out / "A"
    assert bases[1] == out / "A.pptm"
    assert len(warnings) == 1
    assert "collision" in warnings[0]


# --- repair_paths: mixed tree, mirror layout, input untouched ----------------


def test_repair_paths_repairs_mixed_tree_and_leaves_input_untouched(
    tmp_path: Path,
) -> None:
    """A mixed tree repairs rebuild+trim artifacts into the mirror and
    leaves every input byte-for-byte unchanged."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    _write(root, "healthy.pptx",
           build_minimal_pptx(num_slides=2, media_bytes=4096))
    rebuild_src = _write(root / "a", "trunc.pptx", _rebuildable_truncated())
    trim_src = _write(root / "b", "tail.pptx", _trimmable_foreign_tail())
    empty_src = _write(root, "empty.pptx", b"")

    before = _snapshot(root)
    result = repair_paths([root], output_dir=out)
    after = _snapshot(root)

    counts = result.counts()
    assert counts["repaired"] == 2
    assert counts["repaired_rebuild"] == 1
    assert counts["repaired_trim"] == 1
    assert counts["unrepairable"] == 1  # the empty file
    assert (out / "a" / "trunc.repaired.pptx").is_file()
    assert (out / "b" / "tail.repaired.pptx").is_file()
    # The intact and empty inputs produce no artifact in the mirror.
    assert not (out / "healthy.repaired.pptx").exists()
    assert not (out / "empty.repaired.pptx").exists()
    # Nothing under the input tree changed (no write next to any source).
    assert before == after
    assert rebuild_src in after and trim_src in after and empty_src in after
    assert result.unrepaired_remaining() == 1
    assert result.had_errors() is False


def test_repair_paths_extract_writes_recovery_report(tmp_path: Path) -> None:
    """An extract-mode repair writes a REPORT.txt inside the recovery folder."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    _write(root / "deck", "broken.pptx", _extractable_head_zero())

    result = repair_paths([root], output_dir=out)

    counts = result.counts()
    assert counts["repaired"] == 1
    assert counts["repaired_extract"] == 1
    recovery = out / "deck" / "broken.salvaged"
    assert recovery.is_dir()
    report = (recovery / "REPORT.txt").read_text(encoding="utf-8")
    assert "head_zero_fill" in report


# --- repair_paths: skip / force ----------------------------------------------


def test_repair_paths_skips_existing_and_force_overwrites(
    tmp_path: Path,
) -> None:
    """An existing artifact is skipped without --force and overwritten with it."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    _write(root, "trunc.pptx", _rebuildable_truncated())

    first = repair_paths([root], output_dir=out)
    assert first.counts()["repaired"] == 1
    artifact = out / "trunc.repaired.pptx"
    assert artifact.is_file()
    marker = artifact.read_bytes()

    skipped = repair_paths([root], output_dir=out)
    assert skipped.counts()["skipped_existing"] == 1
    assert skipped.counts()["repaired"] == 0
    assert artifact.read_bytes() == marker  # untouched
    assert skipped.unrepaired_remaining() == 1

    forced = repair_paths([root], output_dir=out, force=True)
    assert forced.counts()["repaired"] == 1
    assert artifact.is_file()


# --- repair_paths: in-place --------------------------------------------------


def test_repair_paths_in_place_matches_default_output_path(
    tmp_path: Path,
) -> None:
    """--in-place writes to the single-file command's default output path."""
    root = _mkroot(tmp_path)
    src = _write(root / "nested", "trunc.pptx", _rebuildable_truncated())

    result = repair_paths([root], output_dir=None, in_place=True)

    assert result.output_dir is None
    assert result.counts()["repaired"] == 1
    expected = default_output_path(src, "rebuild")
    assert expected == src.parent / "trunc.repaired.pptx"
    assert expected.is_file()
    assert result.items[0].planned_output == expected


# --- repair_paths: CFB is never attempted ------------------------------------


def test_repair_paths_does_not_attempt_cfb(tmp_path: Path) -> None:
    """A CFB (encrypted/legacy) document is reported unrepairable, unattempted."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    _write(root, "encrypted.pptx", _CFB_SIG + b"\x00\x11\x22\x33" * 500)

    result = repair_paths([root], output_dir=out)

    counts = result.counts()
    assert counts["unrepairable"] == 1
    assert counts["unrepairable_cfb"] == 1
    item = result.items[0]
    assert item.action == "unrepairable"
    assert item.repair is None  # repair_file was never called
    assert not any(out.rglob("*")) if out.exists() else True


# --- repair_paths: empty file counts as unrepairable -------------------------


def test_repair_paths_counts_empty_file_as_unrepairable(tmp_path: Path) -> None:
    """An empty .pptx is diagnosed and tallied as unrepairable (not CFB)."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    _write(root, "empty.pptx", b"")

    result = repair_paths([root], output_dir=out)

    counts = result.counts()
    assert counts["unrepairable"] == 1
    assert counts["unrepairable_cfb"] == 0
    assert result.items[0].repair is not None  # repair_file ran (mode none)
    assert result.items[0].repair.mode == "none"


# --- repair_paths: one failure never aborts the batch ------------------------


def test_repair_paths_isolates_repair_failure_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception on one file is captured as failed; the rest still repair."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    _write(root, "boom.pptx", _rebuildable_truncated())
    _write(root, "fine.pptx", _rebuildable_truncated())

    real_repair_file = batch_module.repair_file

    def _flaky(src: Path, **kwargs: object) -> object:
        if src.name == "boom.pptx":
            raise RuntimeError("injected failure")
        return real_repair_file(src, **kwargs)

    monkeypatch.setattr(batch_module, "repair_file", _flaky)

    result = repair_paths([root], output_dir=out)

    counts = result.counts()
    assert counts["failed"] == 1
    assert counts["repaired"] == 1
    failed = next(item for item in result.items
                  if item.source.path.name == "boom.pptx")
    assert failed.action == "failed"
    assert "RuntimeError" in (failed.error or "")
    assert "injected failure" in (failed.error or "")
    assert (out / "fine.repaired.pptx").is_file()
    assert result.had_errors() is True


# --- repair_paths: an OUTDIR inside a root excludes its own artifacts --------


def test_repair_paths_excludes_outdir_inside_root(tmp_path: Path) -> None:
    """An output dir nested inside a scan root is not diagnosed itself."""
    root = _mkroot(tmp_path)
    out = root / "repaired_out"
    out.mkdir()
    # A pre-existing broken file sitting inside the output dir must be
    # excluded from discovery, while the one in the root proper is not.
    _write(out, "planted.pptx", _rebuildable_truncated())
    _write(root, "real.pptx", _rebuildable_truncated())

    result = repair_paths([root], output_dir=out)

    scanned = {outcome.path.name for outcome in result.scan.outcomes}
    assert "real.pptx" in scanned
    assert "planted.pptx" not in scanned
    assert result.counts()["repaired"] == 1
    # No scanned outcome lives under the output directory.
    resolved_out = out.resolve()
    assert all(resolved_out not in outcome.path.resolve().parents
               for outcome in result.scan.outcomes)


# --- repair_paths: dry-run writes nothing ------------------------------------


def test_repair_paths_dry_run_writes_nothing_and_plans(tmp_path: Path) -> None:
    """--dry-run plans artifacts without writing OUTDIR, the report, or
    anything under the input tree."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    report_dir = tmp_path / "report"
    _write(root / "a", "trunc.pptx", _rebuildable_truncated())
    _write(root, "empty.pptx", b"")

    before = _snapshot(root)
    result = repair_paths([root], output_dir=out, report_dir=report_dir,
                          dry_run=True)
    after = _snapshot(root)

    assert result.dry_run is True
    assert not out.exists()          # aggregate output never created
    assert not report_dir.exists()   # report suppressed under dry-run
    assert before == after           # input tree untouched
    counts = result.counts()
    assert counts["planned"] == 1
    assert counts["unrepairable"] == 1  # the empty file
    planned = next(item for item in result.items if item.action == "planned")
    assert planned.planned_output == out / "a" / "trunc.repaired.pptx"
    # A planned artifact is "handled" for exit-code purposes in dry-run;
    # only the unrepairable empty file remains.
    assert result.unrepaired_remaining() == 1


# --- repair_paths: coordinated cancellation ----------------------------------


def test_repair_paths_repair_progress_cancellation_keeps_partial_output(
    tmp_path: Path,
) -> None:
    """A repair_progress callback that raises OperationCancelled right
    after the first repair aborts phase 2 immediately: the exception
    propagates uncaught, the first file's artifact stays on disk, and the
    second corrupted file is never repaired."""
    root = _mkroot(tmp_path)
    out = tmp_path / "out"
    _write(root, "a.pptx", _rebuildable_truncated(num_slides=2))
    _write(root, "b.pptx", _rebuildable_truncated(num_slides=4))

    calls: list[BatchItem] = []

    def _cancel_after_first(item: BatchItem) -> None:
        calls.append(item)
        raise OperationCancelled("user requested cancellation")

    with pytest.raises(OperationCancelled):
        repair_paths([root], output_dir=out,
                     repair_progress=_cancel_after_first)

    assert len(calls) == 1
    assert calls[0].source.path.name == "a.pptx"
    assert (out / "a.repaired.pptx").is_file()
    assert not (out / "b.repaired.pptx").exists()
