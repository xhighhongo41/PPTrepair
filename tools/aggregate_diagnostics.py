"""Aggregate diagnostic fingerprints collected from ``pptrepair scan``.

Development-time helper for analyzing unknown corruption patterns
(schema_version 1 ``*.diag.json`` files, see
:mod:`pptrepair.diagnostics`). It groups the fingerprints so that
recurring damage geometries stand out, which is the starting point for
designing a new repair strategy in a 1.1.x release.

Usage::

    python tools/aggregate_diagnostics.py DIR_OR_FILE [...]

Directories are searched recursively for ``*.diag.json``. For each
fingerprint the tool prints a one-line geometry signature, then a
summary grouped by verdict, by zero-run boundary alignment, and by
chunk-profile layout (the sequence of content classes, e.g.
``zeros>high_entropy``). No third-party dependencies; this script is
not part of the installed package.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

#: Schema versions this tool understands.
SUPPORTED_SCHEMAS = frozenset({1})


def find_fingerprints(paths: list[Path]) -> list[Path]:
    """Return all ``*.diag.json`` files under *paths*, sorted."""
    found: set[Path] = set()
    for path in paths:
        if path.is_dir():
            found.update(path.rglob("*.diag.json"))
        elif path.is_file():
            found.add(path)
        else:
            print(f"warning: {path}: not found, skipped", file=sys.stderr)
    return sorted(found)


def load_fingerprint(path: Path) -> dict | None:
    """Load one fingerprint, or None (with a warning) when unusable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"warning: {path}: {exc}", file=sys.stderr)
        return None
    if data.get("kind") != "pptrepair-diagnostic-fingerprint":
        print(f"warning: {path}: not a diagnostic fingerprint, skipped",
              file=sys.stderr)
        return None
    if data.get("schema_version") not in SUPPORTED_SCHEMAS:
        print(f"warning: {path}: unsupported schema_version "
              f"{data.get('schema_version')!r}, skipped", file=sys.stderr)
        return None
    return data


def profile_layout(fingerprint: dict) -> str:
    """Return the chunk-profile layout signature (``class>class>...``)."""
    classes = [run["class"] for run in fingerprint.get("chunk_profile", [])]
    return ">".join(classes) if classes else "(empty)"


def zero_run_alignments(fingerprint: dict) -> list[str]:
    """Return ``start/end`` alignment labels for every zero run."""
    structure = fingerprint.get("zip_structure") or {}
    labels = []
    for run in structure.get("zero_runs", []):
        labels.append(
            f"start@{run.get('start_alignment')}/end@{run.get('end_alignment')}")
    return labels


def geometry_line(path: Path, fingerprint: dict) -> str:
    """Return the one-line geometry signature printed per fingerprint."""
    file_info = fingerprint.get("file", {})
    salvage = fingerprint.get("salvage_summary") or {}
    entries = (f"{salvage.get('entries_ok')}/{salvage.get('entries_total')}"
               if salvage else "-")
    return (f"{path.name}: verdict={fingerprint.get('verdict')} "
            f"size={file_info.get('size')} tool={fingerprint.get('tool_version')} "
            f"salvage={entries} profile={profile_layout(fingerprint)}")


def print_summary(fingerprints: list[tuple[Path, dict]]) -> None:
    """Print the grouped aggregation over all loaded fingerprints."""
    by_verdict: Counter[str] = Counter()
    by_layout: Counter[str] = Counter()
    by_alignment: Counter[str] = Counter()
    for _, fp in fingerprints:
        by_verdict[fp.get("verdict", "(missing)")] += 1
        by_layout[profile_layout(fp)] += 1
        for label in zero_run_alignments(fp):
            by_alignment[label] += 1

    def _block(title: str, counter: Counter[str]) -> None:
        print(f"\n{title}:")
        if not counter:
            print("  (none)")
            return
        for key, count in counter.most_common():
            print(f"  {count:4d}  {key}")

    print(f"\n=== {len(fingerprints)} fingerprint(s) aggregated ===")
    _block("By verdict", by_verdict)
    _block("By chunk-profile layout", by_layout)
    _block("By zero-run boundary alignment", by_alignment)


def main(argv: list[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    parser = argparse.ArgumentParser(
        description="Aggregate pptrepair diagnostic fingerprints.")
    parser.add_argument("paths", metavar="DIR_OR_FILE", nargs="+",
                        type=Path,
                        help="directories (searched recursively for "
                             "*.diag.json) or fingerprint files")
    args = parser.parse_args(argv)

    files = find_fingerprints(args.paths)
    loaded = [(path, fp) for path in files
              if (fp := load_fingerprint(path)) is not None]
    if not loaded:
        print("no usable fingerprints found", file=sys.stderr)
        return 1

    for path, fp in loaded:
        print(geometry_line(path, fp))
    print_summary(loaded)
    return 0


if __name__ == "__main__":
    sys.exit(main())
