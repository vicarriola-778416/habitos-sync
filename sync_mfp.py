"""
MyFitnessPal → Supabase: Daily nutrition sync.
Uses the `myfitnesspal` library for MFP scraping.
Designed to run as a GitHub Action (cron daily at 10pm).

Required env vars (set as GitHub Secrets):
  MFP_USERNAME, MFP_PASSWORD
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
  SUPABASE_USER_ID
"""
import os, json, sys
from datetime import date, timedelta
import myfitnesspal
import requests

# ── config ─────────────────────────────────────────────────────────────
MFP_USERNAME = os.environ["MFP_USERNAME"]
MFP_PASSWORD = os.environ["MFP_PASSWORD"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SRK = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
USER_ID      = os.environ["SUPABASE_USER_ID"]
DAYS         = int(os.environ.get("SYNC_DAYS", "7"))

# ── MFP auth ───────────────────────────────────────────────────────────
print("Logging into MyFitnessPal...")
try:
    client = myfitnesspal.Client(MFP_USERNAME, MFP_PASSWORD)
    print("MFP authenticated.")
except Exception as e:
    print(f"MFP login failed: {e}")
    sys.exit(1)

# ── Fetch nutrition ────────────────────────────────────────────────────
rows = []
today = date.today()
for i in range(DAYS):
    d = today - timedelta(days=i)
    ds = d.isoformat()
    try:
        day = client.get_date(d)
        totals = day.totals
        if not totals or totals.get("calories", 0) == 0:
            print(f"  {ds}: no meals logged")
            continue

        cal = round(totals.get("calories", 0))
        pro = round(totals.get("protein", 0))
        carb = round(totals.get("carbohydrates", 0))
        fat = round(totals.get("fat", 0))
        fib = round(totals.get("fiber", 0)) if "fiber" in totals else None
        sug = round(totals.get("sugar", 0)) if "sugar" in totals else None

        print(f"  {ds}: {cal} kcal | P {pro}g | C {carb}g | F {fat}g")

        rows.append({"user_id": USER_ID, "local_date": ds, "metric": "calories",  "value": cal,  "source": "mfp"})
        rows.append({"user_id": USER_ID, "local_date": ds, "metric": "protein_g", "value": pro,  "source": "mfp"})
        rows.append({"user_id": USER_ID, "local_date": ds, "metric": "carbs_g",   "value": carb, "source": "mfp"})
        rows.append({"user_id": USER_ID, "local_date": ds, "metric": "fat_g",     "value": fat,  "source": "mfp"})
        if fib is not None:
            rows.append({"user_id": USER_ID, "local_date": ds, "metric": "fiber_g", "value": fib, "source": "mfp"})
        if sug is not None:
            rows.append({"user_id": USER_ID, "local_date": ds, "metric": "sugar_g", "value": sug, "source": "mfp"})

    except Exception as e:
        print(f"  {ds}: error — {e}")

# ── Upsert to Supabase ────────────────────────────────────────────────
if not rows:
    print("No nutrition data found. Done.")
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
    print(f"OK — {len(rows)} nutrition rows synced.")
else:
    print(f"FAILED — {resp.status_code}: {resp.text}")
    sys.exit(1)
