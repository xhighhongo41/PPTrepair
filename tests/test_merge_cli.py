"""Tests for the ``pptrepair merge`` CLI subcommand.

Exercises :func:`pptrepair.cli.main` end to end through small synthetic
files written under ``tmp_path``, the same way :mod:`test_merge` covers
:func:`pptrepair.merge.merge_restore` directly and
:mod:`test_repair_all_cli` covers ``repair-all``. The real
``broken_ppt/`` / ``normal_ppt/`` sample directories are never touched.
"""

from __future__ import annotations

import io
import json
import struct
import zipfile
from pathlib import Path

import pytest
from fixtures import (build_minimal_jpeg, build_minimal_pptx, find_eocd,
                      make_corrupted_copies, make_edited_version)

import pptrepair.cli as cli_module
from pptrepair import merge as merge_module
from pptrepair.cli import EXIT_CORRUPT, EXIT_ERROR, EXIT_OK, main
from pptrepair.origin import OriginScore

#: Shorthand for the capsys fixture type, to keep signatures short.
CaptureFixture = pytest.CaptureFixture[str]


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    """Write *data* to ``tmp_path / name`` and return the resulting path."""
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _cd_offsets(data: bytes) -> list[int]:
    """Return every entry's local-header offset, ordered, from the CD."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return sorted(info.header_offset for info in archive.infolist())


def _entry_interval(data: bytes, name: str) -> tuple[int, int]:
    """Return the ``[offset, next_offset)`` byte range of member *name*.

    Mirrors :func:`test_merge._entry_interval`.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        offset = archive.getinfo(name).header_offset
    offsets = _cd_offsets(data)
    cd_offset, _size, _eocd = find_eocd(data)
    index = offsets.index(offset)
    end = offsets[index + 1] if index + 1 < len(offsets) else cd_offset
    return offset, end


def _entry_data_span(data: bytes, name: str) -> tuple[int, int]:
    """Return the ``[data_start, data_end)`` compressed-payload range.

    Mirrors :func:`test_merge._entry_data_span`: the local file header
    stays intact so the entry still appears in a header scan but fails
    its CRC.
    """
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        offset = archive.getinfo(name).header_offset
    fields = struct.unpack("<IHHHHHIIIHH", data[offset:offset + 30])
    comp_size = fields[7]
    name_len = fields[9]
    extra_len = fields[10]
    data_start = offset + 30 + name_len + extra_len
    return data_start, data_start + comp_size


def _lineage_versions(media_bytes: int = 60_000, *, add_jpeg: bool = True,
                      seed: int = 0) -> tuple[bytes, bytes]:
    """Return an original archive and a lineage version of it.

    Mirrors :func:`test_merge._lineage_versions`: the version replaces
    slide1 with a longer body so the two differ in size while every
    media part stays byte-identical -- the shape
    :func:`pptrepair.origin.score_origin` recognises as a ``lineage``
    donor rather than a same-save copy.
    """
    base = build_minimal_pptx(num_slides=3, media_bytes=media_bytes, seed=seed)
    if add_jpeg:
        original = make_edited_version(
            base,
            add={"ppt/media/image1.jpeg": build_minimal_jpeg(pad_to=9000)})
    else:
        original = base
    new_slide = (
        b"<p:sld><p:cSld><p:spTree><p:nvGrpSpPr/><p:grpSpPr/><p:sp>"
        b"<p:txBody><a:p><a:r><a:t>Edited slide body for the lineage "
        b"version, padded so the archive size clearly differs.</a:t>"
        b"</a:r></p:txBody></p:sp></p:spTree></p:cSld></p:sld>"
    )
    version = make_edited_version(
        original, replace={"ppt/slides/slide1.xml": new_slide})
    if len(version) == len(original):
        version = make_edited_version(
            original, replace={"ppt/slides/slide1.xml": new_slide + b"X" * 64})
    assert len(version) != len(original)
    return original, version


def _fake_candidate_score(_diag_a, _diag_b) -> OriginScore:
    """Stub :func:`~pptrepair.origin.score_origin` to a candidate-tier call.

    Used so the candidate-gate tests are deterministic, matching
    :func:`test_merge.test_candidate_gate`'s own stub.
    """
    return OriginScore(size_match=True, cd_pair=True, triple_ratio=0.5,
                       name_ratio=0.8, media_ratio=0.0, lineage_score=0.0,
                       tier="candidate", evidence=[])


class _FakeInteractiveStdin:
    """A stand-in for ``sys.stdin`` reporting an interactive terminal."""

    def isatty(self) -> bool:
        """Report True, so :func:`pptrepair.cli.run_merge` prompts."""
        return True


# --- basic merge: complementary copies -----------------------------------


def test_complementary_copies_merge_exits_ok(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Two copies broken in non-overlapping ranges merge to a full output,
    exit 0, and the text summary shows the guarantee."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    media_start, media_end = _entry_interval(data, "ppt/media/image1.png")
    slide_start, slide_end = _entry_interval(data, "ppt/slides/slide1.xml")
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", media_start, media_end)],
        [("zero_range", slide_start, slide_end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    out_path = tmp_path / "out.pptx"

    exit_code = main(["merge", str(path_a), str(path_b), "-o", str(out_path)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert out_path.is_file()
    assert out_path.read_bytes() == data
    assert "=== Merge summary ===" in out_text
    assert "Guarantee: full" in out_text


# --- --json schema ----------------------------------------------------------


def test_json_output_schema(tmp_path: Path, capsys: CaptureFixture) -> None:
    """``--json`` emits the documented schema, with per-source tier/used."""
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    media_start, media_end = _entry_interval(data, "ppt/media/image1.png")
    slide_start, slide_end = _entry_interval(data, "ppt/slides/slide1.xml")
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", media_start, media_end)],
        [("zero_range", slide_start, slide_end)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    out_path = tmp_path / "out.pptx"

    exit_code = main(
        ["merge", str(path_a), str(path_b), "-o", str(out_path), "--json"])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    payload = json.loads(out_text)
    assert payload["schema_version"] == 1
    assert payload["target"] == str(path_a)
    assert payload["guarantee"] == "full"
    assert payload["output"] == str(out_path)
    assert set(payload["provenance_counts"]) == {
        "direct", "crossover", "donor", "donor_unverified", "missing"}
    assert len(payload["sources"]) == 1
    source = payload["sources"][0]
    assert source["path"] == str(path_b)
    assert source["tier"] == "auto"
    assert source["used"] is True
    for key in ("triple_ratio", "name_ratio", "media_ratio", "lineage_score"):
        assert isinstance(source[key], float)
    assert isinstance(payload["notes"], list)


# --- candidate-tier gate -----------------------------------------------------


def test_candidate_tier_not_used_without_flag(
    tmp_path: Path, capsys: CaptureFixture, monkeypatch
) -> None:
    """A candidate-tier source is unused by default (non-interactive test
    run) and a translated warning names it; nothing is left to merge
    against, so the run fails."""
    monkeypatch.setattr(cli_module, "score_origin", _fake_candidate_score)
    monkeypatch.setattr(merge_module, "score_origin", _fake_candidate_score)
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    (corrupted,) = make_corrupted_copies(data, [[("foreign_prefix", 8192)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    path_b = _write(tmp_path, "b.pptx", data)

    exit_code = main(
        ["merge", str(path_a), str(path_b), "-o", str(tmp_path / "out.pptx")])

    err = capsys.readouterr().err
    assert exit_code == EXIT_CORRUPT
    assert f"Candidate-tier source not used: {path_b}" in err
    assert not (tmp_path / "out.pptx").exists()


def test_candidate_tier_used_with_allow_candidate(
    tmp_path: Path, capsys: CaptureFixture, monkeypatch
) -> None:
    """``--allow-candidate`` uses the candidate-tier source without a
    prompt, restoring the target fully."""
    monkeypatch.setattr(cli_module, "score_origin", _fake_candidate_score)
    monkeypatch.setattr(merge_module, "score_origin", _fake_candidate_score)
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    (corrupted,) = make_corrupted_copies(data, [[("foreign_prefix", 8192)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    path_b = _write(tmp_path, "b.pptx", data)
    out_path = tmp_path / "out.pptx"

    exit_code = main(["merge", str(path_a), str(path_b), "-o", str(out_path),
                      "--allow-candidate"])

    capsys.readouterr()
    assert exit_code == EXIT_OK
    assert out_path.read_bytes() == data


# --- lineage-tier donor -------------------------------------------------------


def _write_degraded_lineage_material(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Build two truncated (CD-less) copies plus a lineage donor.

    Mirrors :func:`test_merge.test_degraded_lineage_donor_is_hybrid`'s own
    material: both copies are truncated inside the shared media part, so
    no central directory survives and a donor entry can only be adopted
    unverified.
    """
    original, donor = _lineage_versions(media_bytes=60_000, add_jpeg=False)
    media_start, media_end = _entry_interval(original, "ppt/media/image1.png")
    cut = (media_start + media_end) // 2
    copy_a, copy_b = make_corrupted_copies(original, [
        [("truncate", cut)],
        [("truncate", cut)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    path_donor = _write(tmp_path, "donor.pptx", donor)
    return path_a, path_b, path_donor


def test_lineage_tier_not_used_without_yes(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """Without ``--yes`` (non-interactive test run) the lineage donor is
    unused and a translated warning names it."""
    path_a, path_b, path_donor = _write_degraded_lineage_material(tmp_path)

    exit_code = main(["merge", str(path_a), str(path_b), str(path_donor),
                      "-o", str(tmp_path / "out.pptx")])

    err = capsys.readouterr().err
    assert exit_code == EXIT_OK
    assert f"Lineage-tier source not used: {path_donor}" in err


def test_lineage_tier_used_with_yes_reports_hybrid(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """``--yes`` uses the lineage donor; an unverified donor entry caps
    the run at ``hybrid`` and the summary shows the hybrid warning line."""
    path_a, path_b, path_donor = _write_degraded_lineage_material(tmp_path)

    exit_code = main(["merge", str(path_a), str(path_b), str(path_donor),
                      "-o", str(tmp_path / "out.pptx"), "--yes"])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "Guarantee: hybrid" in out_text
    assert (
        "Warning: part of the output comes from a different version and "
        "is not guaranteed to be identical to the original."
    ) in out_text


# --- interactive confirmation (monkeypatched _ask_yes_no / isatty) ----------


def test_interactive_prompt_accepts_candidate(
    tmp_path: Path, capsys: CaptureFixture, monkeypatch
) -> None:
    """On a simulated interactive terminal, an accepted prompt uses the
    candidate-tier source."""
    monkeypatch.setattr(cli_module.sys, "stdin", _FakeInteractiveStdin())
    monkeypatch.setattr(cli_module, "score_origin", _fake_candidate_score)
    monkeypatch.setattr(merge_module, "score_origin", _fake_candidate_score)
    monkeypatch.setattr(cli_module, "_ask_yes_no", lambda prompt: True)
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    (corrupted,) = make_corrupted_copies(data, [[("foreign_prefix", 8192)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    path_b = _write(tmp_path, "b.pptx", data)
    out_path = tmp_path / "out.pptx"

    exit_code = main(["merge", str(path_a), str(path_b), "-o", str(out_path)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert out_path.read_bytes() == data
    assert f"Source: {path_b}" in out_text  # evidence was printed
    assert "Tier: candidate" in out_text


def test_interactive_prompt_declines_candidate(
    tmp_path: Path, capsys: CaptureFixture, monkeypatch
) -> None:
    """On a simulated interactive terminal, a declined prompt leaves the
    candidate-tier source unused."""
    monkeypatch.setattr(cli_module.sys, "stdin", _FakeInteractiveStdin())
    monkeypatch.setattr(cli_module, "score_origin", _fake_candidate_score)
    monkeypatch.setattr(merge_module, "score_origin", _fake_candidate_score)
    monkeypatch.setattr(cli_module, "_ask_yes_no", lambda prompt: False)
    data = build_minimal_pptx(num_slides=3, media_bytes=200_000)
    (corrupted,) = make_corrupted_copies(data, [[("foreign_prefix", 8192)]])
    path_a = _write(tmp_path, "a.pptx", corrupted)
    path_b = _write(tmp_path, "b.pptx", data)
    out_path = tmp_path / "out.pptx"

    exit_code = main(["merge", str(path_a), str(path_b), "-o", str(out_path)])

    err = capsys.readouterr().err
    assert exit_code == EXIT_CORRUPT
    assert not out_path.exists()
    assert f"Candidate-tier source not used: {path_b}" in err


# --- usage / I/O errors -------------------------------------------------------


def test_nonexistent_source_exits_error(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A SRC path that does not exist is a usage error (exit 2)."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    missing = tmp_path / "missing.pptx"

    exit_code = main(["merge", str(path_a), str(missing)])

    err = capsys.readouterr().err
    assert exit_code == EXIT_ERROR
    assert "pptrepair: error:" in err
    assert "no such file" in err


def test_output_collision_requires_force(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """An existing output path is refused unless --force is given."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    path_b = _write(tmp_path, "b.pptx", data)
    out_path = tmp_path / "out.pptx"
    out_path.write_bytes(b"existing")

    exit_code = main(["merge", str(path_a), str(path_b), "-o", str(out_path)])

    err = capsys.readouterr().err
    assert exit_code == EXIT_ERROR
    assert "already exists" in err
    assert "--force" in err

    exit_code_forced = main(
        ["merge", str(path_a), str(path_b), "-o", str(out_path), "--force"])
    capsys.readouterr()
    assert exit_code_forced == EXIT_OK
    assert out_path.read_bytes() == data


def test_failed_merge_exits_corrupt(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """When nothing usable survives, the run reports failed and exits 1."""
    data = build_minimal_pptx(num_slides=2, media_bytes=30_000)
    cd_offset, _size, _eocd = find_eocd(data)
    pres_start, pres_end = _entry_data_span(data, "ppt/presentation.xml")
    copy_a, copy_b = make_corrupted_copies(data, [
        [("zero_range", pres_start, pres_end), ("truncate", cd_offset)],
        [("zero_range", pres_start, pres_end), ("truncate", cd_offset)],
    ])
    path_a = _write(tmp_path, "a.pptx", copy_a)
    path_b = _write(tmp_path, "b.pptx", copy_b)
    out_path = tmp_path / "out.pptx"

    exit_code = main(["merge", str(path_a), str(path_b), "-o", str(out_path)])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    assert "Guarantee: failed" in out_text
    assert not out_path.exists()


def test_fewer_than_two_sources_is_usage_error(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """A single SRC is a usage error (nothing to merge against)."""
    data = build_minimal_pptx(num_slides=1, media_bytes=4096)
    path_a = _write(tmp_path, "a.pptx", data)

    exit_code = main(["merge", str(path_a)])

    err = capsys.readouterr().err
    assert exit_code == EXIT_ERROR
    assert "pptrepair: error:" in err


# --- --lang ------------------------------------------------------------------


def test_lang_ja_translates_the_summary(
    tmp_path: Path, capsys: CaptureFixture
) -> None:
    """``--lang ja`` renders the merge summary header in Japanese."""
    data = build_minimal_pptx(num_slides=2, media_bytes=20_000)
    path_a = _write(tmp_path, "a.pptx", data)
    path_b = _write(tmp_path, "b.pptx", data)
    out_path = tmp_path / "out.pptx"

    exit_code = main(["merge", str(path_a), str(path_b), "-o", str(out_path),
                      "--lang", "ja"])

    out_text = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "=== マージ概要 ===" in out_text
