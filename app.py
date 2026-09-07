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
import sqlite3
import time
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash
import requests

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"
DB_PATH = BASE_DIR / "leads.db"
COUNTIES_PATH = BASE_DIR / "counties.json"

RENTCAST_VALUE_URL = "https://api.rentcast.io/v1/avm/value"

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
            repairs REAL,
            mao REAL,
            is_deal INTEGER,
            checked_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


# ---------- Core deal math (same logic as before, just reused) ----------

def get_value_estimate(address, api_key):
    headers = {"X-Api-Key": api_key}
    params = {"address": address, "compCount": 15}
    resp = requests.get(RENTCAST_VALUE_URL, headers=headers, params=params, timeout=20)
    if resp.status_code != 200:
        return None, None, resp.text[:200]
    data = resp.json()
    arv = data.get("price")
    subject = data.get("subjectProperty", {}) or {}
    sqft = subject.get("squareFootage")
    return arv, sqft, None


def estimate_repairs(sqft, condition, repair_rates):
    if not sqft:
        return None
    rate = repair_rates.get(condition, repair_rates.get("medium", 20))
    return round(sqft * rate)


def calculate_mao(arv, repairs, mao_percent, wholesale_fee):
    if arv is None or repairs is None:
        return None
    return round((arv * mao_percent) - repairs - wholesale_fee)


def analyze_address(address, asking_price, condition, county, cfg):
    """Returns a dict with all the numbers, ready to save + display."""
    api_key = cfg["rentcast_api_key"]
    mao_percent = cfg.get("mao_percent", 0.70)
    wholesale_fee = cfg.get("wholesale_fee", 10000)
    repair_rates = cfg.get("repair_rates_per_sqft", {"light": 10, "medium": 20, "heavy": 35})

    arv, sqft, error = get_value_estimate(address, api_key)
    repairs = estimate_repairs(sqft, condition, repair_rates) if arv else None
    mao = calculate_mao(arv, repairs, mao_percent, wholesale_fee) if arv else None

    is_deal = False
    if mao is not None and asking_price is not None:
        is_deal = asking_price <= mao

    return {
        "address": address,
        "county": county,
        "asking_price": asking_price,
        "condition": condition,
        "arv": arv,
        "sqft": sqft,
        "repairs": repairs,
        "mao": mao,
        "is_deal": is_deal,
        "error": error,
    }


def save_lead(result):
    conn = get_db()
    conn.execute("""
        INSERT INTO leads (address, county, asking_price, condition, arv, sqft, repairs, mao, is_deal)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result["address"], result.get("county"), result.get("asking_price"),
        result.get("condition"), result.get("arv"), result.get("sqft"),
        result.get("repairs"), result.get("mao"), int(result.get("is_deal", False)),
    ))
    conn.commit()
    conn.close()


# ---------- Routes ----------

@app.route("/")
def home():
    conn = get_db()
    deal_count = conn.execute("SELECT COUNT(*) c FROM leads WHERE is_deal = 1").fetchone()["c"]
    checked_count = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
    conn.close()
    return render_template("index.html", deal_count=deal_count, checked_count=checked_count)


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

    result = analyze_address(address, asking_price, condition, county, cfg)
    save_lead(result)

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

        result = analyze_address(address, asking_price, condition, county, cfg)
        save_lead(result)
        results.append(result)
        time.sleep(request_delay)

    return render_template("import.html", results=results, counties=counties)


@app.route("/deals")
def deals():
    conn = get_db()
    rows = conn.execute("""
        SELECT * FROM leads WHERE is_deal = 1 ORDER BY (mao - asking_price) DESC
    """).fetchall()
    conn.close()
    return render_template("deals.html", deals=rows)


@app.route("/leads")
def all_leads():
    conn = get_db()
    rows = conn.execute("SELECT * FROM leads ORDER BY checked_at DESC LIMIT 200").fetchall()
    conn.close()
    return render_template("leads.html", leads=rows)


@app.route("/sources")
def sources():
    counties = load_counties()
    return render_template("sources.html", counties=counties)


init_db()  # runs on import too, so it's ready under gunicorn on a host like Render

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
