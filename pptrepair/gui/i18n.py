"""GUI-wide translation state for the PySide6 desktop application.

Wraps :mod:`pptrepair.i18n`'s gettext machinery in a small piece of
module-level state so every GUI module can simply
``from pptrepair.gui.i18n import tr`` and call ``tr(msgid)`` without
threading a translator callable through every constructor. This module
never imports PySide6, so it stays importable (and usable) even when
the optional ``[gui]`` extra is not installed -- :func:`pptrepair.cli.run_gui`
relies on that to translate its own "PySide6 is not installed" hint.
"""

from __future__ import annotations

from pptrepair.i18n import get_translator

#: The module-level translator callable, defaulting to English
#: passthrough until :func:`set_language` is called.
_translator = get_translator("en")


def set_language(lang: str) -> None:
    """Switch every subsequent :func:`tr` call to *lang*'s catalog.

    Must only be called from the UI thread, before any widget that
    calls :func:`tr` is constructed -- in practice once, at
    application startup, from :func:`pptrepair.gui.app.main`. This
    module keeps no lock, so calling it concurrently with :func:`tr`
    from a background worker thread, or after widgets have already
    been built, is unsafe. The GUI never retranslates already-built
    widgets when the language changes later (see
    :class:`~pptrepair.gui.settings.SettingsDialog`): a new language
    choice only takes effect after the application restarts.

    :param lang: one of :data:`pptrepair.i18n.SUPPORTED_LANGUAGES`.
    :raises ValueError: when *lang* is not supported (see
        :func:`pptrepair.i18n.get_translator`).
    """
    global _translator
    _translator = get_translator(lang)


def tr(msgid: str) -> str:
    """Translate *msgid* through the currently active GUI language.

    Identity (returns *msgid* unchanged) until :func:`set_language` is
    called with a language other than ``"en"``.

    :param msgid: the English source string, matching a msgid in
        ``pptrepair/locale/pptrepair.pot``.
    """
    return _translator(msgid)
