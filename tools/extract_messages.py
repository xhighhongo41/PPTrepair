#!/usr/bin/env python3
"""Extract translatable message ids from the pptrepair source tree.

Collects every string literal passed to a call of a function named
``tr`` (the translator callable convention used across the code base)
via AST analysis, plus the known dynamic message tables that reach
``tr()`` through a variable:

* ``pptrepair.report.VERDICT_LABELS`` values,
* the field labels of ``pptrepair.extract._CORE_FIELDS``.

Usage::

    python tools/extract_messages.py            # list msgids to stdout
    python tools/extract_messages.py --pot FILE # also write a .pot file

The generated ``.pot`` is the template that the per-language ``.po``
catalogs under ``pptrepair/locale/`` must cover; the catalog
completeness test compares them against this extractor's output.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "pptrepair"


def literal_msgids(source_dir: Path) -> set[str]:
    """Collect string literals passed to ``tr(...)`` calls in *source_dir*."""
    msgids: set[str] = set()
    for py_file in sorted(source_dir.glob("*.py")):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Name) and func.id == "tr"):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                msgids.add(arg.value)
    return msgids


def dynamic_msgids() -> set[str]:
    """Collect msgids that reach ``tr()`` through known message tables."""
    sys.path.insert(0, str(REPO_ROOT))
    from pptrepair.extract import _CORE_FIELDS
    from pptrepair.report import VERDICT_LABELS

    msgids = set(VERDICT_LABELS.values())
    msgids.update(label for label, _ns, _tag in _CORE_FIELDS)
    return msgids


def escape_po(text: str) -> str:
    """Escape *text* for use inside a PO string literal."""
    return (text.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\t", "\\t"))


def write_pot(path: Path, msgids: list[str]) -> None:
    """Write *msgids* as a minimal ``.pot`` template to *path*."""
    lines = [
        'msgid ""',
        'msgstr ""',
        '"Content-Type: text/plain; charset=UTF-8\\n"',
        "",
    ]
    for msgid in msgids:
        lines.append(f'msgid "{escape_po(msgid)}"')
        lines.append('msgstr ""')
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Print all extracted msgids and optionally write a .pot template."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pot", metavar="FILE", default=None,
                        help="write a .pot template to FILE")
    args = parser.parse_args(argv)

    msgids = sorted(literal_msgids(PACKAGE_DIR) | dynamic_msgids())
    for msgid in msgids:
        print(msgid)
    print(f"-- {len(msgids)} message(s)", file=sys.stderr)

    if args.pot is not None:
        write_pot(Path(args.pot), msgids)
    return 0


if __name__ == "__main__":
    sys.exit(main())
