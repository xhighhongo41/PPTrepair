"""Catalog completeness checks for the shipped translation catalogs.

These tests keep the ``.po``/``.mo`` catalogs honest as the code
evolves: every msgid used by the code -- the core CLI (``pptrepair/*.py``)
and the PySide6 GUI (``pptrepair/gui/*.py``) alike, extracted the same
way ``tools/extract_messages.py`` does -- must be covered by every
shipped language, with format placeholders preserved.
"""

from __future__ import annotations

import ast
import gettext
import re
from pathlib import Path

import pytest

from pptrepair.extract import _CORE_FIELDS
from pptrepair.i18n import DOMAIN, LOCALE_DIR, SUPPORTED_LANGUAGES
from pptrepair.report import VERDICT_LABELS

_PACKAGE_DIR = Path(__file__).resolve().parent.parent / "pptrepair"
_PLACEHOLDER_RE = re.compile(r"\{[a-z_]+\}")

_SHIPPED_LANGUAGES = [lang for lang in SUPPORTED_LANGUAGES if lang != "en"]


def _source_msgids() -> set[str]:
    """Collect every msgid the code can pass to a translator.

    Scans both ``pptrepair/*.py`` (the core CLI) and
    ``pptrepair/gui/*.py`` (the PySide6 desktop app) -- non-recursively
    in each, matching :func:`tools.extract_messages.literal_msgids`.
    """
    msgids: set[str] = set(VERDICT_LABELS.values())
    msgids.update(label for label, _ns, _tag in _CORE_FIELDS)
    source_dirs = (_PACKAGE_DIR, _PACKAGE_DIR / "gui")
    for source_dir in source_dirs:
        for py_file in sorted(source_dir.glob("*.py")):
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "tr" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)):
                    msgids.add(node.args[0].value)
    return msgids


def _catalog(lang: str) -> dict[str, str]:
    """Load the compiled catalog for *lang* as a msgid -> msgstr dict."""
    translation = gettext.translation(
        DOMAIN, localedir=LOCALE_DIR, languages=[lang])
    catalog = dict(translation._catalog)
    catalog.pop("", None)  # metadata header
    return catalog


@pytest.mark.parametrize("lang", _SHIPPED_LANGUAGES)
def test_catalog_covers_every_source_msgid(lang: str) -> None:
    """Each shipped language translates exactly the msgids in use."""
    source = _source_msgids()
    catalog = _catalog(lang)
    missing = source - set(catalog)
    stale = set(catalog) - source
    assert not missing, f"{lang}: untranslated msgids: {sorted(missing)}"
    assert not stale, f"{lang}: stale catalog entries: {sorted(stale)}"


@pytest.mark.parametrize("lang", _SHIPPED_LANGUAGES)
def test_catalog_entries_are_translated(lang: str) -> None:
    """No shipped msgstr is empty."""
    for msgid, msgstr in _catalog(lang).items():
        assert msgstr.strip(), f"{lang}: empty translation for {msgid!r}"


@pytest.mark.parametrize("lang", _SHIPPED_LANGUAGES)
def test_catalog_preserves_placeholders(lang: str) -> None:
    """Named format placeholders survive translation untouched."""
    for msgid, msgstr in _catalog(lang).items():
        expected = sorted(_PLACEHOLDER_RE.findall(msgid))
        found = sorted(_PLACEHOLDER_RE.findall(msgstr))
        assert expected == found, (
            f"{lang}: placeholder mismatch for {msgid!r}: "
            f"{expected} != {found}")


def test_japanese_sample_translation() -> None:
    """A known message renders in Japanese through the real machinery."""
    from pptrepair.i18n import get_translator

    tr = get_translator("ja")
    rendered = tr("Slides recovered: {ok} of {total}").format(ok=3, total=10)
    assert rendered == "復元スライド: 10 枚中 3 枚"
