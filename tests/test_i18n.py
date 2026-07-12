"""Tests for :mod:`pptrepair.i18n` and the ``tools/build_i18n`` compiler.

``tools/build_i18n.py`` is not an installed package module, so it is
loaded directly from its file path with :mod:`importlib.util` rather
than imported by name.
"""

from __future__ import annotations

import gettext
import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from pptrepair import i18n

#: Absolute path to the project root (one level above ``tests/``).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_build_i18n() -> ModuleType:
    """Import ``tools/build_i18n.py`` as a standalone module and return it."""
    module_path = _PROJECT_ROOT / "tools" / "build_i18n.py"
    spec = importlib.util.spec_from_file_location("build_i18n", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: The compiler module under test, loaded once for the whole test session.
build_i18n = _load_build_i18n()


# --- pptrepair.i18n.get_translator ------------------------------------------


def test_get_translator_en_is_identity() -> None:
    """``"en"`` returns a callable that returns its argument unchanged."""
    tr = i18n.get_translator("en")

    message = "Slides lost: {lost} of {total}"
    assert tr(message) == message
    assert tr("") == ""


def test_get_translator_unsupported_language_raises() -> None:
    """An unsupported language code raises ``ValueError``."""
    with pytest.raises(ValueError):
        i18n.get_translator("xx")


def test_get_translator_ja_falls_back_without_catalog() -> None:
    """``"ja"`` still works when no catalog has been compiled yet.

    This exercises the real (currently empty) packaged locale
    directory, confirming ``fallback=True`` prevents a missing-catalog
    error and the original English text is returned instead.
    """
    tr = i18n.get_translator("ja")

    message = "Slides lost: {lost} of {total}"
    assert tr(message) == message


# --- tools/build_i18n.py -----------------------------------------------------


#: Sample catalog exercising the header, plain entries, a multi-line
#: (continuation) msgstr and escape sequences (``\n``, ``\t``, ``\"``).
_SAMPLE_PO = '''\
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
"Language: ja\\n"

# A plain greeting.
msgid "Hello, world!"
msgstr "こんにちは、世界!"

msgid "Slides lost: {lost} of {total}"
msgstr "失われたスライド数: {total} 件中 {lost} 件"

msgid "Quoted value and a tab"
msgstr ""
"引用符 \\"値\\" と"
"タブ\\tを含む文字列"
'''


def _write_sample_catalog(tmp_path: Path) -> Path:
    """Write ``_SAMPLE_PO`` under a fresh ``locale/ja/LC_MESSAGES/`` tree.

    :returns: the ``locale`` directory path (the compiler's root).
    """
    catalog_dir = tmp_path / "locale" / "ja" / "LC_MESSAGES"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "pptrepair.po").write_text(_SAMPLE_PO, encoding="utf-8")
    return tmp_path / "locale"


def test_build_i18n_round_trip(tmp_path: Path) -> None:
    """A compiled catalog is loadable via :mod:`gettext` with correct text."""
    locale_dir = _write_sample_catalog(tmp_path)

    exit_code = build_i18n.main(["--locale-dir", str(locale_dir)])

    assert exit_code == 0
    mo_path = locale_dir / "ja" / "LC_MESSAGES" / "pptrepair.mo"
    assert mo_path.is_file()

    translation = gettext.translation(
        "pptrepair", localedir=str(locale_dir), languages=["ja"]
    )
    assert translation.gettext("Hello, world!") == "こんにちは、世界!"


def test_build_i18n_preserves_placeholders(tmp_path: Path) -> None:
    """Named ``str.format`` placeholders survive the .po -> .mo round trip."""
    locale_dir = _write_sample_catalog(tmp_path)
    build_i18n.compile_all(locale_dir)

    translation = gettext.translation(
        "pptrepair", localedir=str(locale_dir), languages=["ja"]
    )
    translated = translation.gettext("Slides lost: {lost} of {total}")

    assert translated == "失われたスライド数: {total} 件中 {lost} 件"
    assert translated.format(lost=3, total=10) == "失われたスライド数: 10 件中 3 件"


def test_build_i18n_continuation_and_escapes(tmp_path: Path) -> None:
    """Continuation lines concatenate and escape sequences are decoded."""
    locale_dir = _write_sample_catalog(tmp_path)
    build_i18n.compile_all(locale_dir)

    translation = gettext.translation(
        "pptrepair", localedir=str(locale_dir), languages=["ja"]
    )
    translated = translation.gettext("Quoted value and a tab")

    assert translated == '引用符 "値" とタブ\tを含む文字列'


def test_build_i18n_message_count_excludes_header(tmp_path: Path) -> None:
    """``compile_all`` reports message counts without the header entry."""
    locale_dir = _write_sample_catalog(tmp_path)

    results = build_i18n.compile_all(locale_dir)

    assert results == [("ja", 3)]


def test_build_i18n_rejects_plural_forms(tmp_path: Path) -> None:
    """``msgid_plural`` entries are reported as an error, not dropped."""
    catalog_dir = tmp_path / "locale" / "ja" / "LC_MESSAGES"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "pptrepair.po").write_text(
        'msgid "one item"\n'
        'msgid_plural "many items"\n'
        'msgstr[0] "1 item"\n'
        'msgstr[1] "N items"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        build_i18n.compile_all(tmp_path / "locale")
