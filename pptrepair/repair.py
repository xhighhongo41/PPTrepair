"""Repair orchestration: diagnose, choose a mode, dispatch, re-verify.

The outcome object carries language-neutral data only; rendering (and
therefore translation) lives in :mod:`pptrepair.report`, except for the
recovery folder's internal text files, which are produced by
:mod:`pptrepair.extract` with the translator handed down from here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from pptrepair.census import (CensusResult, from_central_directory,
                              from_lfh_scan)
from pptrepair.classify import Diagnosis, Verdict, classify
from pptrepair.extract import ExtractResult, extract_salvage
from pptrepair.i18n import get_translator
from pptrepair.rebuild import RebuildResult, rebuild_package
from pptrepair.salvage import SalvagedEntry, SalvageReader, select_salvageable
from pptrepair.scanner import scan_structure

#: Name of the part a rebuild absolutely requires among the salvaged set.
_PRESENTATION_NAME = "ppt/presentation.xml"

#: Matches a salvaged/lost slide part name, capturing its slide number.
_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")

#: Valid --mode values.
MODES = ("auto", "rebuild", "extract")

#: Suffix conventions for default output paths (next to the input).
REBUILD_SUFFIX = ".repaired.pptx"
EXTRACT_SUFFIX = ".salvaged"


@dataclass
class RepairOutcome:
    """Language-neutral result of one repair run."""

    src: Path
    diagnosis: Diagnosis
    mode: str
    """Executed mode: ``"rebuild"`` | ``"extract"`` | ``"none"``
    (nothing to do / nothing possible)."""
    success: bool
    """True when a repair artifact was produced, or when the input was
    already intact (nothing to do counts as success)."""
    output_path: Path | None = None
    salvaged: list[SalvagedEntry] = field(default_factory=list)
    rebuild_result: RebuildResult | None = None
    extract_result: ExtractResult | None = None
    recheck_verdict: str | None = None
    """`check` verdict of the rebuilt file (rebuild mode only)."""
    lost_slide_numbers: list[int] = field(default_factory=list)
    """Slide numbers known to be lost (exact when a CD survived)."""
    lost_entries_total: int = 0
    warnings: list[str] = field(default_factory=list)


def default_output_path(src: Path, mode: str) -> Path:
    """Return the default artifact path next to *src* for *mode*."""
    if mode == "rebuild":
        return src.with_name(src.stem + REBUILD_SUFFIX)
    return src.with_name(src.stem + EXTRACT_SUFFIX)


def repair_file(src: Path, output: Path | None = None, mode: str = "auto",
                force: bool = False, lang: str = "en") -> RepairOutcome:
    """Diagnose *src* and produce the best possible repair artifact.

    Behaviour:

    * The input is opened read-only throughout; nothing is ever written
      next to it except the requested artifact.
    * Automatic mode selection: ``normal`` -> mode "none", success,
      no artifact. ``not_a_zip`` or an empty salvage set -> mode
      "none", failure. A salvage set containing
      ``ppt/presentation.xml`` and at least one slide -> "rebuild",
      anything else -> "extract".
    * ``--mode rebuild``/``extract`` force the respective path;
      rebuild without a salvageable ``ppt/presentation.xml`` raises
      :class:`pptrepair.rebuild.RebuildError` (reported by the CLI as
      an unrepairable input).
    * *output* defaults to :func:`default_output_path`. An existing
      output path raises :class:`OutputExistsError` unless *force* is
      true (an existing extract *directory* is likewise refused).
    * After a rebuild, the artifact is re-diagnosed with the check
      pipeline and the verdict recorded in ``recheck_verdict``.
    * ``lost_slide_numbers`` / ``lost_entries_total`` are computed from
      the diagnosis (exact when the central directory survived).
    * *lang* selects the translator handed to
      :func:`pptrepair.extract.extract_salvage` for the text files
      inside the recovery folder; the outcome object itself stays
      language-neutral.
    """
    diagnosis = _diagnose(src)
    salvaged, salvage_warnings = select_salvageable(diagnosis)
    lost_slide_numbers, lost_entries_total = _lost_entries(diagnosis)

    outcome = RepairOutcome(
        src=src,
        diagnosis=diagnosis,
        mode="none",
        success=False,
        salvaged=salvaged,
        lost_slide_numbers=lost_slide_numbers,
        lost_entries_total=lost_entries_total,
        warnings=list(salvage_warnings),
    )

    chosen_mode = _select_auto_mode(diagnosis, salvaged) if mode == "auto" \
        else mode

    if chosen_mode == "none":
        # Automatic selection only: either the input was already intact,
        # or nothing could be salvaged from it at all.
        outcome.success = diagnosis.verdict == Verdict.NORMAL
        return outcome

    if chosen_mode == "extract" and not salvaged:
        # A forced extract with nothing to write; report failure without
        # touching the filesystem or requiring an output path.
        outcome.mode = "extract"
        outcome.warnings.append(
            "nothing salvageable: no recovery folder was written")
        return outcome

    output_path = (output if output is not None
                   else default_output_path(src, chosen_mode))
    _ensure_output_available(output_path, chosen_mode, force)
    outcome.mode = chosen_mode
    outcome.output_path = output_path

    if chosen_mode == "rebuild":
        _run_rebuild(src, salvaged, output_path, outcome)
    else:
        _run_extract(src, salvaged, output_path, lang, outcome)

    return outcome


def _diagnose(path: Path) -> Diagnosis:
    """Run the scan -> census -> classify pipeline over *path*.

    Same pipeline as ``pptrepair check``, reused here both for the
    initial diagnosis and for the post-rebuild re-check.
    """
    structure = scan_structure(path)
    cd_census = from_central_directory(path)
    lfh_census = from_lfh_scan(path)
    return classify(path, structure, cd_census, lfh_census)


def _select_auto_mode(diagnosis: Diagnosis,
                      salvaged: list[SalvagedEntry]) -> str:
    """Choose the repair mode for ``--mode auto``.

    See :func:`repair_file` for the decision rules.
    """
    if diagnosis.verdict == Verdict.NORMAL:
        return "none"
    if diagnosis.verdict == Verdict.NOT_A_ZIP or not salvaged:
        return "none"
    names = {entry.name for entry in salvaged}
    has_slide = any(entry.category == "slide_xml" for entry in salvaged)
    if _PRESENTATION_NAME in names and has_slide:
        return "rebuild"
    return "extract"


def _lost_entries(diagnosis: Diagnosis) -> tuple[list[int], int]:
    """Compute ``(lost_slide_numbers, lost_entries_total)`` for *diagnosis*.

    Uses the same census :func:`pptrepair.salvage.select_salvageable`
    trusts as its source: the LFH census for ``VERSION_MIX`` /
    ``TAIL_TRUNCATED``, the central-directory census otherwise (falling
    back to the LFH census when the central directory is unavailable).
    """
    verdict = diagnosis.verdict
    if verdict in (Verdict.VERSION_MIX, Verdict.TAIL_TRUNCATED):
        census: CensusResult | None = diagnosis.lfh_census
    elif diagnosis.cd_census is not None:
        census = diagnosis.cd_census
    else:
        census = diagnosis.lfh_census

    if census is None:
        return [], 0

    lost_slide_numbers: list[int] = []
    lost_entries_total = 0
    for entry in census.entries:
        if entry.ok:
            continue
        lost_entries_total += 1
        match = _SLIDE_RE.match(entry.name)
        if match:
            lost_slide_numbers.append(int(match.group(1)))
    return sorted(lost_slide_numbers), lost_entries_total


def _ensure_output_available(output_path: Path, mode: str,
                             force: bool) -> None:
    """Raise :class:`OutputExistsError` when *output_path* is already taken.

    Rebuild targets a single file, extract a directory; an existing
    target is accepted only when *force* is true (an existing extract
    directory is then appended to, never cleared first).
    """
    if force:
        return
    if mode == "rebuild" and output_path.exists():
        raise OutputExistsError(f"output file already exists: {output_path}")
    if mode == "extract" and output_path.is_dir():
        raise OutputExistsError(
            f"output directory already exists: {output_path}")


def _run_rebuild(src: Path, salvaged: list[SalvagedEntry], output_path: Path,
                 outcome: RepairOutcome) -> None:
    """Rebuild *output_path* from *salvaged* and re-diagnose the result.

    :raises pptrepair.rebuild.RebuildError: propagated unchanged when the
        salvage set lacks a usable ``ppt/presentation.xml``.
    """
    with SalvageReader(src) as reader:
        result = rebuild_package(reader, salvaged, output_path)
    outcome.rebuild_result = result
    outcome.warnings.extend(result.warnings)
    outcome.success = True
    recheck = _diagnose(output_path)
    outcome.recheck_verdict = recheck.verdict.value


def _run_extract(src: Path, salvaged: list[SalvagedEntry], output_path: Path,
                 lang: str, outcome: RepairOutcome) -> None:
    """Write the recovery folder at *output_path* from *salvaged*."""
    tr = get_translator(lang)
    with SalvageReader(src) as reader:
        result = extract_salvage(reader, salvaged, output_path, tr)
    outcome.extract_result = result
    outcome.warnings.extend(result.warnings)
    outcome.success = True


class OutputExistsError(Exception):
    """Raised when the output path exists and --force was not given."""
