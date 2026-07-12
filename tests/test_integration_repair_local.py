"""Local-only integration tests: repair the real corrupted corpus.

Same conventions as ``test_integration_local.py``: the corpus and the
expectation file live outside the public repository, so this module
skips itself when they are absent. All repair artifacts are written to
pytest's ``tmp_path`` — never next to the corpus files — so intact
outputs can never end up inside ``broken_ppt/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pptrepair.repair import repair_file

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Expectation file; kept in a git-ignored internal directory because it
#: references real (non-public) file names.
_EXPECTED_FILE = (
    _REPO_ROOT / "開発資料" / "調査データ" / "expected_repair.json"
)


def _load_cases() -> list[tuple[str, dict]]:
    """Load ``(relative_path, expectations)`` pairs, or [] if absent."""
    if not _EXPECTED_FILE.is_file():
        return []
    data = json.loads(_EXPECTED_FILE.read_text(encoding="utf-8"))
    return sorted(data["expected"].items())


_CASES = _load_cases()

if not _CASES:
    pytest.skip(
        "local corpus expectation file not present; skipping repair "
        "integration tests against real files",
        allow_module_level=True,
    )


@pytest.mark.parametrize(("relative_path", "expected"), _CASES)
def test_real_file_repair(relative_path: str, expected: dict,
                          tmp_path: Path) -> None:
    """Each real corpus file must repair exactly as recorded."""
    src = _REPO_ROOT / relative_path
    if not src.is_file():
        pytest.skip(f"corpus file not present: {relative_path}")

    suffix = ".repaired.pptx" if expected["mode"] == "rebuild" else ".salvaged"
    output = tmp_path / (src.stem + suffix)
    outcome = repair_file(src, output=output, mode="auto", force=False)

    assert outcome.mode == expected["mode"], outcome.warnings
    assert outcome.success == expected["success"]

    salvage = outcome.diagnosis.salvage_summary
    assert salvage["entries_ok"] == expected["entries_ok"]
    assert salvage["slides_ok"] == expected["slides_ok"]

    if expected["mode"] == "rebuild":
        assert outcome.recheck_verdict == expected["recheck_verdict"]
        assert output.is_file()
    else:
        assert len(outcome.lost_slide_numbers) == expected["lost_slides"]
        assert output.is_dir()
        assert (output / "parts").is_dir()
        # REPORT.txt is written by the CLI layer, not repair_file itself.
