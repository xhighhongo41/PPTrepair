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
from pptrepair.census import from_central_directory, from_lfh_scan
from pptrepair.classify import Diagnosis, Verdict, classify
from pptrepair.report import render_json, render_text
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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args.files, args.json_output)
    parser.error(f"unknown command: {args.command}")
    return EXIT_ERROR
