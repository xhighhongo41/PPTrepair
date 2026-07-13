"""Local-only integration test: ``pptrepair scan`` over the real corpus.

Same conventions as ``test_integration_local.py``: the corpus and the
expectation file live outside the public repository, so this module
skips itself entirely when either is absent. ``--report`` always
writes under pytest's ``tmp_path``; ``broken_ppt/`` and ``normal_ppt/``
are only ever read here, never written to.
"""

from __future__ import annotations

import json
import unicodedata
from collections import Counter
from pathlib import Path

import pytest

from pptrepair.cli import EXIT_CORRUPT, main

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Expectation file; kept in a git-ignored internal directory because it
#: references real (non-public) file names.
_EXPECTED_FILE = (
    _REPO_ROOT / "開発資料" / "調査データ" / "expected_classification.json"
)


def _load_expected() -> dict[str, str]:
    """Load ``{relative_path: expected_verdict}``, or {} if absent."""
    if not _EXPECTED_FILE.is_file():
        return {}
    data = json.loads(_EXPECTED_FILE.read_text(encoding="utf-8"))
    return data["expected"]


_EXPECTED = _load_expected()

if not _EXPECTED:
    pytest.skip(
        "local corpus expectation file not present; skipping scan "
        "integration test against real files",
        allow_module_level=True,
    )


def test_scan_real_corpus_matches_expected_classification(
    tmp_path: Path,
) -> None:
    """Scanning broken_ppt/ and normal_ppt/ matches the recorded verdicts."""
    roots = [_REPO_ROOT / "broken_ppt", _REPO_ROOT / "normal_ppt"]
    for root in roots:
        if not root.is_dir():
            pytest.skip(f"corpus directory not present: {root}")
    # Only cases whose corpus file is actually present are compared, so a
    # partial local corpus does not fail the whole test.
    present = {
        relative: verdict for relative, verdict in _EXPECTED.items()
        if (_REPO_ROOT / relative).is_file()
    }
    if not present:
        pytest.skip("no corpus files from the expectation file are present")

    report_dir = tmp_path / "report"

    exit_code = main(
        ["scan", *(str(root) for root in roots), "--report", str(report_dir)])

    # The corpus mixes NORMAL and corrupted files but never triggers an
    # unreadable-path error, so corrupted-without-errors is expected.
    assert exit_code == EXIT_CORRUPT

    assert (report_dir / "scan_report.txt").is_file()
    report_json = json.loads(
        (report_dir / "scan_report.json").read_text(encoding="utf-8"))

    assert report_json["summary"]["scanned"] == len(present)
    assert report_json["summary"]["errors"] == 0
    assert len(report_json["files"]) == len(present)

    expected_counts = dict(Counter(present.values()))
    assert report_json["summary"]["verdicts"] == expected_counts

    # Normalize to NFC before comparing: macOS filesystems may return
    # directory entries in NFD form, which would otherwise mismatch the
    # (typically NFC) strings loaded from the JSON expectation file even
    # though both refer to the same on-disk file.
    files_by_path = {
        unicodedata.normalize("NFC", entry["path"]): entry["verdict"]
        for entry in report_json["files"]
    }
    for relative, expected_verdict in present.items():
        absolute = unicodedata.normalize("NFC", str(_REPO_ROOT / relative))
        assert files_by_path.get(absolute) == expected_verdict, relative
