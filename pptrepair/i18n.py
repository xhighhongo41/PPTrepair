"""Translation support for human-facing repair reports.

Standard GNU gettext machinery (:mod:`gettext` from the standard
library) with compiled ``.mo`` catalogs shipped inside the package
under ``pptrepair/locale/<lang>/LC_MESSAGES/pptrepair.mo``.

Only human-facing repair output (stdout report, ``REPORT.txt``, headers
of extracted text files) is translated. Machine-facing output (JSON,
verdict codes) and diagnostic evidence stay in English.

Message conventions:

* English source strings are the msgids, marked with the translator
  callable returned by :func:`get_translator`.
* Placeholders use named ``str.format`` fields, filled *after*
  translation: ``tr("Slides lost: {lost} of {total}").format(...)``.
* Messages are written label-style ("Slides lost: 3") so catalogs need
  no plural-form rules.
"""

from __future__ import annotations

import gettext
from pathlib import Path
from typing import Callable

#: Languages selectable via ``--lang`` (msgids themselves are English).
SUPPORTED_LANGUAGES: tuple[str, ...] = (
    "en", "ja", "zh", "ko", "es", "fr", "de",
)

DEFAULT_LANGUAGE = "en"

#: Directory containing the compiled message catalogs.
LOCALE_DIR = Path(__file__).parent / "locale"

#: gettext domain (catalog file name stem).
DOMAIN = "pptrepair"


def _identity(message: str) -> str:
    """Return *message* unchanged (translator for ``"en"``)."""
    return message


def get_translator(lang: str) -> Callable[[str], str]:
    """Return the translation callable for *lang*.

    ``"en"`` returns an identity function (source strings are already
    English). Other supported languages load the packaged ``.mo``
    catalog with ``fallback=True``, so a missing catalog or message
    silently falls back to English rather than failing.

    :raises ValueError: when *lang* is not in
        :data:`SUPPORTED_LANGUAGES` (the CLI normally prevents this via
        argparse choices).
    """
    if lang not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"unsupported language: {lang!r} "
            f"(supported: {', '.join(SUPPORTED_LANGUAGES)})"
        )
    if lang == "en":
        return _identity
    translation = gettext.translation(
        DOMAIN, localedir=LOCALE_DIR, languages=[lang], fallback=True
    )
    return translation.gettext
