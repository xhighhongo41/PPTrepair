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
from pptrepair.integrity import (inspect_references, inspect_structure,
                                 inspect_timing)
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
    """Executed mode: ``"rebuild"`` | ``"extract"`` | ``"trim"`` |
    ``"none"`` (nothing to do / nothing possible)."""
    success: bool
    """True when a repair artifact was produced, or when the input was
    already intact (nothing to do counts as success)."""
    output_path: Path | None = None
    salvaged: list[SalvagedEntry] = field(default_factory=list)
    rebuild_result: RebuildResult | None = None
    extract_result: ExtractResult | None = None
    recheck_verdict: str | None = None
    """`check` verdict of the produced artifact (rebuild/trim modes
    only)."""
    recheck_dangling_refs: int | None = None
    """Number of dangling relationship references (see
    :mod:`pptrepair.integrity`) found in the produced artifact by the
    post-repair recheck (rebuild/trim modes only); None otherwise."""
    recheck_timing_issues: int | None = None
    """Number of ``p:timing`` shape-id inconsistencies (dangling ``spid``
    references plus media/shape mismatches; see
    :func:`pptrepair.integrity.inspect_timing`) found in the produced
    artifact by the post-repair recheck (rebuild/trim modes only); None
    otherwise."""
    recheck_structure_issues: int | None = None
    """Number of missing required structural relationships (see
    :func:`pptrepair.integrity.inspect_structure`) found in the produced
    artifact by the post-repair recheck (rebuild/trim modes only); None
    otherwise."""
    lost_slide_numbers: list[int] = field(default_factory=list)
    """Slide numbers known to be lost (exact when a CD survived)."""
    lost_entries_total: int = 0
    trimmed_bytes: int | None = None
    """Set only on a successful trim: the number of unindexed bytes that
    followed the EOCD record and were removed."""
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
    * Automatic mode selection: see :func:`_select_auto_mode` for the
      full decision order. In short: ``normal`` -> mode "none",
      success, no artifact; ``not_a_zip``/``empty_file``/
      ``full_zero_fill`` -> mode "none", failure;
      ``tail_foreign_data`` -> "trim" (trailing unindexed data is cut
      off, falling back to rebuild/extract when the trimmed archive
      does not itself check out clean); an empty salvage set -> mode
      "none", failure; a salvage set containing
      ``ppt/presentation.xml`` and at least one slide -> "rebuild",
      anything else -> "extract".
    * ``--mode rebuild``/``extract`` force the respective path;
      rebuild without a salvageable ``ppt/presentation.xml`` raises
      :class:`pptrepair.rebuild.RebuildError` (reported by the CLI as
      an unrepairable input).
    * *output* defaults to :func:`default_output_path`. An existing
      output path raises :class:`OutputExistsError` unless *force* is
      true (an existing extract *directory* is likewise refused).
    * After a rebuild or a successful trim, the artifact is
      re-diagnosed with the check pipeline and the verdict recorded in
      ``recheck_verdict``, and separately re-checked with
      :func:`pptrepair.integrity.inspect_references`,
      :func:`pptrepair.integrity.inspect_timing` and
      :func:`pptrepair.integrity.inspect_structure`, whose counts are
      recorded in ``recheck_dangling_refs``, ``recheck_timing_issues``
      and ``recheck_structure_issues`` respectively (the timing count
      combines dangling ``spid`` references and media/shape mismatches).
      A positive count on a rebuild artifact also appends a warning for
      each affected check (rebuild's own cleanup should already have
      removed them, so this signals a cleanup bug); a positive count
      after a trim is recorded without a warning, since trim reproduces
      the original archive byte-for-byte and any inconsistency predates
      this tool.
    * ``lost_slide_numbers`` / ``lost_entries_total`` are computed from
      the diagnosis (exact when the central directory survived; both
      reset to empty/zero on a successful trim, which loses nothing).
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

    if chosen_mode == "trim":
        output_path = (output if output is not None
                       else default_output_path(src, "rebuild"))
        _ensure_output_available(output_path, "rebuild", force)
        if _run_trim(src, output_path, outcome):
            return outcome
        # The trimmed archive did not check out clean: fall back to the
        # usual salvage-based selection, as if trim had never been tried.
        chosen_mode = _select_salvage_mode(salvaged)
        if chosen_mode == "none":
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

    Decision order (first match wins):

    1. ``normal`` -> "none" (nothing to do).
    2. ``not_a_zip`` / ``empty_file`` / ``full_zero_fill`` -> "none"
       (no content survives to rebuild from).
    3. ``tail_foreign_data`` -> "trim" (cut the unindexed trailing
       data off; :func:`repair_file` falls back to salvage-based
       repair when the trimmed archive does not check out clean).
    4. no salvaged entries -> "none".
    5. a salvage set containing ``ppt/presentation.xml`` and at least
       one slide -> "rebuild", anything else -> "extract".
    """
    if diagnosis.verdict == Verdict.NORMAL:
        return "none"
    if diagnosis.verdict in (Verdict.NOT_A_ZIP, Verdict.EMPTY_FILE,
                             Verdict.FULL_ZERO_FILL):
        return "none"
    if diagnosis.verdict == Verdict.TAIL_FOREIGN_DATA:
        return "trim"
    return _select_salvage_mode(salvaged)


def _select_salvage_mode(salvaged: list[SalvagedEntry]) -> str:
    """Choose "none"/"rebuild"/"extract" from a salvage set alone.

    This is the tail of :func:`_select_auto_mode`'s decision order
    (steps 4-5), factored out so :func:`repair_file` can reapply it as
    the fallback when a "trim" attempt does not check out clean.
    """
    if not salvaged:
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
    ``TAIL_TRUNCATED`` / ``TAIL_FOREIGN_DATA``, the central-directory
    census otherwise (falling back to the LFH census when the central
    directory is unavailable).
    """
    verdict = diagnosis.verdict
    if verdict in (Verdict.VERSION_MIX, Verdict.TAIL_TRUNCATED,
                  Verdict.TAIL_FOREIGN_DATA):
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

    Besides the check-pipeline re-diagnosis, the artifact is
    self-checked with :func:`pptrepair.integrity.inspect_references`,
    :func:`pptrepair.integrity.inspect_timing` and
    :func:`pptrepair.integrity.inspect_structure`: a positive count on
    any of the three means rebuild's own cleanup left something behind,
    which is a cleanup bug worth surfacing rather than silently
    shipping, so each appends its own warning.

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

    # Self-check the cleanup rebuild_package just performed: a positive
    # count here means a dangling reference slipped past it, which is a
    # cleanup bug worth surfacing rather than silently shipping.
    integrity = inspect_references(output_path)
    outcome.recheck_dangling_refs = len(integrity.dangling)
    if outcome.recheck_dangling_refs > 0:
        outcome.warnings.append(
            f"rebuild artifact still contains "
            f"{outcome.recheck_dangling_refs} dangling relationship "
            "reference(s)")

    # Same self-check for the p:timing shape-id inconsistencies (dangling
    # spid references plus media/shape mismatches) rebuild's cleanup is
    # meant to resolve.
    timing = inspect_timing(output_path)
    outcome.recheck_timing_issues = (
        len(timing.dangling) + len(timing.media_mismatch))
    if outcome.recheck_timing_issues > 0:
        outcome.warnings.append(
            f"rebuild artifact has {outcome.recheck_timing_issues} "
            "inconsistent timing reference(s)")

    # Same self-check for required structural relationships (e.g. a slide
    # master that lost its theme relationship when the theme part itself
    # was unsalvageable).
    structure = inspect_structure(output_path)
    outcome.recheck_structure_issues = len(structure.missing)
    if outcome.recheck_structure_issues > 0:
        outcome.warnings.append(
            f"rebuild artifact is missing {outcome.recheck_structure_issues}"
            " required structural relationship(s)")


#: Chunk size (bytes) used when copying the leading archive during trim,
#: so multi-gigabyte inputs are never read into memory in one shot.
_TRIM_CHUNK_SIZE = 8 * 1024 * 1024


def _run_trim(src: Path, output_path: Path, outcome: RepairOutcome) -> bool:
    """Copy the leading, EOCD-terminated archive out of *src*, dropping
    the foreign data appended after it (``TAIL_FOREIGN_DATA``).

    On success, the trimmed archive is also re-checked with
    :func:`pptrepair.integrity.inspect_references`,
    :func:`pptrepair.integrity.inspect_timing` and
    :func:`pptrepair.integrity.inspect_structure`; every count is only
    recorded, never warned about, since trim reproduces the original
    archive byte-for-byte and any inconsistency found predates this tool.

    :returns: True and updates *outcome* for success (the trimmed
        archive re-diagnoses as ``NORMAL``); False when trim did not
        produce a clean archive (or the EOCD geometry is unusable), in
        which case *output_path* is left absent and the caller should
        fall back to salvage-based repair.
    """
    structure = outcome.diagnosis.structure
    eocd = structure.eocd
    eocd_end = eocd.offset + 22 + eocd.comment_length
    if eocd_end > structure.size:
        # Defensive guard: a malformed comment length would place the
        # "leading archive" past the end of the file itself.
        outcome.warnings.append(
            "trim failed: EOCD comment field extends past the end of "
            "the file")
        return False

    # src stays read-only; only the leading eocd_end bytes are copied.
    with open(src, "rb") as src_file, open(output_path, "wb") as dst_file:
        remaining = eocd_end
        while remaining > 0:
            chunk = src_file.read(min(_TRIM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            dst_file.write(chunk)
            remaining -= len(chunk)

    recheck = _diagnose(output_path)
    if recheck.verdict == Verdict.NORMAL:
        outcome.mode = "trim"
        outcome.success = True
        outcome.output_path = output_path
        outcome.trimmed_bytes = structure.size - eocd_end
        outcome.recheck_verdict = recheck.verdict.value
        # Trim never touches the leading archive's bytes, so any
        # dangling reference found here predates this tool; it is only
        # recorded, not warned about (see repair_file's docstring).
        integrity = inspect_references(output_path)
        outcome.recheck_dangling_refs = len(integrity.dangling)
        timing = inspect_timing(output_path)
        outcome.recheck_timing_issues = (
            len(timing.dangling) + len(timing.media_mismatch))
        structure_result = inspect_structure(output_path)
        outcome.recheck_structure_issues = len(structure_result.missing)
        # Trim keeps every indexed entry; a NORMAL re-check means the
        # whole central directory read back cleanly, so nothing is lost.
        outcome.lost_slide_numbers = []
        outcome.lost_entries_total = 0
        return True

    output_path.unlink()
    outcome.warnings.append(
        f"trim produced a {recheck.verdict.value} archive; falling back "
        "to salvage-based repair")
    return False


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
