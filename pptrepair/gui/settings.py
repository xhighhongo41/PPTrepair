"""Persisted preferences for the PySide6 desktop application.

Wraps :class:`~PySide6.QtCore.QSettings` in a typed :class:`Settings`
facade -- one accessor pair per remembered preference, coercing every
value read back to its declared Python type -- and provides the
:class:`SettingsDialog` form the user edits those preferences through.
:class:`~pptrepair.gui.main_window.MainWindow` owns the single
long-lived :class:`Settings` instance; other panels only ever see it
through :meth:`~pptrepair.gui.run_options.RunOptionsPanel.apply_settings`.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

# Reusing RunOptionsPanel's own byte<->spin/unit conversion keeps the
# Preferences dialog's size fields on exactly the same base-1024
# MB/GB convention as the main run panel. Importing these private
# names is an intentional same-package reuse (see run_options.py),
# not a public API; RepairMode is that module's public enum.
from pptrepair.gui.i18n import tr
from pptrepair.gui.run_options import (
    _UNIT_FACTORS,
    RepairMode,
    _bytes_to_spin_unit,
)

#: Supported UI languages: code -> display name, in the order the
#: language combo box lists them. Display names are always shown in
#: their own language, never translated. A stored choice only takes
#: effect after the application restarts (see
#: :mod:`pptrepair.gui.i18n`).
_LANGUAGE_NAMES = {
    "en": "English",
    "ja": "日本語",
    "zh": "中文",
    "ko": "한국어",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
}

#: Default per-file size ceiling: 2 GiB, matching RunOptionsPanel's
#: own hard-coded default.
_DEFAULT_MAX_FILE_BYTES = 2 * 1024 ** 3

#: Maximum number of remembered recent folders.
_MAX_RECENT_FOLDERS = 10

#: QSettings keys. The recent-folders list is deliberately kept out of
#: the "preferences/" group since it is not an edited preference.
_KEY_LANGUAGE = "preferences/language"
_KEY_MAX_FILE_BYTES = "preferences/max_file_bytes"
_KEY_ALLOW_DOWNLOAD = "preferences/allow_download"
_KEY_IGNORE_HIDDEN = "preferences/ignore_hidden"
_KEY_FOLLOW_SYMLINKS = "preferences/follow_symlinks"
_KEY_INCLUDE_FILENAMES = "preferences/include_filenames"
_KEY_OUTPUT_IN_PLACE = "preferences/output_in_place"
_KEY_OUTPUT_DIR = "preferences/output_dir"
_KEY_REPAIR_MODE = "preferences/repair_mode"
_KEY_RECENT_FOLDERS = "recent_folders"


def _as_bool(value: object, default: bool) -> bool:
    """Coerce a value read back from :class:`QSettings` to a bool.

    Handles a native Python bool (most backends round-trip one
    faithfully) as well as the ``"true"``/``"false"`` strings an
    ini-format read has been observed to return instead.

    :param value: the raw value :meth:`QSettings.value` returned.
    :param default: returned for any value that is neither a bool nor
        a recognisable boolean string.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return default


def _as_int(value: object, default: int) -> int:
    """Coerce a value read back from :class:`QSettings` to an int.

    :param value: the raw value :meth:`QSettings.value` returned.
    :param default: returned when *value* cannot be converted.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Settings:
    """Typed, round-trip-safe wrapper around :class:`QSettings`.

    One accessor pair per remembered preference (``language()`` /
    ``set_language()`` and so on), plus :meth:`push_recent_folder` and
    :meth:`clear_recent_folders` for the most-recently-used folder
    list. Every getter coerces the raw stored value back to its
    declared Python type rather than trusting it, since QSettings has
    been observed to degrade some types (bool, int) to plain strings
    depending on platform and backend.
    """

    def __init__(self, backend: QSettings | None = None) -> None:
        """Wrap *backend*, or a default-constructed :class:`QSettings`.

        :param backend: the settings store to read/write through;
            typically an ini-format :class:`QSettings` pointed at a
            scratch file in tests, so the real per-user preferences
            store is never touched. None uses :class:`QSettings`'
            own defaults, which rely on the organisation/application
            name set once in :mod:`pptrepair.gui.app`.
        """
        self._settings = backend if backend is not None else QSettings()

    # -- language --------------------------------------------------

    def language(self) -> str:
        """Return the stored UI language code, defaulting to "en".

        Falls back to "en" for a value outside the supported set
        (e.g. left over from a downgrade).
        """
        code = str(self._settings.value(_KEY_LANGUAGE, "en"))
        return code if code in _LANGUAGE_NAMES else "en"

    def set_language(self, value: str) -> None:
        """Store the UI language code."""
        self._settings.setValue(_KEY_LANGUAGE, value)

    # -- max_file_bytes --------------------------------------------

    def max_file_bytes(self) -> int | None:
        """Return the per-file size ceiling in bytes, or None (no limit).

        Stored as 0 for "no limit", since QSettings has no native way
        to persist None.
        """
        raw = self._settings.value(
            _KEY_MAX_FILE_BYTES, _DEFAULT_MAX_FILE_BYTES)
        value = _as_int(raw, _DEFAULT_MAX_FILE_BYTES)
        return value if value > 0 else None

    def set_max_file_bytes(self, value: int | None) -> None:
        """Store the per-file size ceiling; None is stored as 0."""
        self._settings.setValue(
            _KEY_MAX_FILE_BYTES, 0 if value is None else value)

    # -- boolean flags -----------------------------------------------

    def allow_download(self) -> bool:
        """Return True when cloud-only files may be downloaded on read."""
        return _as_bool(
            self._settings.value(_KEY_ALLOW_DOWNLOAD, False), False)

    def set_allow_download(self, value: bool) -> None:
        """Store whether cloud-only files may be downloaded on read."""
        self._settings.setValue(_KEY_ALLOW_DOWNLOAD, value)

    def ignore_hidden(self) -> bool:
        """Return True when hidden files (name starting with ``.``) are skipped."""
        return _as_bool(
            self._settings.value(_KEY_IGNORE_HIDDEN, True), True)

    def set_ignore_hidden(self, value: bool) -> None:
        """Store whether hidden files (name starting with ``.``) are skipped."""
        self._settings.setValue(_KEY_IGNORE_HIDDEN, value)

    def follow_symlinks(self) -> bool:
        """Return True when a scan should follow symbolic links."""
        return _as_bool(
            self._settings.value(_KEY_FOLLOW_SYMLINKS, False), False)

    def set_follow_symlinks(self, value: bool) -> None:
        """Store whether a scan should follow symbolic links."""
        self._settings.setValue(_KEY_FOLLOW_SYMLINKS, value)

    def include_filenames(self) -> bool:
        """Return True when file basenames join diagnostic fingerprints."""
        return _as_bool(
            self._settings.value(_KEY_INCLUDE_FILENAMES, False), False)

    def set_include_filenames(self, value: bool) -> None:
        """Store whether file basenames join diagnostic fingerprints."""
        self._settings.setValue(_KEY_INCLUDE_FILENAMES, value)

    # -- output destination -------------------------------------------

    def output_in_place(self) -> bool:
        """Return True when a repair should overwrite files in place."""
        return _as_bool(
            self._settings.value(_KEY_OUTPUT_IN_PLACE, True), True)

    def set_output_in_place(self, value: bool) -> None:
        """Store whether a repair should overwrite files in place."""
        self._settings.setValue(_KEY_OUTPUT_IN_PLACE, value)

    def output_dir(self) -> str:
        """Return the stored default output folder (empty when unset)."""
        return str(self._settings.value(_KEY_OUTPUT_DIR, ""))

    def set_output_dir(self, value: str) -> None:
        """Store the default output folder."""
        self._settings.setValue(_KEY_OUTPUT_DIR, value)

    # -- repair mode ----------------------------------------------------

    def repair_mode(self) -> str:
        """Return the stored default repair mode, "single" or "multi".

        Falls back to "single" for any other stored value.
        """
        mode = str(self._settings.value(_KEY_REPAIR_MODE, "single"))
        return mode if mode in ("single", "multi") else "single"

    def set_repair_mode(self, value: str) -> None:
        """Store the default repair mode ("single" or "multi")."""
        self._settings.setValue(_KEY_REPAIR_MODE, value)

    # -- recent folders -----------------------------------------------

    def recent_folders(self) -> list[str]:
        """Return the remembered folders, most recently used first."""
        raw = self._settings.value(_KEY_RECENT_FOLDERS, [])
        if not raw:
            return []
        if isinstance(raw, str):
            # A single remembered folder can round-trip as a bare
            # string rather than a one-element list on some backends.
            return [raw]
        return [str(item) for item in raw]

    def push_recent_folder(self, path: Path) -> None:
        """Record *path* as the most recently used folder.

        Moves an already-remembered *path* to the front instead of
        duplicating it, and caps the remembered list at
        :data:`_MAX_RECENT_FOLDERS` entries.

        :param path: the folder to remember.
        """
        folders = self.recent_folders()
        text = str(path)
        if text in folders:
            folders.remove(text)
        folders.insert(0, text)
        del folders[_MAX_RECENT_FOLDERS:]
        self._settings.setValue(_KEY_RECENT_FOLDERS, folders)

    def clear_recent_folders(self) -> None:
        """Forget every remembered folder."""
        self._settings.setValue(_KEY_RECENT_FOLDERS, [])


class SettingsDialog(QDialog):
    """Modal preferences form backed by a :class:`Settings` instance.

    Loads every persisted value into its matching control on
    construction; confirming through the OK button (:meth:`accept`)
    writes every control's current value back through *settings*.
    Cancelling (or otherwise closing) the dialog discards any
    in-dialog edits -- *settings* itself is left untouched until
    :meth:`accept` runs. Every user-facing string is passed through
    :func:`~pptrepair.gui.i18n.tr`.
    """

    def __init__(
        self, settings: Settings, parent: QWidget | None = None
    ) -> None:
        """Build the form and populate it from *settings*.

        :param settings: the settings store this dialog edits.
        :param parent: optional Qt parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle(tr("Preferences"))
        self._settings = settings

        self._language_combo = self._build_language_combo()
        (self._size_spin, self._unit_combo,
         self._no_limit_check) = self._build_size_controls()
        self._download_check = QCheckBox(tr("Download cloud-only files"))
        self._hidden_check = QCheckBox(
            tr("Ignore hidden files (names starting with '.')"))
        self._symlinks_check = QCheckBox(
            tr("Follow symbolic links while walking"))
        self._filenames_check = QCheckBox(
            tr("Include file basenames in diagnostic fingerprints"))
        (self._in_place_radio, self._into_folder_radio,
         self._output_edit, self._browse_button,
         self._output_group) = self._build_output_controls()
        self._mode_combo = self._build_mode_combo()

        self._build_layout()
        self._connect_signals()
        self._load_from_settings()
        self._sync_output_enabled()
        self._sync_limit_enabled()

    # -- construction helpers ------------------------------------------

    def _build_language_combo(self) -> QComboBox:
        """Return the language combo, each item tagged with its code."""
        combo = QComboBox()
        for code, name in _LANGUAGE_NAMES.items():
            combo.addItem(name, code)
        return combo

    def _build_size_controls(self) -> tuple[QSpinBox, QComboBox, QCheckBox]:
        """Return the max-file-size spin box, unit combo and "No limit" box."""
        spin = QSpinBox()
        spin.setRange(1, 9999)
        unit = QComboBox()
        unit.addItems(["MB", "GB"])
        no_limit = QCheckBox(tr("No limit"))
        return spin, unit, no_limit

    def _build_output_controls(
        self,
    ) -> tuple[QRadioButton, QRadioButton, QLineEdit, QPushButton, QGroupBox]:
        """Return the default-output radio group and its widgets.

        Mirrors :meth:`RunOptionsPanel._build_output_controls`'s
        layout, but edits the *stored default* rather than one run's
        live choice.
        """
        in_place = QRadioButton(tr("Repair in place"))
        into_folder = QRadioButton(tr("Repair into folder:"))

        group = QButtonGroup(self)
        group.addButton(in_place)
        group.addButton(into_folder)

        output_edit = QLineEdit()
        output_edit.setReadOnly(True)
        output_edit.setPlaceholderText(tr("(choose a destination folder)"))
        browse_button = QPushButton(tr("Browse…"))

        box = QGroupBox(tr("Default Output"))
        box_layout = QGridLayout(box)
        box_layout.addWidget(in_place, 0, 0, 1, 3)
        box_layout.addWidget(into_folder, 1, 0)
        box_layout.addWidget(output_edit, 1, 1)
        box_layout.addWidget(browse_button, 1, 2)
        box_layout.setColumnStretch(1, 1)
        return in_place, into_folder, output_edit, browse_button, box

    def _build_mode_combo(self) -> QComboBox:
        """Return the default-repair-mode combo, tagged with its enum."""
        combo = QComboBox()
        combo.addItem(tr("Single-file repair"), RepairMode.SINGLE)
        combo.addItem(tr("Multi-source repair"), RepairMode.MULTI)
        return combo

    def _build_layout(self) -> None:
        """Arrange the controls in a top-to-bottom form."""
        layout = QVBoxLayout(self)

        language_row = QHBoxLayout()
        language_row.addWidget(QLabel(tr("Language:")))
        language_row.addWidget(self._language_combo)
        language_row.addStretch(1)
        layout.addLayout(language_row)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel(tr("Max file size:")))
        size_row.addWidget(self._size_spin)
        size_row.addWidget(self._unit_combo)
        size_row.addWidget(self._no_limit_check)
        size_row.addStretch(1)
        layout.addLayout(size_row)

        layout.addWidget(self._download_check)
        layout.addWidget(self._hidden_check)
        layout.addWidget(self._symlinks_check)
        layout.addWidget(self._filenames_check)

        layout.addWidget(self._output_group)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel(tr("Default repair mode:")))
        mode_row.addWidget(self._mode_combo)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _connect_signals(self) -> None:
        """Wire the interactive controls to their dependent-state slots."""
        self._browse_button.clicked.connect(self._choose_output_dir)
        self._in_place_radio.toggled.connect(self._sync_output_enabled)
        self._into_folder_radio.toggled.connect(self._sync_output_enabled)
        self._no_limit_check.toggled.connect(self._sync_limit_enabled)

    # -- interactive slots -----------------------------------------------

    def _choose_output_dir(self) -> None:
        """Prompt for a default destination folder and select its radio."""
        directory = QFileDialog.getExistingDirectory(
            self, tr("Default Output Folder"))
        if directory:
            self._output_edit.setText(directory)
            self._into_folder_radio.setChecked(True)

    def _sync_output_enabled(self, *_args: object) -> None:
        """Enable the folder edit/browse only when that radio is chosen."""
        into_folder = self._into_folder_radio.isChecked()
        self._output_edit.setEnabled(into_folder)
        self._browse_button.setEnabled(into_folder)

    def _sync_limit_enabled(self, *_args: object) -> None:
        """Disable the size spin/unit while "No limit" is checked."""
        limited = not self._no_limit_check.isChecked()
        self._size_spin.setEnabled(limited)
        self._unit_combo.setEnabled(limited)

    # -- load/save -------------------------------------------------------

    def _load_from_settings(self) -> None:
        """Populate every control from :attr:`_settings`'s current values."""
        settings = self._settings

        language_index = self._language_combo.findData(settings.language())
        if language_index >= 0:
            self._language_combo.setCurrentIndex(language_index)

        max_bytes = settings.max_file_bytes()
        if max_bytes is None:
            self._no_limit_check.setChecked(True)
        else:
            spin_value, unit = _bytes_to_spin_unit(max_bytes)
            self._size_spin.setValue(spin_value)
            self._unit_combo.setCurrentText(unit)

        self._download_check.setChecked(settings.allow_download())
        self._hidden_check.setChecked(settings.ignore_hidden())
        self._symlinks_check.setChecked(settings.follow_symlinks())
        self._filenames_check.setChecked(settings.include_filenames())

        if settings.output_in_place():
            self._in_place_radio.setChecked(True)
        else:
            self._into_folder_radio.setChecked(True)
        self._output_edit.setText(settings.output_dir())

        mode_index = self._mode_combo.findData(
            RepairMode(settings.repair_mode()))
        if mode_index >= 0:
            self._mode_combo.setCurrentIndex(mode_index)

    def accept(self) -> None:
        """Write every control's current value back to :attr:`_settings`.

        Called when the OK button is pressed (wired through
        :class:`QDialogButtonBox`); chains to :meth:`QDialog.accept`
        so the dialog closes with the usual ``Accepted`` result.
        """
        settings = self._settings
        settings.set_language(self._language_combo.currentData())

        if self._no_limit_check.isChecked():
            settings.set_max_file_bytes(None)
        else:
            factor = _UNIT_FACTORS[self._unit_combo.currentText()]
            settings.set_max_file_bytes(self._size_spin.value() * factor)

        settings.set_allow_download(self._download_check.isChecked())
        settings.set_ignore_hidden(self._hidden_check.isChecked())
        settings.set_follow_symlinks(self._symlinks_check.isChecked())
        settings.set_include_filenames(self._filenames_check.isChecked())

        settings.set_output_in_place(self._in_place_radio.isChecked())
        settings.set_output_dir(self._output_edit.text())

        settings.set_repair_mode(self._mode_combo.currentData().value)

        super().accept()
