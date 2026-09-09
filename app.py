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
import re
import sqlite3
import time
from datetime import datetime, timezone
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
            "resend_api_key": os.environ.get("RESEND_API_KEY", ""),
            "alert_from_email": os.environ.get("ALERT_FROM_EMAIL", "onboarding@resend.dev"),
            "alert_to_email": os.environ.get("ALERT_TO_EMAIL", ""),
            "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
            "anthropic_model": os.environ.get("ANTHROPIC_MODEL", ""),
            "sms_bot_name": os.environ.get("SMS_BOT_NAME", "Alex"),
            "sms_bot_max_turns": int(os.environ.get("SMS_BOT_MAX_TURNS", 20)),
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
            seller_name TEXT,
            seller_phone TEXT,
            seller_email TEXT,
            motivation TEXT,
            occupancy TEXT,
            out_of_state_owner INTEGER,
            mortgage_status TEXT,
            known_issues TEXT,
            notes TEXT,
            source TEXT DEFAULT 'manual',
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS buyers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            email TEXT,
            min_price REAL,
            max_price REAL,
            counties TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone_number TEXT UNIQUE NOT NULL,
            transcript TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            lead_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration for databases created before quality signals were added.
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
    new_cols = {
        "motivation": "TEXT", "occupancy": "TEXT", "out_of_state_owner": "INTEGER",
        "mortgage_status": "TEXT", "known_issues": "TEXT",
        "quality_score": "INTEGER", "quality_max": "INTEGER",
        "potential_profit": "REAL", "sqft_source": "TEXT",
        "status": "TEXT DEFAULT 'new'", "notes": "TEXT",
        "seller_name": "TEXT", "seller_phone": "TEXT", "seller_email": "TEXT",
        "source": "TEXT DEFAULT 'manual'",
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


RENTCAST_LISTINGS_URL = "https://api.rentcast.io/v1/listings/sale"

DISTRESS_KEYWORDS = [
    "as-is", "as is", "fixer", "fixer-upper", "handyman", "tlc", "cash only",
    "investor special", "needs work", "distressed", "estate sale", "probate",
    "must sell", "motivated seller", "rehab", "no disclosures", "sold as-is",
    "great investment", "bring your contractor", "gut rehab",
]


def get_sale_listings(city, state, zip_code, limit, api_key):
    """
    Pulls active for-sale listings from RentCast's own listings database —
    the same API key you already have, a different endpoint. Costs one
    API call total regardless of how many listings come back (unlike the
    per-address ARV lookup, which costs one call each).

    RentCast's exact response shape isn't something we've verified against
    a live key yet, so this parses defensively: it accepts either a bare
    list or a dict wrapping the list under a couple of likely key names,
    and never assumes a description field exists (some plans don't return
    listing remarks) — if it's missing, distress-keyword tagging just
    comes back empty rather than breaking.
    """
    headers = {"X-Api-Key": api_key}
    params = {"status": "Active", "limit": limit}
    if zip_code:
        params["zipCode"] = zip_code
    elif city and state:
        params["city"] = city
        params["state"] = state
    else:
        return [], "Enter either a city + state, or a zip code."

    resp = requests.get(RENTCAST_LISTINGS_URL, headers=headers, params=params, timeout=20)
    increment_api_usage()

    if resp.status_code != 200:
        return [], resp.text[:200]

    data = resp.json()
    if isinstance(data, list):
        raw_listings = data
    elif isinstance(data, dict):
        raw_listings = data.get("listings") or data.get("data") or []
    else:
        raw_listings = []

    listings = []
    for item in raw_listings:
        description = (
            item.get("description") or item.get("remarks")
            or item.get("publicRemarks") or ""
        )
        text_blob = f"{description} {item.get('listingType') or ''}".lower()
        matched_keywords = [k for k in DISTRESS_KEYWORDS if k in text_blob]

        listings.append({
            "address": item.get("formattedAddress") or item.get("addressLine1") or "Unknown address",
            "price": item.get("price"),
            "sqft": item.get("squareFootage"),
            "days_on_market": item.get("daysOnMarket"),
            "description": description,
            "distress_keywords": matched_keywords,
        })

    return listings, None


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
                     known_issues=None, manual_sqft=None,
                     seller_name="", seller_phone="", seller_email=""):
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
        "seller_name": seller_name,
        "seller_phone": seller_phone,
        "seller_email": seller_email,
        "motivation": motivation,
        "occupancy": occupancy,
        "out_of_state_owner": out_of_state_owner,
        "mortgage_status": mortgage_status,
        "known_issues": known_issues or [],
        "quality": quality,
    }


def save_lead(result, source="manual"):
    conn = get_db()
    cursor = conn.execute("""
        INSERT INTO leads (address, county, asking_price, condition, arv, sqft, sqft_source, repairs, mao, potential_profit, is_deal,
                            seller_name, seller_phone, seller_email,
                            motivation, occupancy, out_of_state_owner, mortgage_status, known_issues,
                            quality_score, quality_max, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result["address"], result.get("county"), result.get("asking_price"),
        result.get("condition"), result.get("arv"), result.get("sqft"), result.get("sqft_source"),
        result.get("repairs"), result.get("mao"), result.get("potential_profit"),
        int(result.get("is_deal", False)),
        result.get("seller_name"), result.get("seller_phone"), result.get("seller_email"),
        result.get("motivation"), result.get("occupancy"),
        int(bool(result.get("out_of_state_owner"))), result.get("mortgage_status"),
        ",".join(result.get("known_issues") or []),
        result.get("quality", {}).get("score"), result.get("quality", {}).get("max_score"),
        source,
    ))
    new_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return new_id


RESEND_API_URL = "https://api.resend.com/emails"


def send_alert(subject, body, cfg):
    """
    Sends via Resend's HTTPS API — deliberately NOT raw SMTP, because
    Render's free tier blocks outbound SMTP connections at the network
    level (a known platform limitation, not something fixable in this
    code). HTTPS to api.resend.com works the same way RentCast/Nominatim
    already do.

    ALERT_TO_EMAIL can be a real email address, or a carrier's
    email-to-SMS gateway address (e.g. 5551234567@vtext.com) to arrive
    as a text — though sending to anything other than your own Resend
    account email requires verifying a domain first (Resend's anti-spam
    sandbox restriction). Silently no-ops if alerts aren't configured;
    never raises, so a bad alert config can't break the deal-checking flow.
    """
    if not cfg.get("alerts_enabled"):
        return
    required = ["resend_api_key", "alert_from_email", "alert_to_email"]
    if not all(cfg.get(k) for k in required):
        print("Alerts enabled but missing Resend/alert settings — skipping send.")
        return
    try:
        resp = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {cfg['resend_api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "from": cfg["alert_from_email"],
                "to": [cfg["alert_to_email"]],
                "subject": subject,
                "text": body,
            },
            timeout=10,
        )
        if resp.status_code >= 300:
            print(f"Alert send failed: {resp.status_code} {resp.text[:300]}")
    except requests.RequestException as e:
        print(f"Alert send failed: {e}")


def update_lead_status(lead_id, status):
    if status not in VALID_STATUSES:
        return
    conn = get_db()
    conn.execute("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
    conn.commit()
    conn.close()


def get_lead(lead_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    conn.close()
    return row


def update_lead(lead_id, asking_price, condition, county, motivation, occupancy,
                 out_of_state_owner, mortgage_status, known_issues, notes, cfg,
                 seller_name="", seller_phone="", seller_email=""):
    """
    Edits an existing lead's price/condition/seller details and recomputes
    repairs/MAO/profit/is_deal/quality from the ARV and sqft already saved —
    no new RentCast lookup needed, since the address itself isn't changing.
    """
    lead = get_lead(lead_id)
    if not lead:
        return

    mao_percent = cfg.get("mao_percent", 0.70)
    wholesale_fee = cfg.get("wholesale_fee", 10000)
    repair_rates = cfg.get("repair_rates_per_sqft", {"light": 10, "medium": 20, "heavy": 35})

    arv = lead["arv"]
    sqft = lead["sqft"]
    repairs = estimate_repairs(sqft, condition, repair_rates) if arv else None
    mao = calculate_mao(arv, repairs, mao_percent, wholesale_fee) if arv else None
    potential_profit = calculate_profit(arv, repairs, mao_percent, asking_price) if arv else None
    is_deal = mao is not None and asking_price is not None and asking_price <= mao
    quality = score_lead_quality(motivation, occupancy, out_of_state_owner, mortgage_status, known_issues)

    conn = get_db()
    conn.execute("""
        UPDATE leads SET
            asking_price = ?, condition = ?, county = ?, repairs = ?, mao = ?,
            potential_profit = ?, is_deal = ?, seller_name = ?, seller_phone = ?, seller_email = ?,
            motivation = ?, occupancy = ?,
            out_of_state_owner = ?, mortgage_status = ?, known_issues = ?, notes = ?,
            quality_score = ?, quality_max = ?
        WHERE id = ?
    """, (
        asking_price, condition, county, repairs, mao, potential_profit, int(is_deal),
        seller_name, seller_phone, seller_email,
        motivation, occupancy, int(bool(out_of_state_owner)), mortgage_status,
        ",".join(known_issues or []), notes,
        quality["score"], quality["max_score"], lead_id,
    ))
    conn.commit()
    conn.close()


def delete_lead(lead_id):
    conn = get_db()
    conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    conn.commit()
    conn.close()


def add_buyer(name, phone, email, min_price, max_price, counties, notes):
    conn = get_db()
    conn.execute("""
        INSERT INTO buyers (name, phone, email, min_price, max_price, counties, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (name, phone, email, min_price, max_price, counties, notes))
    conn.commit()
    conn.close()


def delete_buyer(buyer_id):
    conn = get_db()
    conn.execute("DELETE FROM buyers WHERE id = ?", (buyer_id,))
    conn.commit()
    conn.close()


def get_all_buyers():
    conn = get_db()
    rows = conn.execute("SELECT * FROM buyers ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return rows


def get_matching_buyers(asking_price, ceiling_price, county):
    """
    A buyer matches if the deal's entry price is within budget (their max
    covers at least the asking price) and their floor doesn't exceed what
    an end buyer could realistically pay (the ceiling price). County is
    only checked if the buyer specified any — an empty list means 'anywhere'.
    """
    buyers = get_all_buyers()
    matches = []
    for b in buyers:
        if b["max_price"] is not None and asking_price is not None and asking_price > b["max_price"]:
            continue
        if b["min_price"] is not None and ceiling_price is not None and ceiling_price < b["min_price"]:
            continue
        buyer_counties = [c.strip().lower() for c in (b["counties"] or "").split(",") if c.strip()]
        if buyer_counties and (not county or county.lower() not in buyer_counties):
            continue
        matches.append(b)
    return matches


# ---------- SMS seller-qualification bot ----------

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

SMS_BOT_SYSTEM_PROMPT_TEMPLATE = """You are {bot_name}, a friendly local real estate investor's assistant \
texting with a homeowner who may be interested in selling their property. Your only job is to have a \
warm, low-pressure conversation and gather enough information for your boss to make a cash offer — \
you never state or imply a price yourself, and you never claim to be human if directly asked.

Keep every message short (1-3 sentences, real text-message style, no bullet points, no markdown).
Ask ONE question at a time. Let the conversation feel natural, not like a form.

Information to gather, in whatever order feels natural:
- The property's full address (street, city, state, zip)
- Why they're considering selling (motivation)
- The property's condition (roughly: light cosmetic work, medium kitchen/bath/systems, or heavy full rehab)
- Whether it's vacant, owner-occupied, or tenant-occupied
- Whether they have a number in mind for what they'd want (asking price) — okay if they don't know
- Their name, if they haven't given it

If someone seems uninterested, annoyed, or asks to be left alone, thank them politely and stop asking questions.

Once you have the address AND at least condition and motivation (asking price is nice but not required), \
end your reply with a new line starting exactly with "QUALIFIED:" followed by a JSON object with these \
keys: address, seller_name, asking_price (number or null), condition (light/medium/heavy), motivation \
(one of: probate, tax_delinquent, divorce, inherited, tired_landlord, foreclosure, relocating, unknown), \
occupancy (vacant/owner/tenant/unknown). Put your normal warm closing message to the seller BEFORE that \
line, since everything before "QUALIFIED:" is what actually gets sent to them — the JSON line itself is \
stripped out before sending, so the seller never sees it.
"""


def build_sms_system_prompt(cfg):
    return SMS_BOT_SYSTEM_PROMPT_TEMPLATE.format(bot_name=cfg.get("sms_bot_name", "Alex"))


def call_claude(messages, system_prompt, cfg):
    """
    Calls the Anthropic Messages API directly (same API this whole app's
    conversation runs on). Returns (reply_text, error) — never raises, so
    a bad key or an outage degrades to a polite fallback message instead
    of a broken webhook response (which Twilio would retry/alarm on).
    """
    if not cfg.get("anthropic_api_key") or not cfg.get("anthropic_model"):
        return None, "ANTHROPIC_API_KEY or ANTHROPIC_MODEL not configured"

    headers = {
        "x-api-key": cfg["anthropic_api_key"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": cfg["anthropic_model"],
        "max_tokens": 400,
        "system": system_prompt,
        "messages": messages,
    }
    try:
        resp = requests.post(ANTHROPIC_API_URL, headers=headers, json=body, timeout=25)
    except requests.RequestException as e:
        return None, str(e)

    if resp.status_code != 200:
        return None, f"{resp.status_code} {resp.text[:200]}"

    data = resp.json()
    text = "".join(
        block.get("text", "") for block in data.get("content", []) if block.get("type") == "text"
    )
    return text, None


QUALIFIED_PATTERN = re.compile(r"QUALIFIED:\s*(\{.*\})", re.DOTALL)


def extract_qualified_block(reply_text):
    """
    Splits a bot reply into (message_to_send, qualified_data). If no
    QUALIFIED: block is present, qualified_data is None and the full
    reply is sent as-is. A malformed JSON block degrades to "not
    qualified yet" rather than crashing the webhook.
    """
    match = QUALIFIED_PATTERN.search(reply_text)
    if not match:
        return reply_text.strip(), None

    message = reply_text[:match.start()].strip()
    try:
        data = json.loads(match.group(1))
    except (json.JSONDecodeError, ValueError):
        return message or reply_text.strip(), None

    return message, data


def get_conversation(phone_number):
    conn = get_db()
    row = conn.execute("SELECT * FROM conversations WHERE phone_number = ?", (phone_number,)).fetchone()
    conn.close()
    return row


def save_conversation(phone_number, transcript, status, lead_id=None):
    conn = get_db()
    existing = conn.execute(
        "SELECT id FROM conversations WHERE phone_number = ?", (phone_number,)
    ).fetchone()
    if existing:
        conn.execute("""
            UPDATE conversations SET transcript = ?, status = ?, lead_id = COALESCE(?, lead_id),
                                      updated_at = CURRENT_TIMESTAMP
            WHERE phone_number = ?
        """, (json.dumps(transcript), status, lead_id, phone_number))
    else:
        conn.execute("""
            INSERT INTO conversations (phone_number, transcript, status, lead_id)
            VALUES (?, ?, ?, ?)
        """, (phone_number, json.dumps(transcript), status, lead_id))
    conn.commit()
    conn.close()


def twiml_response(message):
    """Builds the minimal TwiML XML Twilio expects as a webhook reply."""
    from xml.sax.saxutils import escape
    body = f'<?xml version="1.0" encoding="UTF-8"?><Response>'
    if message:
        body += f"<Message>{escape(message)}</Message>"
    body += "</Response>"
    from flask import Response
    return Response(body, mimetype="text/xml")


OPT_OUT_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}


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
    seller_name = request.form.get("seller_name", "").strip()
    seller_phone = request.form.get("seller_phone", "").strip()
    seller_email = request.form.get("seller_email", "").strip()

    result = analyze_address(
        address, asking_price, condition, county, cfg,
        motivation=motivation, occupancy=occupancy,
        out_of_state_owner=out_of_state_owner, mortgage_status=mortgage_status,
        known_issues=known_issues, manual_sqft=manual_sqft,
        seller_name=seller_name, seller_phone=seller_phone, seller_email=seller_email,
    )
    save_lead(result)

    if result.get("mao") is not None:
        ceiling_price = result["mao"] + cfg.get("wholesale_fee", 10000)
        result["matching_buyers"] = get_matching_buyers(result.get("asking_price"), ceiling_price, county)
    else:
        result["matching_buyers"] = []

    if result.get("is_deal"):
        seller_line = ""
        if result.get("seller_name") or result.get("seller_phone") or result.get("seller_email"):
            seller_line = (
                f"\nSeller: {result.get('seller_name') or '(no name)'}"
                f"  {result.get('seller_phone') or ''}  {result.get('seller_email') or ''}"
            )
        send_alert(
            f"Deal found: {result['address']}",
            f"{result['address']}\n"
            f"Asking: ${result['asking_price']:,.0f}  MAO: ${result['mao']:,.0f}  "
            f"Profit: ${result['potential_profit']:,.0f}"
            f"{seller_line}",
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
        seller_name = (row.get("seller_name") or "").strip()
        seller_phone = (row.get("seller_phone") or "").strip()
        seller_email = (row.get("seller_email") or "").strip()

        result = analyze_address(
            address, asking_price, condition, county, cfg,
            motivation=motivation, occupancy=occupancy,
            out_of_state_owner=out_of_state_owner, mortgage_status=mortgage_status,
            known_issues=known_issues, manual_sqft=manual_sqft,
            seller_name=seller_name, seller_phone=seller_phone, seller_email=seller_email,
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


@app.route("/leads/<int:lead_id>/edit", methods=["GET", "POST"])
def edit_lead(lead_id):
    lead = get_lead(lead_id)
    if not lead:
        flash("That lead doesn't exist (maybe already deleted).")
        return redirect(url_for("all_leads"))

    if request.method == "POST":
        cfg = load_config()
        asking_price = request.form.get("asking_price")
        asking_price = float(asking_price) if asking_price else None
        condition = request.form.get("condition", "medium")
        county = request.form.get("county", "")
        motivation = request.form.get("motivation", "unknown")
        occupancy = request.form.get("occupancy", "unknown")
        out_of_state_owner = request.form.get("out_of_state_owner") == "yes"
        mortgage_status = request.form.get("mortgage_status", "unknown")
        known_issues = request.form.getlist("known_issues")
        notes = request.form.get("notes", "").strip()
        seller_name = request.form.get("seller_name", "").strip()
        seller_phone = request.form.get("seller_phone", "").strip()
        seller_email = request.form.get("seller_email", "").strip()

        update_lead(
            lead_id, asking_price, condition, county, motivation, occupancy,
            out_of_state_owner, mortgage_status, known_issues, notes, cfg,
            seller_name=seller_name, seller_phone=seller_phone, seller_email=seller_email,
        )
        return redirect(request.form.get("redirect_to") or url_for("all_leads"))

    counties = load_counties()
    known_issues_list = (lead["known_issues"] or "").split(",") if lead["known_issues"] else []
    return render_template("edit_lead.html", lead=lead, counties=counties, known_issues_list=known_issues_list)


@app.route("/leads/<int:lead_id>/delete", methods=["POST"])
def delete_lead_route(lead_id):
    delete_lead(lead_id)
    return redirect(request.form.get("redirect_to") or url_for("all_leads"))


@app.route("/buyers", methods=["GET", "POST"])
def buyers():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Buyer needs at least a name.")
            return redirect(url_for("buyers"))
        phone = request.form.get("phone", "").strip()
        email = request.form.get("email", "").strip()
        min_price = request.form.get("min_price")
        min_price = float(min_price) if min_price else None
        max_price = request.form.get("max_price")
        max_price = float(max_price) if max_price else None
        counties = request.form.get("counties", "").strip()
        notes = request.form.get("notes", "").strip()
        add_buyer(name, phone, email, min_price, max_price, counties, notes)
        return redirect(url_for("buyers"))

    return render_template("buyers.html", buyers=get_all_buyers())


@app.route("/buyers/<int:buyer_id>/delete", methods=["POST"])
def delete_buyer_route(buyer_id):
    delete_buyer(buyer_id)
    return redirect(url_for("buyers"))


@app.route("/discover", methods=["GET", "POST"])
def discover():
    counties = load_counties()

    if request.method == "GET":
        return render_template("discover.html", listings=None, analyzed=None, counties=counties)

    cfg = load_config()
    city = request.form.get("city", "").strip()
    state = request.form.get("state", "").strip().upper()
    zip_code = request.form.get("zip_code", "").strip()
    max_price = request.form.get("max_price")
    max_price = float(max_price) if max_price else None
    search_count = min(int(request.form.get("search_count") or 25), 100)
    analyze_count = min(int(request.form.get("analyze_count") or 5), 15)
    condition = request.form.get("condition", "medium")
    county = request.form.get("county", "")

    listings, error = get_sale_listings(city, state, zip_code, search_count, cfg["rentcast_api_key"])

    if error:
        flash(f"Couldn't search listings: {error}")
        return render_template("discover.html", listings=None, analyzed=None, counties=counties)

    if max_price:
        listings = [l for l in listings if l["price"] is None or l["price"] <= max_price]

    # Distressed-language matches first, then cheapest — that's the shortlist worth spending API calls on.
    listings.sort(key=lambda l: (0 if l["distress_keywords"] else 1, l["price"] or 0))

    shortlist = listings[:analyze_count]
    analyzed = []
    for listing in shortlist:
        result = analyze_address(
            listing["address"], listing["price"], condition, county, cfg,
            manual_sqft=listing["sqft"],
        )
        save_lead(result, source="discovery")
        analyzed.append(result)
        time.sleep(cfg.get("request_delay_seconds", 1.5))

    new_deals = [r for r in analyzed if r.get("is_deal")]
    if new_deals:
        lines = [
            f"{r['address']} — Asking ${r['asking_price']:,.0f}, Profit ${r['potential_profit']:,.0f}"
            for r in new_deals[:5]
        ]
        send_alert(f"{len(new_deals)} deal(s) found via Find Deals", "\n".join(lines), cfg)

    return render_template(
        "discover.html", listings=listings, analyzed=analyzed, counties=counties,
        searched_count=len(listings), analyzed_count=len(shortlist),
    )


@app.route("/sms/webhook", methods=["POST"])
def sms_webhook():
    """
    Twilio POSTs here (application/x-www-form-urlencoded) whenever your
    Twilio number receives an SMS. We reply synchronously with TwiML —
    no outbound Twilio API call or credentials needed for the reply itself,
    only for buying the number and pointing it at this URL in the first
    place (done entirely on Twilio's side, not in this app).
    """
    from_number = request.form.get("From", "").strip()
    body = request.form.get("Body", "").strip()
    if not from_number or not body:
        return twiml_response("")

    conv = get_conversation(from_number)
    transcript = json.loads(conv["transcript"]) if conv else []

    if conv and conv["status"] in ("opted_out",):
        return twiml_response("")  # never message an opted-out number again

    if body.strip().lower() in OPT_OUT_WORDS:
        transcript.append({"role": "user", "content": body})
        save_conversation(from_number, transcript, status="opted_out")
        return twiml_response("You won't receive further messages. Reply START to resubscribe.")

    if conv and conv["status"] == "qualified":
        # Already handed off to a human — stop auto-replying so the bot
        # doesn't talk over whoever picks up the conversation next.
        return twiml_response("")

    cfg = load_config()
    transcript.append({"role": "user", "content": body})

    if len(transcript) > cfg.get("sms_bot_max_turns", 20):
        save_conversation(from_number, transcript, status="needs_human")
        send_alert(
            f"Text conversation needs a human: {from_number}",
            "This conversation ran long without qualifying. Take over manually.",
            cfg,
        )
        return twiml_response("Thanks for all the info — let me have my colleague follow up with you directly.")

    reply_text, error = call_claude(transcript, build_sms_system_prompt(cfg), cfg)
    if error:
        save_conversation(from_number, transcript, status="error")
        print(f"SMS bot error for {from_number}: {error}")
        return twiml_response("Thanks for your message! I'm having a small technical hiccup — someone will follow up with you shortly.")

    message_to_send, qualified_data = extract_qualified_block(reply_text)
    transcript.append({"role": "assistant", "content": reply_text})

    if qualified_data:
        result = analyze_address(
            qualified_data.get("address", "") or "",
            qualified_data.get("asking_price"),
            qualified_data.get("condition") or "medium",
            "",
            cfg,
            motivation=qualified_data.get("motivation") or "unknown",
            occupancy=qualified_data.get("occupancy") or "unknown",
            seller_name=qualified_data.get("seller_name") or "",
            seller_phone=from_number,
        )
        lead_id = save_lead(result, source="text_bot")
        save_conversation(from_number, transcript, status="qualified", lead_id=lead_id)
        send_alert(
            f"Qualified seller lead via text: {result['address']}",
            f"From: {from_number}\n"
            f"Address: {result['address']}\n"
            f"Asking: {qualified_data.get('asking_price') or 'not given'}\n"
            f"Condition: {qualified_data.get('condition')}\n"
            f"Motivation: {qualified_data.get('motivation')}\n"
            f"{'DEAL — clears your MAO!' if result.get('is_deal') else ''}",
            cfg,
        )
        return twiml_response(message_to_send or "Thanks! I'll follow up soon.")

    save_conversation(from_number, transcript, status="active")
    return twiml_response(message_to_send)


@app.route("/conversations")
def conversations_list():
    conn = get_db()
    rows = conn.execute("SELECT * FROM conversations ORDER BY updated_at DESC LIMIT 100").fetchall()
    conn.close()
    parsed = []
    for r in rows:
        transcript = json.loads(r["transcript"])
        last_message = transcript[-1]["content"] if transcript else ""
        parsed.append({
            "id": r["id"], "phone_number": r["phone_number"], "status": r["status"],
            "lead_id": r["lead_id"], "updated_at": r["updated_at"],
            "message_count": len(transcript), "last_message": last_message,
        })
    return render_template("conversations.html", conversations=parsed)


@app.route("/conversations/<int:conv_id>")
def conversation_detail(conv_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
    conn.close()
    if not row:
        flash("That conversation doesn't exist.")
        return redirect(url_for("conversations_list"))
    transcript = json.loads(row["transcript"])
    return render_template("conversation_detail.html", conv=row, transcript=transcript)


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
