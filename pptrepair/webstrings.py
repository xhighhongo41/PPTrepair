"""Web版(webapp/)UI文字列のカタログ供給。

Web UIの表示文字列をgettextカタログへ一元化するための唯一の供給点。
ブラウザ側(app.js)はworker経由で :func:`ui_strings` の辞書を受け取り、
``data-i18n`` 属性でDOMへ適用する。msgidの追加・変更時は
``tools/extract_messages.py`` → 各言語po更新 → ``tools/build_i18n.py``
の既存フローで7言語カタログへ反映する。
"""

from __future__ import annotations

from .i18n import get_translator


def ui_strings(lang: str) -> dict[str, str]:
    """Return the Web UI string table translated for *lang*.

    Keys are stable identifiers consumed by ``webapp/app.js``; values
    are the translated user-facing strings. Placeholders (``{limit}``
    etc.) are filled on the JavaScript side after translation.
    """
    tr = get_translator(lang)
    return {
        "tagline": tr("Repair corrupted PowerPoint files right in your "
                      "browser."),
        "privacy_note": tr("Your files never leave this device. All "
                           "processing happens locally in your browser; "
                           "nothing is uploaded to any server."),
        "drop_hint": tr("Drop .pptx / .pptm files here, or click to "
                        "choose files"),
        "loading_engine": tr("Loading the repair engine (about 14 MB, "
                             "first visit only)…"),
        "loading_package": tr("Preparing the repair toolkit…"),
        "ready_note": tr("Ready. Your files are processed on this device "
                         "only."),
        "col_file": tr("File"),
        "col_size": tr("Size"),
        "col_status": tr("Status"),
        "col_download": tr("Result"),
        "status_queued": tr("Queued"),
        "status_processing": tr("Repairing…"),
        "status_intact": tr("Already intact — no repair needed"),
        "status_repaired": tr("Repaired"),
        "status_salvaged": tr("Partially salvaged"),
        "status_unrepairable": tr("Could not repair"),
        "status_error": tr("Error"),
        "download": tr("Download"),
        "show_report": tr("Show details"),
        "err_extension": tr("Unsupported file type (only .pptx / .pptm)."),
        "err_too_large": tr("File exceeds the {limit} MB size limit."),
        "gui_note": tr("Multiple corrupted copies, an intact twin, or "
                       "whole folders to scan? The desktop app can merge "
                       "sources for a better repair."),
        "gui_link": tr("Get the desktop version"),
        "worker_error": tr("The repair engine stopped unexpectedly. "
                           "Reload the page and try again."),
        "lang_label": tr("Language"),
        "footer_license": tr("Free software (GPL-3.0). Files are never "
                             "uploaded."),
        "footer_source": tr("Source code"),
    }
