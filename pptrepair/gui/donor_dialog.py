"""Donor-approval dialog for the desktop app's multi-source repair.

Before a multi-source repair runs, the user reviews -- and confirms --
which donors each corrupted target will be spliced against, mirroring the
CLI's per-source confirmation prompts. This dialog renders the
:class:`~pptrepair.gui.merge_plan.TargetPlan` list as a checkable tree
(one target per top-level item, its donors as checkable children) and,
on acceptance, reports the user's selection as
:class:`~pptrepair.gui.merge_plan.ApprovedMerge` records.

Selection policy matches :func:`pptrepair.merge.merge_restore`'s own
trust model: ``auto``-tier donors (a verified byte-identical copy) are
checked by default, while the weaker ``candidate``/``lineage`` tiers
start unchecked and must be opted into deliberately, since their content
is only cross-checked per entry rather than proven identical up front.
Everything here runs on the UI thread.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pptrepair.gui.i18n import tr
from pptrepair.gui.merge_plan import ApprovedMerge, DonorRef, TargetPlan


class DonorApprovalDialog(QDialog):
    """Modal tree of targets and their donors, checkable for approval.

    Built from a :class:`~pptrepair.gui.merge_plan.TargetPlan` sequence;
    after the user accepts (the OK button reads "Repair"),
    :meth:`approved_plans` reports one
    :class:`~pptrepair.gui.merge_plan.ApprovedMerge` per target carrying
    only the donors left checked. ``auto``-tier donors start checked, the
    weaker tiers unchecked; a target with no donor shows a single
    disabled placeholder child and yields an empty donor selection.
    """

    def __init__(self, plans: Sequence[TargetPlan],
                 parent: QWidget | None = None) -> None:
        """Build the tree from *plans* and wire up the button box.

        :param plans: the per-target donor plans to review, in order.
        :param parent: optional Qt parent widget.
        """
        super().__init__(parent)
        self.setWindowTitle(tr("Multi-source repair"))
        self._plans = list(plans)

        #: Maps each checkable donor child item to its DonorRef, so
        #: approved_plans can read the check states back out.
        self._donor_items: dict[QTreeWidgetItem, DonorRef] = {}
        #: One top-level item per plan, in the same order as _plans.
        self._target_items: list[QTreeWidgetItem] = []

        self._tree = self._build_tree()

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr(
            "Review the donors for each corrupted file, then click Repair.")))
        layout.addWidget(self._tree)
        layout.addWidget(self._build_tier_legend())
        layout.addWidget(self._build_button_box())

    def _build_tier_legend(self) -> QLabel:
        """Return the small, muted label explaining the ``[tier]`` tags.

        Each donor row is suffixed with its trust tier (see the module
        docstring); this legend spells out what the three tags mean, so
        the user does not have to guess before checking a box.
        """
        legend = QLabel("\n".join([
            tr("[auto] — verified same-origin donor (byte-level CRC "
               "evidence). Trusted and pre-checked."),
            tr("[candidate] — probably the same origin, but not fully "
               "verified. Check to use."),
            tr("[lineage] — a different version of the same document; "
               "merged content may differ slightly. Check only if that "
               "is acceptable."),
        ]))
        legend.setWordWrap(True)
        legend.setStyleSheet("color: gray; font-size: 11px;")
        return legend

    def _build_tree(self) -> QTreeWidget:
        """Return the target/donor tree, one branch per plan."""
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        for plan in self._plans:
            target_item = QTreeWidgetItem([str(plan.target)])
            tree.addTopLevelItem(target_item)
            self._target_items.append(target_item)
            if not plan.donors:
                self._add_placeholder(target_item)
            else:
                for donor in plan.donors:
                    self._add_donor(target_item, donor)
            target_item.setExpanded(True)
        return tree

    def _add_placeholder(self, target_item: QTreeWidgetItem) -> None:
        """Add the disabled "no donors" child under *target_item*."""
        child = QTreeWidgetItem([tr(
            "(no donors found — will fall back to single-file repair)")])
        # Neither selectable nor checkable: it is a pure annotation.
        child.setFlags(Qt.ItemFlag.NoItemFlags)
        target_item.addChild(child)

    def _add_donor(self, target_item: QTreeWidgetItem,
                   donor: DonorRef) -> None:
        """Add one checkable donor child under *target_item*.

        ``auto``-tier donors start checked; the weaker tiers start
        unchecked (see the module docstring for the rationale).
        """
        child = QTreeWidgetItem([f"{donor.display}  [{donor.tier}]"])
        child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        checked = donor.tier == "auto"
        child.setCheckState(
            0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        target_item.addChild(child)
        self._donor_items[child] = donor

    def _build_button_box(self) -> QDialogButtonBox:
        """Return the OK ("Repair") / Cancel button box, wired to the dialog."""
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_button.setText(tr("Repair"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        return buttons

    def approved_plans(self) -> list[ApprovedMerge]:
        """Return one :class:`ApprovedMerge` per target from the check states.

        Meant to be read after the dialog is accepted. Each target keeps
        only the donors whose child item is checked; ``allow_candidate``
        / ``allow_lineage`` are derived from the tiers of those checked
        donors. A target with no donor (or none left checked) yields an
        :class:`ApprovedMerge` with an empty ``donors`` tuple.
        """
        approved: list[ApprovedMerge] = []
        for plan, target_item in zip(self._plans, self._target_items):
            checked = self._checked_donors(target_item)
            approved.append(ApprovedMerge(
                target=plan.target,
                donors=tuple(checked),
                allow_candidate=any(d.tier == "candidate" for d in checked),
                allow_lineage=any(d.tier == "lineage" for d in checked)))
        return approved

    def _checked_donors(
            self, target_item: QTreeWidgetItem) -> list[DonorRef]:
        """Return the donors under *target_item* whose item is checked."""
        checked: list[DonorRef] = []
        for index in range(target_item.childCount()):
            child = target_item.child(index)
            donor = self._donor_items.get(child)
            if donor is None:
                continue  # the "no donors" placeholder
            if child.checkState(0) == Qt.CheckState.Checked:
                checked.append(donor)
        return checked
