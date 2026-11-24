"""Local command-line exporter.

Runs the WHOOP OAuth flow in a browser on first use, caches tokens on disk,
writes the export JSON locally and (optionally) uploads it to Google Drive.
"""

from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from . import auth
from .client import WhoopClient
from .config import ConfigError, Settings, load_settings
from .drive import export_filename, serialize_export, upsert_json
from .summary import format_summary_line

logger = logging.getLogger("whoop_exporter")

REPO_ROOT = Path(__file__).resolve().parent.parent
REDIRECT_HOST = "localhost"
REDIRECT_PORT = 8080
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/callback"
AUTH_TIMEOUT_SECONDS = 120
WHOOP_TOKEN_FILE = REPO_ROOT / ".whoop_token.json"
GDRIVE_TOKEN_FILE = REPO_ROOT / ".gdrive_token.json"
GDRIVE_CREDS_FILE = REPO_ROOT / "credentials.json"
GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


# ---------------------------------------------------------------- WHOOP OAuth


def _load_token(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_token(path: Path, token: dict[str, Any]) -> None:
    path.write_text(json.dumps(token), encoding="utf-8")
    path.chmod(0o600)


def _browser_authorize(settings: Settings) -> str:
    """Open the WHOOP consent page and wait for the redirect carrying the code."""
    state = secrets.token_urlsafe(32)
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "code" in query:
                result["code"] = query["code"][0]
            else:
                result["error"] = query.get("error", ["unknown"])[0]
            ok = "code" in result
            self.send_response(200 if ok else 400)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"WHOOP connected. You can close this window."
                if ok
                else b"Authorization failed. Check the terminal."
            )

        def log_message(self, *args: Any) -> None:
            pass

    url = f"{auth.AUTH_URL}?" + urlencode(
        {
            "client_id": settings.whoop_client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": auth.SCOPES,
            "state": state,
        }
    )
    print(f"Opening WHOOP login in your browser. If it doesn't open, visit:\n  {url}\n")
    webbrowser.open(url)

    server = HTTPServer((REDIRECT_HOST, REDIRECT_PORT), Handler)
    server.timeout = AUTH_TIMEOUT_SECONDS
    try:
        server.handle_request()
    finally:
        server.server_close()

    if "code" not in result:
        raise auth.TokenError(f"WHOOP authorization failed: {result.get('error', 'timed out')}")
    return result["code"]


def get_access_token(settings: Settings, token_file: Path = WHOOP_TOKEN_FILE) -> str:
    """Return a valid access token, refreshing or re-authorizing as needed."""
    token = _load_token(token_file)
    if token and auth.is_access_token_fresh(token):
        return token["access_token"]

    if token and token.get("refresh_token"):
        try:
            token = auth.refresh_access_token(
                token["refresh_token"], settings.whoop_client_id, settings.whoop_client_secret
            )
            _save_token(token_file, token)
            return token["access_token"]
        except auth.TokenError as exc:
            logger.warning("Refresh failed (%s); re-authorizing in the browser", exc)

    code = _browser_authorize(settings)
    token = auth.exchange_code(
        code, REDIRECT_URI, settings.whoop_client_id, settings.whoop_client_secret
    )
    _save_token(token_file, token)
    return token["access_token"]


# --------------------------------------------------------------- Google Drive


def _drive_service() -> Any | None:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning("Drive upload skipped: install requirements-local.txt")
        return None
    if not GDRIVE_CREDS_FILE.is_file():
        logger.warning("Drive upload skipped: %s not found", GDRIVE_CREDS_FILE.name)
        return None

    creds = None
    if GDRIVE_TOKEN_FILE.is_file():
        creds = Credentials.from_authorized_user_file(str(GDRIVE_TOKEN_FILE), GDRIVE_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(GDRIVE_CREDS_FILE), GDRIVE_SCOPES)
            creds = flow.run_local_server(port=REDIRECT_PORT + 1)
        GDRIVE_TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
        GDRIVE_TOKEN_FILE.chmod(0o600)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


# ----------------------------------------------------------------------- main


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export WHOOP data to JSON (and Google Drive).")
    parser.add_argument("--days", type=int, help="days of history to fetch (overrides DAYS_BACK)")
    parser.add_argument("--out", type=Path, help="output directory (overrides EXPORT_PATH)")
    parser.add_argument("--no-drive", action="store_true", help="skip the Google Drive upload")
    parser.add_argument(
        "--duckdb",
        type=Path,
        metavar="DB",
        help="also load the export into this DuckDB store (see health_pipeline.store)",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="with --duckdb, fetch only the days since the store's latest WHOOP record "
        "(plus a few days of overlap for re-scored records)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    if args.incremental and not args.duckdb:
        parser.error("--incremental requires --duckdb")
    if args.incremental and args.days:
        parser.error("--incremental and --days are mutually exclusive")
    return args


def _days_to_fetch(args: argparse.Namespace, settings: Settings) -> int:
    if args.days:
        return args.days
    if not args.incremental:
        return settings.days_back
    from health_pipeline.store import HealthStore, incremental_days

    with HealthStore(args.duckdb) as store:
        watermark = store.watermark("whoop")
    days = incremental_days(watermark, datetime.now(UTC).date(), fallback=settings.days_back)
    logger.info("Incremental fetch: %d day(s) (store has WHOOP data up to %s)", days, watermark)
    return days


def _load_into_duckdb(db: Path, export_file: Path) -> None:
    try:
        from health_pipeline.store import HealthStore
    except ImportError:  # pragma: no cover - depends on the environment
        logger.error("DuckDB load skipped: install requirements-local.txt")
        return
    with HealthStore(db) as store:
        print(f"DuckDB {db}: {store.load_file(export_file, 'whoop')}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s"
    )
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2

    days = _days_to_fetch(args, settings)
    out_dir = args.out or settings.export_path or Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)

    client = WhoopClient(get_access_token(settings))
    data = client.fetch_export(days)

    filename = export_filename(datetime.now(UTC).date())
    content = serialize_export(data)
    filepath = out_dir / filename
    filepath.write_bytes(content)
    print(f"Saved {filepath} ({len(content) / 1024:.1f} KB)")

    if not args.no_drive and settings.gdrive_folder_id:
        service = _drive_service()
        if service is not None:
            upsert_json(service, settings.gdrive_folder_id, filename, content)
    elif not args.no_drive:
        logger.info("GDRIVE_FOLDER_ID not set; skipping Drive upload")

    if args.duckdb:
        _load_into_duckdb(args.duckdb, filepath)

    for day in sorted(data["summary"], reverse=True):
        print("  " + format_summary_line(day, data["summary"][day]))
    return 0
