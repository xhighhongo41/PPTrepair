"""Directory-tree ``scan``/``repair-all`` command implementations.

Split out of :mod:`pptrepair.cli` to keep that module within a
manageable size; see :mod:`pptrepair.cli` for the argument parser and
the ``main`` dispatch that use these functions.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pptrepair import batch as batch_module
from pptrepair import i18n
from pptrepair import repair as repair_module
from pptrepair import scan as scan_module
from pptrepair.classify import Verdict
from pptrepair.exit_codes import EXIT_CORRUPT, EXIT_ERROR, EXIT_OK
from pptrepair.report import (
    render_batch_json,
    render_batch_text,
    render_scan_json,
    render_scan_text,
)


def run_scan(roots: list[str], report: str | None, force: bool,
             show_all: bool, lang: str, json_output: bool,
             follow_symlinks: bool, include_filenames: bool,
             allow_download: bool, search_archives: bool,
             max_file_bytes: int | None = None, *,
             ignore_hidden: bool = True) -> int:
    """Scan directory trees, print the results, and return an exit code.

    Implementation requirements:

    * Call :func:`pptrepair.scan.scan_paths` with the options mapped
      through, including *max_file_bytes* (files over the limit are
      excluded from discovery and counted in the scan's
      ``skipped_oversize``; left at its default ``None`` this is a
      no-op) and *ignore_hidden* (hidden files -- name starting with
      ``.`` -- are excluded from discovery and counted in the scan's
      ``skipped_hidden``; left at its default ``True`` this matches
      ``scan_paths``'s own default, so ``--include-hidden`` is what
      flips it to False).
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
            search_archives=search_archives,
            max_file_bytes=max_file_bytes,
            ignore_hidden=ignore_hidden,
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
                   allow_download: bool, search_archives: bool,
                   max_file_bytes: int | None = None, *,
                   ignore_hidden: bool = True) -> int:
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
      through, including *max_file_bytes* and *ignore_hidden* (mirroring
      ``run_scan``'s own no-op / True defaults);
      :class:`pptrepair.repair.OutputExistsError` (an existing
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
            search_archives=search_archives,
            max_file_bytes=max_file_bytes,
            ignore_hidden=ignore_hidden,
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
