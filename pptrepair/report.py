"""Human-readable and JSON rendering of diagnoses and repair outcomes.

This module is a thin facade: the actual rendering code lives in the
``pptrepair.report_*`` modules (split out to keep any single module
within a manageable size). Every name defined here is re-exported
verbatim from one of those modules, so ``from pptrepair.report import
X`` and ``pptrepair.report.X`` keep working exactly as before the
split.
"""

from __future__ import annotations

from pptrepair.report_batch import (
                                    _batch_item_to_dict,
                                    _count_lost_content,
                                    _repair_item_line,
                                    render_batch_json,
                                    render_batch_text,
)
from pptrepair.report_candidates import (
                                    _lineage_candidate_text_lines,
                                    _lineage_candidates_map,
                                    _lineage_candidates_to_json,
                                    _merge_group_map,
                                    _merge_group_text_lines,
                                    _merge_groups_to_json,
                                    _twin_candidate_text_lines,
                                    _twin_candidates_map,
                                    _twin_candidates_to_json,
                                    _twin_reason_label,
)
from pptrepair.report_check import (
                                    _integrity_to_dict,
                                    _structure_integrity_to_dict,
                                    _structure_to_dict,
                                    _timing_integrity_to_dict,
                                    _to_dict,
                                    render_json,
                                    render_text,
)
from pptrepair.report_common import (
                                    _LINEAGE_CANDIDATES_DISPLAY_LIMIT,
                                    _MERGE_GROUP_MIN_FILES,
                                    _TWIN_CANDIDATES_DISPLAY_LIMIT,
                                    ISSUE_URL,
                                    VERDICT_LABELS,
)
from pptrepair.report_repair import render_repair_json, render_repair_text
from pptrepair.report_scan import (
                                    _collect_errors,
                                    _scan_file_entry,
                                    _scan_payload,
                                    _scan_summary_lines,
                                    render_scan_json,
                                    render_scan_text,
)
