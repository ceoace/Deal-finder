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
    # Migration for databases created before quality signals were added.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
    new_cols = {
        "motivation": "TEXT", "occupancy": "TEXT", "out_of_state_owner": "INTEGER",
        "mortgage_status": "TEXT", "known_issues": "TEXT",
        "quality_score": "INTEGER", "quality_max": "INTEGER",
    }
    for col, col_type in new_cols.items():
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {col_type}")
    conn.commit()
    conn.close()


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
