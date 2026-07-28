"""Tests for the GUI's source accumulation (:mod:`pptrepair.gui.sources`).

Skipped wholesale when PySide6 is not installed (the optional ``[gui]``
extra); see :mod:`tests.conftest` for the matching collection guard.
"""

from __future__ import annotations

import os
import stat as stat_module
import unicodedata
from collections.abc import Sequence
from pathlib import Path

import pytest

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import (
    QAction,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
)
from pytestqt.qtbot import QtBot

import pptrepair.gui.sources as sources_module
from pptrepair.gui.main_window import MainWindow
from pptrepair.gui.sources import (
    RejectedSource,
    RejectReason,
    SourceKind,
    SourceListModel,
    classify_source,
)

# --------------------------------------------------------------------------
# classify_source
# --------------------------------------------------------------------------


def test_classify_source_folder(tmp_path: Path) -> None:
    """A directory is classified as a folder source, regardless of name."""
    folder = tmp_path / "some_dir"
    folder.mkdir()
    assert classify_source(folder).kind is SourceKind.FOLDER


def test_classify_source_pptx_file(tmp_path: Path) -> None:
    """A ``.pptx`` file is classified as a file source."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    assert classify_source(path).kind is SourceKind.FILE


def test_classify_source_pptm_uppercase(tmp_path: Path) -> None:
    """A ``.PPTM`` file (uppercase suffix) is still classified as a file."""
    path = tmp_path / "macro.PPTM"
    path.write_bytes(b"")
    assert classify_source(path).kind is SourceKind.FILE


def test_classify_source_zip_archive(tmp_path: Path) -> None:
    """A ``.zip`` file is classified as an archive source."""
    path = tmp_path / "backup.zip"
    path.write_bytes(b"")
    assert classify_source(path).kind is SourceKind.ARCHIVE


def test_classify_source_tar_gz_archive(tmp_path: Path) -> None:
    """A ``.tar.gz`` file is classified as an archive source."""
    path = tmp_path / "backup.tar.gz"
    path.write_bytes(b"")
    assert classify_source(path).kind is SourceKind.ARCHIVE


def test_classify_source_unsupported_text_file(tmp_path: Path) -> None:
    """A plain ``.txt`` file is not classifiable."""
    path = tmp_path / "notes.txt"
    path.write_bytes(b"")
    classification = classify_source(path)
    assert classification.kind is None
    assert classification.reason is RejectReason.UNSUPPORTED_SUFFIX


def test_classify_source_nonexistent_path(tmp_path: Path) -> None:
    """A path that does not exist on disk is not classifiable."""
    classification = classify_source(tmp_path / "missing.pptx")
    assert classification.kind is None
    assert classification.reason is RejectReason.NOT_FOUND


def test_classify_source_legacy_ppt_file(tmp_path: Path) -> None:
    """A legacy ``.ppt`` file (binary format) is not classifiable."""
    path = tmp_path / "legacy.ppt"
    path.write_bytes(b"")
    classification = classify_source(path)
    assert classification.kind is None
    assert classification.reason is RejectReason.UNSUPPORTED_SUFFIX


# --------------------------------------------------------------------------
# classify_source: transient os.stat OSError handling
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fast_stat_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make :func:`classify_source`'s one-shot retry delay instant.

    Applies to every test in this module (including the ones above,
    which never hit the retry path) so a future test cannot
    accidentally reintroduce a real one-second sleep.
    """
    monkeypatch.setattr(sources_module, "_STAT_RETRY_DELAY_S", 0)


def _flaky_stat(real_stat, path_str, fail_times):
    """Return an ``os.stat`` replacement failing *fail_times* then real.

    Every call against *path_str* is counted, whether it raises or
    succeeds, so tests can assert the exact number of ``os.stat``
    attempts :func:`classify_source` made.

    :param real_stat: the original ``os.stat``, called once the
        induced failures are exhausted.
    :param path_str: the path (as a string) to fail on.
    :param fail_times: how many calls against *path_str* raise before
        deferring to *real_stat*.
    """
    calls = {"count": 0}

    def fake_stat(path, *args, **kwargs):
        if str(path) == path_str:
            calls["count"] += 1
            if calls["count"] <= fail_times:
                raise OSError(5, "Input/output error")
        return real_stat(path, *args, **kwargs)

    fake_stat.calls = calls
    return fake_stat


def _failing_open(real_open, path_str):
    """Return an ``os.open`` replacement failing with EIO on *path_str*.

    Disables the open-based fallback for one specific path, so a test
    can reach the ``os.stat`` retry flow that sits behind it.

    :param real_open: the original ``os.open``, used for other paths.
    :param path_str: the path (as a string) to fail on.
    """
    def fake_open(path, *args, **kwargs):
        if str(path) == path_str:
            raise OSError(5, "Input/output error")
        return real_open(path, *args, **kwargs)

    return fake_open


def _counting_open(real_open, path_str):
    """Return an ``os.open`` replacement counting calls on *path_str*.

    :param real_open: the original ``os.open``, always deferred to.
    :param path_str: the path (as a string) whose opens are counted.
    """
    calls = {"count": 0}

    def fake_open(path, *args, **kwargs):
        if str(path) == path_str:
            calls["count"] += 1
        return real_open(path, *args, **kwargs)

    fake_open.calls = calls
    return fake_open


def test_classify_source_retries_once_after_transient_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single transient ``os.stat`` OSError is retried, then succeeds."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    fake_stat = _flaky_stat(sources_module.os.stat, str(path), fail_times=1)
    monkeypatch.setattr(sources_module.os, "stat", fake_stat)

    classification = classify_source(path)

    assert classification.kind is SourceKind.FILE
    # The initial failing attempt plus exactly one retry, never a third.
    assert fake_stat.calls["count"] == 2


def test_classify_source_non_not_found_error_never_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An EIO stat failure is retried, not handed to the open fallback.

    Opening is reserved for the ENOENT pathology; on a wedged mount an
    extra ``os.open`` would only block the GUI thread a second time.
    """
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    fake_stat = _flaky_stat(sources_module.os.stat, str(path), fail_times=1)
    monkeypatch.setattr(sources_module.os, "stat", fake_stat)
    fake_open = _counting_open(sources_module.os.open, str(path))
    monkeypatch.setattr(sources_module.os, "open", fake_open)

    classification = classify_source(path)

    assert classification.kind is SourceKind.FILE
    assert fake_open.calls["count"] == 0
    assert fake_stat.calls["count"] == 2


def test_classify_source_transient_not_found_resolved_by_open_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient ENOENT (bouncing mount) is absorbed without a retry.

    ENOENT is the failure the open-based fallback exists for, and it
    runs before the retry, so a path that is really there is settled
    on the spot -- no second stat, and none of the retry's waiting.
    """
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    real_stat = sources_module.os.stat
    calls = {"count": 0}

    def fake_stat(target, *args, **kwargs):
        if str(target) == str(path):
            calls["count"] += 1
            if calls["count"] == 1:
                raise FileNotFoundError(2, "No such file or directory")
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(sources_module.os, "stat", fake_stat)
    sleep_spy = _SleepSpy()
    monkeypatch.setattr(sources_module, "time", sleep_spy)

    classification = classify_source(path)

    assert classification.kind is SourceKind.FILE
    assert classification.store_path is None
    assert calls["count"] == 1
    assert sleep_spy.calls == []


def test_classify_source_retries_once_when_open_fallback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With both fallbacks empty, a transient ENOENT still gets one retry."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    real_stat = sources_module.os.stat
    calls = {"count": 0}

    def fake_stat(target, *args, **kwargs):
        if str(target) == str(path):
            calls["count"] += 1
            if calls["count"] == 1:
                raise FileNotFoundError(2, "No such file or directory")
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(sources_module.os, "stat", fake_stat)
    monkeypatch.setattr(
        sources_module.os, "open",
        _failing_open(sources_module.os.open, str(path)))
    sleep_spy = _SleepSpy()
    monkeypatch.setattr(sources_module, "time", sleep_spy)

    classification = classify_source(path)

    assert classification.kind is SourceKind.FILE
    # The initial failing attempt plus exactly one retry, never a third.
    assert calls["count"] == 2
    assert len(sleep_spy.calls) == 1


def test_classify_source_persistent_os_error_accepts_archive_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistently unreachable ``.tar.gz`` is still accepted by name."""
    path = tmp_path / "backup.tar.gz"
    fake_stat = _flaky_stat(sources_module.os.stat, str(path), fail_times=99)
    monkeypatch.setattr(sources_module.os, "stat", fake_stat)

    classification = classify_source(path)

    assert classification.kind is SourceKind.ARCHIVE
    # Exactly one retry: the initial attempt plus one more, never a
    # third call.
    assert fake_stat.calls["count"] == 2


def test_classify_source_persistent_os_error_accepts_pptx_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistently unreachable ``.pptx`` is still accepted by name."""
    path = tmp_path / "deck.pptx"
    fake_stat = _flaky_stat(sources_module.os.stat, str(path), fail_times=99)
    monkeypatch.setattr(sources_module.os, "stat", fake_stat)

    classification = classify_source(path)

    assert classification.kind is SourceKind.FILE
    assert fake_stat.calls["count"] == 2


def test_classify_source_persistent_os_error_no_extension_is_access_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistently unreachable, unrecognisable name is an access error."""
    path = tmp_path / "mystery_item"
    fake_stat = _flaky_stat(sources_module.os.stat, str(path), fail_times=99)
    monkeypatch.setattr(sources_module.os, "stat", fake_stat)

    classification = classify_source(path)

    assert classification.kind is None
    assert classification.reason is RejectReason.ACCESS_ERROR
    assert "Input/output error" in classification.detail
    assert fake_stat.calls["count"] == 2


# --------------------------------------------------------------------------
# classify_source: Unicode normalization (NFC/NFD) recovery
# --------------------------------------------------------------------------

#: Root of the fake filesystem below. Deliberately a path no real
#: machine has, so :meth:`Path.resolve` leaves everything built under
#: it untouched and the tests never depend on the host filesystem.
_FAKE_ROOT = "/fake-nas"

#: Japanese names in both normalization forms. The GUI hands over the
#: NFC spelling (drops and file dialogs); the fake filesystem, like the
#: real SMB-mounted NAS, only knows the NFD one.
_ARCHIVE_NFC = unicodedata.normalize(
    "NFC", "Dropboxのバックアップ20210125.tar.gz")
_ARCHIVE_NFD = unicodedata.normalize("NFD", _ARCHIVE_NFC)
_DECK_NFC = unicodedata.normalize("NFC", "プレゼン資料.pptx")
_DECK_NFD = unicodedata.normalize("NFD", _DECK_NFC)
_DIR_NFC = unicodedata.normalize("NFC", "バックアップ")
_DIR_NFD = unicodedata.normalize("NFD", _DIR_NFC)


#: First descriptor number the fake filesystem hands out. Far above
#: anything the real interpreter holds, so a fake descriptor is never
#: confused with a real one by the shared ``os.fstat``/``os.close``
#: patches.
_FAKE_FD_BASE = 9000


class _FakeStat:
    """Minimal ``os.stat_result`` stand-in exposing only ``st_mode``."""

    def __init__(self, mode: int) -> None:
        self.st_mode = mode


class _FakeFsState:
    """Descriptor bookkeeping for the filesystem _install_fake_fs sets up.

    :ivar opened: the paths ``os.open`` was called with, in order.
    :ivar closed: the descriptors ``os.close`` was called with.
    :ivar issued: the descriptors ``os.open`` handed out.
    """

    def __init__(self) -> None:
        self.opened: list[str] = []
        self.closed: list[int] = []
        self.issued: list[int] = []


class _SleepSpy:
    """Stand-in for the ``time`` module recording every ``sleep`` call."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        """Record *seconds* instead of actually sleeping."""
        self.calls.append(seconds)


def _install_fake_fs(
    monkeypatch: pytest.MonkeyPatch,
    files: Sequence[str],
    *,
    stat_blind: bool = False,
    listdir_blind: bool = False,
    open_names: Sequence[str] = (),
    open_mode: int = stat_module.S_IFREG | 0o644,
) -> _FakeFsState:
    """Install a normalization-sensitive fake filesystem under _FAKE_ROOT.

    APFS matches file names across normalization forms, so a real
    temporary directory cannot reproduce the bugs these fallbacks
    exist for. Instead ``os.stat``/``os.lstat``/``os.listdir`` (plus
    ``os.open``/``os.fstat``/``os.close``) are patched to know *files*
    (absolute path strings, each in its exact on-disk spelling) plus
    their ancestor directories and to match them byte-exactly, the way
    SMB does: a differently normalised spelling of the very same name
    raises ``FileNotFoundError``. Paths outside _FAKE_ROOT are handed
    to the real functions, so the rest of the interpreter
    (``Path.resolve`` included) keeps working.

    :param monkeypatch: the active pytest monkeypatch fixture.
    :param files: on-disk spellings of the regular files to publish.
    :param stat_blind: when true, path-based ``os.stat`` fails with
        ENOENT for every path under the root, even a known one --
        the measured SMB state where only ``os.open`` still works.
        ``os.lstat`` stays honest, so normalization recovery can
        still walk the tree.
    :param listdir_blind: when true, listing any directory under the
        root fails with EIO, disabling normalization recovery.
    :param open_names: the spellings ``os.open`` accepts, regardless
        of what ``os.stat`` does; anything else raises ENOENT.
    :param open_mode: the ``st_mode`` ``os.fstat`` reports for a
        descriptor handed out by the fake ``os.open``.
    :returns: the descriptor bookkeeping for leak assertions.
    """
    # Guard the premise that the fake root cannot collide with reality
    # (checked before any patching, so it consults the real os).
    assert not Path(_FAKE_ROOT).exists()

    file_set = set(files)
    open_set = set(open_names)
    known_dirs: set[str] = set()
    for entry in file_set:
        for ancestor in Path(entry).parents:
            known_dirs.add(str(ancestor))

    state = _FakeFsState()
    real_stat = sources_module.os.stat
    real_lstat = sources_module.os.lstat
    real_listdir = sources_module.os.listdir
    real_open = sources_module.os.open
    real_fstat = sources_module.os.fstat
    real_close = sources_module.os.close

    def _lookup(text):
        if text in known_dirs:
            return _FakeStat(stat_module.S_IFDIR | 0o755)
        if text in file_set:
            return _FakeStat(stat_module.S_IFREG | 0o644)
        return None

    def _fake_stat_like(real_func, blind):
        def fake(path, *args, **kwargs):
            text = str(path)
            if not text.startswith(_FAKE_ROOT):
                return real_func(path, *args, **kwargs)
            result = None if blind else _lookup(text)
            if result is None:
                raise FileNotFoundError(2, "No such file or directory", text)
            return result

        return fake

    def fake_listdir(path=".", *args, **kwargs):
        text = str(path)
        if not text.startswith(_FAKE_ROOT):
            return real_listdir(path, *args, **kwargs)
        if listdir_blind:
            raise OSError(5, "Input/output error", text)
        if text not in known_dirs:
            raise NotADirectoryError(20, "Not a directory", text)
        return [
            Path(entry).name
            for entry in sorted(file_set | known_dirs)
            if entry != text and str(Path(entry).parent) == text
        ]

    def fake_open(path, *args, **kwargs):
        text = str(path)
        if not text.startswith(_FAKE_ROOT):
            return real_open(path, *args, **kwargs)
        if text not in open_set:
            raise FileNotFoundError(2, "No such file or directory", text)
        fd = _FAKE_FD_BASE + len(state.issued)
        state.opened.append(text)
        state.issued.append(fd)
        return fd

    def fake_fstat(fd, *args, **kwargs):
        if fd not in state.issued:
            return real_fstat(fd, *args, **kwargs)
        return _FakeStat(open_mode)

    def fake_close(fd, *args, **kwargs):
        if fd not in state.issued:
            return real_close(fd, *args, **kwargs)
        state.closed.append(fd)
        return None

    monkeypatch.setattr(
        sources_module.os, "stat", _fake_stat_like(real_stat, stat_blind))
    monkeypatch.setattr(
        sources_module.os, "lstat", _fake_stat_like(real_lstat, False))
    monkeypatch.setattr(sources_module.os, "listdir", fake_listdir)
    monkeypatch.setattr(sources_module.os, "open", fake_open)
    monkeypatch.setattr(sources_module.os, "fstat", fake_fstat)
    monkeypatch.setattr(sources_module.os, "close", fake_close)
    return state


def _install_unreachable_fs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every filesystem call under _FAKE_ROOT fail with EIO.

    Models a mount that is down rather than a normalization mismatch
    or a stat-blind server: the recovery's ``os.listdir`` and the
    open-based fallback fail too, so classification must fall back to
    the pre-existing retry-then-classify-by-name flow.

    :param monkeypatch: the active pytest monkeypatch fixture.
    """
    def _fake(real_func):
        def fake(path=".", *args, **kwargs):
            text = str(path)
            if not text.startswith(_FAKE_ROOT):
                return real_func(path, *args, **kwargs)
            raise OSError(5, "Input/output error", text)

        return fake

    for name in ("stat", "lstat", "listdir", "open"):
        monkeypatch.setattr(
            sources_module.os, name, _fake(getattr(sources_module.os, name)))


def test_normalization_fixtures_differ_between_forms() -> None:
    """The NFC and NFD spellings used below really are distinct strings."""
    assert _ARCHIVE_NFC != _ARCHIVE_NFD
    assert _DECK_NFC != _DECK_NFD
    assert _DIR_NFC != _DIR_NFD


def test_classify_source_recovers_nfd_archive_from_nfc_path(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An NFC-spelled ``.tar.gz`` finds its NFD twin and is accepted."""
    on_disk = f"{_FAKE_ROOT}/{_ARCHIVE_NFD}"
    _install_fake_fs(monkeypatch, [on_disk])

    classification = classify_source(Path(f"{_FAKE_ROOT}/{_ARCHIVE_NFC}"))

    assert classification.kind is SourceKind.ARCHIVE
    assert classification.reason is None
    assert classification.store_path == Path(on_disk)


def test_classify_source_recovers_nfd_pptx_from_nfc_path(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An NFC-spelled ``.pptx`` finds its NFD twin and is accepted."""
    on_disk = f"{_FAKE_ROOT}/{_DECK_NFD}"
    _install_fake_fs(monkeypatch, [on_disk])

    classification = classify_source(Path(f"{_FAKE_ROOT}/{_DECK_NFC}"))

    assert classification.kind is SourceKind.FILE
    assert classification.store_path == Path(on_disk)


def test_classify_source_recovers_every_mismatched_component(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An intermediate directory is respelled just like the file name."""
    on_disk = f"{_FAKE_ROOT}/{_DIR_NFD}/{_DECK_NFD}"
    _install_fake_fs(monkeypatch, [on_disk])

    classification = classify_source(
        Path(f"{_FAKE_ROOT}/{_DIR_NFC}/{_DECK_NFC}"))

    assert classification.kind is SourceKind.FILE
    assert classification.store_path == Path(on_disk)


def test_classify_source_recovery_does_not_invent_missing_files(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely absent path is still NOT_FOUND, not a near-miss match."""
    _install_fake_fs(monkeypatch, [f"{_FAKE_ROOT}/{_DECK_NFD}"])

    classification = classify_source(Path(f"{_FAKE_ROOT}/missing.pptx"))

    assert classification.kind is None
    assert classification.reason is RejectReason.NOT_FOUND
    assert classification.store_path is None


def test_classify_source_recovery_does_not_sleep(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovered path is accepted without waiting for the stat retry."""
    _install_fake_fs(monkeypatch, [f"{_FAKE_ROOT}/{_ARCHIVE_NFD}"])
    sleep_spy = _SleepSpy()
    monkeypatch.setattr(sources_module, "time", sleep_spy)

    classification = classify_source(Path(f"{_FAKE_ROOT}/{_ARCHIVE_NFC}"))

    assert classification.kind is SourceKind.ARCHIVE
    assert sleep_spy.calls == []


def test_classify_source_unreachable_mount_still_accepts_by_name(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failing stat, listing and open leave the retry-by-name flow intact."""
    _install_unreachable_fs(monkeypatch)
    sleep_spy = _SleepSpy()
    monkeypatch.setattr(sources_module, "time", sleep_spy)

    classification = classify_source(Path(f"{_FAKE_ROOT}/{_ARCHIVE_NFC}"))

    assert classification.kind is SourceKind.ARCHIVE
    assert classification.store_path is None
    # Neither fallback found anything, so the one-shot retry still ran.
    assert len(sleep_spy.calls) == 1


# --------------------------------------------------------------------------
# classify_source: open()/fstat() fallback for a stat-blind mount
# --------------------------------------------------------------------------


def test_classify_source_falls_back_to_open_when_stat_is_blind(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A path only ``os.open`` can reach is accepted through fstat."""
    # No spelling stats and the directory cannot be listed either, so
    # normalization recovery has nothing to offer -- but the dropped
    # (NFC) spelling opens, exactly as measured on the real mount.
    state = _install_fake_fs(
        monkeypatch,
        [f"{_FAKE_ROOT}/{_ARCHIVE_NFD}"],
        stat_blind=True,
        listdir_blind=True,
        open_names=[f"{_FAKE_ROOT}/{_ARCHIVE_NFC}"],
    )
    sleep_spy = _SleepSpy()
    monkeypatch.setattr(sources_module, "time", sleep_spy)

    classification = classify_source(Path(f"{_FAKE_ROOT}/{_ARCHIVE_NFC}"))

    assert classification.kind is SourceKind.ARCHIVE
    assert classification.store_path is None
    assert sleep_spy.calls == []
    # The descriptor was handed back before returning.
    assert state.closed == state.issued != []


def test_classify_source_opens_the_renormalized_spelling_first(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """When stat is blind but listing works, the on-disk spelling wins."""
    on_disk = f"{_FAKE_ROOT}/{_ARCHIVE_NFD}"
    # Both spellings open (as measured), so the stored one can only be
    # the respelled candidate if it is the one tried first.
    state = _install_fake_fs(
        monkeypatch,
        [on_disk],
        stat_blind=True,
        open_names=[f"{_FAKE_ROOT}/{_ARCHIVE_NFC}", on_disk],
    )
    sleep_spy = _SleepSpy()
    monkeypatch.setattr(sources_module, "time", sleep_spy)

    classification = classify_source(Path(f"{_FAKE_ROOT}/{_ARCHIVE_NFC}"))

    assert classification.kind is SourceKind.ARCHIVE
    assert classification.store_path == Path(on_disk)
    assert state.opened == [on_disk]
    assert sleep_spy.calls == []
    assert state.closed == state.issued != []


def test_classify_source_open_fallback_reports_a_directory(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An fstat describing a directory classifies as a folder source."""
    _install_fake_fs(
        monkeypatch,
        [f"{_FAKE_ROOT}/{_DIR_NFD}/{_DECK_NFD}"],
        stat_blind=True,
        listdir_blind=True,
        open_names=[f"{_FAKE_ROOT}/{_DIR_NFC}"],
        open_mode=stat_module.S_IFDIR | 0o755,
    )

    classification = classify_source(Path(f"{_FAKE_ROOT}/{_DIR_NFC}"))

    assert classification.kind is SourceKind.FOLDER
    assert classification.store_path is None


def test_classify_source_open_fallback_missing_path_is_not_found(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent path opens no better than it stats: still NOT_FOUND."""
    _install_fake_fs(
        monkeypatch, [f"{_FAKE_ROOT}/{_DECK_NFD}"], stat_blind=True)
    sleep_spy = _SleepSpy()
    monkeypatch.setattr(sources_module, "time", sleep_spy)

    classification = classify_source(Path(f"{_FAKE_ROOT}/missing_item"))

    assert classification.kind is None
    assert classification.reason is RejectReason.NOT_FOUND
    assert len(sleep_spy.calls) == 1


def test_fstat_via_open_closes_the_descriptor_when_fstat_fails(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing fstat still hands the descriptor back (no fd leak)."""
    closed: list[int] = []
    monkeypatch.setattr(sources_module.os, "open", lambda *a, **kw: 4242)

    def failing_fstat(fd, *args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(sources_module.os, "fstat", failing_fstat)
    monkeypatch.setattr(sources_module.os, "close", closed.append)

    assert sources_module._fstat_via_open(Path("/whatever")) is None
    assert closed == [4242]


def test_fstat_via_open_closes_the_descriptor_on_success(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """The descriptor opened for metadata is closed on the happy path too.

    Also pins the open flags: read-only, and non-blocking so a FIFO
    cannot park the caller waiting for a writer.
    """
    closed: list[int] = []
    opened: list[tuple[object, int]] = []
    expected = _FakeStat(stat_module.S_IFREG | 0o644)

    def fake_open(path, flags, *args, **kwargs):
        opened.append((path, flags))
        return 4243

    monkeypatch.setattr(sources_module.os, "open", fake_open)
    monkeypatch.setattr(
        sources_module.os, "fstat", lambda fd, *a, **kw: expected)
    monkeypatch.setattr(sources_module.os, "close", closed.append)

    assert sources_module._fstat_via_open(Path("/whatever")) is expected
    assert closed == [4243]
    assert opened == [(Path("/whatever"), os.O_RDONLY | os.O_NONBLOCK)]


# --------------------------------------------------------------------------
# SourceListModel
# --------------------------------------------------------------------------


def test_add_paths_adds_and_counts_row(tmp_path: Path) -> None:
    """Adding a supported file appends one row and reports it as added."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")

    result = model.add_paths([path])

    assert model.rowCount() == 1
    assert len(result.added) == 1
    assert result.added[0].path == path.resolve()
    assert result.added[0].kind is SourceKind.FILE
    assert result.duplicates == []
    assert result.rejected == []


def test_add_paths_detects_exact_duplicate(tmp_path: Path) -> None:
    """Adding the same path twice reports the second as a duplicate."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")

    model.add_paths([path])
    result = model.add_paths([path])

    assert model.rowCount() == 1
    assert result.added == []
    assert result.duplicates == [path]


def test_add_paths_detects_duplicate_via_dot_dot(tmp_path: Path) -> None:
    """A ``sub/..``-relative path resolving to an existing entry is a dup."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    (tmp_path / "sub").mkdir()
    indirect_path = tmp_path / "sub" / ".." / "deck.pptx"

    model.add_paths([path])
    result = model.add_paths([indirect_path])

    assert model.rowCount() == 1
    assert result.added == []
    assert result.duplicates == [indirect_path]


def test_add_paths_stores_on_disk_spelling_and_dedups_across_forms(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovered NFC drop is stored as NFD; the NFD drop is a duplicate."""
    on_disk = f"{_FAKE_ROOT}/{_ARCHIVE_NFD}"
    _install_fake_fs(monkeypatch, [on_disk])
    model = SourceListModel()

    first = model.add_paths([Path(f"{_FAKE_ROOT}/{_ARCHIVE_NFC}")])
    second = model.add_paths([Path(on_disk)])

    assert model.rowCount() == 1
    assert [entry.path for entry in model.entries()] == [Path(on_disk)]
    assert first.added[0].kind is SourceKind.ARCHIVE
    assert second.added == []
    assert second.duplicates == [Path(on_disk)]


def test_add_paths_rejects_unsupported(tmp_path: Path) -> None:
    """An unsupported path is reported as rejected, not added."""
    model = SourceListModel()
    path = tmp_path / "notes.txt"
    path.write_bytes(b"")

    result = model.add_paths([path])

    assert model.rowCount() == 0
    assert result.added == []
    assert len(result.rejected) == 1
    assert result.rejected[0].path == path
    assert result.rejected[0].reason is RejectReason.UNSUPPORTED_SUFFIX


def test_display_role_shows_plain_path_for_file(tmp_path: Path) -> None:
    """A file entry's display text is its plain resolved path."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    model.add_paths([path])

    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == str(path.resolve())


def test_display_role_shows_folder_suffix(tmp_path: Path) -> None:
    """A folder entry's display text ends with the ``[folder]`` tag."""
    model = SourceListModel()
    folder = tmp_path / "some_dir"
    folder.mkdir()
    model.add_paths([folder])

    index = model.index(0, 0)
    text = model.data(index, Qt.ItemDataRole.DisplayRole)
    assert text == f"{folder.resolve()} [folder]"


def test_display_role_shows_archive_suffix(tmp_path: Path) -> None:
    """An archive entry's display text ends with the ``[archive]`` tag."""
    model = SourceListModel()
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"")
    model.add_paths([archive])

    index = model.index(0, 0)
    text = model.data(index, Qt.ItemDataRole.DisplayRole)
    assert text == f"{archive.resolve()} [archive]"


def test_remove_rows_multiple_reverse_order_safe(tmp_path: Path) -> None:
    """Removing several rows works regardless of the order they're given in."""
    model = SourceListModel()
    paths = []
    for index in range(4):
        path = tmp_path / f"deck{index}.pptx"
        path.write_bytes(b"")
        paths.append(path)
    model.add_paths(paths)

    # Ascending order deliberately, to exercise the descending-sort guard.
    model.remove_rows([0, 2])

    remaining = [entry.path for entry in model.entries()]
    assert remaining == [paths[1].resolve(), paths[3].resolve()]


def test_clear_empties_the_model(tmp_path: Path) -> None:
    """clear() removes every entry from the model."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    model.add_paths([path])

    model.clear()

    assert model.rowCount() == 0
    assert model.entries() == []


def test_entries_returns_a_copy(tmp_path: Path) -> None:
    """entries() returns a list that mutating does not affect the model."""
    model = SourceListModel()
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    model.add_paths([path])

    entries = model.entries()
    entries.clear()

    assert model.rowCount() == 1


# --------------------------------------------------------------------------
# MainWindow integration: drag-and-drop and menu wiring
# --------------------------------------------------------------------------


@pytest.fixture
def main_window(qtbot: QtBot) -> MainWindow:
    """Build a :class:`MainWindow`, registered with *qtbot* for cleanup."""
    window = MainWindow()
    qtbot.addWidget(window)
    return window


@pytest.fixture(autouse=True)
def _suppress_reject_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise :meth:`MainWindow._show_reject_details` by default.

    ``_register_add_result`` now shows a real, modal ``QMessageBox``
    whenever something is rejected; under the offscreen Qt platform
    that dialog has nothing to click and would hang any test that
    triggers it without expecting to. Applied module-wide so every
    existing drag-and-drop test keeps working unchanged;
    ``test_register_add_result_shows_reject_details_when_rejected``
    below overrides this method itself (on the instance, after this
    fixture has patched the class) to verify the wiring.
    """
    monkeypatch.setattr(
        MainWindow, "_show_reject_details", lambda self, rejected: None)


def _make_drop_mime_data(paths: list[Path]) -> QMimeData:
    """Build a :class:`QMimeData` carrying local file URLs for *paths*."""
    mime_data = QMimeData()
    mime_data.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
    return mime_data


def test_drag_enter_accepts_local_file_urls(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """dragEnterEvent accepts a drag carrying at least one local file URL."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    mime_data = _make_drop_mime_data([path])
    event = QDragEnterEvent(
        QPoint(0, 0),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )

    main_window.dragEnterEvent(event)

    assert event.isAccepted()


def test_drop_event_adds_sources_and_updates_status_bar(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """dropEvent hands dropped files to the model and reports the summary."""
    good_path = tmp_path / "deck.pptx"
    good_path.write_bytes(b"")
    bad_path = tmp_path / "notes.txt"
    bad_path.write_bytes(b"")
    mime_data = _make_drop_mime_data([good_path, bad_path])
    event = QDropEvent(
        QPoint(0, 0),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )

    main_window.dropEvent(event)

    assert event.isAccepted()
    assert main_window._sources.rowCount() == 1
    message = main_window.statusBar().currentMessage()
    assert "Added 1 source(s)" in message
    assert "1 unsupported item(s) rejected" in message
    # No duplicates on this first drop, so that breakdown item is omitted.
    assert "duplicate" not in message


def test_drop_event_reports_duplicate_breakdown(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """A second drop of an already-added path is reported as a duplicate."""
    path = tmp_path / "deck.pptx"
    path.write_bytes(b"")
    main_window._sources.add_paths([path])

    mime_data = _make_drop_mime_data([path])
    event = QDropEvent(
        QPoint(0, 0),
        Qt.DropAction.CopyAction,
        mime_data,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    main_window.dropEvent(event)

    message = main_window.statusBar().currentMessage()
    assert "1 duplicate(s) skipped" in message
    assert "Added" not in message


def test_file_menu_has_source_management_actions(
    main_window: MainWindow,
) -> None:
    """The File menu exposes Add Files…/Add Folder…/Clear Sources actions."""
    file_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "File":
            file_menu = action.menu()
            break
    assert file_menu is not None

    titles = [action.text() for action in file_menu.actions()]
    assert "Add Files…" in titles
    assert "Add Folder…" in titles
    assert "Clear Sources" in titles
    assert "Quit" in titles


def test_add_files_action_has_open_shortcut(main_window: MainWindow) -> None:
    """The "Add Files…" action is bound to the standard Open shortcut."""
    file_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "File":
            file_menu = action.menu()
            break
    assert file_menu is not None

    add_files_action = None
    for action in file_menu.actions():
        if action.text() == "Add Files…":
            add_files_action = action
            break
    assert isinstance(add_files_action, QAction)
    assert add_files_action.shortcut() == QKeySequence(
        QKeySequence.StandardKey.Open
    )


# --------------------------------------------------------------------------
# MainWindow._format_reject_details / _show_reject_details
# --------------------------------------------------------------------------


def test_format_reject_details_not_found(tmp_path: Path) -> None:
    """A NOT_FOUND rejection is described as "not found"."""
    path = tmp_path / "missing.pptx"
    rejected = [RejectedSource(path, RejectReason.NOT_FOUND)]

    text = MainWindow._format_reject_details(rejected)

    assert text == f"{path} — not found"


def test_format_reject_details_access_error(tmp_path: Path) -> None:
    """An ACCESS_ERROR rejection includes the underlying detail text."""
    path = tmp_path / "mystery_item"
    rejected = [
        RejectedSource(path, RejectReason.ACCESS_ERROR, detail="I/O error")]

    text = MainWindow._format_reject_details(rejected)

    assert text == f"{path} — could not be accessed (I/O error)"


def test_format_reject_details_unsupported_suffix(tmp_path: Path) -> None:
    """An UNSUPPORTED_SUFFIX rejection is described as "unsupported"."""
    path = tmp_path / "notes.txt"
    rejected = [RejectedSource(path, RejectReason.UNSUPPORTED_SUFFIX)]

    text = MainWindow._format_reject_details(rejected)

    assert text == f"{path} — unsupported file type"


def test_format_reject_details_joins_multiple_lines(tmp_path: Path) -> None:
    """Several rejections are rendered as one line each, newline-joined."""
    missing = tmp_path / "missing.pptx"
    unsupported = tmp_path / "notes.txt"
    rejected = [
        RejectedSource(missing, RejectReason.NOT_FOUND),
        RejectedSource(unsupported, RejectReason.UNSUPPORTED_SUFFIX),
    ]

    text = MainWindow._format_reject_details(rejected)

    assert text == (
        f"{missing} — not found\n{unsupported} — unsupported file type")


def test_register_add_result_shows_reject_details_when_rejected(
    main_window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_register_add_result calls _show_reject_details on any rejection."""
    calls: list[Sequence[RejectedSource]] = []
    monkeypatch.setattr(
        main_window, "_show_reject_details",
        lambda rejected: calls.append(rejected))
    bad_path = tmp_path / "notes.txt"
    bad_path.write_bytes(b"")

    result = main_window._sources.add_paths([bad_path])
    main_window._register_add_result(result)

    assert calls == [result.rejected]


def test_register_add_result_skips_reject_details_when_nothing_rejected(
    main_window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_register_add_result leaves _show_reject_details untouched otherwise."""
    calls: list[Sequence[RejectedSource]] = []
    monkeypatch.setattr(
        main_window, "_show_reject_details",
        lambda rejected: calls.append(rejected))
    good_path = tmp_path / "deck.pptx"
    good_path.write_bytes(b"")

    result = main_window._sources.add_paths([good_path])
    main_window._register_add_result(result)

    assert calls == []
