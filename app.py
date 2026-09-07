#!/usr/bin/env python3
"""
Wholesaling Deal Finder — a simple web app.

Three things it does:
  1. "Check an address" — type in one address, get a plain-English
     yes/no on whether it's a good wholesale deal.
  2. "Import a lead list" — upload a CSV of addresses (from a county
     tax delinquent list, etc.) and it checks all of them at once.
  3. "My deals" — see every address that has ever come back a YES.

Run it with:
    python app.py
Then open http://127.0.0.1:5000 in a browser.
"""

import csv
import io
import json
import os
import smtplib
import sqlite3
import time
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
import requests

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "leads.db"
COUNTIES_PATH = BASE_DIR / "counties.json"

RENTCAST_VALUE_URL = "https://api.rentcast.io/v1/avm/value"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-to-something-random")


# ---------- Config & DB helpers ----------

def load_config():
    """
    Settings come from environment variables first (used on Render/other
    hosts, where you set them in the dashboard instead of a file). If
    RENTCAST_API_KEY isn't set as an env var, falls back to config.json
    for local development.
    """
    env_key = os.environ.get("RENTCAST_API_KEY")
    if env_key:
        return {
            "rentcast_api_key": env_key,
            "mao_percent": float(os.environ.get("MAO_PERCENT", 0.70)),
            "wholesale_fee": float(os.environ.get("WHOLESALE_FEE", 10000)),
            "repair_rates_per_sqft": {
                "light": float(os.environ.get("REPAIR_RATE_LIGHT", 10)),
                "medium": float(os.environ.get("REPAIR_RATE_MEDIUM", 20)),
                "heavy": float(os.environ.get("REPAIR_RATE_HEAVY", 35)),
            },
            "request_delay_seconds": float(os.environ.get("REQUEST_DELAY_SECONDS", 1.5)),
            "rentcast_monthly_limit": int(os.environ.get("RENTCAST_MONTHLY_LIMIT", 50)),
            "alerts_enabled": os.environ.get("ALERTS_ENABLED", "false").lower() == "true",
            "smtp_host": os.environ.get("SMTP_HOST", ""),
            "smtp_port": int(os.environ.get("SMTP_PORT", 587)),
            "smtp_user": os.environ.get("SMTP_USER", ""),
            "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
            "alert_from_email": os.environ.get("ALERT_FROM_EMAIL", ""),
            "alert_to_email": os.environ.get("ALERT_TO_EMAIL", ""),
        }

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            "No RENTCAST_API_KEY environment variable set, and config.json "
            "not found either. For local use: copy config.example.json to "
            "config.json and add your free RentCast API key. For a cloud "
            "deploy: set RENTCAST_API_KEY as an environment variable instead."
        )
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_counties():
    if not COUNTIES_PATH.exists():
        return []
    with open(COUNTIES_PATH) as f:
        return json.load(f)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL,
            county TEXT,
            asking_price REAL,
            condition TEXT,
            arv REAL,
            sqft REAL,
            sqft_source TEXT,
            repairs REAL,
            mao REAL,
            potential_profit REAL,
            is_deal INTEGER,
            status TEXT DEFAULT 'new',
            motivation TEXT,
            occupancy TEXT,
            out_of_state_owner INTEGER,
            mortgage_status TEXT,
            known_issues TEXT,
            quality_score INTEGER,
            quality_max INTEGER,
            checked_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS api_usage (
            month TEXT PRIMARY KEY,
            count INTEGER NOT NULL DEFAULT 0
        )
    """)
    # Migration for databases created before quality signals were added.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
    new_cols = {
        "motivation": "TEXT", "occupancy": "TEXT", "out_of_state_owner": "INTEGER",
        "mortgage_status": "TEXT", "known_issues": "TEXT",
        "quality_score": "INTEGER", "quality_max": "INTEGER",
        "potential_profit": "REAL", "sqft_source": "TEXT",
        "status": "TEXT DEFAULT 'new'",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
    conn.execute("UPDATE leads SET status = 'new' WHERE status IS NULL")
    conn.commit()
    conn.close()


VALID_STATUSES = ["new", "contacted", "offer_made", "under_contract", "closed", "dead"]
STATUS_LABELS = {
    "new": "New",
    "contacted": "Contacted seller",
    "offer_made": "Offer made",
    "under_contract": "Under contract",
    "closed": "Closed",
    "dead": "Dead",
}


def current_month_key():
    return datetime.now(timezone.utc).strftime("%Y-%m")


def increment_api_usage():
    conn = get_db()
    month = current_month_key()
    conn.execute("""
        INSERT INTO api_usage (month, count) VALUES (?, 1)
        ON CONFLICT(month) DO UPDATE SET count = count + 1
    """, (month,))
    conn.commit()
    conn.close()


def get_api_usage_count():
    conn = get_db()
    row = conn.execute("SELECT count FROM api_usage WHERE month = ?", (current_month_key(),)).fetchone()
    conn.close()
    return row["count"] if row else 0


DISTRESSED_MOTIVATIONS = {"probate", "tax_delinquent", "divorce", "inherited", "tired_landlord", "foreclosure"}

MOTIVATION_LABELS = {
    "probate": "Probate (owner passed away)",
    "tax_delinquent": "Behind on property taxes",
    "divorce": "Divorce",
    "inherited": "Inherited, nobody wants it",
    "tired_landlord": "Tired landlord",
    "foreclosure": "Pre-foreclosure",
    "relocating": "Relocating",
    "unknown": "Not sure / other",
}

ISSUE_LABELS = {
    "foundation": "Foundation issue",
    "roof": "Roof issue",
    "structural": "Structural issue",
    "mold": "Mold",
}


def score_lead_quality(motivation, occupancy, out_of_state_owner, mortgage_status, known_issues):
    """
    A simple, transparent checklist — not a black-box score. Each 'green
    flag' present adds a point. Serious known issues are surfaced as a
    separate caution, since they can blow past a generic repair estimate.
    """
    signals = []

    is_distressed = motivation in DISTRESSED_MOTIVATIONS
    signals.append({
        "label": "Seller likely motivated",
        "detail": MOTIVATION_LABELS.get(motivation, "Not sure / other"),
        "positive": is_distressed,
    })

    is_vacant = occupancy == "vacant"
    signals.append({
        "label": "Property is vacant",
        "detail": {"vacant": "Vacant", "owner": "Owner lives there",
                    "tenant": "Tenant lives there", "unknown": "Not sure"}.get(occupancy, "Not sure"),
        "positive": is_vacant,
    })

    signals.append({
        "label": "Owner lives out of state/area",
        "detail": "Yes" if out_of_state_owner else "No / not sure",
        "positive": bool(out_of_state_owner),
    })

    is_free_clear = mortgage_status == "free_and_clear"
    signals.append({
        "label": "Free and clear (no mortgage)",
        "detail": {"free_and_clear": "Free and clear", "has_mortgage": "Has a mortgage",
                    "unknown": "Not sure"}.get(mortgage_status, "Not sure"),
        "positive": is_free_clear,
    })

    score = sum(1 for s in signals if s["positive"])
    max_score = len(signals)

    issue_list = [i for i in (known_issues or []) if i in ISSUE_LABELS]
    cautions = [ISSUE_LABELS[i] for i in issue_list]

    return {
        "signals": signals,
        "score": score,
        "max_score": max_score,
        "cautions": cautions,
    }


# ---------- Core deal math (same logic as before, just reused) ----------

def get_value_estimate(address, api_key):
    headers = {"X-Api-Key": api_key}
    params = {"address": address, "compCount": 15}
    resp = requests.get(RENTCAST_VALUE_URL, headers=headers, params=params, timeout=20)
    increment_api_usage()  # count the attempt regardless of outcome — RentCast bills the call either way

    if resp.status_code != 200:
        return None, None, resp.text[:200], []
    data = resp.json()
    arv = data.get("price")
    subject = data.get("subjectProperty", {}) or {}
    sqft = subject.get("squareFootage")

    comps = []
    for c in (data.get("comparables") or [])[:5]:
        comps.append({
            "address": c.get("formattedAddress") or c.get("addressLine1") or "Unknown address",
            "price": c.get("price"),
            "sqft": c.get("squareFootage"),
            "distance": c.get("distance"),
            "correlation": c.get("correlation"),
        })

    return arv, sqft, None, comps


def estimate_repairs(sqft, condition, repair_rates):
    if not sqft:
        return None
    rate = repair_rates.get(condition, repair_rates.get("medium", 20))
    return round(sqft * rate)


def calculate_mao(arv, repairs, mao_percent, wholesale_fee):
    if arv is None or repairs is None:
        return None
    return round((arv * mao_percent) - repairs - wholesale_fee)


def calculate_profit(arv, repairs, mao_percent, asking_price):
    """
    What you'd actually net if you got the property under contract at
    asking_price and assigned it to an end buyer at the ceiling price
    (ARV x mao_percent - repairs) they'd be willing to pay. This already
    includes your wholesale fee — it's not on top of it.
    """
    if arv is None or repairs is None or asking_price is None:
        return None
    return round((arv * mao_percent) - repairs - asking_price)


def analyze_address(address, asking_price, condition, county, cfg,
                     motivation="unknown", occupancy="unknown",
                     out_of_state_owner=False, mortgage_status="unknown",
                     known_issues=None, manual_sqft=None):
    """Returns a dict with all the numbers, ready to save + display."""
    api_key = cfg["rentcast_api_key"]
    mao_percent = cfg.get("mao_percent", 0.70)
    wholesale_fee = cfg.get("wholesale_fee", 10000)
    repair_rates = cfg.get("repair_rates_per_sqft", {"light": 10, "medium": 20, "heavy": 35})

    arv, sqft, error, comps = get_value_estimate(address, api_key)

    sqft_source = "auto" if sqft else None
    if manual_sqft:
        sqft = manual_sqft
        sqft_source = "manual"

    repairs = estimate_repairs(sqft, condition, repair_rates) if arv else None
    mao = calculate_mao(arv, repairs, mao_percent, wholesale_fee) if arv else None
    potential_profit = calculate_profit(arv, repairs, mao_percent, asking_price) if arv else None

    is_deal = False
    if mao is not None and asking_price is not None:
        is_deal = asking_price <= mao

    quality = score_lead_quality(motivation, occupancy, out_of_state_owner, mortgage_status, known_issues)

    return {
        "address": address,
        "county": county,
        "asking_price": asking_price,
        "condition": condition,
        "arv": arv,
        "sqft": sqft,
        "sqft_source": sqft_source,
        "repairs": repairs,
        "mao": mao,
        "potential_profit": potential_profit,
        "is_deal": is_deal,
        "error": error,
        "comps": comps,
        "motivation": motivation,
        "occupancy": occupancy,
        "out_of_state_owner": out_of_state_owner,
        "mortgage_status": mortgage_status,
        "known_issues": known_issues or [],
        "quality": quality,
    }


def save_lead(result):
    conn = get_db()
    conn.execute("""
        INSERT INTO leads (address, county, asking_price, condition, arv, sqft, sqft_source, repairs, mao, potential_profit, is_deal,
                            motivation, occupancy, out_of_state_owner, mortgage_status, known_issues,
                            quality_score, quality_max)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result["address"], result.get("county"), result.get("asking_price"),
        result.get("condition"), result.get("arv"), result.get("sqft"), result.get("sqft_source"),
        result.get("repairs"), result.get("mao"), result.get("potential_profit"),
        int(result.get("is_deal", False)),
        result.get("motivation"), result.get("occupancy"),
        int(bool(result.get("out_of_state_owner"))), result.get("mortgage_status"),
        ",".join(result.get("known_issues") or []),
        result.get("quality", {}).get("score"), result.get("quality", {}).get("max_score"),
    ))
    conn.commit()
    conn.close()


def send_alert(subject, body, cfg):
    """
    Sends an email — which can just as easily be a carrier's email-to-SMS
    gateway address (e.g. 5551234567@vtext.com) set as ALERT_TO_EMAIL, so
    this doubles as a free 'text alert' with no SMS provider needed.
    Silently no-ops if alerts aren't configured; never raises, so a bad
    alert config can't break the actual deal-checking flow.
    """
    if not cfg.get("alerts_enabled"):
        return
    required = ["smtp_host", "smtp_user", "smtp_password", "alert_from_email", "alert_to_email"]
    if not all(cfg.get(k) for k in required):
        print("Alerts enabled but missing SMTP/alert settings — skipping send.")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = cfg["alert_from_email"]
        msg["To"] = cfg["alert_to_email"]
        with smtplib.SMTP(cfg["smtp_host"], cfg.get("smtp_port", 587), timeout=10) as server:
            server.starttls()
            server.login(cfg["smtp_user"], cfg["smtp_password"])
            server.send_message(msg)
    except Exception as e:
        print(f"Alert send failed: {e}")


def update_lead_status(lead_id, status):
    if status not in VALID_STATUSES:
        return
    conn = get_db()
    conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    conn.commit()
    conn.close()


# ---------- Routes ----------

@app.route("/")
def home():
    conn = get_db()
    deal_count = conn.execute("SELECT COUNT(*) c FROM leads WHERE is_deal = 1").fetchone()["c"]
    checked_count = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    conn.close()
    api_used = get_api_usage_count()
    try:
        api_limit = load_config().get("rentcast_monthly_limit", 50)
    except FileNotFoundError:
        api_limit = 50
    return render_template(
        "index.html", deal_count=deal_count, checked_count=checked_count,
        api_used=api_used, api_limit=api_limit,
    )


@app.route("/check", methods=["GET", "POST"])
def check():
    if request.method == "GET":
        counties = load_counties()
        return render_template("check.html", result=None, counties=counties)

    cfg = load_config()
    address = request.form["address"].strip()
    asking_price = request.form.get("asking_price")
    asking_price = float(asking_price) if asking_price else None
    condition = request.form.get("condition", "medium")
    county = request.form.get("county")
    motivation = request.form.get("motivation", "unknown")
    occupancy = request.form.get("occupancy", "unknown")
    out_of_state_owner = request.form.get("out_of_state_owner") == "yes"
    mortgage_status = request.form.get("mortgage_status", "unknown")
    known_issues = request.form.getlist("known_issues")
    manual_sqft = request.form.get("square_footage")
    manual_sqft = float(manual_sqft) if manual_sqft else None

    result = analyze_address(
        address, asking_price, condition, county, cfg,
        motivation=motivation, occupancy=occupancy,
        out_of_state_owner=out_of_state_owner, mortgage_status=mortgage_status,
        known_issues=known_issues, manual_sqft=manual_sqft,
    )
    save_lead(result)

    if result.get("is_deal"):
        send_alert(
            f"Deal found: {result['address']}",
            f"{result['address']}\n"
            f"Asking: ${result['asking_price']:,.0f}  MAO: ${result['mao']:,.0f}  "
            f"Profit: ${result['potential_profit']:,.0f}",
            cfg,
        )

    counties = load_counties()
    return render_template("check.html", result=result, counties=counties)


@app.route("/import", methods=["GET", "POST"])
def import_leads():
    counties = load_counties()

    if request.method == "GET":
        return render_template("import.html", results=None, counties=counties)

    cfg = load_config()
    file = request.files.get("csv_file")
    if not file:
        flash("Please choose a CSV file first.")
        return redirect(url_for("import_leads"))

    stream = io.StringIO(file.stream.read().decode("utf-8"))
    reader = csv.DictReader(stream)

    request_delay = cfg.get("request_delay_seconds", 1.5)
    results = []
    for row in reader:
        address = (row.get("address") or "").strip()
        if not address:
            continue
        asking_price = row.get("asking_price")
        asking_price = float(asking_price) if asking_price else None
        condition = (row.get("condition") or "medium").strip().lower()
        county = (row.get("county") or "").strip()
        motivation = (row.get("motivation") or "unknown").strip().lower()
        occupancy = (row.get("occupancy") or "unknown").strip().lower()
        out_of_state_owner = (row.get("out_of_state_owner") or "").strip().lower() in ("yes", "true", "1")
        mortgage_status = (row.get("mortgage_status") or "unknown").strip().lower()
        known_issues_raw = (row.get("known_issues") or "").strip()
        known_issues = [i.strip().lower() for i in known_issues_raw.split(";") if i.strip()]
        manual_sqft = row.get("square_footage")
        manual_sqft = float(manual_sqft) if manual_sqft else None

        result = analyze_address(
            address, asking_price, condition, county, cfg,
            motivation=motivation, occupancy=occupancy,
            out_of_state_owner=out_of_state_owner, mortgage_status=mortgage_status,
            known_issues=known_issues, manual_sqft=manual_sqft,
        )
        save_lead(result)
        results.append(result)
        time.sleep(request_delay)

    new_deals = [r for r in results if r.get("is_deal")]
    if new_deals:
        lines = [
            f"{r['address']} — Asking ${r['asking_price']:,.0f}, Profit ${r['potential_profit']:,.0f}"
            for r in new_deals[:5]
        ]
        more = f"\n...and {len(new_deals) - 5} more" if len(new_deals) > 5 else ""
        send_alert(
            f"{len(new_deals)} deal(s) found in your import",
            "\n".join(lines) + more,
            cfg,
        )

    return render_template("import.html", results=results, counties=counties)


@app.route("/deals")
def deals():
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM leads WHERE is_deal = 1 ORDER BY potential_profit DESC
    """).fetchall()
    conn.close()
    return render_template("deals.html", deals=rows, statuses=VALID_STATUSES, status_labels=STATUS_LABELS)


@app.route("/leads")
def all_leads():
    conn = get_db()
    rows = conn.execute("SELECT * FROM leads ORDER BY checked_at DESC LIMIT 200").fetchall()
    conn.close()
    return render_template("leads.html", leads=rows, statuses=VALID_STATUSES, status_labels=STATUS_LABELS)


@app.route("/leads/<int:lead_id>/status", methods=["POST"])
def update_status(lead_id):
    status = request.form.get("status", "")
    update_lead_status(lead_id, status)
    return redirect(request.form.get("redirect_to") or url_for("deals"))


@app.route("/sources")
def sources():
    counties = load_counties()
    return render_template("sources.html", counties=counties)


@app.route("/api/address-suggest")
def address_suggest():
    """
    Free address autocomplete via OpenStreetMap's Nominatim search — no API
    key needed. Nominatim's usage policy asks for a real User-Agent and
    reasonable request volume, which the frontend's debounce (see
    check.html) keeps well within.
    """
    query = request.args.get("q", "").strip()
    if len(query) < 5:
        return jsonify([])

    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "format": "json",
                "addressdetails": 1,
                "limit": 5,
                "countrycodes": "us",
                "q": query,
            },
            headers={"User-Agent": "DealFinderApp/1.0 (personal wholesaling tool)"},
            timeout=5,
        )
        if resp.status_code != 200:
            return jsonify([])
        results = resp.json()
        suggestions = [r.get("display_name") for r in results if r.get("display_name")]
        return jsonify(suggestions)
    except requests.RequestException:
        return jsonify([])


init_db()  # runs on import too, so it's ready under gunicorn on a host like Render

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
