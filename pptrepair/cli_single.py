"""Single-file ``check``/``repair``/``salvage`` command implementations.

Split out of :mod:`pptrepair.cli` to keep that module within a
manageable size; see :mod:`pptrepair.cli` for the argument parser and
the ``main`` dispatch that use these functions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable

from pptrepair import i18n
from pptrepair import repair as repair_module
from pptrepair import rescue as rescue_module
from pptrepair import scan as scan_module
from pptrepair.classify import Diagnosis, Verdict
from pptrepair.exit_codes import EXIT_CORRUPT, EXIT_ERROR, EXIT_OK
from pptrepair.integrity import (RefIntegrityResult, StructureIntegrityResult,
                                 TimingIntegrityResult, inspect_references,
                                 inspect_structure, inspect_timing)
from pptrepair.rebuild import RebuildError
from pptrepair.report import (render_json, render_repair_json,
                              render_repair_text, render_text)


def run_check(files: list[str], json_output: bool) -> int:
    """Diagnose *files*, print reports to stdout, and return an exit code.

    Implementation requirements:

    * Run the scanner -> census -> classify pipeline per file.
    * A nonexistent or unreadable path prints an error to stderr and
      forces exit code 2, but remaining files are still processed.
    * A file diagnosed as NORMAL is additionally passed through
      :func:`pptrepair.integrity.inspect_references`,
      :func:`pptrepair.integrity.inspect_timing` and
      :func:`pptrepair.integrity.inspect_structure`; an unexpected
      failure in any of the three is reported the same way as a
      diagnosis failure (stderr + exit code 2), with all three results
      falling back to None, but does not stop the remaining files. Any
      other verdict skips this pass entirely (all three results stay
      None), since check's exit code never depends on any of them
      either way.
    * With ``json_output`` a single JSON array covering all successfully
      diagnosed files goes to stdout; otherwise one text report per
      file.
    * Exit code: 2 on any per-file error, else 1 if any verdict is not
      NORMAL, else 0. None of the three integrity results ever changes
      this: a package with dangling references, timing inconsistencies
      or missing structural relationships is still reported as normal
      (see ``開発資料/v1.1.2実装計画.md`` §4.3 and §10 addendum item C
      for the rationale).
    """
    had_error = False
    diagnoses: list[Diagnosis] = []
    integrities: list[RefIntegrityResult | None] = []
    timings: list[TimingIntegrityResult | None] = []
    structures: list[StructureIntegrityResult | None] = []

    for file in files:
        diagnosis, error_message = _diagnose_file(file)
        if error_message is not None:
            print(f"pptrepair: error: {error_message}", file=sys.stderr)
            had_error = True
            continue
        assert diagnosis is not None
        diagnoses.append(diagnosis)

        integrity: RefIntegrityResult | None = None
        timing: TimingIntegrityResult | None = None
        structure: StructureIntegrityResult | None = None
        if diagnosis.verdict == Verdict.NORMAL:
            try:
                integrity = inspect_references(Path(file))
                timing = inspect_timing(Path(file))
                structure = inspect_structure(Path(file))
            except Exception as exc:
                # Defensive: a verdict of NORMAL already implies the
                # archive opened cleanly once, so this should not
                # normally trigger.
                print(f"pptrepair: error: {file}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                had_error = True
                integrity = timing = structure = None
        integrities.append(integrity)
        timings.append(timing)
        structures.append(structure)

    if json_output:
        print(render_json(diagnoses, integrities, timings, structures))
    else:
        for index, diagnosis in enumerate(diagnoses):
            if index > 0:
                print()
            print(render_text(diagnosis, ref_integrity=integrities[index],
                              timing=timings[index],
                              structure=structures[index]))

    if had_error:
        return EXIT_ERROR
    if any(diagnosis.verdict != Verdict.NORMAL for diagnosis in diagnoses):
        return EXIT_CORRUPT
    return EXIT_OK


def _diagnose_file(file: str) -> tuple[Diagnosis | None, str | None]:
    """Run the scan/census/classify pipeline on one file.

    Returns ``(diagnosis, None)`` on success, or ``(None, message)`` when
    the path is unusable or the pipeline raises; *message* is meant to
    be printed to stderr and never includes the ``pptrepair: error:``
    prefix (added by the caller). The pipeline itself lives in
    :func:`pptrepair.scan.diagnose_file`, shared with ``scan``.
    """
    return scan_module.diagnose_file(Path(file))


def run_repair(file: str, output: str | None, mode: str, force: bool,
               lang: str, json_output: bool) -> int:
    """Repair one file, print the report, and return an exit code.

    Implementation requirements:

    * Validate the input path like ``run_check`` (stderr + exit 2 on a
      missing/non-regular file); catch unexpected pipeline exceptions
      the same way.
    * Call :func:`pptrepair.repair.repair_file`;
      :class:`pptrepair.repair.OutputExistsError` prints a translated
      hint to use ``--force`` and returns 2;
      :class:`pptrepair.rebuild.RebuildError` (forced rebuild without a
      presentation part) reports an unrepairable input and returns 1.
    * Print :func:`pptrepair.report.render_repair_json` when
      *json_output* is set, otherwise
      :func:`pptrepair.report.render_repair_text` with the *lang*
      translator; in extract mode also write that text as
      ``REPORT.txt`` inside the recovery folder (UTF-8).
    * Exit code: 0 when ``outcome.success`` (artifact produced, or the
      input was already intact), 1 when nothing was recoverable, 2 on
      usage/IO errors.
    """
    path = Path(file)
    if not path.exists():
        print(f"pptrepair: error: {file}: no such file", file=sys.stderr)
        return EXIT_ERROR
    if not path.is_file():
        print(f"pptrepair: error: {file}: not a regular file",
              file=sys.stderr)
        return EXIT_ERROR

    tr = i18n.get_translator(lang)
    output_path = Path(output) if output is not None else None

    try:
        outcome = repair_module.repair_file(
            path, output_path, mode, force, lang)
    except repair_module.OutputExistsError as exc:
        print(f"pptrepair: error: {exc}", file=sys.stderr)
        print(tr("Hint: pass --force to overwrite the existing output."),
              file=sys.stderr)
        return EXIT_ERROR
    except RebuildError as exc:
        print(f"pptrepair: error: unrepairable: {exc}", file=sys.stderr)
        return EXIT_CORRUPT
    except Exception as exc:
        # Any other pipeline failure (bad input, I/O error while reading
        # or writing) is reported the same way run_check reports one.
        print(f"pptrepair: error: {file}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_ERROR

    if json_output:
        print(render_repair_json(outcome))
    else:
        text = render_repair_text(outcome, tr)
        print(text)
        if outcome.mode == "extract" and outcome.success:
            assert outcome.output_path is not None
            report_path = outcome.output_path / "REPORT.txt"
            report_path.write_text(text, encoding="utf-8")

    return EXIT_OK if outcome.success else EXIT_CORRUPT


def run_salvage(file: str, output: str | None, force: bool, lang: str,
                json_output: bool) -> int:
    """Rescue surviving content from one file and return an exit code.

    Implementation requirements:

    * Validate the input path like ``run_repair`` (stderr + exit 2 on a
      missing/non-regular file); catch unexpected pipeline exceptions the
      same way.
    * Call :func:`pptrepair.rescue.rescue_file`;
      :class:`pptrepair.repair.OutputExistsError` prints a translated
      hint to use ``--force`` and returns 2.
    * A ``NORMAL`` verdict prints a translated "nothing to salvage"
      notice (no output folder was created) and returns 0.
    * Otherwise print :func:`_render_salvage_summary` with the *lang*
      translator, or the report JSON when *json_output* is set.
    * Exit code: 0 when at least one item was rescued (or the input was
      already intact), 1 when nothing could be rescued, 2 on usage/IO
      errors.
    """
    path = Path(file)
    if not path.exists():
        print(f"pptrepair: error: {file}: no such file", file=sys.stderr)
        return EXIT_ERROR
    if not path.is_file():
        print(f"pptrepair: error: {file}: not a regular file",
              file=sys.stderr)
        return EXIT_ERROR

    tr = i18n.get_translator(lang)
    output_path = Path(output) if output is not None else None

    try:
        result = rescue_module.rescue_file(
            path, output_path, force=force, lang=lang)
    except repair_module.OutputExistsError as exc:
        print(f"pptrepair: error: {exc}", file=sys.stderr)
        print(tr("Hint: pass --force to overwrite the existing output."),
              file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        # Any other pipeline failure (bad input, I/O error) is reported
        # the way run_check/run_repair report one.
        print(f"pptrepair: error: {file}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return EXIT_ERROR

    if result.verdict == Verdict.NORMAL:
        if json_output:
            print(json.dumps(result.report, indent=2, ensure_ascii=False))
        else:
            print(tr("Nothing to salvage: the file is already an intact "
                     "PowerPoint package."))
        return EXIT_OK

    if json_output:
        print(json.dumps(result.report, indent=2, ensure_ascii=False))
    else:
        print(_render_salvage_summary(result, tr))

    return EXIT_OK if result.rescued_count() > 0 else EXIT_CORRUPT


def _render_salvage_summary(result: rescue_module.RescueResult,
                            tr: Callable[[str], str]) -> str:
    """Render the human-readable per-stage summary of a rescue run."""
    lines = [tr("=== Salvage summary ===")]
    if result.output_dir is not None:
        lines.append(tr("Output: {path}").format(path=result.output_dir))
    lines.append(tr("Entries recovered: {n}").format(n=result.entries_saved))
    lines.append(tr("Images carved: {n}").format(n=result.carved_images))
    lines.append(tr("Partial XML parts: {n}").format(n=result.partial_xml))
    lines.append(
        tr("Text lines recovered: {n}").format(n=result.text_lines))
    if result.carved_images > 0:
        lines.append(tr("Note: carved images may contain unrelated data "
                        "that overwrote the file."))
    if result.rescued_count() == 0:
        lines.append(tr("Nothing could be salvaged from this file."))
    if result.warnings:
        lines.append(tr("Warnings:"))
        lines.extend(f"  {warning}" for warning in result.warnings)
    return "\n".join(lines)
