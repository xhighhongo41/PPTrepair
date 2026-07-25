#!/bin/sh
# 検証パイプライン一本化スクリプト。
# 使い方: tools/check.sh [pytest追加引数...]
# ruff check → pytest の順に実行し、いずれかが失敗したら非0で終了する。
# 構文エラーはruffが検出するため、旧来のpy_compile工程は廃止した。
set -e
cd "$(dirname "$0")/.."

ruff check pptrepair tests tools
.venv/bin/python -m pytest tests/ -q "$@"
