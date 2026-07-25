"""Scan-results table model and panel for the desktop application.

Renders the outcome of one scan -- diagnosed files, skipped files and
donor material mined from archives -- as a flat, colour-coded table,
with a one-line summary above it. Everything here runs on the UI
thread; the panel is fed a :class:`~pptrepair.gui.worker.GuiScanResult`
that the worker produced off-thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from pptrepair.classify import Verdict
from pptrepair.gui.worker import GuiScanResult
from pptrepair.scan import ArchiveMaterial, FileOutcome, ScanResult
from pptrepair.walker import WalkResult

#: Table column headers, in order.
_COLUMNS = ("Path", "Status", "Detail")

#: Foreground colours per row category; ``None`` keeps the theme default.
_CATEGORY_COLORS: dict[str, QColor | None] = {
    "normal": None,
    "corrupted": QColor("#c0392b"),
    "error": QColor("#c0392b"),
    "skipped": QColor("#7f8c8d"),
    "material": QColor("#2980b9"),
}


@dataclass(frozen=True)
class _ResultRow:
    """One rendered table row.

    :ivar path: the Path-column text (a filesystem path, or an
        ``"<archive>::<member>"`` label for donor material).
    :ivar status: the Status-column text.
    :ivar detail: the Detail-column text (empty when there is nothing
        useful to add).
    :ivar category: colour bucket -- one key of :data:`_CATEGORY_COLORS`.
    """

    path: str
    status: str
    detail: str
    category: str


def _row_for_outcome(outcome: FileOutcome) -> _ResultRow:
    """Build the table row for one on-disk :class:`FileOutcome`."""
    path_text = str(outcome.path)
    if outcome.diagnosis is None:
        # Pipeline failure: no verdict, only an error message.
        return _ResultRow(path_text, "error", outcome.error or "", "error")
    verdict = outcome.diagnosis.verdict
    if verdict == Verdict.NORMAL:
        return _ResultRow(path_text, verdict.value, "", "normal")
    evidence = outcome.diagnosis.evidence
    detail = evidence[0] if evidence else ""
    return _ResultRow(path_text, verdict.value, detail, "corrupted")


def _skip_rows(walk: WalkResult) -> list[_ResultRow]:
    """Build the skipped/error rows from a discovery :class:`WalkResult`."""
    rows: list[_ResultRow] = []
    skip_labels = (
        (walk.skipped_oversize, "skipped (size limit)"),
        (walk.skipped_cloud, "skipped (cloud-only)"),
        (walk.skipped_legacy, "skipped (legacy .ppt)"),
        (walk.skipped_temp, "skipped (temp file)"),
    )
    for paths, label in skip_labels:
        for path in paths:
            rows.append(_ResultRow(str(path), label, "", "skipped"))
    for path, message in walk.errors:
        rows.append(_ResultRow(str(path), "error", message, "error"))
    return rows


def _row_for_material(material: ArchiveMaterial) -> _ResultRow:
    """Build the table row for one archive :class:`ArchiveMaterial`."""
    if material.diagnosis is not None:
        detail = material.diagnosis.verdict.value
    else:
        detail = material.error or ""
    # Donor material is always named through its "<archive>::<member>"
    # label, never the temporary path it was briefly extracted to.
    return _ResultRow(material.display(), "material", detail, "material")


def _build_rows(result: GuiScanResult) -> list[_ResultRow]:
    """Flatten a :class:`GuiScanResult` into table rows.

    Order: diagnosed files, then skipped/errored discovery entries,
    then archive donor material.
    """
    rows: list[_ResultRow] = []
    scan = result.scan
    if scan is not None:
        rows.extend(_row_for_outcome(outcome) for outcome in scan.outcomes)
        rows.extend(_skip_rows(scan.walk))
    rows.extend(_row_for_material(material) for material in result.materials)
    return rows


class ScanResultsModel(QAbstractTableModel):
    """Flat table model over the rows of one :class:`GuiScanResult`.

    Three columns -- Path, Status, Detail -- with ``DisplayRole`` text
    and a ``ForegroundRole`` colour per row category (corrupted/error
    red, skipped grey, material blue, normal default).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialize an empty model.

        :param parent: optional Qt parent object.
        """
        super().__init__(parent)
        self._rows: list[_ResultRow] = []

    def set_result(self, result: GuiScanResult) -> None:
        """Rebuild every row from *result*.

        Wrapped in ``beginResetModel``/``endResetModel`` so any attached
        view refreshes wholesale. Runs on the UI thread.

        :param result: the scan outcome to display.
        """
        self.beginResetModel()
        self._rows = _build_rows(result)
        self.endResetModel()

    def rowCount(  # noqa: N802 (Qt's required camelCase override)
        self, parent: QModelIndex = QModelIndex()
    ) -> int:
        """Return the number of result rows.

        :param parent: unused; this is a flat (non-tree) table model.
        """
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(  # noqa: N802 (Qt's required camelCase override)
        self, parent: QModelIndex = QModelIndex()
    ) -> int:
        """Return the fixed column count (Path/Status/Detail).

        :param parent: unused; this is a flat (non-tree) table model.
        """
        if parent.isValid():
            return 0
        return len(_COLUMNS)

    def data(  # noqa: N802 (Qt's required camelCase override)
        self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        """Return the cell text or foreground colour for *index*.

        :param index: cell to query; must be valid and in range.
        :param role: ``Qt.DisplayRole`` for the cell text or
            ``Qt.ForegroundRole`` for the row's category colour.
        :returns: the requested value, or ``None`` for any other role
            or an out-of-range index.
        """
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]

        if role == Qt.ItemDataRole.DisplayRole:
            return (row.path, row.status, row.detail)[index.column()]

        if role == Qt.ItemDataRole.ForegroundRole:
            color = _CATEGORY_COLORS.get(row.category)
            return QBrush(color) if color is not None else None

        return None

    def headerData(  # noqa: N802 (Qt's required camelCase override)
        self, section: int, orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole
    ) -> Any:
        """Return the horizontal header label for *section*.

        :param section: column index for horizontal headers.
        :param orientation: only ``Horizontal`` yields a label.
        :param role: only ``Qt.DisplayRole`` is served.
        """
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation != Qt.Orientation.Horizontal:
            return None
        if not (0 <= section < len(_COLUMNS)):
            return None
        return _COLUMNS[section]


def _summary_text(result: GuiScanResult) -> str:
    """Compose the one-line summary shown above the results table."""
    scan: ScanResult | None = result.scan
    outcomes = scan.outcomes if scan is not None else []

    intact = sum(1 for o in outcomes
                 if o.diagnosis is not None
                 and o.diagnosis.verdict == Verdict.NORMAL)
    corrupted = sum(1 for o in outcomes
                    if o.diagnosis is not None
                    and o.diagnosis.verdict != Verdict.NORMAL)
    errors = sum(1 for o in outcomes if o.diagnosis is None)
    scanned = len(outcomes)

    skipped = 0
    if scan is not None:
        walk = scan.walk
        skipped = (len(walk.skipped_oversize) + len(walk.skipped_cloud)
                   + len(walk.skipped_legacy) + len(walk.skipped_temp)
                   + len(walk.errors))
    materials = len(result.materials)

    head = f"Scanned {scanned} file(s): {corrupted} corrupted, {intact} intact"
    if errors:
        head += f", {errors} error(s)"

    tail: list[str] = []
    if skipped:
        tail.append(f"{skipped} skipped")
    if materials:
        tail.append(f"{materials} archive material(s)")
    if tail:
        return f"{head} — {', '.join(tail)}"
    return head


class ResultsPanel(QWidget):
    """Summary label above a scan-results :class:`QTableView`.

    Fed a :class:`GuiScanResult` through :meth:`show_result`; the Path
    column stretches to fill the width. Starts empty and can be reset
    through :meth:`clear`.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the summary label and results table.

        :param parent: optional Qt parent widget.
        """
        super().__init__(parent)
        self._summary = ""

        self._summary_label = QLabel("")
        self._model = ScanResultsModel(self)
        self._table = self._build_table()

        layout = QVBoxLayout(self)
        layout.addWidget(self._summary_label)
        layout.addWidget(self._table)

    def _build_table(self) -> QTableView:
        """Return the read-only results table bound to the model."""
        table = QTableView()
        table.setModel(self._model)
        table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        # Path column stretches; the other two size to their contents.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        return table

    def show_result(self, result: GuiScanResult) -> None:
        """Display *result*: rebuild the table and refresh the summary.

        Runs on the UI thread.

        :param result: the scan outcome to render.
        """
        self._model.set_result(result)
        self._summary = _summary_text(result)
        self._summary_label.setText(self._summary)

    def summary_text(self) -> str:
        """Return the currently displayed summary line (empty when clear)."""
        return self._summary

    def clear(self) -> None:
        """Reset to the empty state (no rows, no summary)."""
        self._model.set_result(GuiScanResult(scan=None))
        self._summary = ""
        self._summary_label.setText("")
