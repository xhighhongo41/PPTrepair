"""Entry point for the PySide6 desktop application.

PySide6 is imported at this module's top level: :mod:`pptrepair.cli`
imports this module lazily, inside a ``try``/``except ImportError``
block, so the ``[gui]`` extra stays optional for the rest of the
package.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

import pptrepair
from pptrepair.gui.i18n import set_language
from pptrepair.gui.main_window import MainWindow
from pptrepair.gui.settings import Settings


def _set_macos_menu_bar_name(name: str) -> None:
    """Rename the macOS application menu from "Python" to *name*.

    An unbundled Python process has no .app Info.plist, so macOS
    falls back to the interpreter bundle's CFBundleName ("Python").
    Rewriting the in-memory info dictionary through PyObjC before
    AppKit builds the menu bar fixes the label. No-op on other
    platforms, or when pyobjc is unavailable (the menu then simply
    keeps its default label).
    """
    if sys.platform != "darwin":
        return
    try:
        from Foundation import NSBundle
    except ImportError:
        return

    bundle = NSBundle.mainBundle()
    if bundle is None:
        return
    info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
    if info is None:
        return
    info["CFBundleName"] = name
    info["CFBundleDisplayName"] = name


def main() -> int:
    """Run the PPTrepair desktop application.

    Creates the :class:`~PySide6.QtWidgets.QApplication`, applies the
    persisted UI language, shows the main window and runs the Qt event
    loop to completion.

    :returns: the Qt event loop's exit code, suitable for use as the
        process exit code.
    """
    # Must run before QApplication is constructed, which is when AppKit
    # reads CFBundleName to build the menu bar's application menu.
    _set_macos_menu_bar_name("PPTrepair")

    # Set once, ahead of any QSettings() construction (see
    # pptrepair.gui.settings.Settings), so QSettings' own
    # organisation/application-based default storage path is stable.
    QCoreApplication.setOrganizationName("PPTrepair")
    app = QApplication(sys.argv)
    app.setApplicationName("PPTrepair")
    app.setApplicationVersion(pptrepair.__version__)

    # Must run before any widget is built (see
    # pptrepair.gui.i18n.set_language's threading/ordering contract);
    # the GUI never retranslates widgets already on screen, so this is
    # the only place the language is applied.
    set_language(Settings().language())

    window = MainWindow()
    window.show()

    return app.exec()
