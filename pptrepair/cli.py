"""Command-line interface.

``pptrepair check [--json] FILE [FILE ...]``
``pptrepair repair-all (-o OUTDIR | --in-place) DIR [DIR ...]``

Exit codes:

* 0 — every examined file is an intact PowerPoint package (``repair``/
  ``repair-all``: every corrupted file found was successfully repaired)
* 1 — at least one file is corrupted (or not a ZIP at all); for
  ``repair``/``repair-all`` this means at least one corrupted file was
  left unrepaired (unrepairable, an existing output skipped without
  ``--force``, or a repair failure)
* 2 — usage error, unreadable path, or unexpected internal error

All inputs are opened read-only; this tool never writes to the files
it examines (``repair``/``repair-all`` write only their repair
artifact(s); ``scan --report``/``repair-all --report`` write only
inside their report directory).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Callable

import pptrepair
from pptrepair import batch as batch_module
from pptrepair import i18n
from pptrepair import repair as repair_module
from pptrepair import rescue as rescue_module
from pptrepair import scan as scan_module
from pptrepair.classify import Diagnosis, Verdict
from pptrepair.integrity import (RefIntegrityResult, StructureIntegrityResult,
                                 TimingIntegrityResult, inspect_references,
                                 inspect_structure, inspect_timing)
from pptrepair.rebuild import RebuildError
from pptrepair.report import (render_batch_json, render_batch_text,
                              render_json, render_repair_json,
                              render_repair_text, render_scan_json,
                              render_scan_text, render_text)

EXIT_OK = 0
EXIT_CORRUPT = 1
EXIT_ERROR = 2


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser (subcommand: ``check``)."""
    parser = argparse.ArgumentParser(
        prog="pptrepair",
        description=(
            "Diagnose PowerPoint .pptx files corrupted while stored on "
            "OneDrive."
        ),
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {pptrepair.__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser(
        "check",
        help="classify files as intact or as a known corruption pattern",
        description=(
            "Inspect each FILE and report whether it is an intact "
            "PowerPoint package or matches a known OneDrive corruption "
            "pattern. Files are opened read-only."
        ),
    )
    check.add_argument("files", metavar="FILE", nargs="+",
                       help=".pptx file(s) to examine")
    check.add_argument("--json", action="store_true", dest="json_output",
                       help="emit a JSON array instead of text reports")

    repair = subparsers.add_parser(
        "repair",
        help="repair a corrupted file, or salvage its surviving content",
        description=(
            "Diagnose FILE and produce the best possible repair "
            "artifact: a rebuilt .pptx when the package can be made "
            "consistent again, or a recovery folder of salvaged "
            "images, media and text otherwise. The input file itself "
            "is never modified."
        ),
    )
    repair.add_argument("file", metavar="FILE",
                        help="corrupted .pptx file to repair")
    repair.add_argument("-o", "--output", metavar="PATH", default=None,
                        help=(
                            "output path (default: <name>.repaired.pptx "
                            "or <name>.salvaged/ next to FILE)"
                        ))
    repair.add_argument("--mode", choices=repair_module.MODES,
                        default="auto",
                        help="repair strategy (default: auto)")
    repair.add_argument("--force", action="store_true",
                        help="overwrite an existing output path")
    repair.add_argument("--lang", choices=i18n.SUPPORTED_LANGUAGES,
                        default=i18n.DEFAULT_LANGUAGE,
                        help="language of the human-readable report "
                             "(default: en)")
    repair.add_argument("--json", action="store_true", dest="json_output",
                        help="emit a JSON object instead of a text report")

    salvage = subparsers.add_parser(
        "salvage",
        help="rescue readable content from an unrepairable file",
        description=(
            "Pull whatever content still survives out of a badly damaged "
            "FILE: readable package entries, images carved from the raw "
            "bytes, partially decoded XML and best-effort text. The "
            "input file itself is never modified."
        ),
    )
    salvage.add_argument("file", metavar="FILE",
                         help="corrupted .pptx file to salvage")
    salvage.add_argument("-o", "--output", metavar="OUTDIR", default=None,
                         help=("output folder (default: <name>.rescued/ "
                               "next to FILE)"))
    salvage.add_argument("--force", action="store_true",
                         help="reuse an existing output folder")
    salvage.add_argument("--lang", choices=i18n.SUPPORTED_LANGUAGES,
                         default=i18n.DEFAULT_LANGUAGE,
                         help="language of the human-readable summary "
                              "(default: en)")
    salvage.add_argument("--json", action="store_true", dest="json_output",
                         help="emit the salvage report as JSON instead of "
                              "a text summary")

    scan = subparsers.add_parser(
        "scan",
        help="recursively scan directories for corrupted PowerPoint files",
        description=(
            "Walk each DIR recursively, diagnose every .pptx/.pptm file "
            "found, and print a summary of intact and corrupted files. "
            "Files are opened read-only; nothing is written unless "
            "--report is given."
        ),
    )
    scan.add_argument("roots", metavar="DIR", nargs="+",
                      help="directory (or file) to scan")
    scan.add_argument("--report", metavar="DIR", default=None,
                      help=(
                          "write scan_report.txt / scan_report.json and, "
                          "for unknown corruption patterns, shareable "
                          "anonymous diagnostic fingerprints into DIR"
                      ))
    scan.add_argument("--force", action="store_true",
                      help="reuse an existing --report directory")
    scan.add_argument("--all", action="store_true", dest="show_all",
                      help="list every scanned file, not only corrupted "
                           "ones")
    scan.add_argument("--lang", choices=i18n.SUPPORTED_LANGUAGES,
                      default=i18n.DEFAULT_LANGUAGE,
                      help="language of the human-readable output "
                           "(default: en)")
    scan.add_argument("--json", action="store_true", dest="json_output",
                      help="emit a JSON object instead of text output")
    scan.add_argument("--follow-symlinks", action="store_true",
                      help="follow symbolic links while walking "
                           "(default: ignore them)")
    scan.add_argument("--include-filenames", action="store_true",
                      help="include file basenames in diagnostic "
                           "fingerprints (default: anonymous)")
    scan.add_argument("--allow-download", action="store_true",
                      help=(
                          "also read cloud-only placeholder files; this "
                          "makes the sync client download their content "
                          "and may take long and use significant disk "
                          "space (default: skip them without downloading)"
                      ))

    repair_all = subparsers.add_parser(
        "repair-all",
        help="recursively repair corrupted PowerPoint files under one or "
             "more directories",
        description=(
            "Walk each DIR recursively, diagnose every .pptx/.pptm file "
            "found (like scan), and repair every corrupted one, either "
            "into an aggregate output directory that mirrors the input "
            "tree or, with --in-place, next to its own source. Files are "
            "opened read-only; only the produced artifacts (and, with "
            "--report, the report directory) are written."
        ),
    )
    repair_all.add_argument("roots", metavar="DIR", nargs="+",
                            help="directory (or file) to scan and repair")
    output_group = repair_all.add_mutually_exclusive_group(required=True)
    output_group.add_argument(
        "-o", "--output-dir", metavar="OUTDIR", default=None,
        help="aggregate output directory; artifacts are written under it, "
             "mirroring the input tree structure")
    output_group.add_argument(
        "--in-place", action="store_true",
        help="write each artifact next to its own source file, like a "
             "per-file `pptrepair repair` run")
    repair_all.add_argument("--report", metavar="DIR", default=None,
                            help=(
                                "write scan_report.txt/json, "
                                "repair_report.txt/json and, for unknown "
                                "corruption patterns, shareable anonymous "
                                "diagnostic fingerprints into DIR"
                            ))
    repair_all.add_argument("--force", action="store_true",
                            help="overwrite existing artifacts, and reuse "
                                 "an existing --report directory")
    repair_all.add_argument("--all", action="store_true", dest="show_all",
                            help="list every scanned file, not only "
                                 "corrupted ones")
    repair_all.add_argument("--dry-run", action="store_true",
                            help="write nothing; only diagnose and show "
                                 "the repair plan")
    repair_all.add_argument("--lang", choices=i18n.SUPPORTED_LANGUAGES,
                            default=i18n.DEFAULT_LANGUAGE,
                            help="language of the human-readable output "
                                 "(default: en)")
    repair_all.add_argument("--json", action="store_true", dest="json_output",
                            help="emit a JSON object instead of text output")
    repair_all.add_argument("--follow-symlinks", action="store_true",
                            help="follow symbolic links while walking "
                                 "(default: ignore them)")
    repair_all.add_argument("--include-filenames", action="store_true",
                            help="include file basenames in diagnostic "
                                 "fingerprints (default: anonymous)")
    repair_all.add_argument("--allow-download", action="store_true",
                            help=(
                                "also read cloud-only placeholder files; "
                                "this makes the sync client download their "
                                "content and may take long and use "
                                "significant disk space (default: skip "
                                "them without downloading)"
                            ))
    return parser


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


def run_scan(roots: list[str], report: str | None, force: bool,
             show_all: bool, lang: str, json_output: bool,
             follow_symlinks: bool, include_filenames: bool,
             allow_download: bool) -> int:
    """Scan directory trees, print the results, and return an exit code.

    Implementation requirements:

    * Call :func:`pptrepair.scan.scan_paths` with the options mapped
      through.
      :class:`pptrepair.repair.OutputExistsError` (existing ``--report``
      dir without ``--force``) prints the error plus a translated
      ``--force`` hint to stderr and returns 2, mirroring ``repair``.
    * Text mode streams one line per diagnosed file through the
      ``progress`` callback while scanning: corrupted files always,
      intact ones only with *show_all*; format ``{path}: {verdict}``
      (and `` -> {error}`` for pipeline failures, streamed to stderr).
      JSON mode passes no callback and prints nothing during the scan.
    * Walk errors (nonexistent roots, unreadable directories) print one
      ``pptrepair: error: {message}`` line each to stderr in both
      modes, so an exit-2 scan never masquerades as a clean 0-file
      report.
    * In both modes, a translated ``Downloading cloud-only file: ...``
      notice goes to stderr (flushed) right before a placeholder target
      is read with ``--allow-download``, so the terminal is never
      silent while the sync client hydrates a file.
    * After the scan, text mode prints
      :func:`render_scan_text(result, tr, include_files=False)
      <pptrepair.report.render_scan_text>`; JSON mode prints
      :func:`render_scan_json <pptrepair.report.render_scan_json>`.
    * With ``--report``, additionally write ``scan_report.txt``
      (``render_scan_text(result, tr, include_files=True)`` + trailing
      newline, UTF-8) and ``scan_report.json``
      (``render_scan_json(result)`` + trailing newline, UTF-8) into the
      report directory — in *both* output modes.
    * Exit code: 2 when ``result.had_errors()``, else 1 when any
      diagnosed file is not NORMAL, else 0 (cloud-skips alone never
      change the exit code).
    """
    tr = i18n.get_translator(lang)
    report_dir = Path(report) if report is not None else None

    def _report_progress(outcome: scan_module.FileOutcome) -> None:
        if outcome.error is not None:
            # diagnose_file's error message already carries the path
            # ("{path}: {reason}"); stream it as-is.
            print(outcome.error, file=sys.stderr)
            return
        assert outcome.diagnosis is not None
        if show_all or outcome.diagnosis.verdict != Verdict.NORMAL:
            print(f"{outcome.path}: {outcome.diagnosis.verdict.value}")

    def _announce_download(path: Path) -> None:
        # Reading the placeholder blocks until the sync client has
        # downloaded it, so flush the notice out first. stderr keeps
        # the stdout verdict stream / JSON parseable.
        print(tr("Downloading cloud-only file: {path}").format(path=path),
              file=sys.stderr, flush=True)

    try:
        result = scan_module.scan_paths(
            [Path(root) for root in roots],
            report_dir=report_dir,
            force=force,
            follow_symlinks=follow_symlinks,
            allow_download=allow_download,
            include_filenames=include_filenames,
            progress=None if json_output else _report_progress,
            on_download=_announce_download,
        )
    except repair_module.OutputExistsError as exc:
        print(f"pptrepair: error: {exc}", file=sys.stderr)
        print(tr("Hint: pass --force to overwrite the existing output."),
              file=sys.stderr)
        return EXIT_ERROR

    # Walk errors (nonexistent roots, unreadable directories) are not
    # streamed by the progress callback, so a silent exit-2 would look
    # like a clean 0-file scan; report them the way `check` does. The
    # message already carries the offending path.
    for _path, message in result.walk.errors:
        print(f"pptrepair: error: {message}", file=sys.stderr)

    if json_output:
        print(render_scan_json(result))
    else:
        print(render_scan_text(result, tr, include_files=False))

    if report_dir is not None:
        text_report = render_scan_text(result, tr, include_files=True)
        (report_dir / "scan_report.txt").write_text(
            text_report + "\n", encoding="utf-8")
        json_report = render_scan_json(result)
        (report_dir / "scan_report.json").write_text(
            json_report + "\n", encoding="utf-8")

    if result.had_errors():
        return EXIT_ERROR
    if result.corrupted():
        return EXIT_CORRUPT
    return EXIT_OK


def run_repair_all(roots: list[str], output_dir: str | None, in_place: bool,
                   report: str | None, force: bool, show_all: bool,
                   dry_run: bool, lang: str, json_output: bool,
                   follow_symlinks: bool, include_filenames: bool,
                   allow_download: bool) -> int:
    """Diagnose and repair directory trees, print the results, and return
    an exit code.

    Implementation requirements:

    * In aggregate mode (``in_place`` is False), an *output_dir* that
      already exists as a regular file (not a directory) is a usage
      error: print ``pptrepair: error: ...`` to stderr and return 2
      before anything is scanned. An existing *directory* is fine and
      needs no ``--force`` (unlike ``--report``): per-artifact existence
      is checked by :func:`pptrepair.batch.repair_paths` itself.
    * Call :func:`pptrepair.batch.repair_paths` with the options mapped
      through; :class:`pptrepair.repair.OutputExistsError` (an existing
      ``--report`` dir without ``--force``) prints the error plus a
      translated ``--force`` hint to stderr and returns 2, mirroring
      ``repair``/``scan``.
    * Text mode streams phase 1 exactly like ``run_scan``'s own
      ``progress`` callback (corrupted files always, intact ones only
      with *show_all*, pipeline errors to stderr) and additionally
      streams one untranslated line per phase-2 :class:`BatchItem
      <pptrepair.batch.BatchItem>` as it is produced: ``{path}: repaired
      ({mode}) -> {output}`` when repaired, ``{path}: planned
      ({output})`` when *dry_run* only predicted an artifact, ``{path}:
      skipped (output exists)`` when an existing artifact blocked it
      without ``--force``, ``pptrepair: error: {path}: {error}`` to
      stderr when the repair raised, and ``{path}: {action}`` for every
      other action (``unrepairable``). JSON mode passes no callback for
      either phase and prints nothing while scanning/repairing.
    * A translated ``Downloading cloud-only file: ...`` notice goes to
      stderr (flushed) right before a placeholder target is read with
      ``--allow-download``, exactly like ``run_scan``.
    * Walk errors (nonexistent roots, unreadable directories) print one
      ``pptrepair: error: {message}`` line each to stderr in both modes.
    * After the run, text mode prints
      :func:`render_batch_text(result, tr, include_files=False)
      <pptrepair.report.render_batch_text>`; JSON mode prints
      :func:`render_batch_json <pptrepair.report.render_batch_json>`.
    * With ``--report`` and *not* ``dry_run``, additionally write, all
      UTF-8 with a trailing newline: ``scan_report.txt``/``.json`` (the
      phase-1 :class:`~pptrepair.scan.ScanResult`, ``include_files=True``)
      and ``repair_report.txt``/``.json`` (the full batch result,
      ``include_files=True``) into the report directory. Under
      *dry_run*, nothing is written even when ``--report`` is given
      (:func:`pptrepair.batch.repair_paths` itself never touches the
      report directory in that case either).
    * Exit code: 2 when ``result.had_errors()``, else 1 when
      ``result.unrepaired_remaining() > 0`` (in *dry_run* this counts
      everything that is not ``"repaired"`` or ``"planned"``), else 0.
    """
    tr = i18n.get_translator(lang)
    output_dir_path = Path(output_dir) if output_dir is not None else None
    report_dir = Path(report) if report is not None else None

    if not in_place and output_dir_path is not None \
            and output_dir_path.is_file():
        print(f"pptrepair: error: {output_dir}: output path already "
              "exists and is not a directory", file=sys.stderr)
        return EXIT_ERROR

    def _report_progress(outcome: scan_module.FileOutcome) -> None:
        if outcome.error is not None:
            # diagnose_file's error message already carries the path
            # ("{path}: {reason}"); stream it as-is.
            print(outcome.error, file=sys.stderr)
            return
        assert outcome.diagnosis is not None
        if show_all or outcome.diagnosis.verdict != Verdict.NORMAL:
            print(f"{outcome.path}: {outcome.diagnosis.verdict.value}")

    def _announce_download(path: Path) -> None:
        # Reading the placeholder blocks until the sync client has
        # downloaded it, so flush the notice out first. stderr keeps
        # the stdout verdict/JSON stream parseable.
        print(tr("Downloading cloud-only file: {path}").format(path=path),
              file=sys.stderr, flush=True)

    def _repair_progress(item: batch_module.BatchItem) -> None:
        # Plain, untranslated output, matching the phase-1 per-file
        # stream's own convention (machine-facing path/action/mode
        # values are never translated).
        path = item.source.path
        if item.action == "repaired":
            mode = item.repair.mode if item.repair is not None else "-"
            print(f"{path}: repaired ({mode}) -> {item.planned_output}")
        elif item.action == "planned":
            print(f"{path}: planned ({item.planned_output})")
        elif item.action == "skipped_existing":
            print(f"{path}: skipped (output exists)")
        elif item.action == "failed":
            print(f"pptrepair: error: {path}: {item.error}", file=sys.stderr)
        else:
            print(f"{path}: {item.action}")

    try:
        result = batch_module.repair_paths(
            [Path(root) for root in roots],
            output_dir=output_dir_path,
            in_place=in_place,
            report_dir=report_dir,
            force=force,
            dry_run=dry_run,
            follow_symlinks=follow_symlinks,
            allow_download=allow_download,
            include_filenames=include_filenames,
            lang=lang,
            progress=None if json_output else _report_progress,
            repair_progress=None if json_output else _repair_progress,
            on_download=_announce_download,
        )
    except repair_module.OutputExistsError as exc:
        print(f"pptrepair: error: {exc}", file=sys.stderr)
        print(tr("Hint: pass --force to overwrite the existing output."),
              file=sys.stderr)
        return EXIT_ERROR

    # Walk errors are not streamed by the progress callback; report them
    # the way `scan` does so an exit-2 run never masquerades as clean.
    for _path, message in result.scan.walk.errors:
        print(f"pptrepair: error: {message}", file=sys.stderr)

    if json_output:
        print(render_batch_json(result))
    else:
        print(render_batch_text(result, tr, include_files=False))

    if report_dir is not None and not dry_run:
        scan_text_report = render_scan_text(
            result.scan, tr, include_files=True)
        (report_dir / "scan_report.txt").write_text(
            scan_text_report + "\n", encoding="utf-8")
        scan_json_report = render_scan_json(result.scan)
        (report_dir / "scan_report.json").write_text(
            scan_json_report + "\n", encoding="utf-8")
        repair_text_report = render_batch_text(result, tr, include_files=True)
        (report_dir / "repair_report.txt").write_text(
            repair_text_report + "\n", encoding="utf-8")
        repair_json_report = render_batch_json(result)
        (report_dir / "repair_report.json").write_text(
            repair_json_report + "\n", encoding="utf-8")

    if result.had_errors():
        return EXIT_ERROR
    if result.unrepaired_remaining() > 0:
        return EXIT_CORRUPT
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args.files, args.json_output)
    if args.command == "repair":
        return run_repair(args.file, args.output, args.mode, args.force,
                          args.lang, args.json_output)
    if args.command == "salvage":
        return run_salvage(args.file, args.output, args.force, args.lang,
                           args.json_output)
    if args.command == "scan":
        return run_scan(args.roots, args.report, args.force, args.show_all,
                        args.lang, args.json_output, args.follow_symlinks,
                        args.include_filenames, args.allow_download)
    if args.command == "repair-all":
        return run_repair_all(
            args.roots, args.output_dir, args.in_place, args.report,
            args.force, args.show_all, args.dry_run, args.lang,
            args.json_output, args.follow_symlinks, args.include_filenames,
            args.allow_download)
    parser.error(f"unknown command: {args.command}")
    return EXIT_ERROR
