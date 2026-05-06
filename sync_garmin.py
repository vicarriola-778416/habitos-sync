"""
Garmin Connect → Supabase: Sleep Score sync.
Uses the `garth` library for reliable Garmin auth.
Designed to run as a GitHub Action (cron daily at 8:30am).

Required env vars (set as GitHub Secrets):
  GARMIN_EMAIL, GARMIN_PASSWORD
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  SUPABASE_USER_ID  (your app user's UUID from auth.users)
"""
import os, json, sys
from datetime import date, timedelta
import garth
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
garth.login(GARMIN_EMAIL, GARMIN_PASSWORD)
print(f"Authenticated as: {garth.client.username}")

# ── Fetch sleep data ───────────────────────────────────────────────────
rows = []
today = date.today()
for i in range(DAYS):
    d = today - timedelta(days=i)
    ds = d.isoformat()
    try:
        sleep = garth.connectapi(
            f"/wellness-service/wellness/dailySleepData/{garth.client.username}",
            params={"date": ds, "nonSleepBufferMinutes": 60},
        )
        if not sleep:
            print(f"  {ds}: no data")
            continue

        dto = sleep.get("dailySleepDTO", sleep)
        scores = dto.get("sleepScores", {})
        score = (
            scores.get("overall", {}).get("value")
            or scores.get("totalDuration", {}).get("value")
        )
        if score is None:
            print(f"  {ds}: data but no score")
            continue

        score = round(score)
        print(f"  {ds}: score={score}")
        rows.append({
            "user_id": USER_ID,
            "local_date": ds,
            "metric": "sleep_score",
            "value": score,
            "source": "garmin",
            "raw": json.dumps({
                "score": score,
                "sleepTimeSeconds": dto.get("sleepTimeSeconds", 0),
                "deepSleepSeconds": dto.get("deepSleepSeconds", 0),
                "lightSleepSeconds": dto.get("lightSleepSeconds", 0),
                "remSleepSeconds": dto.get("remSleepSeconds", 0),
                "awakeSleepSeconds": dto.get("awakeSleepSeconds", 0),
            }),
        })
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
