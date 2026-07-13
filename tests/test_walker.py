"""Tests for :mod:`pptrepair.walker`.

All fixtures are built directly under ``tmp_path``; no real .pptx sample
files and no network access are involved. Directory contents here are
empty stand-ins (a few bytes of dummy data) since discovery never opens
a file.
"""

from __future__ import annotations

import os
from pathlib import Path

from pptrepair import walker
from pptrepair.walker import discover_targets, is_cloud_placeholder


class _FakeStat:
    """A minimal stand-in for ``os.stat_result`` exposing only the
    attributes a test needs, so :func:`is_cloud_placeholder` can be
    exercised without a real placeholder file."""

    def __init__(self, **attrs: int) -> None:
        for name, value in attrs.items():
            setattr(self, name, value)


# --- is_cloud_placeholder ---------------------------------------------------


def test_is_cloud_placeholder_true_for_macos_dataless_flag() -> None:
    """SF_DATALESS in st_flags marks a macOS File Provider placeholder."""
    st = _FakeStat(st_flags=walker.SF_DATALESS)
    assert is_cloud_placeholder(st) is True


def test_is_cloud_placeholder_true_for_windows_recall_attribute() -> None:
    """A RECALL_ON_DATA_ACCESS bit marks a Windows Cloud Filter placeholder."""
    st = _FakeStat(
        st_file_attributes=walker.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS)
    assert is_cloud_placeholder(st) is True


def test_is_cloud_placeholder_false_when_markers_are_zero() -> None:
    """Present but zero-valued markers are not a placeholder."""
    st = _FakeStat(st_flags=0, st_file_attributes=0)
    assert is_cloud_placeholder(st) is False


def test_is_cloud_placeholder_false_when_attributes_are_absent() -> None:
    """Platforms without st_flags/st_file_attributes yield a plain False."""
    st = _FakeStat()
    assert is_cloud_placeholder(st) is False


# --- discover_targets: recursion, ordering, classification -----------------


def test_discover_targets_recursive_deterministic_order(tmp_path: Path) -> None:
    """Nested directories are walked top-down with sorted entries at
    each level, producing a fully deterministic target order."""
    root = tmp_path / "root"
    (root / "a").mkdir(parents=True)
    (root / "b.pptx").write_bytes(b"data")
    (root / "z.pptx").write_bytes(b"data")
    (root / "a" / "c.pptx").write_bytes(b"data")
    (root / "a" / "d.PPTX").write_bytes(b"data")

    result = discover_targets([root])

    assert result.targets == [
        root / "b.pptx",
        root / "z.pptx",
        root / "a" / "c.pptx",
        root / "a" / "d.PPTX",
    ]


def test_discover_targets_classifies_by_suffix(tmp_path: Path) -> None:
    """.pptx/.pptm (any case) become targets, .ppt is legacy, others ignored."""
    root = tmp_path / "root"
    root.mkdir()
    pptx = root / "a.pptx"
    pptx_upper = root / "b.PPTX"
    pptm = root / "c.pptm"
    legacy = root / "d.ppt"
    unrelated = root / "e.txt"
    for path in (pptx, pptx_upper, pptm, legacy, unrelated):
        path.write_bytes(b"data")

    result = discover_targets([root])

    assert result.targets == [pptx, pptx_upper, pptm]
    assert result.skipped_legacy == [legacy]
    assert unrelated not in result.targets
    assert unrelated not in result.skipped_legacy


def test_discover_targets_skips_office_temp_files(tmp_path: Path) -> None:
    """A ``~$`` owner/lock file is bucketed as skipped_temp, not a target."""
    root = tmp_path / "root"
    root.mkdir()
    temp = root / "~$deck.pptx"
    real = root / "deck.pptx"
    temp.write_bytes(b"data")
    real.write_bytes(b"data")

    result = discover_targets([root])

    assert result.skipped_temp == [temp]
    assert result.targets == [real]


def test_discover_targets_root_as_single_file(tmp_path: Path) -> None:
    """A root that is itself a file is classified directly, no walk needed."""
    file_path = tmp_path / "solo.pptx"
    file_path.write_bytes(b"data")

    result = discover_targets([file_path])

    assert result.targets == [file_path]


def test_discover_targets_nonexistent_root_recorded_as_error(
        tmp_path: Path) -> None:
    """A root that does not exist is recorded in errors, not raised."""
    missing = tmp_path / "does_not_exist"

    result = discover_targets([missing])

    assert result.targets == []
    assert len(result.errors) == 1
    assert result.errors[0][0] == missing


# --- discover_targets: symlink handling -------------------------------------


def test_discover_targets_ignores_file_symlink_by_default(
        tmp_path: Path) -> None:
    """A symlinked file is invisible by default, but discovered when
    follow_symlinks is requested."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.pptx"
    outside.write_bytes(b"data")
    link = root / "link.pptx"
    link.symlink_to(outside)

    default_result = discover_targets([root])
    assert default_result.targets == []

    followed_result = discover_targets([root], follow_symlinks=True)
    assert followed_result.targets == [link]


def test_discover_targets_ignores_directory_symlink_by_default(
        tmp_path: Path) -> None:
    """A symlinked directory is not descended into by default, but its
    contents are discovered when follow_symlinks is requested."""
    root = tmp_path / "root"
    root.mkdir()
    real = tmp_path / "real_target"
    real.mkdir()
    (real / "inner.pptx").write_bytes(b"data")
    link_dir = root / "link_dir"
    link_dir.symlink_to(real, target_is_directory=True)

    default_result = discover_targets([root])
    assert default_result.targets == []

    followed_result = discover_targets([root], follow_symlinks=True)
    assert followed_result.targets == [link_dir / "inner.pptx"]


def test_discover_targets_always_follows_symlinked_root(
        tmp_path: Path) -> None:
    """An explicitly named root is followed through its own symlink even
    with the default follow_symlinks=False (find -H convention)."""
    real = tmp_path / "real_root"
    real.mkdir()
    (real / "deck.pptx").write_bytes(b"data")
    link_root = tmp_path / "link_root"
    link_root.symlink_to(real, target_is_directory=True)
    file_target = tmp_path / "real.pptx"
    file_target.write_bytes(b"data")
    link_file_root = tmp_path / "link.pptx"
    link_file_root.symlink_to(file_target)

    result = discover_targets([link_root, link_file_root])

    assert result.targets == [link_root / "deck.pptx", link_file_root]
    assert result.errors == []


def test_discover_targets_follows_symlinks_without_infinite_loop(
        tmp_path: Path) -> None:
    """A symlink cycle (pointing back to an ancestor) does not hang the
    walk and does not cause the same directory to be visited twice."""
    root = tmp_path / "root"
    child = root / "child"
    child.mkdir(parents=True)
    (child / "file.pptx").write_bytes(b"data")
    loop = child / "loop"
    loop.symlink_to(root, target_is_directory=True)

    result = discover_targets([root], follow_symlinks=True)

    assert result.targets == [child / "file.pptx"]
    assert result.errors == []


# --- discover_targets: cloud placeholder skipping ---------------------------


def test_discover_targets_skips_cloud_placeholder_file(
        tmp_path: Path, monkeypatch) -> None:
    """A file flagged as a cloud placeholder is skipped, not diagnosed."""
    root = tmp_path / "root"
    root.mkdir()
    cloud_file = root / "cloud.pptx"
    cloud_file.write_bytes(b"data")
    normal_file = root / "normal.pptx"
    normal_file.write_bytes(b"data")
    cloud_ino = os.lstat(cloud_file).st_ino

    monkeypatch.setattr(
        walker, "is_cloud_placeholder",
        lambda st: st.st_ino == cloud_ino)

    result = discover_targets([root])

    assert cloud_file in result.skipped_cloud
    assert cloud_file not in result.targets
    assert result.targets == [normal_file]


def test_discover_targets_skips_cloud_placeholder_directory(
        tmp_path: Path, monkeypatch) -> None:
    """A directory flagged as a cloud placeholder is recorded but never
    listed, so its contents are never discovered."""
    root = tmp_path / "root"
    root.mkdir()
    cloud_dir = root / "cloud_dir"
    cloud_dir.mkdir()
    (cloud_dir / "inner.pptx").write_bytes(b"data")
    normal_file = root / "normal.pptx"
    normal_file.write_bytes(b"data")
    cloud_ino = os.lstat(cloud_dir).st_ino

    monkeypatch.setattr(
        walker, "is_cloud_placeholder",
        lambda st: st.st_ino == cloud_ino)

    result = discover_targets([root])

    assert cloud_dir in result.skipped_cloud
    assert result.targets == [normal_file]


def test_discover_targets_allow_download_bypasses_cloud_check(
        tmp_path: Path, monkeypatch) -> None:
    """With allow_download=True, placeholders are treated as ordinary
    candidates and the placeholder check itself is never consulted."""
    root = tmp_path / "root"
    root.mkdir()
    cloud_file = root / "cloud.pptx"
    cloud_file.write_bytes(b"data")
    cloud_ino = os.lstat(cloud_file).st_ino

    monkeypatch.setattr(
        walker, "is_cloud_placeholder",
        lambda st: st.st_ino == cloud_ino)

    result = discover_targets([root], allow_download=True)

    assert result.skipped_cloud == []
    assert result.targets == [cloud_file]


# --- discover_targets: error handling ---------------------------------------


def test_discover_targets_permission_error_recorded_and_continues(
        tmp_path: Path) -> None:
    """An unreadable subdirectory is recorded in errors, while sibling
    files elsewhere in the tree are still discovered."""
    root = tmp_path / "root"
    root.mkdir()
    blocked = root / "blocked"
    blocked.mkdir()
    (blocked / "inner.pptx").write_bytes(b"data")
    ok_file = root / "ok.pptx"
    ok_file.write_bytes(b"data")

    blocked.chmod(0o000)
    try:
        result = discover_targets([root])
    finally:
        # Restore permissions so tmp_path cleanup can remove the tree.
        blocked.chmod(0o755)

    assert result.targets == [ok_file]
    assert len(result.errors) == 1
    assert result.errors[0][0] == blocked
