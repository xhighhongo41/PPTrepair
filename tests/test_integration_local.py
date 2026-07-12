"""Local-only integration tests against real corrupted presentations.

These tests verify the classification pipeline against the project's
private corpus of real OneDrive-corrupted .pptx files and their intact
backups. The corpus and the expectation file live outside the public
repository (they contain personal data), so this module skips itself
entirely when either is absent — for example on a fresh public clone
or in CI.

The expectation file maps repository-relative file paths to expected
``Verdict`` values::

    {"expected": {"<relative path>": "<verdict value>", ...}}
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pptrepair.census import from_central_directory, from_lfh_scan
from pptrepair.classify import classify
from pptrepair.scanner import scan_structure

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Expectation file; kept in a git-ignored internal directory because it
#: references real (non-public) file names.
_EXPECTED_FILE = (
    _REPO_ROOT / "開発資料" / "調査データ" / "expected_classification.json"
)


def _load_cases() -> list[tuple[str, str]]:
    """Load ``(relative_path, expected_verdict)`` pairs, or [] if absent."""
    if not _EXPECTED_FILE.is_file():
        return []
    data = json.loads(_EXPECTED_FILE.read_text(encoding="utf-8"))
    return sorted(data["expected"].items())


_CASES = _load_cases()

if not _CASES:
    pytest.skip(
        "local corpus expectation file not present; skipping integration "
        "tests against real files",
        allow_module_level=True,
    )


@pytest.mark.parametrize(("relative_path", "expected_verdict"), _CASES)
def test_real_file_classification(relative_path: str,
                                  expected_verdict: str) -> None:
    """Each real corpus file must classify exactly as recorded."""
    path = _REPO_ROOT / relative_path
    if not path.is_file():
        pytest.skip(f"corpus file not present: {relative_path}")

    structure = scan_structure(path)
    cd_census = from_central_directory(path)
    lfh_census = from_lfh_scan(path)
    diagnosis = classify(path, structure, cd_census, lfh_census)

    assert diagnosis.verdict.value == expected_verdict, (
        f"{relative_path}: expected {expected_verdict}, "
        f"got {diagnosis.verdict.value} (evidence: {diagnosis.evidence})"
    )
