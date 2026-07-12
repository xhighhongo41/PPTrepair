"""Command-line interface.

``pptrepair check [--json] FILE [FILE ...]``

Exit codes:

* 0 — every examined file is an intact PowerPoint package
* 1 — at least one file is corrupted (or not a ZIP at all)
* 2 — usage error, unreadable path, or unexpected internal error

All inputs are opened read-only; this tool never writes to the files
it examines.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pptrepair
from pptrepair import i18n
from pptrepair import repair as repair_module
from pptrepair.census import from_central_directory, from_lfh_scan
from pptrepair.classify import Diagnosis, Verdict, classify
from pptrepair.rebuild import RebuildError
from pptrepair.report import (render_json, render_repair_json,
                              render_repair_text, render_text)
from pptrepair.scanner import scan_structure

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
    return parser


def run_check(files: list[str], json_output: bool) -> int:
    """Diagnose *files*, print reports to stdout, and return an exit code.

    Implementation requirements:

    * Run the scanner -> census -> classify pipeline per file.
    * A nonexistent or unreadable path prints an error to stderr and
      forces exit code 2, but remaining files are still processed.
    * With ``json_output`` a single JSON array covering all successfully
      diagnosed files goes to stdout; otherwise one text report per
      file.
    * Exit code: 2 on any per-file error, else 1 if any verdict is not
      NORMAL, else 0.
    """
    had_error = False
    diagnoses: list[Diagnosis] = []

    for file in files:
        diagnosis, error_message = _diagnose_file(file)
        if error_message is not None:
            print(f"pptrepair: error: {error_message}", file=sys.stderr)
            had_error = True
            continue
        assert diagnosis is not None
        diagnoses.append(diagnosis)

    if json_output:
        print(render_json(diagnoses))
    else:
        for index, diagnosis in enumerate(diagnoses):
            if index > 0:
                print()
            print(render_text(diagnosis))

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
    prefix (added by the caller).
    """
    path = Path(file)
    if not path.exists():
        return None, f"{file}: no such file"
    if not path.is_file():
        return None, f"{file}: not a regular file"

    try:
        structure = scan_structure(path)
        cd_census = from_central_directory(path)
        lfh_census = from_lfh_scan(path)
        diagnosis = classify(path, structure, cd_census, lfh_census)
    except Exception as exc:
        # Any pipeline failure is reported for this file only; processing
        # of the remaining files must continue.
        return None, f"{file}: {type(exc).__name__}: {exc}"
    return diagnosis, None


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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args.files, args.json_output)
    if args.command == "repair":
        return run_repair(args.file, args.output, args.mode, args.force,
                          args.lang, args.json_output)
    parser.error(f"unknown command: {args.command}")
    return EXIT_ERROR
