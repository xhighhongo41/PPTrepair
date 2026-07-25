"""Scan/repair-results table/tree models and panel for the desktop app.

Renders the outcome of one scan -- diagnosed files, skipped files and
donor material mined from archives -- as a flat, colour-coded table on
a "Files" tab, the twin-/lineage-/merge-restoration candidates computed
from that same outcome as a tree on a "Candidates" tab, and the outcome
of one single-file repair run as a flat, colour-coded table on a
"Repair" tab, with a one-line summary shown above all three. Everything
here runs on the UI thread; the panel is fed a
:class:`~pptrepair.gui.worker.GuiScanResult` (:meth:`show_result`) or a
:class:`~pptrepair.batch.BatchResult` (:meth:`show_repair_result`) that
the matching worker produced off-thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QStackedWidget,
    QTableView,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pptrepair.batch import BatchItem, BatchResult
from pptrepair.classify import Verdict
from pptrepair.gui.worker import (
    GuiScanResult,
    MergeItemOutcome,
    MultiRepairResult,
)
from pptrepair.repair import RepairOutcome

# The three candidate-computation functions below are report.py's own
# algorithms (kept intentionally separate from its text/JSON
# rendering) reused here so the GUI's Candidates tab shows exactly the
# same twin/lineage/merge candidates as ``pptrepair scan``'s report.
# Importing these private names -- and the private result dataclass
# _LineageCandidate, needed only for type hints -- is an intentional
# same-package reuse, not a public API.
from pptrepair.report_candidates import (
    _lineage_candidates_map,
    _LineageCandidate,
    _merge_group_map,
    _twin_candidates_map,
)
from pptrepair.scan import ArchiveMaterial, FileOutcome, ScanResult
from pptrepair.twin import TwinCandidate
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


# --------------------------------------------------------------------------
# Candidates tree
# --------------------------------------------------------------------------


def _twin_candidate_label(path: Path, candidate: TwinCandidate) -> str:
    """Render one twin-candidate item's text: "target -> candidate".

    *candidate*'s display name is its ``"<archive>::<member>"`` label
    when it was materialized from an archive, else its plain path.
    """
    display = (candidate.member_label if candidate.member_label is not None
               else str(candidate.path))
    return f"{path} → {display}"


def _lineage_candidate_label(path: Path, candidate: _LineageCandidate) -> str:
    """Render one lineage-candidate item's text, with its lineage score."""
    return (f"{path} → {candidate.display} "
            f"(score {candidate.score.lineage_score:.2f})")


def _build_twin_branch(
    twin_map: dict[Path, list[TwinCandidate]],
) -> QTreeWidgetItem | None:
    """Return the "Twin candidates" top-level item, or None when empty."""
    if not twin_map:
        return None
    root = QTreeWidgetItem(["Twin candidates"])
    for path, candidates in twin_map.items():
        for candidate in candidates:
            QTreeWidgetItem(root, [_twin_candidate_label(path, candidate)])
    return root


def _build_lineage_branch(
    lineage_map: dict[Path, list[_LineageCandidate]],
) -> QTreeWidgetItem | None:
    """Return the "Lineage candidates" top-level item, or None when empty."""
    if not lineage_map:
        return None
    root = QTreeWidgetItem(["Lineage candidates"])
    for path, candidates in lineage_map.items():
        for candidate in candidates:
            QTreeWidgetItem(
                root, [_lineage_candidate_label(path, candidate)])
    return root


def _build_merge_branch(groups: list[dict]) -> QTreeWidgetItem | None:
    """Return the "Merge groups" top-level item, or None when empty.

    Each group is shown as "group (size <bytes>)", with one child
    item per member file (each rendered through its own display name).
    """
    if not groups:
        return None
    root = QTreeWidgetItem(["Merge groups"])
    for group in groups:
        group_item = QTreeWidgetItem(root, [f"group (size {group['size']})"])
        for merge_file in group["files"]:
            QTreeWidgetItem(group_item, [merge_file.display])
    return root


def _build_candidate_branches(result: GuiScanResult) -> list[QTreeWidgetItem]:
    """Build every non-empty candidate branch for *result*.

    Computes the twin/lineage/merge candidate maps from the scan
    outcome (empty when *result* carries no on-disk scan) plus any
    mined archive materials, through the same functions
    :mod:`pptrepair.report_candidates` uses for the text/JSON reports.
    """
    outcomes = result.scan.outcomes if result.scan is not None else []
    materials = result.materials
    branches = (
        _build_twin_branch(_twin_candidates_map(outcomes, materials)),
        _build_lineage_branch(_lineage_candidates_map(outcomes, materials)),
        _build_merge_branch(_merge_group_map(outcomes, materials)),
    )
    return [branch for branch in branches if branch is not None]


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

    def rowCount(
        self, parent: QModelIndex = QModelIndex()
    ) -> int:
        """Return the number of result rows.

        :param parent: unused; this is a flat (non-tree) table model.
        """
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(
        self, parent: QModelIndex = QModelIndex()
    ) -> int:
        """Return the fixed column count (Path/Status/Detail).

        :param parent: unused; this is a flat (non-tree) table model.
        """
        if parent.isValid():
            return 0
        return len(_COLUMNS)

    def data(
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

    def headerData(
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


# --------------------------------------------------------------------------
# Repair results table
# --------------------------------------------------------------------------

#: Repair table column headers, in order.
_REPAIR_COLUMNS = ("Source", "Action", "Output")

#: Foreground colours per repair-row category; ``None`` keeps the theme
#: default (used for the dry-run-only "planned" category, never
#: produced by this milestone's single-file repair worker).
_REPAIR_CATEGORY_COLORS: dict[str, QColor | None] = {
    "repaired": QColor("#27ae60"),
    "problem": QColor("#c0392b"),
    "skipped": QColor("#7f8c8d"),
    "planned": None,
}


@dataclass(frozen=True)
class _RepairRow:
    """One rendered repair-table row.

    :ivar source: the Source-column text (the repaired file's path).
    :ivar action: the Action-column text, e.g. ``"repaired (rebuild)"``.
    :ivar output: the Output-column text (empty when nothing was, or
        would be, written).
    :ivar category: colour bucket -- one key of
        :data:`_REPAIR_CATEGORY_COLORS`.
    """

    source: str
    action: str
    output: str
    category: str


def _repair_row_for_item(item: BatchItem) -> _RepairRow:
    """Build the repair-table row for one :class:`BatchItem`.

    Mirrors :mod:`pptrepair.report_batch`'s own machine-facing action
    codes, but rendered for a human reader: ``"repaired"`` gains its
    mode in parentheses (e.g. ``"repaired (rebuild)"``) when the repair
    actually ran, and ``"skipped_existing"`` reads as
    ``"skipped (exists)"``. ``"unrepairable"``/``"failed"`` and the
    dry-run-only ``"planned"`` action are shown as-is.
    """
    source = str(item.source.path)
    output = str(item.planned_output) if item.planned_output is not None else ""

    if item.action == "repaired":
        mode = item.repair.mode if item.repair is not None else None
        action = f"repaired ({mode})" if mode is not None else "repaired"
        category = "repaired"
    elif item.action == "skipped_existing":
        action = "skipped (exists)"
        category = "skipped"
    elif item.action in ("unrepairable", "failed"):
        action = item.action
        category = "problem"
    else:
        # "planned": only ever produced under repair_paths(dry_run=True),
        # which this milestone's GUI worker never requests.
        action = item.action
        category = "planned"

    return _RepairRow(source, action, output, category)


def _merge_row_for_item(item: MergeItemOutcome) -> _RepairRow:
    """Build the repair-table row for one multi-source merge outcome.

    A successful merge reads as ``"merged"`` (green) with its artifact
    path in the Output column; a failed one reads as ``"merge failed"``
    (red) with the failure reason there instead.
    """
    source = str(item.target)
    if item.success:
        output = str(item.output_path) if item.output_path is not None else ""
        return _RepairRow(source, "merged", output, "repaired")
    return _RepairRow(source, "merge failed", item.detail, "problem")


def _repair_row_for_outcome(outcome: RepairOutcome) -> _RepairRow:
    """Build the repair-table row for one fallback :class:`RepairOutcome`.

    A successful single-file repair reads as ``"repaired (<mode>)"``
    (green) with its artifact path; an unsuccessful one reads as
    ``"failed"`` (when it raised, mode ``"failed"``) or ``"unrepairable"``
    (red), with nothing written.
    """
    source = str(outcome.src)
    output = (str(outcome.output_path)
              if outcome.output_path is not None else "")
    if outcome.success:
        return _RepairRow(source, f"repaired ({outcome.mode})", output,
                          "repaired")
    action = "failed" if outcome.mode == "failed" else "unrepairable"
    return _RepairRow(source, action, output, "problem")


def _multi_repair_rows(
        merges: list[MergeItemOutcome],
        fallbacks: list[RepairOutcome]) -> list[_RepairRow]:
    """Flatten a multi-source run's merges and fallbacks into table rows.

    Merges first (in run order), then the donor-less fallback repairs.
    """
    rows = [_merge_row_for_item(item) for item in merges]
    rows.extend(_repair_row_for_outcome(outcome) for outcome in fallbacks)
    return rows


class RepairResultsModel(QAbstractTableModel):
    """Flat table model over the :class:`BatchItem` list of one repair run.

    Three columns -- Source, Action, Output -- with ``DisplayRole`` text
    and a ``ForegroundRole`` colour per row category (repaired green,
    unrepairable/failed red, skipped grey).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """Initialize an empty model.

        :param parent: optional Qt parent object.
        """
        super().__init__(parent)
        self._rows: list[_RepairRow] = []

    def set_result(self, result: BatchResult) -> None:
        """Rebuild every row from *result*.

        Wrapped in ``beginResetModel``/``endResetModel`` so any attached
        view refreshes wholesale. Runs on the UI thread.

        :param result: the repair outcome to display.
        """
        self.beginResetModel()
        self._rows = [_repair_row_for_item(item) for item in result.items]
        self.endResetModel()

    def set_multi_result(self, result: MultiRepairResult) -> None:
        """Rebuild every row from one multi-source repair *result*.

        Wrapped in ``beginResetModel``/``endResetModel`` so any attached
        view refreshes wholesale. Runs on the UI thread.

        :param result: the multi-source repair outcome to display.
        """
        self.beginResetModel()
        self._rows = _multi_repair_rows(result.merges, result.fallbacks)
        self.endResetModel()

    def clear(self) -> None:
        """Reset to the empty state (no rows)."""
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def rowCount(
        self, parent: QModelIndex = QModelIndex()
    ) -> int:
        """Return the number of repair-result rows.

        :param parent: unused; this is a flat (non-tree) table model.
        """
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(
        self, parent: QModelIndex = QModelIndex()
    ) -> int:
        """Return the fixed column count (Source/Action/Output).

        :param parent: unused; this is a flat (non-tree) table model.
        """
        if parent.isValid():
            return 0
        return len(_REPAIR_COLUMNS)

    def data(
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
            return (row.source, row.action, row.output)[index.column()]

        if role == Qt.ItemDataRole.ForegroundRole:
            color = _REPAIR_CATEGORY_COLORS.get(row.category)
            return QBrush(color) if color is not None else None

        return None

    def headerData(
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
        if not (0 <= section < len(_REPAIR_COLUMNS)):
            return None
        return _REPAIR_COLUMNS[section]


def _repair_summary_text(result: BatchResult) -> str:
    """Compose the one-line summary shown above the repair table.

    E.g. ``"Repaired 3, unrepairable 1, skipped 1"``, with a trailing
    ``", failed {n}"`` only when at least one repair attempt failed.
    """
    counts = result.counts()
    text = (f"Repaired {counts['repaired']}, "
            f"unrepairable {counts['unrepairable']}, "
            f"skipped {counts['skipped_existing']}")
    if counts["failed"]:
        text += f", failed {counts['failed']}"
    return text


def _multi_repair_summary_text(result: MultiRepairResult) -> str:
    """Compose the one-line summary shown above a multi-source repair table.

    E.g. ``"Merged 2, merge failed 1, fallback repaired 1"``, with each
    zero-count clause omitted.
    """
    merged = sum(1 for item in result.merges if item.success)
    merge_failed = len(result.merges) - merged
    repaired = sum(1 for outcome in result.fallbacks if outcome.success)
    fallback_failed = len(result.fallbacks) - repaired

    parts = [f"Merged {merged}"]
    if merge_failed:
        parts.append(f"merge failed {merge_failed}")
    if result.fallbacks:
        parts.append(f"fallback repaired {repaired}")
        if fallback_failed:
            parts.append(f"fallback failed {fallback_failed}")
    return ", ".join(parts)


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


#: Placeholder text shown on the Candidates tab when a scan found no
#: twin, lineage or merge candidates at all.
_NO_CANDIDATES_TEXT = "(no candidates found)"


class ResultsPanel(QWidget):
    """Summary label above a "Files"/"Candidates"/"Repair" tab widget.

    Fed a :class:`GuiScanResult` through :meth:`show_result`: the
    "Files" tab holds the flat, colour-coded results table (its Path
    column stretching to fill the width); the "Candidates" tab holds a
    tree of twin-/lineage-/merge-restoration candidates computed from
    that same result, falling back to a placeholder label when none
    were found. Fed a :class:`~pptrepair.batch.BatchResult` through
    :meth:`show_repair_result`: the "Repair" tab holds a flat,
    colour-coded table of that run's per-file outcomes, and becomes the
    current tab. Starts empty and can be reset through :meth:`clear`;
    the last *scan* result shown is kept for :meth:`last_result`, so a
    repair step can act on it without re-scanning.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        """Build the summary label and the Files/Candidates/Repair tabs.

        :param parent: optional Qt parent widget.
        """
        super().__init__(parent)
        self._summary = ""
        self._last_result: GuiScanResult | None = None

        self._summary_label = QLabel("")
        self._model = ScanResultsModel(self)
        self._table = self._build_table()
        self._candidates_stack = self._build_candidates_stack()
        self._repair_model = RepairResultsModel(self)
        self._repair_table = self._build_repair_table()

        self._tabs = QTabWidget()
        self._tabs.addTab(self._table, "Files")
        self._tabs.addTab(self._candidates_stack, "Candidates")
        self._tabs.addTab(self._repair_table, "Repair")

        layout = QVBoxLayout(self)
        layout.addWidget(self._summary_label)
        layout.addWidget(self._tabs)

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

    def _build_repair_table(self) -> QTableView:
        """Return the read-only repair-results table bound to its model."""
        table = QTableView()
        table.setModel(self._repair_model)
        table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        header = table.horizontalHeader()
        # Source column stretches; the other two size to their contents.
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        return table

    def _build_candidates_stack(self) -> QStackedWidget:
        """Return the Candidates tab's tree/placeholder toggle widget."""
        self._candidates_tree = QTreeWidget()
        self._candidates_tree.setHeaderHidden(True)

        self._candidates_placeholder = QLabel(_NO_CANDIDATES_TEXT)
        self._candidates_placeholder.setAlignment(
            Qt.AlignmentFlag.AlignCenter)

        stack = QStackedWidget()
        stack.addWidget(self._candidates_tree)
        stack.addWidget(self._candidates_placeholder)
        return stack

    def show_result(self, result: GuiScanResult) -> None:
        """Display *result*: rebuild both tabs and refresh the summary.

        Runs on the UI thread.

        :param result: the scan outcome to render.
        """
        self._last_result = result
        self._model.set_result(result)
        self._summary = _summary_text(result)
        self._summary_label.setText(self._summary)
        self._show_candidates(result)

    def _show_candidates(self, result: GuiScanResult) -> None:
        """Rebuild the Candidates tree, or fall back to the placeholder."""
        self._candidates_tree.clear()
        branches = _build_candidate_branches(result)
        if not branches:
            self._candidates_stack.setCurrentWidget(
                self._candidates_placeholder)
            return
        self._candidates_tree.addTopLevelItems(branches)
        self._candidates_tree.expandAll()
        self._candidates_stack.setCurrentWidget(self._candidates_tree)

    def show_repair_result(self, result: BatchResult) -> None:
        """Display *result*: rebuild the Repair tab and switch to it.

        Runs on the UI thread. Does not touch the Files/Candidates
        tabs or :meth:`last_result` -- those still reflect the scan
        this repair ran against.

        :param result: the repair outcome to render.
        """
        self._repair_model.set_result(result)
        self._summary = _repair_summary_text(result)
        self._summary_label.setText(self._summary)
        self._tabs.setCurrentWidget(self._repair_table)

    def show_multi_repair_result(self, result: MultiRepairResult) -> None:
        """Display a multi-source *result*: rebuild the Repair tab and show it.

        Runs on the UI thread. Like :meth:`show_repair_result`, it does
        not touch the Files/Candidates tabs or :meth:`last_result` -- the
        scan those tabs reflect is the one this repair ran against.

        :param result: the multi-source repair outcome to render.
        """
        self._repair_model.set_multi_result(result)
        self._summary = _multi_repair_summary_text(result)
        self._summary_label.setText(self._summary)
        self._tabs.setCurrentWidget(self._repair_table)

    def summary_text(self) -> str:
        """Return the currently displayed summary line (empty when clear)."""
        return self._summary

    def last_result(self) -> GuiScanResult | None:
        """Return the most recently displayed result, or None before one is.

        Kept so a later milestone's repair step can act on the same
        scan without asking the user to run it again.
        """
        return self._last_result

    def clear(self) -> None:
        """Reset to the empty state (no rows, no candidates, no summary)."""
        self._last_result = None
        self._model.set_result(GuiScanResult(scan=None))
        self._repair_model.clear()
        self._summary = ""
        self._summary_label.setText("")
        self._candidates_tree.clear()
        self._candidates_stack.setCurrentWidget(self._candidates_placeholder)
