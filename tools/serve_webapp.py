"""Web版のローカル確認用サーバー。

webapp/_headers(Cloudflare Pages用)と同じセキュリティヘッダーを付けて
webapp/ を静的配信する。本番とローカルでCSP挙動を一致させるための
開発用ツール。ヘッダーを変更するときは webapp/_headers と本ファイルの
HEADERS を両方更新すること。

使い方: python tools/serve_webapp.py [port]  (既定8760)
"""

from __future__ import annotations

import functools
import http.server
import sys
from pathlib import Path
from typing import ClassVar

WEBAPP = Path(__file__).resolve().parent.parent / "webapp"

# webapp/_headers と同内容を維持する(コメント参照)
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        "script-src 'self' 'wasm-unsafe-eval'; "
        "worker-src 'self'; "
        "style-src 'self'; "
        "img-src 'self'; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map: ClassVar[dict[str, str]] = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".wasm": "application/wasm",
        ".whl": "application/octet-stream",
        ".json": "application/json",
    }

    def end_headers(self):
        for name, value in HEADERS.items():
            self.send_header(name, value)
        super().end_headers()


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8760
    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", port), functools.partial(Handler, directory=str(WEBAPP))
    )
    print(f"serving webapp/ on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
