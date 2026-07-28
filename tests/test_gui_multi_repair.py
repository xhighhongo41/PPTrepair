"""Tests for the GUI's multi-source (merge) repair feature.

Covers the Qt-free donor planning (:mod:`pptrepair.gui.merge_plan`), the
donor-approval dialog (:mod:`pptrepair.gui.donor_dialog`), the
background :class:`~pptrepair.gui.worker.MultiRepairWorker`, and the
multi-source wiring added to
:class:`pptrepair.gui.main_window.MainWindow`. Skipped wholesale when
PySide6 is not installed (the optional ``[gui]`` extra); see
:mod:`tests.conftest` for the matching collection guard.

Every fixture is a real byte stream written to disk and driven through
the actual diagnose -> score -> splice pipeline, matching the approach of
:mod:`tests.test_merge`, so the merge is exercised against exactly the
recorded metadata the CLI would see.
"""

from __future__ import annotations

import io
import os
import tempfile
import zipfile
from pathlib import Path

import pytest
from fixtures import (
    build_minimal_pptx,
    find_eocd,
    header_offset,
    make_corrupted_copies,
    truncate,
)

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QTreeWidgetItem
from pytestqt.qtbot import QtBot

from pptrepair.batch import plan_output_bases, repair_paths
from pptrepair.classify import Verdict
from pptrepair.gui import worker as worker_module
from pptrepair.gui.donor_dialog import DonorApprovalDialog
from pptrepair.gui.main_window import MainWindow
from pptrepair.gui.merge_plan import (
    ApprovedMerge,
    DonorRef,
    TargetPlan,
    build_target_plans,
)
from pptrepair.gui.run_options import RepairMode
from pptrepair.gui.worker import (
    GuiScanResult,
    MultiRepairRequest,
    MultiRepairResult,
    MultiRepairWorker,
)
from pptrepair.merge import MERGE_SUFFIX
from pptrepair.scan import (
    ArchiveMaterial,
    ArchiveMaterialCache,
    diagnose_archive_materials,
    diagnose_file,
    scan_paths,
)

# --------------------------------------------------------------------------
# fixture helpers
# --------------------------------------------------------------------------


def _mkroot(tmp_path: Path, name: str = "root") -> Path:
    """Create and return an empty scan-root directory under *tmp_path*."""
    root = tmp_path / name
    root.mkdir()
    return root


def _write(dir_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``dir_path / name`` and return the resulting path."""
    path = dir_path / name
    path.write_bytes(data)
    return path


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    """Write *entries* to a new zip archive at *path*."""
    with zipfile.ZipFile(path, mode="w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _entry_interval(data: bytes, name: str) -> tuple[int, int]:
    """Return the ``[offset, next_offset)`` byte range of member *name*.

    Mirrors :mod:`tests.test_merge`'s own helper: the end is the next
    entry's header offset, or the central-directory start when *name* is
    the last entry.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        offsets = sorted(info.header_offset for info in archive.infolist())
    offset = header_offset(data, name)
    cd_offset, _size, _eocd = find_eocd(data)
    index = offsets.index(offset)
    end = offsets[index + 1] if index + 1 < len(offsets) else cd_offset
    return offset, end


def _complementary_pair(original: bytes) -> tuple[bytes, bytes]:
    """Return two same-origin copies broken in non-overlapping ranges.

    Copy A destroys the media entry, copy B destroys slide1; each missing
    range survives in the other, so the pair merges byte-identically.
    Both keep their central directory, so they score ``auto`` against
    each other.
    """
    media = _entry_interval(original, "ppt/media/image1.png")
    slide1 = _entry_interval(original, "ppt/slides/slide1.xml")
    copy_a, copy_b = make_corrupted_copies(original, [
        [("zero_range", *media)],
        [("zero_range", *slide1)],
    ])
    return copy_a, copy_b


def _media_zeroed(original: bytes) -> bytes:
    """Return a copy of *original* with only its media entry destroyed.

    The central directory survives, so it scores ``auto`` against a
    pristine copy that can then supply the missing media part.
    """
    media = _entry_interval(original, "ppt/media/image1.png")
    return make_corrupted_copies(original, [[("zero_range", *media)]])[0]


def _rebuildable_truncated(num_slides: int = 3, seed: int = 0) -> bytes:
    """Build a TAIL_TRUNCATED fixture that rebuilds with every slide intact."""
    data = build_minimal_pptx(num_slides=num_slides, media_bytes=4096,
                              seed=seed)
    cutoff = header_offset(data, "ppt/media/image1.png")
    return truncate(data, cutoff)


def _disk_donor(path: Path, tier: str = "auto") -> DonorRef:
    """Return an on-disk :class:`DonorRef` for *path*."""
    return DonorRef(display=str(path), tier=tier, path=path, material=None)


# --------------------------------------------------------------------------
# build_target_plans
# --------------------------------------------------------------------------


def test_build_target_plans_pairs_same_origin_copies(tmp_path: Path) -> None:
    """Two same-origin corrupted copies become each other's auto donor."""
    root = _mkroot(tmp_path)
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000)
    copy_a, copy_b = _complementary_pair(original)
    _write(root, "a.pptx", copy_a)
    _write(root, "b.pptx", copy_b)

    result = GuiScanResult(scan=scan_paths([root]))
    plans = build_target_plans(result)

    assert len(plans) == 2
    for plan in plans:
        assert len(plan.donors) == 1
        donor = plan.donors[0]
        assert donor.tier == "auto"
        assert donor.path is not None
        assert donor.material is None


def test_build_target_plans_excludes_unrelated_file(tmp_path: Path) -> None:
    """An unrelated (different-origin) file is never offered as a donor."""
    root = _mkroot(tmp_path)
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000, seed=1)
    (corrupted,) = make_corrupted_copies(original, [[("foreign_prefix", 4096)]])
    _write(root, "target.pptx", corrupted)
    # A structurally valid but wholly unrelated presentation.
    _write(root, "other.pptx",
           build_minimal_pptx(num_slides=2, media_bytes=30_000, seed=99))

    result = GuiScanResult(scan=scan_paths([root]))
    plans = build_target_plans(result)

    # Only the corrupted file is a target; the unrelated file scores
    # "rejected", so the target has no usable donor.
    assert len(plans) == 1
    assert plans[0].target.name == "target.pptx"
    assert plans[0].donors == ()


def test_build_target_plans_surfaces_archive_material_donor(
    tmp_path: Path
) -> None:
    """An intact copy kept inside a backup archive becomes an auto donor."""
    root = _mkroot(tmp_path)
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000, seed=2)
    _write(root, "broken.pptx", _media_zeroed(original))
    archive_path = _write(root, "backup.zip", b"")
    _write_zip(archive_path, {"backup/intact.pptx": original})

    materials, _notes = diagnose_archive_materials([archive_path])
    result = GuiScanResult(scan=scan_paths([root]), materials=materials)
    plans = build_target_plans(result)

    assert len(plans) == 1
    material_donors = [d for d in plans[0].donors if d.material is not None]
    assert len(material_donors) == 1
    donor = material_donors[0]
    assert donor.path is None
    assert donor.tier == "auto"
    assert donor.display == f"{archive_path}::backup/intact.pptx"


# --------------------------------------------------------------------------
# DonorApprovalDialog
# --------------------------------------------------------------------------


def _tiered_plan() -> TargetPlan:
    """Return a plan carrying one donor of each usable tier."""
    return TargetPlan(
        target=Path("/x/broken.pptx"),
        donors=(
            DonorRef("/x/a.pptx", "auto", Path("/x/a.pptx"), None),
            DonorRef("/x/b.pptx", "candidate", Path("/x/b.pptx"), None),
            DonorRef("/x/c.pptx", "lineage", Path("/x/c.pptx"), None),
        ))


def test_donor_dialog_initial_checkstates_only_auto(qtbot: QtBot) -> None:
    """Only the auto-tier donor is pre-checked; the weaker tiers are not."""
    dialog = DonorApprovalDialog([_tiered_plan()])
    qtbot.addWidget(dialog)
    dialog.show()

    [approved] = dialog.approved_plans()
    assert [donor.tier for donor in approved.donors] == ["auto"]
    assert approved.allow_candidate is False
    assert approved.allow_lineage is False


def test_donor_dialog_checking_candidate_sets_allow_flag(
    qtbot: QtBot
) -> None:
    """Checking a candidate donor adds it and raises allow_candidate."""
    dialog = DonorApprovalDialog([_tiered_plan()])
    qtbot.addWidget(dialog)
    dialog.show()

    for child, donor in dialog._donor_items.items():
        if donor.tier == "candidate":
            child.setCheckState(0, Qt.CheckState.Checked)

    [approved] = dialog.approved_plans()
    tiers = sorted(donor.tier for donor in approved.donors)
    assert tiers == ["auto", "candidate"]
    assert approved.allow_candidate is True
    assert approved.allow_lineage is False


def test_donor_dialog_no_donor_plan_yields_empty_selection(
    qtbot: QtBot
) -> None:
    """A donor-less target shows a disabled placeholder and approves empty."""
    plan = TargetPlan(target=Path("/x/lonely.pptx"), donors=())
    dialog = DonorApprovalDialog([plan])
    qtbot.addWidget(dialog)
    dialog.show()

    [approved] = dialog.approved_plans()
    assert approved.target == Path("/x/lonely.pptx")
    assert approved.donors == ()
    assert approved.allow_candidate is False
    assert approved.allow_lineage is False


def test_donor_dialog_shows_tier_legend(qtbot: QtBot) -> None:
    """The dialog shows a legend explaining each of the three tier tags."""
    dialog = DonorApprovalDialog([_tiered_plan()])
    qtbot.addWidget(dialog)
    dialog.show()

    labels_text = "\n".join(
        label.text() for label in dialog.findChildren(QLabel))
    assert "[auto]" in labels_text
    assert "[candidate]" in labels_text
    assert "[lineage]" in labels_text


def _dialog_with_placeholder(qtbot: QtBot) -> DonorApprovalDialog:
    """Return a dialog covering tiered donors plus a donor-less target.

    Mixes a tiered plan (auto/candidate/lineage) with a donor-less plan,
    so the bulk-action tests can also confirm the placeholder row is
    left untouched throughout.
    """
    plans = [_tiered_plan(), TargetPlan(target=Path("/x/lonely.pptx"),
                                        donors=())]
    dialog = DonorApprovalDialog(plans)
    qtbot.addWidget(dialog)
    dialog.show()
    return dialog


def _bulk_action_button(dialog: DonorApprovalDialog,
                        label: str) -> QPushButton:
    """Return the dialog's QPushButton whose text matches *label*."""
    [button] = [b for b in dialog.findChildren(QPushButton)
                if b.text() == label]
    return button


def _placeholder_item(dialog: DonorApprovalDialog) -> QTreeWidgetItem:
    """Return the disabled "no donors" child of the donor-less target."""
    # The donor-less plan is the second one built in _dialog_with_placeholder.
    target_item = dialog._target_items[1]
    assert target_item.childCount() == 1
    return target_item.child(0)


def test_donor_dialog_select_all_checks_every_donor(qtbot: QtBot) -> None:
    """The "Select all" button checks every donor across every target."""
    dialog = _dialog_with_placeholder(qtbot)

    _bulk_action_button(dialog, "Select all").click()

    assert all(item.checkState(0) == Qt.CheckState.Checked
              for item in dialog._donor_items)
    [tiered_approved, lonely_approved] = dialog.approved_plans()
    tiers = sorted(donor.tier for donor in tiered_approved.donors)
    assert tiers == ["auto", "candidate", "lineage"]
    assert tiered_approved.allow_candidate is True
    assert tiered_approved.allow_lineage is True
    assert lonely_approved.donors == ()
    # The placeholder row is never checkable, so it stays untouched.
    assert (_placeholder_item(dialog).flags()
            == Qt.ItemFlag.NoItemFlags)


def test_donor_dialog_deselect_all_unchecks_every_donor(qtbot: QtBot) -> None:
    """The "Deselect all" button unchecks every donor across every target."""
    dialog = _dialog_with_placeholder(qtbot)

    _bulk_action_button(dialog, "Deselect all").click()

    assert all(item.checkState(0) == Qt.CheckState.Unchecked
              for item in dialog._donor_items)
    [tiered_approved, lonely_approved] = dialog.approved_plans()
    assert tiered_approved.donors == ()
    assert tiered_approved.allow_candidate is False
    assert tiered_approved.allow_lineage is False
    assert lonely_approved.donors == ()
    assert (_placeholder_item(dialog).flags()
            == Qt.ItemFlag.NoItemFlags)


def test_donor_dialog_reset_to_default_restores_auto_only(
    qtbot: QtBot
) -> None:
    """"Reset to default" restores the initial auto-only checkstate."""
    dialog = _dialog_with_placeholder(qtbot)

    # Scramble the check states away from their initial policy first.
    _bulk_action_button(dialog, "Select all").click()

    _bulk_action_button(dialog, "Reset to default").click()

    for item, donor in dialog._donor_items.items():
        expected = (Qt.CheckState.Checked if donor.tier == "auto"
                    else Qt.CheckState.Unchecked)
        assert item.checkState(0) == expected
    [tiered_approved, lonely_approved] = dialog.approved_plans()
    assert [donor.tier for donor in tiered_approved.donors] == ["auto"]
    assert tiered_approved.allow_candidate is False
    assert tiered_approved.allow_lineage is False
    assert lonely_approved.donors == ()
    assert (_placeholder_item(dialog).flags()
            == Qt.ItemFlag.NoItemFlags)


# --------------------------------------------------------------------------
# MultiRepairWorker
# --------------------------------------------------------------------------


def test_multi_worker_disk_merge_writes_normal_output(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """Two on-disk copies merge into an aggregate output that checks NORMAL."""
    root = _mkroot(tmp_path)
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000)
    copy_a, copy_b = _complementary_pair(original)
    path_a = _write(root, "a.pptx", copy_a)
    path_b = _write(root, "b.pptx", copy_b)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    merge = ApprovedMerge(
        target=path_a, donors=(_disk_donor(path_b),),
        allow_candidate=False, allow_lineage=False)
    worker = MultiRepairWorker(MultiRepairRequest(
        merges=(merge,), fallback_targets=(), roots=(root,),
        output_dir=output_dir, in_place=False))

    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    result = blocker.args[0]
    assert isinstance(result, MultiRepairResult)
    [item] = result.merges
    assert item.success
    assert item.output_path == output_dir / f"a{MERGE_SUFFIX}"
    assert item.output_path.exists()
    diagnosis, _error = diagnose_file(item.output_path)
    assert diagnosis.verdict == Verdict.NORMAL
    assert worker.wait(5000)


def test_multi_worker_archive_donor_merge_succeeds(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """A donor kept inside a backup archive is materialized and spliced in."""
    root = _mkroot(tmp_path)
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000, seed=3)
    target = _write(root, "broken.pptx", _media_zeroed(original))
    archive_path = _write(root, "backup.zip", b"")
    _write_zip(archive_path, {"backup/intact.pptx": original})

    materials, _notes = diagnose_archive_materials([archive_path])
    [material] = materials
    donor = DonorRef(display=material.display(), tier="auto", path=None,
                     material=material)
    merge = ApprovedMerge(
        target=target, donors=(donor,),
        allow_candidate=False, allow_lineage=False)
    worker = MultiRepairWorker(MultiRepairRequest(
        merges=(merge,), fallback_targets=(), roots=(root,),
        output_dir=None, in_place=True))

    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    [item] = blocker.args[0].merges
    assert item.success
    assert item.output_path == root / f"broken{MERGE_SUFFIX}"
    assert item.output_path.exists()
    diagnosis, _error = diagnose_file(item.output_path)
    assert diagnosis.verdict == Verdict.NORMAL
    # The temporary extraction path must never leak into a note/detail.
    assert "pptrepair-gui-merge-" not in item.detail
    assert worker.wait(5000)


def _mined_archive_donor(
    root: Path, cache: ArchiveMaterialCache | None = None, seed: int = 4,
) -> tuple[Path, Path, ArchiveMaterial, bytes]:
    """Build a broken target plus a backup archive holding its intact twin.

    The archive is mined exactly as a GUI scan would mine it, through
    *cache* when one is given -- so the returned material's member is
    already extracted under that cache's root, which is the state the
    repair phase actually inherits.

    :returns: ``(target, archive path, material, the intact bytes)``.
    """
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000, seed=seed)
    target = _write(root, "broken.pptx", _media_zeroed(original))
    archive_path = _write(root, "backup.zip", b"")
    _write_zip(archive_path, {"backup/intact.pptx": original})
    materials, _notes = diagnose_archive_materials([archive_path], cache=cache)
    [material] = materials
    return target, archive_path, material, original


def _archive_merge_request(target: Path, material: ArchiveMaterial,
                           root: Path) -> MultiRepairRequest:
    """Return an in-place request merging *target* against one archive donor."""
    donor = DonorRef(display=material.display(), tier="auto", path=None,
                     material=material)
    merge = ApprovedMerge(target=target, donors=(donor,),
                          allow_candidate=False, allow_lineage=False)
    return MultiRepairRequest(
        merges=(merge,), fallback_targets=(), roots=(root,),
        output_dir=None, in_place=True)


def test_multi_worker_reuses_the_scan_cache_instead_of_re_extracting(
    qtbot: QtBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A donor the scan already extracted is spliced straight from the
    session cache: the archive is never opened again for it."""
    root = _mkroot(tmp_path)
    cache_root = tmp_path / "cache"
    cache = ArchiveMaterialCache(cache_root)
    target, _archive_path, material, _original = _mined_archive_donor(
        root, cache)
    calls: list[object] = []
    real_materialize = worker_module.materialize

    def _recording_materialize(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(worker_module, "materialize", _recording_materialize)

    worker = MultiRepairWorker(
        _archive_merge_request(target, material, root), cache=cache)
    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    [item] = blocker.args[0].merges
    assert calls == []
    assert item.success
    assert item.output_path == root / f"broken{MERGE_SUFFIX}"
    diagnosis, _error = diagnose_file(item.output_path)
    assert diagnosis.verdict == Verdict.NORMAL
    # The donor is still named by its in-archive label, never by the
    # cache path it was served from.
    assert str(cache_root) not in item.detail
    assert worker.wait(5000)


def test_multi_worker_uses_the_cached_file_itself_without_copying(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """The cached donor is handed to the merge where it already lies,
    under the cache root, not copied into the run's scratch directory."""
    root = _mkroot(tmp_path)
    cache_root = tmp_path / "cache"
    cache = ArchiveMaterialCache(cache_root)
    target, archive_path, material, original = _mined_archive_donor(
        root, cache)
    worker = MultiRepairWorker(
        _archive_merge_request(target, material, root), cache=cache)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        material_paths, display = worker._materialize_donors(tmp_root)

        donor_path = material_paths[material.member]
        assert donor_path == cache.member_path(archive_path, material.member)
        assert cache_root in donor_path.parents
        assert donor_path.read_bytes() == original
        # The merge still names the donor by its in-archive label.
        assert display[donor_path] == material.display()
        assert list(tmp_root.iterdir()) == []


def test_multi_worker_falls_back_when_the_cache_cannot_serve_the_donor(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """A cached extraction that has since disappeared is re-extracted from
    the archive into the run's own scratch directory."""
    root = _mkroot(tmp_path)
    cache = ArchiveMaterialCache(tmp_path / "cache")
    target, archive_path, material, original = _mined_archive_donor(
        root, cache)
    cached_path = cache.member_path(archive_path, material.member)
    assert cached_path is not None
    cached_path.unlink()
    worker = MultiRepairWorker(
        _archive_merge_request(target, material, root), cache=cache)

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        material_paths, display = worker._materialize_donors(tmp_root)

        donor_path = material_paths[material.member]
        assert tmp_root in donor_path.parents
        assert donor_path.read_bytes() == original
        assert display[donor_path] == material.display()


def test_multi_worker_without_a_cache_extracts_the_donor_itself(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """With no cache at all the worker keeps its pre-cache behaviour:
    every archive donor is extracted into the run's scratch directory."""
    root = _mkroot(tmp_path)
    target, _archive_path, material, original = _mined_archive_donor(root)
    worker = MultiRepairWorker(_archive_merge_request(target, material, root))

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        material_paths, display = worker._materialize_donors(tmp_root)

        donor_path = material_paths[material.member]
        assert tmp_root in donor_path.parents
        assert donor_path.read_bytes() == original
        assert display[donor_path] == material.display()


def test_multi_worker_donorless_target_falls_back_to_single_repair(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """A donor-less corrupted file is repaired on its own bytes."""
    root = _mkroot(tmp_path)
    bad = _write(root, "bad.pptx", _rebuildable_truncated())

    repaired: list[object] = []
    worker = MultiRepairWorker(MultiRepairRequest(
        merges=(), fallback_targets=(bad,), roots=(root,),
        output_dir=None, in_place=True))
    worker.file_repaired.connect(lambda outcome: repaired.append(outcome))

    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    result = blocker.args[0]
    assert result.merges == []
    [outcome] = result.fallbacks
    assert outcome.success
    assert outcome.mode == "rebuild"
    assert outcome.output_path is not None
    assert outcome.output_path.exists()
    assert outcome.output_path.parent == bad.parent
    assert len(repaired) == 1
    assert worker.wait(5000)


def test_multi_worker_cancellation_stops_before_next_merge(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """Cancelling on the first merge_done leaves the second merge unprocessed.

    The cancel flag is polled at the top of each merge iteration, so a
    cancellation requested from the first merge's ``merge_done`` slot
    stops the run before the second merge is ever attempted -- its output
    file is never written.
    """
    root = _mkroot(tmp_path)
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000)
    a1, b1 = _complementary_pair(original)
    a2, b2 = _complementary_pair(build_minimal_pptx(num_slides=3,
                                                    media_bytes=60_000, seed=7))
    path_a1 = _write(root, "a1.pptx", a1)
    path_b1 = _write(root, "b1.pptx", b1)
    path_a2 = _write(root, "a2.pptx", a2)
    path_b2 = _write(root, "b2.pptx", b2)

    merges = (
        ApprovedMerge(path_a1, (_disk_donor(path_b1),), False, False),
        ApprovedMerge(path_a2, (_disk_donor(path_b2),), False, False),
    )
    worker = MultiRepairWorker(MultiRepairRequest(
        merges=merges, fallback_targets=(), roots=(root,),
        output_dir=None, in_place=True))

    done: list[object] = []
    worker.merge_done.connect(lambda item: done.append(item))
    finished: list[object] = []
    worker.finished_ok.connect(lambda result: finished.append(result))
    # A blocking-queued connection makes the stop deterministic: the
    # worker thread parks at the first emit until this UI-thread slot has
    # set the cancel flag, so the next iteration is guaranteed to raise.
    worker.merge_done.connect(
        lambda _item: worker.cancel(),
        Qt.ConnectionType.BlockingQueuedConnection)

    with qtbot.waitSignal(worker.cancelled, timeout=15000):
        worker.start()

    assert finished == []
    assert len(done) == 1
    assert (path_a1.parent / f"a1{MERGE_SUFFIX}").exists()
    assert not (path_a2.parent / f"a2{MERGE_SUFFIX}").exists()
    assert worker.wait(5000)


def test_multi_worker_output_dir_mirrors_single_mode_tree(
    qtbot: QtBot, tmp_path: Path
) -> None:
    """The aggregate merge output mirrors the input tree like repair_paths.

    A corrupted file nested under ``sub/`` is merged into an output
    directory; its artifact lands at the same suffix-less base
    :func:`pptrepair.batch.plan_output_bases` (the very helper
    single-file :func:`pptrepair.batch.repair_paths` uses) computes, and
    at the same relative position an actual single-mode repair produces.
    """
    root = _mkroot(tmp_path)
    sub = root / "sub"
    sub.mkdir()
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000, seed=5)
    (corrupted,) = make_corrupted_copies(original, [[("foreign_prefix", 4096)]])
    target = _write(sub, "a.pptx", corrupted)
    # A pristine sibling copy (an auto donor; NORMAL, so never a target).
    donor_path = _write(sub, "b.pptx", original)
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    merge = ApprovedMerge(
        target=target, donors=(_disk_donor(donor_path),),
        allow_candidate=False, allow_lineage=False)
    worker = MultiRepairWorker(MultiRepairRequest(
        merges=(merge,), fallback_targets=(), roots=(root,),
        output_dir=output_dir, in_place=False))

    with qtbot.waitSignal(worker.finished_ok, timeout=15000) as blocker:
        worker.start()

    [item] = blocker.args[0].merges
    assert item.success
    (expected_base,), _warnings = plan_output_bases(
        [target], [root], output_dir)
    assert item.output_path == expected_base.with_name(
        expected_base.name + MERGE_SUFFIX)
    assert item.output_path.relative_to(output_dir) == Path("sub/a.merged.pptx")

    # A real single-mode run places the same file's artifact under the
    # same mirrored subdirectory (only the mode's suffix differs).
    single_out = tmp_path / "single"
    single_out.mkdir()
    single = repair_paths([root], output_dir=single_out, in_place=False)
    single_item = next(it for it in single.items
                       if it.source.path == target)
    assert single_item.planned_output is not None
    assert (single_item.planned_output.parent.relative_to(single_out)
            == item.output_path.parent.relative_to(output_dir))
    assert worker.wait(5000)


# --------------------------------------------------------------------------
# MainWindow integration
# --------------------------------------------------------------------------


@pytest.fixture
def main_window(qtbot: QtBot) -> MainWindow:
    """Build a :class:`MainWindow`, registered with *qtbot* for cleanup."""
    window = MainWindow()
    qtbot.addWidget(window)
    return window


def _scan_and_wait(main_window: MainWindow, qtbot: QtBot) -> None:
    """Run a scan on *main_window*'s accumulated sources and wait for it."""
    main_window._start_scan()
    qtbot.waitUntil(lambda: main_window._scan_worker is None, timeout=15000)


def _select_multi_mode(main_window: MainWindow) -> None:
    """Switch the run-options panel to multi-source repair mode."""
    combo = main_window._run_options._mode_combo
    combo.setCurrentIndex(combo.findData(RepairMode.MULTI))


def _repair_actions(main_window: MainWindow) -> list[str]:
    """Return the Action-column text of every current Repair-tab row."""
    model = main_window._results_panel._repair_model
    return [model.index(row, 1).data() for row in range(model.rowCount())]


def test_main_window_multi_repair_populates_repair_tab(
    main_window: MainWindow, qtbot: QtBot, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multi mode: an accepted dialog runs merges and fills the Repair tab."""
    # Accept the donor dialog without showing it; its auto donors are
    # pre-checked at construction, so approved_plans yields real merges.
    monkeypatch.setattr(
        DonorApprovalDialog, "exec",
        lambda self: QDialog.DialogCode.Accepted)

    root = _mkroot(tmp_path)
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000)
    copy_a, copy_b = _complementary_pair(original)
    _write(root, "a.pptx", copy_a)
    _write(root, "b.pptx", copy_b)
    main_window._sources.add_paths([root])
    _scan_and_wait(main_window, qtbot)

    _select_multi_mode(main_window)
    assert main_window._repair_button.isEnabled()

    output_dir = tmp_path / "out"
    output_dir.mkdir()
    main_window._run_options._into_folder_radio.setChecked(True)
    main_window._run_options._output_edit.setText(str(output_dir))

    main_window._start_repair()
    qtbot.waitUntil(lambda: main_window._multi_repair_worker is None,
                    timeout=15000)

    actions = _repair_actions(main_window)
    # Both corrupted copies are targets and each other's auto donor, so
    # both merge successfully.
    assert actions.count("merged") == 2
    assert main_window._open_output_button.isEnabled()
    assert not main_window._open_output_button.isHidden()


def test_main_window_multi_repair_reuses_the_scan_cache_for_archive_donors(
    main_window: MainWindow, qtbot: QtBot, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window's session cache spans both phases: the archive dropped
    as donor material is read during the scan and never again during the
    repair that splices one of its members in."""
    monkeypatch.setattr(
        DonorApprovalDialog, "exec",
        lambda self: QDialog.DialogCode.Accepted)

    root = _mkroot(tmp_path)
    original = build_minimal_pptx(num_slides=3, media_bytes=60_000, seed=8)
    _write(root, "broken.pptx", _media_zeroed(original))
    archive_path = tmp_path / "backup.zip"
    _write_zip(archive_path, {"backup/intact.pptx": original})
    main_window._sources.add_paths([root, archive_path])
    _scan_and_wait(main_window, qtbot)

    calls: list[object] = []
    real_materialize = worker_module.materialize

    def _recording_materialize(*args: object, **kwargs: object) -> object:
        calls.append(args)
        return real_materialize(*args, **kwargs)

    monkeypatch.setattr(worker_module, "materialize", _recording_materialize)

    _select_multi_mode(main_window)
    main_window._start_repair()
    qtbot.waitUntil(lambda: main_window._multi_repair_worker is None,
                    timeout=15000)

    assert calls == []
    assert _repair_actions(main_window).count("merged") == 1
    merged = root / f"broken{MERGE_SUFFIX}"
    diagnosis, _error = diagnose_file(merged)
    assert diagnosis.verdict == Verdict.NORMAL


def test_main_window_sources_changed_clears_stale_result(
    main_window: MainWindow, qtbot: QtBot, tmp_path: Path
) -> None:
    """Adding a source after a scan clears the now-stale result and disables Repair."""
    root = _mkroot(tmp_path)
    _write(root, "bad.pptx", _rebuildable_truncated())
    main_window._sources.add_paths([root])
    _scan_and_wait(main_window, qtbot)
    assert main_window._repair_button.isEnabled()
    assert main_window._results_panel.last_result() is not None

    # A source change invalidates the displayed scan result.
    extra = _mkroot(tmp_path, "extra")
    _write(extra, "more.pptx", _rebuildable_truncated(seed=1))
    main_window._sources.add_paths([extra])

    assert main_window._results_panel.last_result() is None
    assert not main_window._repair_button.isEnabled()
    assert (main_window.statusBar().currentMessage()
            == "Sources changed — please scan again")
