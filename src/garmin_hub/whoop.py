"""WHOOP, through its official OAuth2 API (v2).

You register your own app at developer-dashboard.whoop.com and pass its id and secret in
WHOOP_CLIENT_ID / WHOOP_CLIENT_SECRET. Tokens are cached in ~/.whoop-hub/tokens.json (owner-only).
"""

import json
import os
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from . import db

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer"
# `offline` is what gets us a refresh token, so you only log in once.
SCOPES = "offline read:recovery read:cycles read:sleep read:workout read:profile"
REDIRECT_URI = os.getenv("WHOOP_REDIRECT_URI", "http://localhost:8765/callback")
TOKEN_PATH = Path(os.getenv("WHOOP_TOKENS", "~/.whoop-hub/tokens.json")).expanduser()

COLLECTIONS = {
    "recovery": "/v2/recovery",
    "sleep": "/v2/activity/sleep",
    "cycle": "/v2/cycle",
    "workout": "/v2/activity/workout",
}
PAGE_LIMIT = 25  # WHOOP's maximum
REQUEST_DELAY_SECONDS = 0.7  # stays under WHOOP's 100 requests per minute
LOGIN_TIMEOUT_SECONDS = 300
REFRESH_MARGIN_SECONDS = 60


class WhoopAuthError(RuntimeError):
    pass


class WhoopRateLimited(RuntimeError):
    pass


def _credentials() -> tuple[str, str]:
    client_id, client_secret = os.getenv("WHOOP_CLIENT_ID"), os.getenv("WHOOP_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise WhoopAuthError(
            "Set WHOOP_CLIENT_ID and WHOOP_CLIENT_SECRET from your app at developer-dashboard.whoop.com."
        )
    return client_id, client_secret


def _http_json(request: urllib.request.Request) -> dict:
    """One HTTP call. Kept separate so tests can replace it."""
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def _token_request(fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(
        TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
    )
    try:
        tokens = _http_json(request)
    except urllib.error.HTTPError as exc:
        raise WhoopAuthError(f"WHOOP refused the token request (HTTP {exc.code}). Run: uv run garmin-hub whoop-login") from exc
    return {**tokens, "expires_at": time.time() + int(tokens.get("expires_in", 3600))}


def save_tokens(tokens: dict, path: Path = TOKEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tokens, f)


def load_tokens(path: Path = TOKEN_PATH) -> dict:
    if not path.exists():
        raise WhoopAuthError("Not connected to WHOOP yet. Run: uv run garmin-hub whoop-login")
    return json.loads(path.read_text())


# --- Login ---------------------------------------------------------------------------------


def authorize_url(client_id: str, state: str) -> str:
    query = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT_URI, "scope": SCOPES, "state": state}
    return f"{AUTH_URL}?{urllib.parse.urlencode(query)}"


def code_from_redirect(redirected_url: str, expected_state: str) -> str:
    params = urllib.parse.parse_qs(urllib.parse.urlparse(redirected_url).query)
    if "error" in params:
        raise WhoopAuthError(f"WHOOP login was declined: {params['error'][0]}")
    if params.get("state", [""])[0] != expected_state:
        raise WhoopAuthError("The login reply didn't match this login attempt. Start again.")
    if "code" not in params:
        raise WhoopAuthError("No authorization code in the redirect URL.")
    return params["code"][0]


def _wait_for_local_redirect() -> str:
    """Serve one request on the localhost redirect URI and return the URL WHOOP sent the browser to."""
    target = urllib.parse.urlparse(REDIRECT_URI)
    received: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"WHOOP is connected. You can close this tab.")

        def log_message(self, *_):
            pass

    with HTTPServer((target.hostname, target.port or 80), Handler) as httpd:
        httpd.timeout = LOGIN_TIMEOUT_SECONDS
        httpd.handle_request()
    if not received:
        raise WhoopAuthError("Timed out waiting for the WHOOP login. Run whoop-login again.")
    return received[0]


def login_interactive() -> dict:
    client_id, client_secret = _credentials()
    state = secrets.token_hex(4)  # WHOOP wants exactly 8 characters
    url = authorize_url(client_id, state)
    print(f"Approve access in your browser. If it doesn't open, go to:\n{url}\n")
    local = urllib.parse.urlparse(REDIRECT_URI).hostname in ("localhost", "127.0.0.1")
    webbrowser.open(url)
    redirected = _wait_for_local_redirect() if local else input("Paste the full URL you were sent to: ").strip()
    code = code_from_redirect(redirected, state)
    tokens = _token_request({
        "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI,
        "client_id": client_id, "client_secret": client_secret,
    })
    save_tokens(tokens)
    return tokens


# --- API client ----------------------------------------------------------------------------


class Client:
    def __init__(self, token_path: Path = TOKEN_PATH):
        self.token_path = token_path
        self.tokens = load_tokens(token_path)

    def _refresh(self) -> None:
        # WHOOP rotates refresh tokens: the old one stops working, so save the new pair straight away.
        client_id, client_secret = _credentials()
        self.tokens = _token_request({
            "grant_type": "refresh_token", "refresh_token": self.tokens["refresh_token"],
            "client_id": client_id, "client_secret": client_secret, "scope": "offline",
        })
        save_tokens(self.tokens, self.token_path)

    def get(self, path: str, params: dict | None = None, _retried: bool = False) -> dict:
        if time.time() > self.tokens.get("expires_at", 0) - REFRESH_MARGIN_SECONDS:
            self._refresh()
        url = f"{API_BASE}{path}?{urllib.parse.urlencode(params or {})}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.tokens['access_token']}", "Accept": "application/json"}
        )
        try:
            return _http_json(request)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and not _retried:
                self._refresh()
                return self.get(path, params, _retried=True)
            if exc.code == 401:
                raise WhoopAuthError("WHOOP rejected the saved login. Run: uv run garmin-hub whoop-login") from exc
            if exc.code == 429:
                raise WhoopRateLimited("WHOOP rate-limited us. Progress is saved; try again in a minute.") from exc
            raise

    def collection(self, kind: str, start: str, delay: float = REQUEST_DELAY_SECONDS) -> Iterator[dict]:
        """Every record of one kind since `start` (ISO time), following WHOOP's next_token pages."""
        params = {"limit": PAGE_LIMIT, "start": start}
        while True:
            page = self.get(COLLECTIONS[kind], params)
            yield from page.get("records", [])
            next_token = page.get("next_token")
            if not next_token:
                return
            params = {**params, "nextToken": next_token}
            time.sleep(delay)


# --- Sync ----------------------------------------------------------------------------------


def sync(client: Client, conn: sqlite3.Connection, days: int, now: datetime | None = None,
         delay: float = REQUEST_DELAY_SECONDS) -> dict:
    """Upsert the last `days` of every WHOOP collection. Re-fetching is safe; recent records get rescored."""
    now = now or datetime.now(UTC)
    start = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    counts = {}
    for kind in COLLECTIONS:
        n = 0
        for record in client.collection(kind, start, delay):
            db.save_whoop(conn, kind, record)
            n += 1
        conn.commit()
        counts[kind] = n
        print(f"  whoop {kind}: {n}", file=sys.stderr)
    return counts
