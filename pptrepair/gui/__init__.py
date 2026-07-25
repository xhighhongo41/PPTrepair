"""PySide6 desktop interface for PPTrepair.

This package is only usable when the optional ``[gui]`` extra (PySide6)
is installed; this module itself, however, never imports PySide6, so
importing :mod:`pptrepair.gui` stays safe even without the extra. The
GUI is launched through :func:`pptrepair.gui.app.main`, invoked by
``pptrepair gui`` (see :mod:`pptrepair.cli`).
"""

from __future__ import annotations
