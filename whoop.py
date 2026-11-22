#!/usr/bin/env python3
"""
WHOOP Data Exporter for RECOMP Weight Loss Tracker
===================================================
Fetches recovery, sleep, workout, and cycle data from WHOOP API v2
and uploads directly to a Google Drive folder.

Setup:
  1. pip install requests google-api-python-client google-auth-httplib2 google-auth-oauthlib
  2. Create a credentials.json from Google Cloud Console (see README)
  3. Run: python whoop.py
  4. First run opens browser for WHOOP + Google login (one-time each)
  5. Subsequent runs auto-refresh both tokens
"""

import json
import os
import sys
import time
import webbrowser
import hashlib
import secrets
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs

try:
    import requests
except ImportError:
    print("Install requests: pip install requests")
    sys.exit(1)

try:
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request as GRequest
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

# ============================================================
# CONFIGURATION — values loaded from .config.json (not committed to git)
# ============================================================

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".config.json")


def _load_config():
    if not os.path.exists(CONFIG_FILE):
        print(f"\n[ERROR] Missing {CONFIG_FILE}")
        print("  Copy .config.example.json to .config.json and fill in your WHOOP app credentials.")
        sys.exit(1)
    with open(CONFIG_FILE) as f:
        return json.load(f)


_config = _load_config()
CLIENT_ID = _config["WHOOP_CLIENT_ID"]
CLIENT_SECRET = _config["WHOOP_CLIENT_SECRET"]

# Where to save exported data — adjust to your Google Drive sync path
# Mac example: /Users/yourname/Google Drive/My Drive/Health Auto Export/
# Windows example: C:/Users/yourname/Google Drive/Health Auto Export/
EXPORT_PATH = os.path.expanduser("~/Google Drive/My Drive/Health Auto Export/")

# How many days of history to fetch (default: 7)
DAYS_BACK = 21

# ============================================================
# WHOOP API Constants
# ============================================================

AUTH_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer"
REDIRECT_URI = "http://localhost:8080/callback"
SCOPES = "read:recovery read:sleep read:workout read:cycles read:body_measurement read:profile offline"
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".whoop_token.json")

# ============================================================
# Google Drive Constants
# ============================================================

GDRIVE_FOLDER_ID = "16B1_24A3AqpXc6u8fLNTkMP-O2bCztgP"
GDRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
GDRIVE_TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gdrive_token.json")
GDRIVE_CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.json")


# ============================================================
# OAuth 2.0 Flow
# ============================================================

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handles the OAuth redirect callback."""
    auth_code = None

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if "code" in query:
            OAuthCallbackHandler.auth_code = query["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"""
            <html><body style="font-family:sans-serif;text-align:center;padding:60px;">
            <h1 style="color:#22c55e;">WHOOP Connected!</h1>
            <p>You can close this window and return to the terminal.</p>
            </body></html>
            """)
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Authorization failed. Check terminal for details.")
            error = query.get("error", ["unknown"])[0]
            print(f"\n[ERROR] Authorization failed: {error}")

    def log_message(self, format, *args):
        pass  # Suppress HTTP logs


def get_auth_code():
    """Opens browser for WHOOP login and captures the auth code."""
    state = secrets.token_urlsafe(32)
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    auth_url = f"{AUTH_URL}?{urlencode(params)}"

    print("\n[1/4] Opening WHOOP login in your browser...")
    print(f"  If it doesn't open, visit: {auth_url}\n")
    webbrowser.open(auth_url)

    server = HTTPServer(("localhost", 8080), OAuthCallbackHandler)
    server.timeout = 120
    print("  Waiting for WHOOP authorization (timeout: 2 minutes)...")

    while OAuthCallbackHandler.auth_code is None:
        server.handle_request()

    server.server_close()
    return OAuthCallbackHandler.auth_code


def exchange_code_for_token(code):
    """Exchanges the authorization code for access + refresh tokens."""
    print("[2/4] Exchanging code for access token...")
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    resp.raise_for_status()
    token_data = resp.json()
    token_data["obtained_at"] = time.time()
    save_token(token_data)
    print("  Access token obtained and saved!")
    return token_data


def refresh_access_token(refresh_token):
    """Refreshes the access token using the refresh token."""
    print("[*] Refreshing access token...")
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    if resp.status_code != 200:
        print("  [!] Refresh failed — need to re-authorize.")
        return None
    token_data = resp.json()
    token_data["obtained_at"] = time.time()
    save_token(token_data)
    print("  Token refreshed!")
    return token_data


def save_token(token_data):
    with open(TOKEN_FILE, "w") as f:
        json.dump(token_data, f)


def load_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return None


def get_valid_token():
    """Returns a valid access token, refreshing or re-authorizing as needed."""
    token_data = load_token()

    if token_data:
        # Check if token is still valid (with 5 min buffer)
        expires_in = token_data.get("expires_in", 3600)
        obtained_at = token_data.get("obtained_at", 0)
        if time.time() < obtained_at + expires_in - 300:
            return token_data["access_token"]

        # Try refreshing
        if "refresh_token" in token_data:
            new_token = refresh_access_token(token_data["refresh_token"])
            if new_token:
                return new_token["access_token"]

    # Need full re-authorization
    code = get_auth_code()
    token_data = exchange_code_for_token(code)
    return token_data["access_token"]


# ============================================================
# WHOOP API Data Fetching
# ============================================================

def api_get(access_token, endpoint, params=None):
    """Makes an authenticated GET request to the WHOOP API."""
    headers = {"Authorization": f"Bearer {access_token}"}
    url = f"{API_BASE}{endpoint}"
    resp = requests.get(url, headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_all_pages(access_token, endpoint, start_date, end_date):
    """Fetches all pages of data for a given endpoint."""
    all_records = []
    params = {
        "start": start_date.isoformat() + "Z",
        "end": end_date.isoformat() + "Z",
        "limit": 25,
    }

    while True:
        data = api_get(access_token, endpoint, params)
        records = data.get("records", [])
        all_records.extend(records)

        next_token = data.get("next_token")
        if not next_token or not records:
            break
        params["nextToken"] = next_token

    return all_records


def fetch_whoop_data(access_token, days_back=7):
    """Fetches all WHOOP data for the specified time period."""
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days_back)

    print(f"[3/4] Fetching WHOOP data ({days_back} days)...\n")

    # User profile
    print("  Fetching profile...")
    try:
        profile = api_get(access_token, "/v1/user/profile/basic")
    except:
        profile = {}

    # Body measurements
    print("  Fetching body measurements...")
    try:
        body = api_get(access_token, "/v1/user/measurement/body")
    except:
        body = {}

    # Cycles (contains strain data)
    print("  Fetching cycles (strain)...")
    cycles = fetch_all_pages(access_token, "/v1/cycle", start_date, end_date)
    print(f"    Found {len(cycles)} cycles")

    # Recovery (fetched per-cycle, not paginated)
    print("  Fetching recovery...")
    recovery = []
    for cycle in cycles:
        cycle_id = cycle.get("id")
        if cycle_id:
            try:
                rec = api_get(access_token, f"/v2/cycle/{cycle_id}/recovery")
                rec["cycle_id"] = cycle_id
                rec["cycle_start"] = cycle.get("start", "")
                recovery.append(rec)
            except Exception as e:
                pass  # Some cycles may not have recovery data yet
    print(f"    Found {len(recovery)} recovery records")

    # Sleep
    print("  Fetching sleep...")
    sleep = fetch_all_pages(access_token, "/v2/activity/sleep", start_date, end_date)
    print(f"    Found {len(sleep)} sleep records")

    # Workouts
    print("  Fetching workouts...")
    workouts = fetch_all_pages(access_token, "/v2/activity/workout", start_date, end_date)
    print(f"    Found {len(workouts)} workouts")

    return {
        "export_date": datetime.now().isoformat(),
        "period": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "days": days_back,
        },
        "profile": profile,
        "body_measurements": body,
        "cycles": cycles,
        "recovery": recovery,
        "sleep": sleep,
        "workouts": workouts,
        "summary": build_daily_summary(cycles, recovery, sleep, workouts),
    }


def build_daily_summary(cycles, recovery, sleep, workouts):
    """Builds a day-by-day summary for easy dashboard consumption."""
    daily = {}

    for cycle in cycles:
        start = cycle.get("start", "")[:10]
        if start not in daily:
            daily[start] = {}
        score = cycle.get("score", {})
        daily[start]["strain"] = score.get("strain")
        daily[start]["kilojoule"] = score.get("kilojoule")
        daily[start]["average_heart_rate"] = score.get("average_heart_rate")
        daily[start]["max_heart_rate"] = score.get("max_heart_rate")

    for rec in recovery:
        score = rec.get("score", {})
        # Use cycle_start we attached during fetch
        date = rec.get("cycle_start", "")[:10]
        if not date:
            date = rec.get("created_at", "")[:10]
        if date:
            if date not in daily:
                daily[date] = {}
            daily[date]["recovery_score"] = score.get("recovery_score")
            daily[date]["resting_heart_rate"] = score.get("resting_heart_rate")
            daily[date]["hrv_rmssd_milli"] = score.get("hrv_rmssd_milli")
            daily[date]["spo2_percentage"] = score.get("spo2_percentage")
            daily[date]["skin_temp_celsius"] = score.get("skin_temp_celsius")

    for s in sleep:
        start = s.get("start", "")[:10]
        if start not in daily:
            daily[start] = {}
        score = s.get("score", {})
        daily[start]["sleep_performance_percentage"] = score.get("sleep_performance_percentage")
        daily[start]["sleep_efficiency_percentage"] = score.get("sleep_efficiency_percentage")
        daily[start]["sleep_consistency_percentage"] = score.get("sleep_consistency_percentage")
        daily[start]["total_sleep_time_milli"] = score.get("total_in_bed_time_milli")
        daily[start]["rem_sleep_time_milli"] = score.get("stage_summary", {}).get("total_rem_sleep_time_milli")
        daily[start]["sws_sleep_time_milli"] = score.get("stage_summary", {}).get("total_slow_wave_sleep_time_milli")
        daily[start]["light_sleep_time_milli"] = score.get("stage_summary", {}).get("total_light_sleep_time_milli")
        daily[start]["awake_time_milli"] = score.get("stage_summary", {}).get("total_awake_time_milli")
        daily[start]["respiratory_rate"] = score.get("respiratory_rate")

    for w in workouts:
        start = w.get("start", "")[:10]
        if start not in daily:
            daily[start] = {}
        if "workouts" not in daily[start]:
            daily[start]["workouts"] = []
        score = w.get("score", {})
        daily[start]["workouts"].append({
            "sport_id": w.get("sport_id"),
            "strain": score.get("strain"),
            "average_heart_rate": score.get("average_heart_rate"),
            "max_heart_rate": score.get("max_heart_rate"),
            "kilojoule": score.get("kilojoule"),
            "distance_meter": score.get("distance_meter"),
            "zone_durations": score.get("zone_duration", {}),
        })

    return daily


# ============================================================
# Google Drive Upload
# ============================================================

def get_gdrive_service():
    """Returns an authenticated Google Drive service, handling token refresh/login."""
    if not GDRIVE_AVAILABLE:
        return None
    if not os.path.exists(GDRIVE_CREDS_FILE):
        print(f"  [!] {GDRIVE_CREDS_FILE} not found — skipping Drive upload.")
        print("      See setup instructions to enable automatic Drive upload.")
        return None

    creds = None
    if os.path.exists(GDRIVE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GDRIVE_TOKEN_FILE, GDRIVE_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(GRequest())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(GDRIVE_CREDS_FILE, GDRIVE_SCOPES)
            creds = flow.run_local_server(port=8081)
        with open(GDRIVE_TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    return build("drive", "v3", credentials=creds)


def upload_to_gdrive(filepath, filename):
    """Uploads (or replaces) a file in the configured Google Drive folder."""
    if not GDRIVE_AVAILABLE:
        print("  [!] Drive upload skipped — run: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib")
        return
    service = get_gdrive_service()
    if not service:
        return

    # Replace if file with same name already exists in the folder
    existing = service.files().list(
        q=f"name='{filename}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
        fields="files(id)"
    ).execute().get("files", [])

    media = MediaFileUpload(filepath, mimetype="application/json", resumable=False)

    if existing:
        service.files().update(fileId=existing[0]["id"], media_body=media).execute()
        print(f"  Drive: replaced existing file in folder.")
    else:
        service.files().create(
            body={"name": filename, "parents": [GDRIVE_FOLDER_ID]},
            media_body=media,
            fields="id"
        ).execute()
        print(f"  Drive: uploaded new file to folder.")


# ============================================================
# Export
# ============================================================

def save_export(data):
    """Saves the exported data as JSON."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"WhoopExport-{date_str}.json"

    # Try Google Drive path first
    if os.path.exists(EXPORT_PATH):
        filepath = os.path.join(EXPORT_PATH, filename)
    else:
        # Fallback to current directory
        filepath = filename
        print(f"  [!] Google Drive path not found: {EXPORT_PATH}")
        print(f"  [!] Saving to current directory instead.")

    print(f"\n[4/4] Saving export to: {filepath}")
    with open(filepath, "w") as f:
        json.dump(data, f, indent=2, default=str)

    size_kb = os.path.getsize(filepath) / 1024
    print(f"  Done! File size: {size_kb:.1f} KB")

    upload_to_gdrive(filepath, filename)
    return filepath


# ============================================================
# Main
# ============================================================

def main():
    print("=" * 50)
    print("  WHOOP Data Exporter for RECOMP")
    print("=" * 50)

    # Ensure export directory exists
    os.makedirs(EXPORT_PATH, exist_ok=True)

    # Get valid access token
    access_token = get_valid_token()

    # Fetch data
    data = fetch_whoop_data(access_token, DAYS_BACK)

    # Save
    filepath = save_export(data)

    # Print summary
    summary = data.get("summary", {})
    print(f"\n{'='*50}")
    print("  DAILY SUMMARY")
    print(f"{'='*50}")
    for date in sorted(summary.keys(), reverse=True):
        d = summary[date]
        rec = d.get("recovery_score", "—")
        strain = d.get("strain", "—")
        rhr = d.get("resting_heart_rate", "—")
        hrv = d.get("hrv_rmssd_milli", "—")
        if hrv and hrv != "—":
            hrv = f"{hrv:.0f}"
        sleep_perf = d.get("sleep_performance_percentage", "—")
        print(f"  {date}  Recovery: {rec}%  Strain: {strain}  RHR: {rhr}  HRV: {hrv}  Sleep: {sleep_perf}%")

    print(f"\nExport complete! File saved to: {filepath}")
    print("This file will sync to Google Drive automatically.")


if __name__ == "__main__":
    main()
