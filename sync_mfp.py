"""
MyFitnessPal (public diary) → Supabase: Daily nutrition sync.
Scrapes the PUBLIC diary page — no login needed.
Designed to run as a GitHub Action (cron daily at 10pm).

Tracked metrics:
  calories      — total food calories
  exercise_cal  — exercise calories burned
  net_calories  — food minus exercise (goal: < 1880)
  protein_g     — grams (goal: >= 1.5g × bodyweight kg)
  carbs_g, fat_g, fiber_g, sugar_g, sodium_mg
  alcohol_g     — grams of alcohol (parsed from entries)
  had_alcohol   — 1 if alcohol detected, 0 otherwise

Required env vars (set as GitHub Secrets):
  MFP_USERNAME         (the diary owner, e.g. "al778416")
  SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_USER_ID
"""
import os, sys, re, json
from datetime import date, timedelta
import requests
from bs4 import BeautifulSoup

# ── config ─────────────────────────────────────────────────────────────
MFP_EMAIL    = os.environ.get("MFP_EMAIL", "")       # login email
MFP_PASSWORD = os.environ.get("MFP_PASSWORD", "")
MFP_USER     = os.environ.get("MFP_USERNAME", "al778416")  # diary URL username
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SRK = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
USER_ID      = os.environ["SUPABASE_USER_ID"]
DAYS         = int(os.environ.get("SYNC_DAYS", "7"))
BASE_URL     = "https://www.myfitnesspal.com/food/diary"

# Alcohol keywords (Spanish + English)
ALCOHOL_KEYWORDS = [
    "cerveza", "beer", "vino", "wine", "whisky", "whiskey", "vodka",
    "gin", "ron", "rum", "tequila", "mezcal", "fernet", "aperol",
    "spritz", "cocktail", "coctel", "alcohol", "lager", "ale", "ipa",
    "stout", "malbec", "cabernet", "merlot", "champagne", "prosecco",
    "sidra", "cider", "margarita", "mojito", "sangria", "pisco",
]

def parse_number(text):
    """Extract a number from text like '2,100' or '1.5' or '--'."""
    if not text:
        return 0
    cleaned = text.strip().replace(",", "").replace(".", "")
    # Handle '--' or empty
    match = re.search(r"[\d,.]+", text.strip().replace(",", ""))
    if match:
        try:
            return int(float(match.group()))
        except ValueError:
            return 0
    return 0


SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
})

# Log into MFP using NextAuth API (modern MFP auth flow)
print("Logging into MFP...")
logged_in = False
try:
    # Method 1: NextAuth CSRF + credentials callback
    csrf_resp = SESSION.get("https://www.myfitnesspal.com/api/auth/csrf", timeout=15)
    if csrf_resp.status_code == 200:
        csrf_data = csrf_resp.json()
        csrf_token = csrf_data.get("csrfToken", "")
        print(f"  NextAuth CSRF: {'found' if csrf_token else 'not found'}")

        if csrf_token and MFP_EMAIL and MFP_PASSWORD:
            login_resp = SESSION.post(
                "https://www.myfitnesspal.com/api/auth/callback/credentials",
                data={
                    "csrfToken": csrf_token,
                    "email": MFP_EMAIL,
                    "password": MFP_PASSWORD,
                    "redirect": "false",
                    "json": "true",
                },
                timeout=15,
                allow_redirects=True,
            )
            print(f"  Login POST: {login_resp.status_code}, cookies: {len(SESSION.cookies)}")
            if login_resp.status_code == 200:
                logged_in = True
                print("  NextAuth login OK")
            else:
                print(f"  Login response: {login_resp.text[:200]}")

    # Method 2: Traditional form login as fallback
    if not logged_in and MFP_EMAIL and MFP_PASSWORD:
        print("  Trying traditional login...")
        login_page = SESSION.get("https://www.myfitnesspal.com/account/login", timeout=15)
        from bs4 import BeautifulSoup as BS
        soup_login = BS(login_page.text, "html.parser")
        csrf_input = soup_login.find("input", {"name": "authenticity_token"})
        csrf = csrf_input.get("value", "") if csrf_input else ""
        if not csrf:
            meta = soup_login.find("meta", {"name": "csrf-token"})
            csrf = meta.get("content", "") if meta else ""

        login_resp = SESSION.post(
            "https://www.myfitnesspal.com/account/login",
            data={
                "authenticity_token": csrf,
                "user[email]": MFP_EMAIL,
                "user[password]": MFP_PASSWORD,
                "commit": "Log In",
            },
            timeout=15,
            allow_redirects=True,
        )
        if "login" not in login_resp.url.lower():
            logged_in = True
            print(f"  Traditional login OK → {login_resp.url[:50]}")
        else:
            print("  Traditional login also failed")

except Exception as e:
    print(f"  Login error: {e}")

if not logged_in:
    print("  WARNING: Not logged in. Diary access may fail.")
print(f"  Total cookies: {len(SESSION.cookies)}")


def fetch_diary(username, d):
    """Fetch and parse one day's diary page."""
    ds = d.isoformat()
    # Try both English and Spanish URLs
    urls_to_try = [
        f"https://www.myfitnesspal.com/food/diary/{username}?date={ds}",
        f"https://www.myfitnesspal.com/es/food/diary/{username}?date={ds}",
    ]

    resp = None
    for url in urls_to_try:
        try:
            resp = SESSION.get(url, timeout=15)
            if resp.status_code == 200:
                break
            print(f"  {ds}: {resp.status_code} at {url.split('.com')[1][:40]}")
        except Exception as e:
            print(f"  {ds}: error {e}")
            continue

    if not resp or resp.status_code != 200:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Parse totals from the bottom row ───────────────────────────────
    # MFP uses a table with class "total" for the daily totals row
    result = {
        "date": ds,
        "calories": 0,
        "fat_g": 0,
        "carbs_g": 0,
        "protein_g": 0,
        "sodium_mg": 0,
        "sugar_g": 0,
        "fiber_g": 0,
        "exercise_cal": 0,
        "net_calories": 0,
        "alcohol_g": 0,
        "had_alcohol": 0,
    }

    # Try to find the totals row
    total_row = soup.find("tr", class_="total") or soup.find("tfoot")
    if total_row:
        cells = total_row.find_all("td")
        # MFP column order (standard view):
        # [Label, Calories, Fat, Cholest, Sodium, Carbs, Fiber, Sugar, Protein]
        # But it varies — let's find by header mapping
        vals = [parse_number(c.get_text()) for c in cells]
        if len(vals) >= 7:
            # Standard: label, cal, fat, cholest?, sodium?, carbs, fiber?, sugar?, protein
            result["calories"] = vals[1] if len(vals) > 1 else 0
            result["fat_g"] = vals[2] if len(vals) > 2 else 0
            result["carbs_g"] = vals[5] if len(vals) > 5 else 0
            result["protein_g"] = vals[-1] if vals else 0  # protein is usually last

    # Try alternate: MFP sometimes puts totals in a specific div
    if result["calories"] == 0:
        # Look for nutrient-column data attributes
        for span in soup.find_all("span", {"class": "nutrient-column"}):
            label = span.get("data-nutrient", "")
            val = parse_number(span.get_text())
            if "calorie" in label.lower():
                result["calories"] = val
            elif "fat" in label.lower():
                result["fat_g"] = val
            elif "carb" in label.lower():
                result["carbs_g"] = val
            elif "protein" in label.lower():
                result["protein_g"] = val

    # Try yet another pattern: look for the "Totals" text and parse siblings
    if result["calories"] == 0:
        for el in soup.find_all(string=re.compile(r"Total|Totales", re.I)):
            row = el.find_parent("tr")
            if row:
                cells = row.find_all("td")
                vals = [parse_number(c.get_text()) for c in cells]
                if len(vals) >= 5 and vals[1] > 0:
                    result["calories"] = vals[1]
                    result["fat_g"] = vals[2] if len(vals) > 2 else 0
                    # Protein is typically the last numeric column
                    result["protein_g"] = vals[-1] if len(vals) > 3 else 0
                    if len(vals) > 5:
                        result["carbs_g"] = vals[5]
                    break

    # ── Exercise calories ──────────────────────────────────────────────
    exercise_row = None
    for el in soup.find_all(string=re.compile(r"Exercise|Ejercicio|Cardiovascular", re.I)):
        row = el.find_parent("tr")
        if row:
            exercise_row = row
            break
    if exercise_row:
        cells = exercise_row.find_all("td")
        vals = [parse_number(c.get_text()) for c in cells]
        if vals:
            # Exercise calories is usually in the second column
            result["exercise_cal"] = vals[1] if len(vals) > 1 else vals[0]

    # Net calories
    result["net_calories"] = result["calories"] - result["exercise_cal"]

    # ── Alcohol detection from food entries ────────────────────────────
    food_entries = []
    for row in soup.find_all("tr", class_="bottom"):
        name_cell = row.find("td", class_="first")
        if name_cell:
            food_entries.append(name_cell.get_text().strip().lower())
    # Also check all links in the diary
    for link in soup.find_all("a", class_="js-show-edit-food"):
        food_entries.append(link.get_text().strip().lower())
    # Broader: any table cell that looks like a food name
    if not food_entries:
        for row in soup.find_all("tr"):
            first_td = row.find("td")
            if first_td and first_td.get_text().strip():
                food_entries.append(first_td.get_text().strip().lower())

    for entry in food_entries:
        for kw in ALCOHOL_KEYWORDS:
            if kw in entry:
                result["had_alcohol"] = 1
                break
        if result["had_alcohol"]:
            break

    if result["calories"] == 0:
        print(f"  {ds}: no calories found (page may be empty or format changed)")
        # Save HTML snippet for debugging
        print(f"  page length: {len(resp.text)}, title: {soup.title.string if soup.title else 'none'}")
        return None

    return result


# ── Main ───────────────────────────────────────────────────────────────
print(f"Fetching MFP diary for user: {MFP_USER}")
rows = []
today = date.today()

for i in range(DAYS):
    d = today - timedelta(days=i)
    day = fetch_diary(MFP_USER, d)
    if not day:
        continue

    ds = day["date"]
    net = day["net_calories"]
    status = "OK" if net <= 1880 else "OVER"
    print(f"  {ds}: {day['calories']} cal - {day['exercise_cal']} exercise = {net} net [{status}] | "
          f"P {day['protein_g']}g | C {day['carbs_g']}g | F {day['fat_g']}g | "
          f"alcohol: {'YES' if day['had_alcohol'] else 'no'}")

    metrics = {
        "calories": day["calories"],
        "exercise_cal": day["exercise_cal"],
        "net_calories": day["net_calories"],
        "protein_g": day["protein_g"],
        "carbs_g": day["carbs_g"],
        "fat_g": day["fat_g"],
    }
    if day.get("fiber_g"): metrics["fiber_g"] = day["fiber_g"]
    if day.get("sugar_g"): metrics["sugar_g"] = day["sugar_g"]
    if day.get("sodium_mg"): metrics["sodium_mg"] = day["sodium_mg"]
    metrics["had_alcohol"] = day["had_alcohol"]

    for metric, value in metrics.items():
        rows.append({
            "user_id": USER_ID,
            "local_date": ds,
            "metric": metric,
            "value": value,
            "source": "mfp",
        })

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
