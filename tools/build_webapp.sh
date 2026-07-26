#!/bin/bash
# v2.1 Web版ビルド導線:
#   1. pptrepair wheelを生成して webapp/wheels/ へ配置(manifest.json付き)
#   2. Pyodideコアをバージョン・sha256固定でGitHubリリースから取得し
#      webapp/vendor/pyodide/ へ展開(取得済みならスキップ)
# 成果物のwebapp/はそのまま静的配信できる(ローカル確認: tools/serve_webapp.py)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WEBAPP="$REPO/webapp"

PYODIDE_VERSION="314.0.3"
PYODIDE_SHA256="49b651e9f406f9a9fb1b4db3bf61d871791a0415649fb7b113fe346c5ddb58bb"
PYODIDE_URL="https://github.com/pyodide/pyodide/releases/download/${PYODIDE_VERSION}/pyodide-core-${PYODIDE_VERSION}.tar.bz2"

PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "$REPO/.venv/bin/python" ]; then
        PYTHON="$REPO/.venv/bin/python"
    else
        PYTHON="python3"
    fi
fi

echo "== wheel =="
rm -rf "$WEBAPP/wheels"
mkdir -p "$WEBAPP/wheels"
"$PYTHON" -m pip wheel "$REPO" --no-deps -w "$WEBAPP/wheels" -q
WHEEL_NAME="$(basename "$(ls "$WEBAPP/wheels"/pptrepair-*.whl)")"
VERSION="$("$PYTHON" -c "import sys; sys.path.insert(0, '$REPO'); import pptrepair; print(pptrepair.__version__)")"
printf '{"wheel": "%s", "version": "%s", "pyodide": "%s"}\n' \
    "$WHEEL_NAME" "$VERSION" "$PYODIDE_VERSION" > "$WEBAPP/wheels/manifest.json"
echo "wheel: $WHEEL_NAME (pptrepair $VERSION)"

echo "== pyodide core =="
STAMP="$WEBAPP/vendor/pyodide/.version"
if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$PYODIDE_VERSION" ]; then
    echo "pyodide $PYODIDE_VERSION already in place; skipping download"
else
    TMP="$(mktemp -d)"
    trap 'rm -rf "$TMP"' EXIT
    echo "downloading $PYODIDE_URL"
    curl -fsSL -o "$TMP/pyodide.tar.bz2" "$PYODIDE_URL"
    GOT="$("$PYTHON" -c "import hashlib, sys; \
print(hashlib.sha256(open(sys.argv[1], 'rb').read()).hexdigest())" \
        "$TMP/pyodide.tar.bz2")"
    if [ "$GOT" != "$PYODIDE_SHA256" ]; then
        echo "ERROR: pyodide-core sha256 mismatch" >&2
        echo "  expected: $PYODIDE_SHA256" >&2
        echo "  got:      $GOT" >&2
        exit 1
    fi
    rm -rf "$WEBAPP/vendor/pyodide"
    mkdir -p "$WEBAPP/vendor"
    tar xjf "$TMP/pyodide.tar.bz2" -C "$WEBAPP/vendor"
    printf '%s' "$PYODIDE_VERSION" > "$STAMP"
    echo "pyodide $PYODIDE_VERSION fetched and verified"
fi

echo "== done =="
echo "webapp/ is ready. Local check: $PYTHON $REPO/tools/serve_webapp.py"
