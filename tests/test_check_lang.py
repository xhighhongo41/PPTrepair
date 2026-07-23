"""Tests for ``--lang`` support on the ``pptrepair check`` command.

``check`` was the only subcommand still missing ``--lang`` (v1.3
follow-up item); these tests exercise it the same way
:mod:`test_repair`/:mod:`test_merge_cli` exercise ``--lang`` for their
own commands: verdict codes and paths stay English/untranslated data,
only the surrounding descriptive text is translated, ``--json`` output
is completely unaffected, and the default (``--lang`` omitted) report
stays byte-identical to the pre-``--lang`` English text.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from fixtures import build_minimal_pptx, zero_prefix

from pptrepair.cli import EXIT_CORRUPT, EXIT_OK, main

#: Shorthand for the capsys fixture type, to keep signatures short.
CaptureFixture = pytest.CaptureFixture[str]

#: Small media payload so fixtures stay fast to build and scan.
_MEDIA_BYTES = 600_000

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

    Mirrors :mod:`test_cli`'s own helper of the same name: a
    structurally intact package whose slide XML points at a
    relationship id that part's own ``.rels`` no longer defines, which
    surfaces as ``check``'s ``Reference integrity:`` line without
    affecting the ``normal`` verdict.
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


# --- 1. --lang ja translates a NORMAL file's report -----------------------


def test_check_lang_ja_translates_normal_file_report(
    tmp_path: Path, capsys: CaptureFixture,
) -> None:
    """``--lang ja`` renders a NORMAL verdict's label and the reference-
    integrity summary in Japanese; the verdict code itself stays
    English."""
    data = _inject_dangling_ref(build_minimal_pptx(media_bytes=_MEDIA_BYTES))
    path = _write(tmp_path, "dangling.pptx", data)

    exit_code = main(["check", "--lang", "ja", str(path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_OK
    assert "判定: normal" in out  # verdict code stays untranslated data
    assert "無傷のPowerPointパッケージ" in out  # translated verdict label
    assert "参照整合性:" in out  # translated "Reference integrity:"
    assert "初回起動時にPowerPointが一度限りの修復を提案する場合があります。" in out


# --- 2. --lang ja translates a corrupted file's report --------------------


def test_check_lang_ja_translates_broken_file_report(
    tmp_path: Path, capsys: CaptureFixture,
) -> None:
    """``--lang ja`` translates the corrupted-file report's descriptive
    text (verdict label and the salvageability line), keeping the
    verdict code and every number/path untranslated."""
    data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    path = _write(tmp_path, "head_zero.pptx", data)

    exit_code = main(["check", "--lang", "ja", str(path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    assert "head_zero_fill" in out  # verdict code stays untranslated data
    assert "破損: 先頭領域がゼロで上書きされています" in out  # translated label
    assert "復元可能:" in out  # translated "Salvageable:"


# --- 3. default (--lang omitted) output is unchanged, still English -------


def test_check_default_lang_output_matches_explicit_en(
    tmp_path: Path, capsys: CaptureFixture,
) -> None:
    """Omitting ``--lang`` renders exactly the same text as ``--lang en``
    -- and, in particular, exactly the pre-``--lang`` English wording
    (``Salvageable:`` etc.), so existing English-output expectations
    keep working unmodified."""
    data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    path = _write(tmp_path, "head_zero.pptx", data)

    default_exit = main(["check", str(path)])
    default_out = capsys.readouterr().out

    en_exit = main(["check", "--lang", "en", str(path)])
    en_out = capsys.readouterr().out

    assert default_exit == en_exit == EXIT_CORRUPT
    assert default_out == en_out
    assert "Salvageable:" in default_out
    assert "head_zero_fill" in default_out


# --- 4. --lang de smoke test ------------------------------------------------


def test_check_lang_de_smoke(tmp_path: Path, capsys: CaptureFixture) -> None:
    """A second language (German) also renders through its shipped
    catalog, as a smoke test alongside the more detailed ja coverage."""
    data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    path = _write(tmp_path, "head_zero.pptx", data)

    exit_code = main(["check", "--lang", "de", str(path)])

    out = capsys.readouterr().out
    assert exit_code == EXIT_CORRUPT
    assert "head_zero_fill" in out  # verdict code stays untranslated data
    assert "Rettbar:" in out  # translated "Salvageable:"


# --- 5. --json output is unaffected by --lang -------------------------------


def test_check_json_output_ignores_lang(
    tmp_path: Path, capsys: CaptureFixture,
) -> None:
    """``--json`` is machine-facing and never translated, regardless of
    ``--lang``."""
    data = zero_prefix(build_minimal_pptx(media_bytes=_MEDIA_BYTES), 262144)
    path = _write(tmp_path, "head_zero.pptx", data)

    main(["check", "--json", str(path)])
    json_default = capsys.readouterr().out

    main(["check", "--json", "--lang", "ja", str(path)])
    json_ja = capsys.readouterr().out

    assert json_default == json_ja
    payload = json.loads(json_ja)
    assert payload[0]["verdict"] == "head_zero_fill"
