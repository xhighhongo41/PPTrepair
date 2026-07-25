"""Central-widget panel showing the accumulated source list.

Split out of :mod:`pptrepair.gui.main_window` to keep that module
focused on window-level concerns (menu bar, drag-and-drop, status
bar); this module owns the empty-state/list-state toggle and the
"Add Files…"/"Add Folder…"/"Remove Selected"/"Clear All" controls
around a shared :class:`~pptrepair.gui.sources.SourceListModel`.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pptrepair.gui.i18n import tr
from pptrepair.gui.sources import SourceListModel


def _file_dialog_filter() -> str:
    """Return the "Add Files…" dialog's filter string.

    PowerPoint targets first, then every archive suffix
    :mod:`pptrepair.archive` recognises, then an unrestricted fallback.
    Built at call time (rather than as a module-level constant) so its
    two category labels always reflect the currently active language.
    """
    return (
        f"{tr('PowerPoint files / archives')} (*.pptx *.pptm *.zip *.tar "
        f"*.tar.gz *.tgz *.tar.bz2 *.tar.xz);;{tr('All files')} (*)"
    )


class SourcePanel(QWidget):
    """Empty placeholder or populated list view, plus its button row.

    A ``QStackedWidget`` shows a placeholder label while *model* is
    empty and switches to a ``QListView`` bound to *model* as soon as
    the first source is added (and back again once the list is
    emptied), tracking the model's row-count changes automatically.

    Additions made through this panel's own "Add Files…"/"Add Folder…"
    dialogs (including the equivalent File-menu actions wired to them)
    emit :attr:`sources_added` with the resulting
    :class:`~pptrepair.gui.sources.AddResult`, so the host window can
    summarise them on its status bar exactly as it already does for
    drag-and-drop.
    """

    #: Emitted after an "Add Files…"/"Add Folder…" dialog adds sources;
    #: carries the :class:`~pptrepair.gui.sources.AddResult`.
    sources_added = Signal(object)

    def __init__(
        self, model: SourceListModel, parent: QWidget | None = None
    ) -> None:
        """Build the stacked view and button row around *model*.

        :param model: the shared source list model; ownership of the
            model itself stays with the caller (the main window), so
            it can also be reached from drag-and-drop handling there.
        :param parent: optional Qt parent widget.
        """
        super().__init__(parent)
        self._model = model

        self._placeholder = self._build_placeholder()
        self._list_view = self._build_list_view()
        self._stack = QStackedWidget()
        self._stack.addWidget(self._placeholder)
        self._stack.addWidget(self._list_view)

        layout = QVBoxLayout(self)
        layout.addWidget(self._stack)
        layout.addLayout(self._build_button_row())

        self._model.rowsInserted.connect(self._sync_stack_page)
        self._model.rowsRemoved.connect(self._sync_stack_page)
        self._model.modelReset.connect(self._sync_stack_page)
        self._sync_stack_page()

    def _build_placeholder(self) -> QLabel:
        """Return the empty-state placeholder label."""
        placeholder = QLabel(tr("Drop PowerPoint files or folders here"))
        placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        placeholder.setStyleSheet(
            "border: 2px dashed gray; border-radius: 8px; color: gray;"
        )
        return placeholder

    def _build_list_view(self) -> QListView:
        """Return the source list view, bound to the shared model."""
        list_view = QListView()
        list_view.setModel(self._model)
        list_view.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        return list_view

    def _build_button_row(self) -> QHBoxLayout:
        """Return the "Add Files…"/"Add Folder…"/remove/clear row."""
        row = QHBoxLayout()

        add_files_button = QPushButton(tr("Add Files…"))
        add_files_button.clicked.connect(self.add_files)
        row.addWidget(add_files_button)

        add_folder_button = QPushButton(tr("Add Folder…"))
        add_folder_button.clicked.connect(self.add_folder)
        row.addWidget(add_folder_button)

        remove_button = QPushButton(tr("Remove Selected"))
        remove_button.clicked.connect(self.remove_selected)
        row.addWidget(remove_button)

        clear_button = QPushButton(tr("Clear All"))
        clear_button.clicked.connect(self._model.clear)
        row.addWidget(clear_button)

        return row

    def _sync_stack_page(self, *_args: object) -> None:
        """Show the list view once populated, the placeholder otherwise.

        :param _args: ignored signal arguments (``rowsInserted`` and
            ``rowsRemoved`` pass row-range details this method does
            not need).
        """
        page = self._list_view if self._model.rowCount() > 0 else self._placeholder
        self._stack.setCurrentWidget(page)

    def add_files(self) -> None:
        """Open a file-selection dialog and add the chosen files.

        Emits :attr:`sources_added` with the resulting
        :class:`~pptrepair.gui.sources.AddResult` when the dialog was
        confirmed, so the host window can report the outcome.
        """
        file_names, _selected_filter = QFileDialog.getOpenFileNames(
            self, tr("Add Files"), "", _file_dialog_filter()
        )
        if file_names:
            result = self._model.add_paths(Path(name) for name in file_names)
            self.sources_added.emit(result)

    def add_folder(self) -> None:
        """Open a folder-selection dialog and add the chosen folder.

        Emits :attr:`sources_added` with the resulting
        :class:`~pptrepair.gui.sources.AddResult` when the dialog was
        confirmed, so the host window can report the outcome.
        """
        directory = QFileDialog.getExistingDirectory(self, tr("Add Folder"))
        if directory:
            result = self._model.add_paths([Path(directory)])
            self.sources_added.emit(result)

    def remove_selected(self) -> None:
        """Remove the rows currently selected in the list view."""
        rows = {index.row() for index in self._list_view.selectedIndexes()}
        self._model.remove_rows(sorted(rows, reverse=True))
