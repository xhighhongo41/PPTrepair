"""v2.1 Web版まわりのテスト。

対象:

* :mod:`pptrepair.webstrings` — UI文字列テーブルの完全性と7言語翻訳
* webapp/_headers と tools/serve_webapp.py のセキュリティヘッダー同期
* wheelビルドのスモークテスト(.moカタログ同梱の確認)
"""

from __future__ import annotations

import re
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from pptrepair.i18n import SUPPORTED_LANGUAGES
from pptrepair.webstrings import ui_strings

REPO = Path(__file__).resolve().parent.parent

# app.jsが参照するUI文字列キー(renderRow/onResult/初期化で使用)
REQUIRED_KEYS = {
    "tagline", "privacy_note", "drop_hint",
    "loading_engine", "loading_package", "ready_note",
    "col_file", "col_size", "col_status", "col_download",
    "status_queued", "status_processing", "status_intact",
    "status_repaired", "status_salvaged", "status_unrepairable",
    "status_error",
    "download", "show_report", "err_extension", "err_too_large",
    "gui_note", "gui_link", "worker_error", "lang_label",
    "footer_license", "footer_source",
}


def test_ui_strings_covers_required_keys() -> None:
    """英語テーブルがapp.jsの要求キーを全て備える。"""
    strings = ui_strings("en")
    assert REQUIRED_KEYS == set(strings)
    assert all(v for v in strings.values())


@pytest.mark.parametrize("lang", [l for l in SUPPORTED_LANGUAGES
                                  if l != "en"])
def test_ui_strings_translated(lang: str) -> None:
    """各言語で全キーが翻訳済み(=英語と異なる)である。

    "Error"→西語"Error"のような同綴りの正当な翻訳もあるため、
    全体の8割以上が英語と異なれば翻訳済みとみなし、
    プレースホルダ({limit}等)は全言語で保存されていることを厳密に確認する。
    """
    en = ui_strings("en")
    translated = ui_strings(lang)
    assert set(translated) == set(en)
    differing = sum(1 for k in en if translated[k] != en[k])
    assert differing >= len(en) * 0.8, (
        f"{lang}: only {differing}/{len(en)} keys translated"
    )
    for key, value in en.items():
        for placeholder in re.findall(r"\{[a-z_]+\}", value):
            assert placeholder in translated[key], (
                f"{lang}:{key} lost placeholder {placeholder}"
            )


def test_headers_file_matches_dev_server() -> None:
    """webapp/_headers(本番)とserve_webapp.py(ローカル)のヘッダー一致。"""
    sys.path.insert(0, str(REPO / "tools"))
    try:
        from serve_webapp import HEADERS
    finally:
        sys.path.pop(0)
    text = (REPO / "webapp" / "_headers").read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()
             if ln.strip() and not ln.startswith("/")]
    parsed = dict(ln.split(": ", 1) for ln in lines)
    assert parsed == HEADERS


def test_wheel_build_bundles_catalogs(tmp_path: Path) -> None:
    """pip wheelで生成したwheelに全言語の.moが同梱される。"""
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", str(REPO),
         "--no-deps", "-w", str(tmp_path), "-q"],
        check=True, capture_output=True, text=True,
    )
    wheels = list(tmp_path.glob("pptrepair-*.whl"))
    assert len(wheels) == 1
    names = zipfile.ZipFile(wheels[0]).namelist()
    for lang in SUPPORTED_LANGUAGES:
        if lang == "en":
            continue
        assert (
            f"pptrepair/locale/{lang}/LC_MESSAGES/pptrepair.mo" in names
        ), f"missing catalog for {lang}"
