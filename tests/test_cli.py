"""Tests for :mod:`pptrepair.cli`.

Exercises the full scanner -> census -> classify -> report pipeline
through :func:`pptrepair.cli.main`, using small synthetic archives
written under ``tmp_path``. ``subprocess`` is intentionally not used
(the process is invoked in-process via ``main()``), and the real
``broken_ppt/`` / ``normal_ppt/`` sample directories are never touched.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, truncate, zero_prefix

from pptrepair.cli import EXIT_CORRUPT, EXIT_ERROR, EXIT_OK, main

#: Small media payload so fixtures stay fast to build and scan.
_MEDIA_BYTES = 600_000

#: Shorthand for the capsys fixture type, to keep signatures short.
CaptureFixture = pytest.CaptureFixture[str]

#: Relationship-reference namespace, matching pptrepair.integrity.R_NS.
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the resulting path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _inject_dangling_ref(data: bytes,
                         part: str = "ppt/slides/slide1.xml") -> bytes:
    """Return a copy of the .pptx *data* with a dangling ``r:embed``
    reference spliced into *part*.

    Simulates a structurally intact package (see v1.1.2 plan §4.3)
    whose slide XML still points at a relationship id that part's own
    ``.rels`` no longer defines -- the exact situation
    ``check``/``pptrepair.integrity`` is meant to surface without
    affecting the ``normal`` verdict or exit code.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        contents = {info.filename: archive.read(info.filename)
                    for info in infos}

    original = contents[part].decode("utf-8")
    injected = original.replace(
        "<p:grpSpPr/>",
        "<p:grpSpPr/><p:pic><p:blipFill>"
        f'<a:blip xmlns:r="{_R_NS}" r:embed="rId99"/>'
        "</p:blipFill></p:pic>",
        1,
    )
    assert injected != original, "injection anchor not found in fixture"
    contents[part] = injected.encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as rebuilt:
        for info in infos:
            rebuilt.writestr(info.filename, contents[info.filename])
    return buffer.getvalue()


def _add_theme_relationship_to_slide_master(data: bytes) -> bytes:
    """Return a copy of the .pptx *data* with a theme relationship added
    to ``ppt/slideMasters/slideMaster1.xml.rels``.

    A real .pptx carries the theme relationship on the slide master's
    own ``.rels`` (as :func:`pptrepair.integrity.inspect_structure`
    requires), but the shared ``build_minimal_pptx`` fixture only wires
    it onto ``presentation.xml.rels``; this patches just that one
    part's bytes so a structurally complete package can be used to
    exercise a clean ``structure_integrity`` result.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        contents = {info.filename: archive.read(info.filename)
                    for info in infos}

    part = "ppt/slideMasters/_rels/slideMaster1.xml.rels"
    original = contents[part].decode("utf-8")
    injected = original.replace(
        "</Relationships>",
        f'<Relationship Id="rIdTheme" Type="{_R_NS}/theme" '
        'Target="../theme/theme1.xml"/></Relationships>',
        1,
    )
    assert injected != original, "injection anchor not found in fixture"
    contents[part] = injected.encode("utf-8")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as rebuilt:
        for info in infos:
            rebuilt.writestr(info.filename, contents[info.filename])
    return buffer.getvalue()


def test_single_healthy_file(tmp_path: Path, capsys: CaptureFixture) -> None:
    """A single intact file yields exit code 0 and a NORMAL text report."""
    data = build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    path = _write(tmp_path, "healthy.pptx", data)

    exit_code = main(["check", str(path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "normal" in out
    assert "===" in out


def test_head_zero_fill_file(tmp_path: Path, capsys: CaptureFixture) -> None:
    """A head-zero-filled file yields exit 1 and the verdict + salvage line."""
    data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    path = _write(tmp_path, "head_zero.pptx", data)

    exit_code = main(["check", str(path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    assert "head_zero_fill" in out
    assert "Salvageable:" in out


def test_tail_truncated_file(tmp_path: Path, capsys: CaptureFixture) -> None:
    """A truncated file yields exit code 1 and reports TAIL_TRUNCATED."""
    data = truncate(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 2000)
    path = _write(tmp_path, "truncated.pptx", data)

    exit_code = main(["check", str(path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    assert "tail_truncated" in out


def test_healthy_and_broken_files_both_reported(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Two files (one healthy, one corrupted) both produce a text report."""
    healthy = _write(
        tmp_path, "healthy.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    )
    broken_data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    broken = _write(tmp_path, "broken.pptx", broken_data)

    exit_code = main(["check", str(healthy), str(broken)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    # Each report's header line is "=== <path> ==="; the opening marker
    # ("=== " with a trailing space) appears exactly once per report.
    assert out.count("=== ") == 2


def test_nonexistent_path_reports_error(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A nonexistent path yields exit code 2 with an stderr error message."""
    missing = tmp_path / "does_not_exist.pptx"

    exit_code = main(["check", str(missing)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ERROR
    assert captured.err.strip() != ""


def test_nonexistent_path_and_healthy_file_still_reports_healthy(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A missing path forces exit 2 but a healthy sibling is still reported."""
    missing = tmp_path / "does_not_exist.pptx"
    healthy = _write(
        tmp_path, "healthy.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    )

    exit_code = main(["check", str(missing), str(healthy)])

    captured = capsys.readouterr()
    assert exit_code == EXIT_ERROR
    assert captured.err.strip() != ""
    assert "normal" in captured.out
    assert "===" in captured.out


def test_json_output_schema(tmp_path: Path, capsys: CaptureFixture) -> None:
    """``--json`` emits a parseable array with the documented per-entry schema."""
    healthy = _write(
        tmp_path, "healthy.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES)
    )
    broken_data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    broken = _write(tmp_path, "broken.pptx", broken_data)

    exit_code = main(["check", "--json", str(healthy), str(broken)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    payload = json.loads(out)
    assert len(payload) == 2

    verdicts = set()
    for entry in payload:
        assert set(entry.keys()) >= {
            "path", "verdict", "label", "evidence", "salvage", "structure",
        }
        verdicts.add(entry["verdict"])
        assert set(entry["structure"].keys()) == {
            "size", "head_kind", "zero_bytes", "eocd_present", "lfh_count",
        }
    assert verdicts == {"normal", "head_zero_fill"}


def test_exit_code_constants() -> None:
    """Exit code constants must match the documented 0/1/2 contract."""
    assert EXIT_OK == 0
    assert EXIT_CORRUPT == 1
    assert EXIT_ERROR == 2


# --- v1.1.2: reference-integrity check on structurally normal files -----


def test_check_reports_dangling_reference_but_stays_normal(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A structurally intact file with a dangling XML relationship
    reference still exits 0 (the verdict stays normal), but the text
    report surfaces a reference-integrity warning line."""
    data = _inject_dangling_ref(build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    path = _write(tmp_path, "dangling.pptx", data)

    exit_code = main(["check", str(path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "normal" in out
    assert "Reference integrity:" in out


def test_check_json_reports_xml_ref_integrity(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """``--json`` reports a positive dangling_count for a package with a
    dangling reference, and an all-clean result for one without."""
    dangling_data = _inject_dangling_ref(
        build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    dangling_path = _write(tmp_path, "dangling.pptx", dangling_data)
    clean_path = _write(
        tmp_path, "clean.pptx", build_minimal_pptx(media_bytes=_MEDIA_BYTES))

    exit_code = main(["check", "--json", str(dangling_path), str(clean_path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    payload = json.loads(out)
    by_path = {entry["path"]: entry for entry in payload}

    dangling_entry = by_path[str(dangling_path)]
    assert dangling_entry["verdict"] == "normal"
    assert dangling_entry["xml_ref_integrity"]["dangling_count"] > 0

    clean_entry = by_path[str(clean_path)]
    assert clean_entry["xml_ref_integrity"] == {
        "dangling_count": 0, "parts": [],
    }


# --- v1.1.2 addendum: timing/structure integrity in check --json ---------


def test_check_json_reports_timing_and_structure_integrity(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """``--json`` includes timing_integrity/structure_integrity keys
    alongside xml_ref_integrity for structurally normal files, all
    reporting a clean result for a structurally complete package."""
    clean_data = _add_theme_relationship_to_slide_master(
        build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    dangling_data = _inject_dangling_ref(clean_data)
    dangling_path = _write(tmp_path, "dangling.pptx", dangling_data)
    clean_path = _write(tmp_path, "clean.pptx", clean_data)

    exit_code = main(
        ["check", "--json", str(dangling_path), str(clean_path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    payload = json.loads(out)
    by_path = {entry["path"]: entry for entry in payload}

    dangling_entry = by_path[str(dangling_path)]
    assert dangling_entry["verdict"] == "normal"
    assert dangling_entry["xml_ref_integrity"]["dangling_count"] > 0
    assert "timing_integrity" in dangling_entry
    assert "structure_integrity" in dangling_entry

    clean_entry = by_path[str(clean_path)]
    assert clean_entry["xml_ref_integrity"] == {
        "dangling_count": 0, "parts": [],
    }
    assert clean_entry["timing_integrity"] == {
        "dangling_count": 0, "media_mismatch_count": 0, "parts": [],
    }
    assert clean_entry["structure_integrity"] == {
        "missing_count": 0, "items": [],
    }


# --- _parse_max_file_size (--max-file-size argparse type) -----------------


@pytest.mark.parametrize("text, expected", [
    ("12345", 12345),
    ("500M", 524288000),
    ("2G", 2147483648),
    ("1.5K", 1536),
    ("2gb", 2147483648),
])
def test_parse_max_file_size_accepts_valid_input(
    text: str, expected: int
) -> None:
    """Plain byte counts and K/M/G/T-suffixed magnitudes parse correctly."""
    from pptrepair.cli import _parse_max_file_size

    assert _parse_max_file_size(text) == expected


@pytest.mark.parametrize("text", ["abc", "", "-1", "0", "1X"])
def test_parse_max_file_size_rejects_invalid_input(text: str) -> None:
    """Malformed grammar and non-positive sizes raise ArgumentTypeError."""
    import argparse

    from pptrepair.cli import _parse_max_file_size

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_max_file_size(text)
