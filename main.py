import functions_framework
import json
import os
import time
import requests
from datetime import datetime, timedelta

import google.auth
from google.cloud import secretmanager
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload

# ============================================================
# Configuration — WHOOP client id/secret loaded from .config.json (not committed to git)
# ============================================================

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".config.json")

with open(CONFIG_FILE) as f:
    _config = json.load(f)

CLIENT_ID = _config["WHOOP_CLIENT_ID"]
CLIENT_SECRET = _config["WHOOP_CLIENT_SECRET"]

GDRIVE_FOLDER_ID = "16B1_24A3AqpXc6u8fLNTkMP-O2bCztgP"

# Secret Manager secret ID (just the name, not the full resource path)
# Store the WHOOP refresh token here before deploying.
WHOOP_SECRET_ID = "whoop-refresh-token"

TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE = "https://api.prod.whoop.com/developer"
DAYS_BACK = 7

# ============================================================
# Secret Manager — WHOOP refresh token storage
# ============================================================

def _secret_client():
    return secretmanager.SecretManagerServiceClient()


def get_refresh_token(project_id):
    """Reads the WHOOP refresh token from Secret Manager."""
    client = _secret_client()
    name = f"projects/{project_id}/secrets/{WHOOP_SECRET_ID}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8").strip()


def save_refresh_token(project_id, refresh_token):
    """Adds a new version of the refresh token secret."""
    client = _secret_client()
    parent = f"projects/{project_id}/secrets/{WHOOP_SECRET_ID}"
    client.add_secret_version(
        request={
            "parent": parent,
            "payload": {"data": refresh_token.encode("utf-8")},
        }
    )


# ============================================================
# WHOOP OAuth — token exchange using refresh token
# ============================================================

def get_access_token(project_id):
    """
    Exchanges the stored refresh token for a new access token.
    WHOOP rotates refresh tokens on each use, so the new one is saved back.
    """
    refresh_token = get_refresh_token(project_id)
    resp = requests.post(TOKEN_URL, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
    })
    resp.raise_for_status()
    data = resp.json()

    # WHOOP rotates the refresh token — save the new one
    if "refresh_token" in data:
        save_refresh_token(project_id, data["refresh_token"])

    return data["access_token"]


# ============================================================
# WHOOP API helpers
# ============================================================

def api_get(access_token, endpoint, params=None):
    headers = {"Authorization": f"Bearer {access_token}"}
    resp = requests.get(f"{API_BASE}{endpoint}", headers=headers, params=params)
    resp.raise_for_status()
    return resp.json()


def fetch_all_pages(access_token, endpoint, start_date, end_date):
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
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days_back)

    try:
        profile = api_get(access_token, "/v1/user/profile/basic")
    except Exception:
        profile = {}

    try:
        body = api_get(access_token, "/v1/user/measurement/body")
    except Exception:
        body = {}

    cycles = fetch_all_pages(access_token, "/v1/cycle", start_date, end_date)

    recovery = []
    for cycle in cycles:
        cycle_id = cycle.get("id")
        if cycle_id:
            try:
                rec = api_get(access_token, f"/v2/cycle/{cycle_id}/recovery")
                rec["cycle_id"] = cycle_id
                rec["cycle_start"] = cycle.get("start", "")
                recovery.append(rec)
            except Exception:
                pass

    sleep = fetch_all_pages(access_token, "/v2/activity/sleep", start_date, end_date)
    workouts = fetch_all_pages(access_token, "/v2/activity/workout", start_date, end_date)

    return {
        "export_date": datetime.utcnow().isoformat(),
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
            "sport_name": w.get("sport_name"),
            "strain": score.get("strain"),
            "average_heart_rate": score.get("average_heart_rate"),
            "max_heart_rate": score.get("max_heart_rate"),
            "kilojoule": score.get("kilojoule"),
            "distance_meter": score.get("distance_meter"),
            "zone_durations": score.get("zone_durations", {}),
        })

    return daily


# ============================================================
# Google Drive upload
# ============================================================

def upload_to_drive(data):
    """Uploads WHOOP JSON to a Shared Drive using the function's service account."""
    credentials, project = google.auth.default(
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    service = build("drive", "v3", credentials=credentials)

    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    filename = f"WhoopExport-{date_str}.json"
    file_content = json.dumps(data, indent=2, default=str).encode("utf-8")

    # supportsAllDrives=True is required for Shared Drive operations
    existing = service.files().list(
        q=f"name='{filename}' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
        fields="files(id)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute().get("files", [])

    media = MediaInMemoryUpload(file_content, mimetype="application/json", resumable=False)

    if existing:
        result = service.files().update(
            fileId=existing[0]["id"],
            media_body=media,
            supportsAllDrives=True,
        ).execute()
        print(f"Drive: updated {filename} (id={result['id']})")
    else:
        result = service.files().create(
            body={"name": filename, "parents": [GDRIVE_FOLDER_ID]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        print(f"Drive: created {filename} (id={result['id']})")

    return result["id"]


# ============================================================
# Cloud Function entrypoint
# ============================================================

@functions_framework.http
def trigger_upload(request):
    """HTTP Cloud Function — triggered by Cloud Scheduler."""
    try:
        _, project_id = google.auth.default()
        print(f"Project: {project_id} | Fetching {DAYS_BACK} days of WHOOP data...")

        access_token = get_access_token(project_id)
        data = fetch_whoop_data(access_token, DAYS_BACK)

        summary = data.get("summary", {})
        for date in sorted(summary.keys(), reverse=True):
            d = summary[date]
            print(
                f"  {date}  Recovery: {d.get('recovery_score')}%  "
                f"Strain: {d.get('strain')}  HRV: {d.get('hrv_rmssd_milli')}"
            )

        file_id = upload_to_drive(data)
        return f"OK — WhoopExport uploaded (Drive file id={file_id})", 200

    except Exception as e:
        print(f"Error: {e}")
        return f"Error: {e}", 500
