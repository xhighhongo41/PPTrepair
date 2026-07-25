"""Run-options panel for the PySide6 desktop application.

A compact widget collecting the choices that drive a scan/repair run:
the repair mode, where repaired files go, whether cloud-only files may
be downloaded, and an optional per-file size ceiling. Only the download
and size settings are consumed by the current scan milestone; the
repair mode and output destination are captured here (and read back
through the property accessors) but wired into the actual repair in a
later milestone. Every control's initial value comes from
:meth:`RunOptionsPanel.apply_settings`, which the host window calls
with the persisted :class:`~pptrepair.gui.settings.Settings`.

All accessors and mutators on :class:`RunOptionsPanel` run on the UI
thread, like every Qt widget.
"""

from __future__ import annotations

import enum
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QWidget,
)

from pptrepair.gui.i18n import tr

if TYPE_CHECKING:  # avoid a runtime import cycle with settings.py
    from pptrepair.gui.settings import Settings


class RepairMode(enum.Enum):
    """How the repair stage should treat the accumulated sources."""

    #: Repair each corrupted file on its own, using only its own bytes.
    SINGLE = "single"
    #: Repair by merging donor material across several related sources.
    MULTI = "multi"


#: Byte multipliers for the size-unit combo, base 1024.
_UNIT_FACTORS = {
    "MB": 1024 ** 2,
    "GB": 1024 ** 3,
}


def _bytes_to_spin_unit(value: int) -> tuple[int, str]:
    """Convert a byte count back to a (spin value, unit) pair.

    The inverse of the multiplication :meth:`RunOptionsPanel.max_file_bytes`
    performs: prefers GB when *value* divides evenly into gibibytes,
    falling back to MB (rounded, then clamped to the spin box's 1-9999
    range) otherwise. Also reused by
    :class:`~pptrepair.gui.settings.SettingsDialog` for its own,
    identically-scaled size fields.

    :param value: a positive byte count (a max-file-size ceiling).
    """
    gb_value, remainder = divmod(value, _UNIT_FACTORS["GB"])
    if remainder == 0 and 1 <= gb_value <= 9999:
        return gb_value, "GB"
    mb_value = max(1, min(9999, round(value / _UNIT_FACTORS["MB"])))
    return mb_value, "MB"


class RunOptionsPanel(QWidget):
    """Compact panel of scan/repair options with typed accessors.

    Exposes the user's choices through :meth:`repair_mode`,
    :meth:`in_place`, :meth:`output_dir`, :meth:`allow_download` and
    :meth:`max_file_bytes`; :meth:`set_enabled_for_running` greys the
    whole panel out while a scan is in progress. Emits
    :attr:`mode_changed` whenever the repair mode selection changes, so
    the host window can re-evaluate the Repair action without reaching
    into the private mode combo.
    """

    #: Emitted with the newly selected :class:`RepairMode` whenever the
    #: repair-mode combo changes (by the user or programmatically).
    mode_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the option controls with their hard-coded defaults.

        :param parent: optional Qt parent widget.
        """
        super().__init__(parent)

        self._mode_combo = self._build_mode_combo()
        (self._in_place_radio, self._into_folder_radio,
         self._output_edit, self._browse_button,
         self._output_group) = self._build_output_controls()
        self._download_check = QCheckBox(tr("Download cloud-only files"))
        (self._size_spin, self._unit_combo,
         self._no_limit_check) = self._build_size_controls()

        self._build_layout()
        self._connect_signals()

        # Apply the initial dependent-enable states (output edit follows
        # the radio selection; the size spin/unit follow "No limit").
        self._sync_output_enabled()
        self._sync_limit_enabled()

    # -- construction helpers ------------------------------------------

    def _build_mode_combo(self) -> QComboBox:
        """Return the repair-mode combo, each item tagged with its enum."""
        combo = QComboBox()
        combo.addItem(tr("Single-file repair"), RepairMode.SINGLE)
        combo.addItem(tr("Multi-source repair"), RepairMode.MULTI)
        return combo

    def _build_output_controls(
        self,
    ) -> tuple[QRadioButton, QRadioButton, QLineEdit, QPushButton, QGroupBox]:
        """Return the output-destination radio group and its widgets.

        The two radio buttons share a :class:`QButtonGroup` so exactly
        one is ever selected; "Repair in place" is the default.
        """
        in_place = QRadioButton(tr("Repair in place"))
        in_place.setChecked(True)
        into_folder = QRadioButton(tr("Repair into folder:"))

        group = QButtonGroup(self)
        group.addButton(in_place)
        group.addButton(into_folder)

        output_edit = QLineEdit()
        output_edit.setReadOnly(True)
        output_edit.setPlaceholderText(tr("(choose a destination folder)"))
        browse_button = QPushButton(tr("Browse…"))

        box = QGroupBox(tr("Output"))
        box_layout = QGridLayout(box)
        box_layout.addWidget(in_place, 0, 0, 1, 3)
        box_layout.addWidget(into_folder, 1, 0)
        box_layout.addWidget(output_edit, 1, 1)
        box_layout.addWidget(browse_button, 1, 2)
        box_layout.setColumnStretch(1, 1)
        return in_place, into_folder, output_edit, browse_button, box

    def _build_size_controls(self) -> tuple[QSpinBox, QComboBox, QCheckBox]:
        """Return the max-file-size spin box, unit combo and "No limit" box.

        Defaults to a 2 GB ceiling with the limit enabled.
        """
        spin = QSpinBox()
        spin.setRange(1, 9999)
        spin.setValue(2)

        unit = QComboBox()
        unit.addItems(["MB", "GB"])
        unit.setCurrentText("GB")

        no_limit = QCheckBox(tr("No limit"))
        return spin, unit, no_limit

    def _build_layout(self) -> None:
        """Arrange the controls in a compact two-region grid."""
        layout = QGridLayout(self)

        layout.addWidget(QLabel(tr("Repair mode:")), 0, 0)
        layout.addWidget(self._mode_combo, 0, 1)

        layout.addWidget(self._output_group, 1, 0, 1, 2)

        layout.addWidget(self._download_check, 2, 0, 1, 2)

        size_row = QHBoxLayout()
        size_row.addWidget(QLabel(tr("Max file size:")))
        size_row.addWidget(self._size_spin)
        size_row.addWidget(self._unit_combo)
        size_row.addWidget(self._no_limit_check)
        size_row.addStretch(1)
        layout.addLayout(size_row, 3, 0, 1, 2)

    def _connect_signals(self) -> None:
        """Wire the interactive controls to their dependent-state slots."""
        self._browse_button.clicked.connect(self._choose_output_dir)
        self._in_place_radio.toggled.connect(self._sync_output_enabled)
        self._into_folder_radio.toggled.connect(self._sync_output_enabled)
        self._no_limit_check.toggled.connect(self._sync_limit_enabled)
        self._mode_combo.currentIndexChanged.connect(self._emit_mode_changed)

    def _emit_mode_changed(self, *_args: object) -> None:
        """Re-broadcast a repair-mode combo change as :attr:`mode_changed`."""
        self.mode_changed.emit(self.repair_mode())

    # -- interactive slots ---------------------------------------------

    def _choose_output_dir(self) -> None:
        """Prompt for a destination folder and select the folder radio."""
        directory = QFileDialog.getExistingDirectory(
            self, tr("Repair Into Folder"))
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

    # -- typed accessors -----------------------------------------------

    def repair_mode(self) -> RepairMode:
        """Return the currently selected :class:`RepairMode`."""
        return self._mode_combo.currentData()

    def in_place(self) -> bool:
        """Return True when repairs should overwrite the source in place."""
        return self._in_place_radio.isChecked()

    def output_dir(self) -> Path | None:
        """Return the chosen destination folder, or ``None`` for in-place.

        Also returns ``None`` when the folder option is selected but no
        directory has been picked yet.
        """
        if self.in_place():
            return None
        text = self._output_edit.text().strip()
        return Path(text) if text else None

    def allow_download(self) -> bool:
        """Return True when cloud-only files may be downloaded on read."""
        return self._download_check.isChecked()

    def max_file_bytes(self) -> int | None:
        """Return the per-file size ceiling in bytes, or ``None``.

        ``None`` when "No limit" is checked; otherwise the spin value
        converted through the unit combo with a base-1024 multiplier
        (e.g. 2 GB -> ``2_147_483_648``).
        """
        if self._no_limit_check.isChecked():
            return None
        factor = _UNIT_FACTORS[self._unit_combo.currentText()]
        return self._size_spin.value() * factor

    # -- settings integration -------------------------------------------

    def apply_settings(self, settings: Settings) -> None:
        """Initialise every control from *settings*'s persisted defaults.

        Called once at startup (right after this panel is
        constructed, with the freshly loaded
        :class:`~pptrepair.gui.settings.Settings`) and again whenever
        the user confirms the Preferences dialog, so a stored-default
        change takes effect immediately. Runs on the UI thread.

        :param settings: the settings store to read the defaults from.
        """
        mode_index = self._mode_combo.findData(
            RepairMode(settings.repair_mode()))
        if mode_index >= 0:
            self._mode_combo.setCurrentIndex(mode_index)

        if settings.output_in_place():
            self._in_place_radio.setChecked(True)
        else:
            self._output_edit.setText(settings.output_dir())
            self._into_folder_radio.setChecked(True)

        self._download_check.setChecked(settings.allow_download())

        max_bytes = settings.max_file_bytes()
        self._no_limit_check.setChecked(max_bytes is None)
        if max_bytes is not None:
            spin_value, unit = _bytes_to_spin_unit(max_bytes)
            self._size_spin.setValue(spin_value)
            self._unit_combo.setCurrentText(unit)

        self._sync_output_enabled()
        self._sync_limit_enabled()

    # -- running-state control -----------------------------------------

    def set_enabled_for_running(self, running: bool) -> None:
        """Grey the whole panel out while a scan is running.

        :param running: True to disable every control for the duration
            of a scan; False to restore normal interactivity (including
            the dependent enable/disable of the folder edit and the size
            spin/unit).
        """
        for widget in (self._mode_combo, self._in_place_radio,
                       self._into_folder_radio, self._output_edit,
                       self._browse_button, self._download_check,
                       self._size_spin, self._unit_combo,
                       self._no_limit_check):
            widget.setEnabled(not running)
        if not running:
            # Restore the conditional enable states the blanket toggle
            # above just overrode.
            self._sync_output_enabled()
            self._sync_limit_enabled()
