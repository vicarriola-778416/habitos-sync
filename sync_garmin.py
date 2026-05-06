"""
Garmin Connect → Supabase: Sleep Score sync.
Uses the `garminconnect` library (actively maintained).
Designed to run as a GitHub Action (cron daily at 8:30am).

Required env vars (set as GitHub Secrets):
  GARMIN_EMAIL, GARMIN_PASSWORD
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  SUPABASE_USER_ID  (your app user's UUID from auth.users)
"""
import os, json, sys
from datetime import date, timedelta
from garminconnect import Garmin
import requests

# ── config ─────────────────────────────────────────────────────────────
GARMIN_EMAIL    = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
SUPABASE_URL    = os.environ["SUPABASE_URL"]
SUPABASE_SRK    = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
USER_ID         = os.environ["SUPABASE_USER_ID"]
DAYS            = int(os.environ.get("SYNC_DAYS", "14"))

# ── Garmin auth ────────────────────────────────────────────────────────
print("Logging into Garmin Connect...")
client = Garmin(GARMIN_EMAIL, GARMIN_PASSWORD)
client.login()
name = client.get_full_name() or client.display_name or "unknown"
print(f"Authenticated as: {name}")

# ── Fetch sleep data ───────────────────────────────────────────────────
rows = []
today = date.today()
for i in range(DAYS):
    d = today - timedelta(days=i)
    ds = d.isoformat()
    try:
        sleep = client.get_sleep_data(ds)
        if not sleep:
            print(f"  {ds}: no data")
            continue

        dto = sleep.get("dailySleepDTO", sleep)
        scores = dto.get("sleepScores", {})
        score = scores.get("overall", {}).get("value")
        if score is None:
            # Try alternate location
            score = scores.get("qualityScore", {}).get("value")
        if score is None:
            print(f"  {ds}: data but no score (keys: {list(scores.keys())})")
            continue

        score = round(score)
        total_sec = dto.get("sleepTimeSeconds", 0)
        deep_sec = dto.get("deepSleepSeconds", 0)
        light_sec = dto.get("lightSleepSeconds", 0)
        rem_sec = dto.get("remSleepSeconds", 0)
        awake_sec = dto.get("awakeSleepSeconds", 0)
        total_min = round(total_sec / 60) if total_sec else 0
        hours = round(total_sec / 3600, 1) if total_sec else 0

        print(f"  {ds}: score={score}, {hours}h (deep {round(deep_sec/60)}m, rem {round(rem_sec/60)}m)")

        raw = json.dumps({
            "score": score,
            "sleepTimeSeconds": total_sec,
            "deepSleepSeconds": deep_sec,
            "lightSleepSeconds": light_sec,
            "remSleepSeconds": rem_sec,
            "awakeSleepSeconds": awake_sec,
        })

        rows.append({"user_id": USER_ID, "local_date": ds, "metric": "sleep_score", "value": score, "source": "garmin", "raw": raw})
        if total_min > 0:
            rows.append({"user_id": USER_ID, "local_date": ds, "metric": "sleep_minutes", "value": total_min, "source": "garmin"})
        if deep_sec > 0:
            rows.append({"user_id": USER_ID, "local_date": ds, "metric": "sleep_deep_minutes", "value": round(deep_sec / 60), "source": "garmin"})
        if light_sec > 0:
            rows.append({"user_id": USER_ID, "local_date": ds, "metric": "sleep_light_minutes", "value": round(light_sec / 60), "source": "garmin"})
        if rem_sec > 0:
            rows.append({"user_id": USER_ID, "local_date": ds, "metric": "sleep_rem_minutes", "value": round(rem_sec / 60), "source": "garmin"})
        if awake_sec > 0:
            rows.append({"user_id": USER_ID, "local_date": ds, "metric": "sleep_awake_minutes", "value": round(awake_sec / 60), "source": "garmin"})
    except Exception as e:
        print(f"  {ds}: error — {e}")

# ── Upsert to Supabase ────────────────────────────────────────────────
if not rows:
    print("No sleep scores found. Done.")
    sys.exit(0)

print(f"\nUpserting {len(rows)} rows to Supabase...")
headers = {
    "apikey": SUPABASE_SRK,
    "Authorization": f"Bearer {SUPABASE_SRK}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}
resp = requests.post(
    f"{SUPABASE_URL}/rest/v1/body_metrics",
    headers=headers,
    json=rows,
)
if resp.status_code in (200, 201):
    print(f"OK — {len(rows)} sleep scores synced.")
else:
    print(f"FAILED — {resp.status_code}: {resp.text}")
    sys.exit(1)
