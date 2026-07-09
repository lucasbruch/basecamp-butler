"""One-time interactive OAuth handshake with Basecamp.

Opens the authorization URL, captures the redirect code on a tiny local server,
exchanges it for tokens, discovers your bc3 account, and stores everything in
Postgres so the poller can run unattended.

Usage:
    docker compose run --rm -p 8000:8000 app python scripts/authorize.py
    # or locally, with DATABASE_URL pointing at your db:
    python scripts/authorize.py
"""
from __future__ import annotations

import http.server
import socket
import threading
import urllib.parse
import webbrowser

from app.basecamp.auth import build_authorize_url, discover_account, exchange_code, store_token
from app.config import settings
from app.db import init_db, session_scope

_captured: dict[str, str] = {}
_done = threading.Event()


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != urllib.parse.urlparse(settings.basecamp_redirect_uri).path:
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        if "code" in qs:
            _captured["code"] = qs["code"][0]
            self.wfile.write(b"<h1>Authorized. You can close this tab and return to the terminal.</h1>")
        else:
            self.wfile.write(b"<h1>No code received.</h1>")
        _done.set()

    def log_message(self, *args):  # silence default logging
        pass


def _redirect_port() -> int:
    parsed = urllib.parse.urlparse(settings.basecamp_redirect_uri)
    return parsed.port or 8000


def main() -> None:
    if not settings.basecamp_client_id or not settings.basecamp_client_secret:
        raise SystemExit(
            "Set BASECAMP_CLIENT_ID and BASECAMP_CLIENT_SECRET in .env first."
        )

    init_db()

    port = _redirect_port()
    server = http.server.HTTPServer(("0.0.0.0", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = build_authorize_url()
    print("\nOpen this URL in your browser to authorize Basecamp:\n")
    print("   " + url + "\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass

    print(f"Waiting for the redirect on {settings.basecamp_redirect_uri} …")
    if not _done.wait(timeout=300):
        raise SystemExit("Timed out waiting for authorization.")
    server.shutdown()

    code = _captured.get("code")
    if not code:
        raise SystemExit("Authorization failed — no code received.")

    print("Exchanging code for tokens…")
    token_data = exchange_code(code)
    account_id, api_href = discover_account(token_data["access_token"])
    with session_scope() as db:
        store_token(db, token_data, account_id=account_id, api_href=api_href)

    print(f"\n✅ Connected to Basecamp account {account_id} ({api_href}).")
    print("Tokens stored. You can now `docker compose up -d`.")


if __name__ == "__main__":
    main()
