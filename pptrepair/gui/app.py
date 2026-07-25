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


def main() -> int:
    """Run the PPTrepair desktop application.

    Creates the :class:`~PySide6.QtWidgets.QApplication`, applies the
    persisted UI language, shows the main window and runs the Qt event
    loop to completion.

    :returns: the Qt event loop's exit code, suitable for use as the
        process exit code.
    """
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
