"""Command-line interface.

``pptrepair check [--json] FILE [FILE ...]``

Exit codes:

* 0 — every examined file is an intact PowerPoint package
* 1 — at least one file is corrupted (or not a ZIP at all)
* 2 — usage error, unreadable path, or unexpected internal error

All inputs are opened read-only; this tool never writes to the files
it examines (``scan --report`` writes only inside its report
directory).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pptrepair
from pptrepair import i18n
from pptrepair import repair as repair_module
from pptrepair import scan as scan_module
from pptrepair.classify import Diagnosis, Verdict
from pptrepair.integrity import RefIntegrityResult, inspect_references
from pptrepair.rebuild import RebuildError
from pptrepair.report import (render_json, render_repair_json,
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
    return parser


def run_check(files: list[str], json_output: bool) -> int:
    """Diagnose *files*, print reports to stdout, and return an exit code.

    Implementation requirements:

    * Run the scanner -> census -> classify pipeline per file.
    * A nonexistent or unreadable path prints an error to stderr and
      forces exit code 2, but remaining files are still processed.
    * A file diagnosed as NORMAL is additionally passed through
      :func:`pptrepair.integrity.inspect_references`; an unexpected
      failure there is reported the same way as a diagnosis failure
      (stderr + exit code 2) but does not stop the remaining files. Any
      other verdict skips this pass (its :class:`RefIntegrityResult` is
      None), since check's exit code never depends on it either way.
    * With ``json_output`` a single JSON array covering all successfully
      diagnosed files goes to stdout; otherwise one text report per
      file.
    * Exit code: 2 on any per-file error, else 1 if any verdict is not
      NORMAL, else 0. The reference-integrity result never changes this:
      a package with dangling references is still reported as normal
      (see ``開発資料/v1.1.2実装計画.md`` §4.3 for the rationale).
    """
    had_error = False
    diagnoses: list[Diagnosis] = []
    integrities: list[RefIntegrityResult | None] = []

    for file in files:
        diagnosis, error_message = _diagnose_file(file)
        if error_message is not None:
            print(f"pptrepair: error: {error_message}", file=sys.stderr)
            had_error = True
            continue
        assert diagnosis is not None
        diagnoses.append(diagnosis)

        integrity: RefIntegrityResult | None = None
        if diagnosis.verdict == Verdict.NORMAL:
            try:
                integrity = inspect_references(Path(file))
            except Exception as exc:
                # Defensive: a verdict of NORMAL already implies the
                # archive opened cleanly once, so this should not
                # normally trigger.
                print(f"pptrepair: error: {file}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                had_error = True
        integrities.append(integrity)

    if json_output:
        print(render_json(diagnoses, integrities))
    else:
        for index, diagnosis in enumerate(diagnoses):
            if index > 0:
                print()
            print(render_text(diagnosis, ref_integrity=integrities[index]))

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


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return run_check(args.files, args.json_output)
    if args.command == "repair":
        return run_repair(args.file, args.output, args.mode, args.force,
                          args.lang, args.json_output)
    if args.command == "scan":
        return run_scan(args.roots, args.report, args.force, args.show_all,
                        args.lang, args.json_output, args.follow_symlinks,
                        args.include_filenames, args.allow_download)
    parser.error(f"unknown command: {args.command}")
    return EXIT_ERROR
