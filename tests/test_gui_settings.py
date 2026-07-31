"""Tests for the GUI's persisted preferences (:mod:`pptrepair.gui.settings`).

Covers the typed :class:`~pptrepair.gui.settings.Settings` wrapper, its
:class:`~pptrepair.gui.settings.SettingsDialog` form,
:meth:`~pptrepair.gui.run_options.RunOptionsPanel.apply_settings` and the
Run/Settings-menu and recent-folders wiring added to
:class:`~pptrepair.gui.main_window.MainWindow`. Skipped wholesale when
PySide6 is not installed (the optional ``[gui]`` extra); see
:mod:`tests.conftest` for the matching collection guard.

Every :class:`~pptrepair.gui.settings.Settings` instance below is built
through :func:`_open_settings`, backed by a scratch ini file under
*tmp_path*, so no test ever touches the real per-user preferences store.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PySide6 = pytest.importorskip("PySide6")

# Force the offscreen Qt platform plugin before any widget is created, so
# the suite runs headlessly (e.g. in CI, with no display available).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction
from pytestqt.qtbot import QtBot

from pptrepair.gui.main_window import MainWindow
from pptrepair.gui.run_options import RepairMode, RunOptionsPanel
from pptrepair.gui.settings import Settings, SettingsDialog

# --------------------------------------------------------------------------
# fixture helpers
# --------------------------------------------------------------------------


def _open_settings(path: Path) -> Settings:
    """Return a :class:`Settings` backed by the ini file at *path*.

    Injects an explicit ``IniFormat`` backend so the real, per-user
    preferences store is never touched.
    """
    backend = QSettings(str(path), QSettings.Format.IniFormat)
    return Settings(backend)


def _write_and_reload(tmp_path: Path, mutate) -> Settings:
    """Write through one :class:`Settings`, then read it back afresh.

    *mutate* is called with a freshly opened :class:`Settings`; that
    instance's backend is explicitly synced to disk before a second,
    independent :class:`Settings` (over the same ini file) is opened
    and returned, so the assertions that follow exercise a genuine
    round trip through storage rather than an in-memory cache.
    """
    path = tmp_path / "settings.ini"
    writer = _open_settings(path)
    mutate(writer)
    writer._settings.sync()
    return _open_settings(path)


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def test_settings_defaults(tmp_path: Path) -> None:
    """A fresh Settings reports every documented default."""
    settings = _open_settings(tmp_path / "settings.ini")
    assert settings.language() == "en"
    assert settings.max_file_bytes() == 2_147_483_648
    assert settings.allow_download() is False
    assert settings.ignore_hidden() is True
    assert settings.follow_symlinks() is False
    assert settings.include_filenames() is False
    assert settings.output_in_place() is True
    assert settings.output_dir() == ""
    assert settings.repair_mode() == "single"
    assert settings.recent_folders() == []


def test_settings_language_roundtrips_through_disk(tmp_path: Path) -> None:
    """set_language()/language() round-trip a supported code through disk."""
    reloaded = _write_and_reload(
        tmp_path, lambda s: s.set_language("ja"))
    assert reloaded.language() == "ja"


def test_settings_language_falls_back_for_unsupported_code(
    tmp_path: Path,
) -> None:
    """An unsupported stored language code falls back to "en"."""
    reloaded = _write_and_reload(
        tmp_path, lambda s: s.set_language("xx"))
    assert reloaded.language() == "en"


def test_settings_max_file_bytes_roundtrips_as_int(tmp_path: Path) -> None:
    """A positive ceiling round-trips as the same Python int through disk."""
    reloaded = _write_and_reload(
        tmp_path, lambda s: s.set_max_file_bytes(524_288_000))
    value = reloaded.max_file_bytes()
    assert value == 524_288_000
    assert isinstance(value, int)


def test_settings_max_file_bytes_none_roundtrips_as_none(
    tmp_path: Path,
) -> None:
    """None (no limit), stored internally as 0, reloads as None."""
    reloaded = _write_and_reload(
        tmp_path, lambda s: s.set_max_file_bytes(None))
    assert reloaded.max_file_bytes() is None


@pytest.mark.parametrize(
    ("setter", "getter"),
    [
        ("set_allow_download", "allow_download"),
        ("set_ignore_hidden", "ignore_hidden"),
        ("set_follow_symlinks", "follow_symlinks"),
        ("set_include_filenames", "include_filenames"),
        ("set_output_in_place", "output_in_place"),
    ],
)
def test_settings_bool_flags_roundtrip_as_bool(
    tmp_path: Path, setter: str, getter: str
) -> None:
    """Every boolean flag reloads as an actual Python bool, not a string.

    Guards against QSettings' documented type-degradation risk (a
    stored True round-tripping as the string "true").
    """
    reloaded = _write_and_reload(
        tmp_path, lambda s: getattr(s, setter)(True))
    value = getattr(reloaded, getter)()
    assert value is True

    reloaded_false = _write_and_reload(
        tmp_path, lambda s: getattr(s, setter)(False))
    assert getattr(reloaded_false, getter)() is False


def test_settings_output_dir_roundtrips(tmp_path: Path) -> None:
    """The default output folder round-trips as the same string."""
    target = str(tmp_path / "out")
    reloaded = _write_and_reload(
        tmp_path, lambda s: s.set_output_dir(target))
    assert reloaded.output_dir() == target


def test_settings_repair_mode_roundtrips(tmp_path: Path) -> None:
    """A valid repair mode string round-trips unchanged."""
    reloaded = _write_and_reload(
        tmp_path, lambda s: s.set_repair_mode("multi"))
    assert reloaded.repair_mode() == "multi"


def test_settings_repair_mode_falls_back_for_invalid_value(
    tmp_path: Path,
) -> None:
    """An unrecognised stored repair mode falls back to "single"."""
    reloaded = _write_and_reload(
        tmp_path, lambda s: s.set_repair_mode("bogus"))
    assert reloaded.repair_mode() == "single"


def test_settings_recent_folders_moves_duplicate_to_front(
    tmp_path: Path,
) -> None:
    """Re-pushing an already-remembered folder moves it to the front."""
    settings = _open_settings(tmp_path / "settings.ini")
    settings.push_recent_folder(Path("/a/b"))
    settings.push_recent_folder(Path("/c/d"))
    settings.push_recent_folder(Path("/a/b"))
    assert settings.recent_folders() == ["/a/b", "/c/d"]


def test_settings_recent_folders_capped_at_ten(tmp_path: Path) -> None:
    """Pushing more than ten folders keeps only the ten most recent."""
    settings = _open_settings(tmp_path / "settings.ini")
    for index in range(12):
        settings.push_recent_folder(Path(f"/x/{index}"))
    folders = settings.recent_folders()
    assert len(folders) == 10
    assert folders[0] == "/x/11"
    assert "/x/0" not in folders
    assert "/x/1" not in folders


def test_settings_recent_folders_roundtrip_through_disk(
    tmp_path: Path,
) -> None:
    """Several remembered folders survive a genuine reload from disk."""
    def _mutate(settings: Settings) -> None:
        settings.push_recent_folder(Path("/a/b"))
        settings.push_recent_folder(Path("/c/d"))

    reloaded = _write_and_reload(tmp_path, _mutate)
    assert reloaded.recent_folders() == ["/c/d", "/a/b"]


def test_settings_recent_folders_single_entry_roundtrips_as_list(
    tmp_path: Path,
) -> None:
    """One remembered folder still reloads as a one-element list.

    Some QSettings backends round-trip a single stored list entry as
    a bare string instead of a one-element list; :meth:`recent_folders`
    normalises that back.
    """
    reloaded = _write_and_reload(
        tmp_path, lambda s: s.push_recent_folder(Path("/only")))
    assert reloaded.recent_folders() == ["/only"]


def test_settings_clear_recent_folders(tmp_path: Path) -> None:
    """clear_recent_folders() empties the remembered list."""
    settings = _open_settings(tmp_path / "settings.ini")
    settings.push_recent_folder(Path("/a/b"))
    settings.clear_recent_folders()
    assert settings.recent_folders() == []


# --------------------------------------------------------------------------
# SettingsDialog
# --------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Build an isolated :class:`Settings`, registered with *tmp_path*."""
    return _open_settings(tmp_path / "settings.ini")


def test_settings_dialog_loads_current_values(
    qtbot: QtBot, settings: Settings, tmp_path: Path
) -> None:
    """The dialog's controls reflect *settings*' current values on open."""
    settings.set_language("ja")
    settings.set_allow_download(True)
    settings.set_follow_symlinks(True)
    settings.set_include_filenames(True)
    settings.set_repair_mode("multi")
    settings.set_output_in_place(False)
    settings.set_output_dir(str(tmp_path))
    settings.set_max_file_bytes(None)

    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)

    assert dialog._language_combo.currentData() == "ja"
    assert dialog._download_check.isChecked() is True
    assert dialog._symlinks_check.isChecked() is True
    assert dialog._filenames_check.isChecked() is True
    assert dialog._mode_combo.currentData() is RepairMode.MULTI
    assert dialog._into_folder_radio.isChecked() is True
    assert dialog._output_edit.text() == str(tmp_path)
    assert dialog._no_limit_check.isChecked() is True


def test_settings_dialog_accept_writes_settings(
    qtbot: QtBot, settings: Settings, tmp_path: Path
) -> None:
    """Confirming the dialog writes every control's value to *settings*."""
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)

    dialog._language_combo.setCurrentIndex(
        dialog._language_combo.findData("ja"))
    dialog._no_limit_check.setChecked(False)
    dialog._size_spin.setValue(500)
    dialog._unit_combo.setCurrentText("MB")
    dialog._download_check.setChecked(True)
    dialog._symlinks_check.setChecked(True)
    dialog._filenames_check.setChecked(True)
    dialog._into_folder_radio.setChecked(True)
    dialog._output_edit.setText(str(tmp_path))
    dialog._mode_combo.setCurrentIndex(
        dialog._mode_combo.findData(RepairMode.MULTI))

    dialog.accept()

    assert settings.language() == "ja"
    assert settings.max_file_bytes() == 524_288_000
    assert settings.allow_download() is True
    assert settings.follow_symlinks() is True
    assert settings.include_filenames() is True
    assert settings.output_in_place() is False
    assert settings.output_dir() == str(tmp_path)
    assert settings.repair_mode() == "multi"


def test_settings_dialog_reject_leaves_settings_untouched(
    qtbot: QtBot, settings: Settings
) -> None:
    """Cancelling the dialog leaves the injected Settings unmodified."""
    dialog = SettingsDialog(settings)
    qtbot.addWidget(dialog)

    dialog._download_check.setChecked(True)
    dialog.reject()

    assert settings.allow_download() is False


# --------------------------------------------------------------------------
# RunOptionsPanel.apply_settings
# --------------------------------------------------------------------------


@pytest.fixture
def run_options(qtbot: QtBot) -> RunOptionsPanel:
    """Build a :class:`RunOptionsPanel`, registered with *qtbot*."""
    panel = RunOptionsPanel()
    qtbot.addWidget(panel)
    return panel


def test_apply_settings_updates_mode_download_and_output(
    run_options: RunOptionsPanel, settings: Settings, tmp_path: Path
) -> None:
    """apply_settings() reflects the mode/download/output defaults."""
    settings.set_repair_mode("multi")
    settings.set_allow_download(True)
    settings.set_output_in_place(False)
    settings.set_output_dir(str(tmp_path))

    run_options.apply_settings(settings)

    assert run_options.repair_mode() is RepairMode.MULTI
    assert run_options.allow_download() is True
    assert run_options.in_place() is False
    assert run_options.output_dir() == tmp_path


def test_apply_settings_updates_no_limit(
    run_options: RunOptionsPanel, settings: Settings
) -> None:
    """apply_settings() checks "No limit" when settings has no ceiling."""
    settings.set_max_file_bytes(None)

    run_options.apply_settings(settings)

    assert run_options.max_file_bytes() is None
    assert run_options._no_limit_check.isChecked() is True


def test_apply_settings_updates_size_spin_and_unit(
    run_options: RunOptionsPanel, settings: Settings
) -> None:
    """apply_settings() converts a non-GB-aligned ceiling to MB."""
    settings.set_max_file_bytes(500 * 1024 ** 2)

    run_options.apply_settings(settings)

    assert run_options.max_file_bytes() == 500 * 1024 ** 2
    assert run_options._unit_combo.currentText() == "MB"
    assert run_options._size_spin.value() == 500


# --------------------------------------------------------------------------
# MainWindow integration: Run/Settings menus and Recent Folders
# --------------------------------------------------------------------------


@pytest.fixture
def main_window(qtbot: QtBot, tmp_path: Path) -> MainWindow:
    """Build a :class:`MainWindow`, its settings swapped for an isolated
    ini-backed store so this test never touches the real, per-user
    preferences.

    Registered with *qtbot* for cleanup.
    """
    window = MainWindow()
    qtbot.addWidget(window)
    window._settings = _open_settings(tmp_path / "settings.ini")
    return window


def test_run_menu_has_scan_repair_cancel_actions(
    main_window: MainWindow,
) -> None:
    """The Run menu lists Scan, Repair and Cancel, in that order.

    Uses a plain, inline ``for`` loop (rather than a helper function
    returning ``action.menu()``) to look up the Run menu: an
    intermediate scope's teardown has been observed to invalidate the
    ``QMenu`` handle before it is used -- the same class of issue
    :meth:`MainWindow._build_separator` already documents and works
    around.
    """
    run_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "Run":
            run_menu = action.menu()
            break
    assert run_menu is not None

    titles = [action.text() for action in run_menu.actions()]
    assert titles == ["Scan", "Repair", "Cancel"]


def test_scan_action_enabled_state_matches_scan_button(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """The Run menu's Scan action tracks the Scan button's enabled state."""
    assert main_window._scan_action.isEnabled() is False
    assert (main_window._scan_action.isEnabled()
            == main_window._scan_button.isEnabled())

    deck = tmp_path / "deck.pptx"
    deck.write_bytes(b"")
    main_window._sources.add_paths([deck])

    assert main_window._scan_action.isEnabled() is True
    assert (main_window._scan_action.isEnabled()
            == main_window._scan_button.isEnabled())


def test_repair_action_disabled_like_repair_button(
    main_window: MainWindow,
) -> None:
    """The Repair action stays disabled, mirroring the Repair button."""
    assert main_window._repair_action.isEnabled() is False
    assert main_window._repair_action.isEnabled() == \
        main_window._repair_button.isEnabled()


def test_settings_menu_has_preferences_action(
    main_window: MainWindow,
) -> None:
    """The Settings menu exposes a Preferences… action with its own role.

    See :func:`test_run_menu_has_scan_repair_cancel_actions` for why
    the Settings menu lookup below is an inline loop.
    """
    settings_menu = None
    for action in main_window.menuBar().actions():
        if action.text() == "Settings":
            settings_menu = action.menu()
            break
    assert settings_menu is not None

    preferences_action = None
    for action in settings_menu.actions():
        if action.text() == "Preferences…":
            preferences_action = action
            break
    assert preferences_action is not None
    assert preferences_action.menuRole() == QAction.MenuRole.PreferencesRole


def test_recent_folders_menu_starts_empty(main_window: MainWindow) -> None:
    """The Recent Folders submenu shows a disabled "(empty)" placeholder."""
    main_window._rebuild_recent_folders_menu()
    actions = main_window._recent_folders_menu.actions()
    assert [action.text() for action in actions] == ["(empty)"]
    assert actions[0].isEnabled() is False


def test_recent_folders_menu_lists_a_newly_added_folder(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """A folder added through the source list is remembered and listed."""
    folder = tmp_path / "sub"
    folder.mkdir()
    result = main_window._sources.add_paths([folder])
    main_window._register_add_result(result)

    main_window._rebuild_recent_folders_menu()
    titles = [action.text()
              for action in main_window._recent_folders_menu.actions()]
    assert str(folder.resolve()) in titles
    assert "Clear Menu" in titles


def test_recent_folders_menu_clear_menu_empties_settings(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """The "Clear Menu" action forgets every remembered folder."""
    folder = tmp_path / "sub"
    folder.mkdir()
    result = main_window._sources.add_paths([folder])
    main_window._register_add_result(result)
    assert main_window._settings.recent_folders() != []

    main_window._rebuild_recent_folders_menu()
    clear_action = next(
        action for action in main_window._recent_folders_menu.actions()
        if action.text() == "Clear Menu")
    clear_action.trigger()

    assert main_window._settings.recent_folders() == []


def test_recent_folder_menu_item_adds_it_back_to_sources(
    main_window: MainWindow, tmp_path: Path
) -> None:
    """Clicking a Recent Folders entry re-adds that folder to the sources."""
    folder = tmp_path / "sub"
    folder.mkdir()
    result = main_window._sources.add_paths([folder])
    main_window._register_add_result(result)
    main_window._sources.clear()
    assert main_window._sources.rowCount() == 0

    main_window._rebuild_recent_folders_menu()
    folder_action = next(
        action for action in main_window._recent_folders_menu.actions()
        if action.text() == str(folder.resolve()))
    folder_action.trigger()

    assert main_window._sources.rowCount() == 1
