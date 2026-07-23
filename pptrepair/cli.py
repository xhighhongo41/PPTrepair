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
import tempfile
from pathlib import Path
from typing import Callable

import pptrepair
from pptrepair import archive as archive_module
from pptrepair import i18n
from pptrepair import merge as merge_module
from pptrepair import repair as repair_module
from pptrepair import scan as scan_module
from pptrepair.classify import Diagnosis
from pptrepair.cli_batch import run_repair_all, run_scan
from pptrepair.cli_single import run_check, run_repair, run_salvage
from pptrepair.exit_codes import EXIT_CORRUPT, EXIT_ERROR, EXIT_OK
from pptrepair.origin import OriginScore, score_origin


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

    merge = subparsers.add_parser(
        "merge",
        help="reconstruct one file by splicing entries out of several "
             "same-origin copies",
        description=(
            "Reconstruct SRC1 (the target) by byte-splicing archive "
            "entries out of the other SRC files that share its origin: "
            "an automatically trusted byte-identical copy, a weaker "
            "size- and name-matched candidate, or -- confirmed "
            "interactively or with --yes -- a lineage-tier different "
            "saved version used as an entry-level donor. Every input "
            "file is opened read-only."
        ),
    )
    merge.add_argument("sources", metavar="SRC", nargs="+",
                       help="target file, followed by one or more other "
                            "copies or versions to merge from")
    merge.add_argument("-o", "--output", metavar="PATH", default=None,
                       help=(
                           "output path (default: <target-stem>.merged.pptx "
                           "next to the target)"
                       ))
    merge.add_argument("--force", action="store_true",
                       help="overwrite an existing output path")
    merge.add_argument("--allow-candidate", action="store_true",
                       help="also use candidate-tier sources without an "
                            "interactive prompt")
    merge.add_argument("--yes", action="store_true",
                       help="also use candidate- and lineage-tier sources "
                            "without an interactive prompt")
    merge.add_argument("--lang", choices=i18n.SUPPORTED_LANGUAGES,
                       default=i18n.DEFAULT_LANGUAGE,
                       help="language of the human-readable summary "
                            "(default: en)")
    merge.add_argument("--json", action="store_true", dest="json_output",
                       help="emit the merge report as JSON instead of a "
                            "text summary")

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
    scan.add_argument("--search-archives", action="store_true",
                      help=(
                          "also mine backup archives (zip/tar) found while "
                          "walking for intact twins or older versions of "
                          "corrupted files, offered as restore candidates "
                          "only; archived files are never counted or "
                          "repaired (default: ignore archives)"
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
    repair_all.add_argument("--search-archives", action="store_true",
                            help=(
                                "also mine backup archives (zip/tar) found "
                                "while walking for intact twins or older "
                                "versions of corrupted files, offered as "
                                "restore candidates only; archived files "
                                "are never counted or repaired (default: "
                                "ignore archives)"
                            ))
    return parser


def run_merge(sources: list[str], output: str | None, force: bool,
              allow_candidate: bool, yes: bool, lang: str,
              json_output: bool) -> int:
    """Reconstruct ``sources[0]`` from the other given same-origin sources.

    Implementation requirements:

    * At least two SRC files are required; fewer is a usage error
      (stderr + exit 2), and every given path is validated to exist as
      a regular file the same way ``repair``/``salvage`` validate a
      single input, before anything is diagnosed.
    * The first SRC (the target) can never itself be a backup archive
      (:func:`pptrepair.archive.is_archive`); that is a translated usage
      error (stderr + exit 2) instead of trying to diagnose the archive
      file as a PowerPoint package.
    * Every SRC after the target that *is* a backup archive is expanded,
      under one :class:`tempfile.TemporaryDirectory` spanning the rest of
      the run, into the plain ``.pptx``/``.pptm`` members it holds (see
      :func:`_expand_archive_sources`); every other SRC is used
      unchanged. An archive contributing no member is dropped with a
      note; if fewer than two sources remain after expansion the run
      falls back to the same usage error as too few raw SRC arguments.
      A materialized member is named to the user, everywhere, only
      through its ``"<archive>::<member>"`` label -- never through the
      temporary path it was extracted to.
    * The first SRC is the target; :func:`pptrepair.scan.diagnose_file`
      diagnoses it, and an undiagnosable target is a fatal error (stderr
      + exit 2, mirroring ``check``/``repair``).
    * Every other SRC is diagnosed and scored against the target with
      :func:`pptrepair.origin.score_origin` (see
      :func:`_score_other_sources`); a source that fails to diagnose is
      reported to stderr and excluded, without failing the whole run.
    * :func:`_select_sources` decides which scored sources are cleared
      to feed the splice: ``auto`` tier always, ``candidate`` with
      ``--allow-candidate``/``--yes`` or an interactive confirmation,
      ``lineage`` with ``--yes`` or an interactive confirmation,
      ``rejected`` never. Every source left unused prints one
      translated line to stderr.
    * With at least one approved source,
      :func:`pptrepair.merge.merge_restore` is called with
      ``[target, *approved]``, ``allow_candidate``/``allow_lineage`` set
      when at least one candidate-/lineage-tier source was approved, and
      *lang*; :class:`pptrepair.repair.OutputExistsError` prints a
      translated ``--force`` hint and returns 2, like ``repair``.
    * With no approved source, ``merge_restore`` is not called (it
      requires at least two sources); a synthetic ``"failed"``
      :class:`~pptrepair.merge.MergeOutcome` is reported instead, with
      no output produced.
    * Every note collected while expanding archive sources is merged into
      ``outcome.notes`` ahead of ``merge_restore``'s own notes, so it
      reaches both the text summary and the ``--json`` output.
    * Prints :func:`_merge_json_payload` when *json_output* is set,
      otherwise :func:`_render_merge_summary` with the *lang* translator.
    * Exit code: 0 when ``outcome.guarantee`` is ``"full"``, ``"partial"``
      or ``"hybrid"`` (an output was produced), 1 when ``"failed"``.
    """
    if len(sources) < 2:
        print("pptrepair: error: merge requires at least two SRC files",
              file=sys.stderr)
        return EXIT_ERROR

    for src in sources:
        path = Path(src)
        if not path.exists():
            print(f"pptrepair: error: {src}: no such file", file=sys.stderr)
            return EXIT_ERROR
        if not path.is_file():
            print(f"pptrepair: error: {src}: not a regular file",
                  file=sys.stderr)
            return EXIT_ERROR

    tr = i18n.get_translator(lang)
    target_path = Path(sources[0])
    if archive_module.is_archive(target_path):
        message = tr(
            "The merge target cannot be a file inside an archive: {path}. "
            "Extract it to a plain file and pass that instead."
        ).format(path=target_path)
        print(f"pptrepair: error: {message}", file=sys.stderr)
        return EXIT_ERROR

    with tempfile.TemporaryDirectory(prefix="pptrepair-merge-") as tmp_dir:
        other_paths, display, origin, expand_notes = _expand_archive_sources(
            [Path(src) for src in sources[1:]], Path(tmp_dir))

        if not other_paths:
            # Every non-target SRC was either an archive with no usable
            # member, or expansion left nothing: same shortage as too few
            # raw SRC arguments.
            print("pptrepair: error: merge requires at least two SRC files",
                  file=sys.stderr)
            return EXIT_ERROR

        target_diag, target_error = scan_module.diagnose_file(target_path)
        if target_diag is None:
            print(f"pptrepair: error: {target_path}: {target_error}",
                  file=sys.stderr)
            return EXIT_ERROR

        scored, diagnose_warnings = _score_other_sources(
            target_diag, other_paths, display)
        for warning in diagnose_warnings:
            print(warning, file=sys.stderr)

        approved, used_candidate, used_lineage, select_warnings = (
            _select_sources(
                scored, allow_candidate=allow_candidate, yes=yes,
                is_tty=sys.stdin.isatty(), tr=tr, display=display))
        for warning in select_warnings:
            print(warning, file=sys.stderr)

        output_path = Path(output) if output is not None else None
        if approved:
            try:
                outcome = merge_module.merge_restore(
                    [target_path, *approved], output=output_path,
                    force=force, allow_candidate=used_candidate,
                    allow_lineage=used_lineage, lang=lang, display=display)
            except repair_module.OutputExistsError as exc:
                print(f"pptrepair: error: {exc}", file=sys.stderr)
                print(tr(
                    "Hint: pass --force to overwrite the existing output."),
                    file=sys.stderr)
                return EXIT_ERROR
        else:
            # merge_restore itself requires at least two sources; with none
            # approved there is nothing left to splice the target against.
            outcome = merge_module.MergeOutcome(
                output_path=None, guarantee="failed", provenances=[],
                scores=[score for _path, _diag, score in scored],
                notes=["no other usable source remained after selection"])
        # Archive-expansion notes happened first, chronologically.
        outcome.notes = [*expand_notes, *outcome.notes]

        if json_output:
            payload = _merge_json_payload(
                target_path, outcome, scored, approved, display, origin)
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(_render_merge_summary(outcome, tr, display))

        return EXIT_CORRUPT if outcome.guarantee == "failed" else EXIT_OK


def _expand_archive_sources(
    other_paths: list[Path], tmp_dir: Path
) -> tuple[list[Path], dict[Path, str], dict[Path, str], list[str]]:
    """Replace every archive SRC with its extracted usable members.

    Each path in *other_paths* that :func:`pptrepair.archive.is_archive`
    recognises as a backup archive is expanded, via
    :func:`pptrepair.archive.list_members` and
    :func:`pptrepair.archive.materialize`, into plain on-disk copies of
    its ``.pptx``/``.pptm`` members under their own subdirectory of
    *tmp_dir* (one subdirectory per archive, so destination names never
    collide across archives); every other path is passed through
    unchanged. An archive that yields no usable member contributes
    nothing and is noted.

    :returns: ``(expanded, display, origin, notes)`` --

        * *expanded*: *other_paths* with each archive replaced by the
          temporary paths of its extracted members, in encounter order;
        * *display*: maps each temporary member path to its
          ``"<archive>::<member>"`` label
          (:meth:`pptrepair.archive.ArchiveMember.display`), so no
          user-facing text ever names the temporary file directly;
        * *origin*: maps each temporary member path to the archive
          file's own path (as a string), for a JSON ``origin_archive``
          field;
        * *notes*: one English line per enumeration/extraction problem
          (encrypted, corrupt or unreadable members, plus an archive
          left with no usable member at all), in encounter order. Every
          path named in a note is either the archive file itself or an
          in-archive member label, never a temporary path.
    """
    expanded: list[Path] = []
    display: dict[Path, str] = {}
    origin: dict[Path, str] = {}
    notes: list[str] = []
    for index, path in enumerate(other_paths):
        if not archive_module.is_archive(path):
            expanded.append(path)
            continue
        members, list_notes = archive_module.list_members(path)
        notes.extend(list_notes)
        if not members:
            if not list_notes:
                notes.append(
                    f"archive {path} has no usable .pptx/.pptm member; "
                    "skipped")
            continue
        member_dir = tmp_dir / f"archive{index:04d}"
        member_dir.mkdir()
        extracted, materialize_notes = archive_module.materialize(
            path, members, member_dir)
        notes.extend(materialize_notes)
        for member, dest_path in extracted.items():
            expanded.append(dest_path)
            display[dest_path] = member.display()
            origin[dest_path] = str(path)
    return expanded, display, origin, notes


def _display_label(path: Path,
                   display: dict[Path, str] | None) -> str | Path:
    """Return *path*'s user-facing label.

    When *display* maps *path* (a source materialized from inside an
    archive) to its ``"<archive>::<member>"`` label, that label is
    returned so the temporary on-disk path is never shown to the user;
    otherwise *path* itself is returned unchanged.
    """
    if display is None:
        return path
    return display.get(path, path)


def _score_other_sources(
    target_diag: Diagnosis, others: list[Path],
    display: dict[Path, str] | None = None,
) -> tuple[list[tuple[Path, Diagnosis, OriginScore]], list[str]]:
    """Diagnose and score every non-target source against *target_diag*.

    Returns the scored ``(path, diagnosis, score)`` triples, in source
    order, plus one stderr-ready (untranslated, ``pptrepair: error:``
    prefixed) warning line for every source that could not be diagnosed
    -- excluded from scoring, but not fatal to the run. A source is named
    in that warning through :func:`_display_label`, given *display*, so a
    source materialized from an archive is never named by its temporary
    path.
    """
    scored: list[tuple[Path, Diagnosis, OriginScore]] = []
    warnings: list[str] = []
    for path in others:
        diagnosis, error = scan_module.diagnose_file(path)
        if diagnosis is None:
            label = _display_label(path, display)
            warnings.append(f"pptrepair: error: {label}: {error}")
            continue
        score = score_origin(target_diag, diagnosis)
        scored.append((path, diagnosis, score))
    return scored, warnings


def _select_sources(
    scored: list[tuple[Path, Diagnosis, OriginScore]], *,
    allow_candidate: bool, yes: bool, is_tty: bool,
    tr: Callable[[str], str], display: dict[Path, str] | None = None,
) -> tuple[list[Path], bool, bool, list[str]]:
    """Decide which scored sources the merge should use.

    Returns ``(approved, used_candidate, used_lineage, warnings)``:
    *approved* is the ordered list of source paths cleared to feed
    :func:`~pptrepair.merge.merge_restore`; *used_candidate*/
    *used_lineage* report whether at least one candidate-/lineage-tier
    source was approved (to pass through as ``allow_candidate``/
    ``allow_lineage``); *warnings* holds one translated line per source
    left unused, ready to print to stderr, each source named through
    :func:`_display_label` given *display*.

    An ``auto``-tier source is always used. A ``candidate``-tier source
    is used when *allow_candidate* or *yes* is set, or -- only when
    *is_tty* -- when the user confirms it via :func:`_confirm_source`. A
    ``lineage``-tier source likewise needs *yes* or an interactive
    confirmation. A ``rejected``-tier source is never used. A
    non-interactive run without the relevant flag, and any declined
    prompt, leaves the source unused.
    """
    approved: list[Path] = []
    used_candidate = False
    used_lineage = False
    warnings: list[str] = []
    for path, _diagnosis, score in scored:
        if score.tier == "auto":
            approved.append(path)
            continue
        if score.tier == "candidate":
            use = allow_candidate or yes or (
                is_tty and _confirm_source(path, score, tr, display))
            if use:
                approved.append(path)
                used_candidate = True
            else:
                warnings.append(tr(
                    "Candidate-tier source not used: {path} (pass "
                    "--allow-candidate, --yes, or confirm the prompt to "
                    "include it)").format(path=_display_label(path, display)))
            continue
        if score.tier == "lineage":
            use = yes or (is_tty and _confirm_source(path, score, tr, display))
            if use:
                approved.append(path)
                used_lineage = True
            else:
                warnings.append(tr(
                    "Lineage-tier source not used: {path} (pass --yes "
                    "or confirm the prompt to include it)"
                ).format(path=_display_label(path, display)))
            continue
        warnings.append(tr(
            "Source not used (not the same origin): {path}"
        ).format(path=_display_label(path, display)))
    return approved, used_candidate, used_lineage, warnings


def _confirm_source(path: Path, score: OriginScore,
                    tr: Callable[[str], str],
                    display: dict[Path, str] | None = None) -> bool:
    """Print *score*'s evidence for *path* and ask whether to use it.

    Printed evidence: the file name (or, given *display*, its
    ``"<archive>::<member>"`` label -- see :func:`_display_label`), its
    tier, whether its size matches the target, its triple/name/media
    ratios (as percentages) and its lineage score, each label translated
    via *tr* (tier/match/ratio values themselves stay untranslated, like
    verdict codes elsewhere in this module). The actual y/N read is
    delegated to :func:`_ask_yes_no`, isolated so tests can monkeypatch
    it.
    """
    print(tr("Source: {path}").format(path=_display_label(path, display)))
    print(tr("Tier: {tier}").format(tier=score.tier))
    print(tr("Size match: {value}").format(
        value="yes" if score.size_match else "no"))
    print(tr("Triple ratio: {pct}%").format(
        pct=f"{score.triple_ratio * 100:.1f}"))
    print(tr("Name ratio: {pct}%").format(
        pct=f"{score.name_ratio * 100:.1f}"))
    print(tr("Media ratio: {pct}%").format(
        pct=f"{score.media_ratio * 100:.1f}"))
    print(tr("Lineage score: {value}").format(
        value=f"{score.lineage_score:.3f}"))
    return _ask_yes_no(tr("Use this source for the merge? [y/N] "))


def _ask_yes_no(prompt: str) -> bool:
    """Print *prompt* and read a y/N answer from stdin (default No).

    Isolated as a module-level function so tests can monkeypatch it in
    place of driving real interactive input.
    """
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _merge_json_payload(
    target_path: Path, outcome: "merge_module.MergeOutcome",
    scored: list[tuple[Path, Diagnosis, OriginScore]], approved: list[Path],
    display: dict[Path, str] | None = None,
    origin: dict[Path, str] | None = None,
) -> dict:
    """Build the JSON-schema dict for the merge command's ``--json`` output.

    Schema (stable for tests)::

        {
          "schema_version": 1,
          "target": str,
          "guarantee": str,          # MergeOutcome.guarantee
          "output": str | null,
          "provenance_counts": {"direct": int, "crossover": int,
                                 "donor": int, "donor_unverified": int,
                                 "missing": int},
          "sources": [
            {"path": str, "tier": str, "used": bool,
             "triple_ratio": float, "name_ratio": float,
             "media_ratio": float, "lineage_score": float,
             "origin_archive": str | null}, ...
          ],
          "notes": [str, ...]
        }

    ``sources`` covers every non-target source that could be diagnosed
    and scored (in *scored*'s order), regardless of whether it was
    approved; a source excluded before diagnosis (e.g. it failed to
    diagnose) is absent. A source's ``"path"`` is its
    ``"<archive>::<member>"`` label (:func:`_display_label`) when
    *display* maps it -- i.e. it was materialized from an archive SRC --
    and its ``"origin_archive"`` is then that archive file's own path (a
    plain source's is ``null``), from *origin*; the temporary path a
    materialized source actually lives at never appears in either field.
    """
    approved_set = set(approved)
    provenance_counts = {
        "direct": 0, "crossover": 0, "donor": 0, "donor_unverified": 0,
        "missing": 0,
    }
    for provenance in outcome.provenances:
        provenance_counts[provenance.method] = (
            provenance_counts.get(provenance.method, 0) + 1)
    return {
        "schema_version": 1,
        "target": str(target_path),
        "guarantee": outcome.guarantee,
        "output": str(outcome.output_path)
        if outcome.output_path is not None else None,
        "provenance_counts": provenance_counts,
        "sources": [
            {
                "path": str(_display_label(path, display)),
                "tier": score.tier,
                "used": path in approved_set,
                "triple_ratio": score.triple_ratio,
                "name_ratio": score.name_ratio,
                "media_ratio": score.media_ratio,
                "lineage_score": score.lineage_score,
                "origin_archive": (origin or {}).get(path),
            }
            for path, _diagnosis, score in scored
        ],
        "notes": list(outcome.notes),
    }


def _render_merge_summary(outcome: "merge_module.MergeOutcome",
                          tr: Callable[[str], str],
                          display: dict[Path, str] | None = None) -> str:
    """Render one merge outcome as a human-readable, translated summary.

    Every entry-provenance source named in the summary is shown through
    :func:`_display_label`, given *display*, so a source materialized
    from an archive SRC is named by its ``"<archive>::<member>"`` label
    rather than the temporary path it was extracted to.
    """
    lines = [tr("=== Merge summary ===")]
    lines.append(
        tr("Guarantee: {guarantee}").format(guarantee=outcome.guarantee))
    if outcome.output_path is not None:
        lines.append(tr("Output: {path}").format(path=outcome.output_path))

    method_counts = {
        "direct": 0, "crossover": 0, "donor": 0, "donor_unverified": 0,
        "missing": 0,
    }
    for provenance in outcome.provenances:
        method_counts[provenance.method] = (
            method_counts.get(provenance.method, 0) + 1)
    for method in ("direct", "crossover", "donor", "donor_unverified",
                   "missing"):
        if method_counts[method]:
            lines.append(f"  {method}: {method_counts[method]}")

    source_counts: dict[Path, int] = {}
    for provenance in outcome.provenances:
        if provenance.source is not None:
            source_counts[provenance.source] = (
                source_counts.get(provenance.source, 0) + 1)
    if source_counts:
        lines.append(tr("Entries by source:"))
        for source, count in source_counts.items():
            lines.append(f"  {_display_label(source, display)}: {count}")

    if outcome.guarantee == "hybrid":
        lines.append(tr(
            "Warning: part of the output comes from a different version "
            "and is not guaranteed to be identical to the original."))

    if outcome.notes:
        lines.append(tr("Notes:"))
        lines.extend(f"  - {note}" for note in outcome.notes)

    return "\n".join(lines)


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
    if args.command == "merge":
        return run_merge(args.sources, args.output, args.force,
                         args.allow_candidate, args.yes, args.lang,
                         args.json_output)
    if args.command == "scan":
        return run_scan(args.roots, args.report, args.force, args.show_all,
                        args.lang, args.json_output, args.follow_symlinks,
                        args.include_filenames, args.allow_download,
                        args.search_archives)
    if args.command == "repair-all":
        return run_repair_all(
            args.roots, args.output_dir, args.in_place, args.report,
            args.force, args.show_all, args.dry_run, args.lang,
            args.json_output, args.follow_symlinks, args.include_filenames,
            args.allow_download, args.search_archives)
    parser.error(f"unknown command: {args.command}")
    return EXIT_ERROR
