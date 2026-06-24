#!/usr/bin/env python3
"""
options_snapshot.py — Server-side relay for the Nifty Weekly Options page.

WHY THIS EXISTS
----------------
options.html (a static GitHub Pages file) cannot call api.kite.trade or
kite.zerodha.com directly from the browser — Zerodha's API does not send
CORS headers and rejects browser-origin requests outright (confirmed via
Kite Connect's own developer forum). This is true regardless of whether
the user is logged into Kite in their browser.

This script runs server-side (inside the bot's already-authenticated
GitHub Actions session) and does the actual Kite calls instead: fetches
Nifty 50 spot + India VIX, computes today's weekday-based OTM Put/Call
strikes using the exact same formula as options.html's JS, fetches LTP +
delta for those two option symbols, and writes the result to a small JSON
file. That file gets pushed to gh-pages (same mechanism as
status_rd1858.json), and options.html just fetches it as a static file —
a same-origin GET with zero CORS issues, since it's not a live Kite call
at all from the browser's perspective.

Usage (called from the workflow's background loop, similar to the
existing status_rd1858.json pusher):
    python3 options_snapshot.py          # runs once, writes the JSON, exits
"""

import json
import math
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from io import StringIO

sys.path.insert(0, str(Path(__file__).parent))

IST = timezone(timedelta(hours=5, minutes=30))
SNAPSHOT_PATH = Path("/tmp/options_snapshot_rd1858.json")

API_BASE = "https://kite.zerodha.com/oms"
INSTRUMENTS_URL = "https://api.kite.trade/instruments"

# Same defaults as options.html's JS — kept in sync deliberately.
DEFAULT_DIVISORS = {1: 3300, 2: 3800, 3: 4500, 4: 6000, 5: 8000}  # Mon..Fri
DEFAULT_MULT_PCT = 150
STRIKE_STEP = 100
MONTH_CODES = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "O", "N", "D"]


def load_enctoken():
    enc_file = Path(__file__).parent / "config" / "enctoken.json"
    try:
        data = json.loads(enc_file.read_text())
        return data.get("enctoken")
    except Exception as e:
        print(f"[options-snap] ERROR: could not read enctoken: {e}")
        return None


def make_session(enctoken):
    import requests
    s = requests.Session()
    s.headers.update({
        "Authorization": f"enctoken {enctoken}",
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://kite.zerodha.com/",
    })
    return s


def fetch_instruments(session):
    """Fetch the live Zerodha instruments dump (needed to resolve tokens
    for indices and the computed option symbols)."""
    import pandas as pd
    import requests
    for url, use_auth in [(INSTRUMENTS_URL, False), (f"{API_BASE}/instruments/NSE", True)]:
        try:
            r = (session if use_auth else requests.Session()).get(url, timeout=15)
            if r.status_code == 200 and r.text.strip():
                df = pd.read_csv(StringIO(r.text), low_memory=False)
                df.columns = [c.lower() for c in df.columns]
                return df
        except Exception as e:
            print(f"[options-snap] instruments fetch {url}: {e}")
    return None


def resolve_token(df_inst, tradingsymbol, segment=None):
    if df_inst is None:
        return None
    try:
        if segment:
            m = df_inst[(df_inst["tradingsymbol"] == tradingsymbol) & (df_inst["segment"] == segment)]
            if not m.empty:
                return str(int(m.iloc[0]["instrument_token"]))
        m = df_inst[df_inst["tradingsymbol"] == tradingsymbol]
        if not m.empty:
            return str(int(m.iloc[0]["instrument_token"]))
    except Exception as e:
        print(f"[options-snap] resolve_token({tradingsymbol}): {e}")
    return None


def fetch_quote(session, exchange, token):
    try:
        key = f"{exchange}:{token}"
        r = session.get(f"{API_BASE}/quote", params={"i": key}, timeout=8)
        if r.status_code == 200:
            d = r.json().get("data", {}).get(key, {})
            return d.get("last_price")
    except Exception as e:
        print(f"[options-snap] fetch_quote({exchange}:{token}): {e}")
    return None


def floor_to(x, step):
    return math.floor(x / step) * step


def ceil_to(x, step):
    return math.ceil(x / step) * step


def this_week_expiry(ref):
    """Next Tuesday on/after ref (Nifty's weekly expiry day since Sep 2025).
    Does not account for exchange holidays shifting expiry to Monday."""
    days_until_tue = (1 - ref.weekday() + 7) % 7  # Mon=0..Sun=6; Tue=1
    return ref + timedelta(days=days_until_tue)


def build_option_symbol(underlying, expiry_date, strike, opt_type):
    yy = f"{expiry_date.year % 100:02d}"
    m = MONTH_CODES[expiry_date.month - 1]
    dd = f"{expiry_date.day:02d}"
    return f"{underlying}{yy}{m}{dd}{strike}{opt_type}"


def fetch_option_chain_leg(session, underlying, expiry_date, strike, opt_type):
    months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    expiry_str = f"{expiry_date.day:02d}{months[expiry_date.month-1]}{expiry_date.year % 100:02d}"
    try:
        url = f"https://api.kite.trade/oi/chain/{underlying}"
        r = session.get(url, params={"expiry": expiry_str}, timeout=10)
        if r.status_code != 200:
            return {"ltp": None, "delta": None, "oi": None, "iv": None}
        chain = r.json().get("data", [])
        entry = next((e for e in chain if e.get("strike_price") == strike), None)
        if not entry:
            return {"ltp": None, "delta": None, "oi": None, "iv": None}
        key = "call_options" if opt_type == "CE" else "put_options"
        leg = entry.get(key, {}) or {}
        ltp = leg.get("last_price") or (leg.get("option_chain") or {}).get("last_price")
        delta = (leg.get("greeks") or {}).get("delta") or (leg.get("option_chain") or {}).get("delta")
        oi = leg.get("oi") or (leg.get("option_chain") or {}).get("oi")
        iv = (leg.get("greeks") or {}).get("iv") or (leg.get("option_chain") or {}).get("iv")
        return {
            "ltp":   float(ltp) if ltp is not None else None,
            "delta": float(delta) if delta is not None else None,
            "oi":    float(oi) if oi is not None else None,
            "iv":    float(iv) if iv is not None else None,
        }
    except Exception as e:
        print(f"[options-snap] fetch_option_chain_leg({underlying} {strike}{opt_type}): {e}")
        return {"ltp": None, "delta": None, "oi": None, "iv": None, "error": str(e)}


def main():
    now = datetime.now(IST)
    weekday = now.isoweekday()  # 1=Mon..7=Sun

    enctoken = load_enctoken()
    if not enctoken:
        write_snapshot({"error": "no_enctoken", "updated_at": now.isoformat()})
        return

    session = make_session(enctoken)
    df_inst = fetch_instruments(session)

    nifty_token = resolve_token(df_inst, "NIFTY 50", "NSE-INDICES") or "256265"
    vix_token   = resolve_token(df_inst, "INDIA VIX", "NSE-INDICES") or "264969"

    spot = fetch_quote(session, "NSE", nifty_token)
    vix  = fetch_quote(session, "NSE", vix_token)

    result = {
        "updated_at": now.isoformat(),
        "weekday":    weekday,
        "spot":       spot,
        "vix":        vix,
        "put":  {"symbol": None, "strike": None, "ltp": None, "delta": None, "oi": None, "iv": None},
        "call": {"symbol": None, "strike": None, "ltp": None, "delta": None, "oi": None, "iv": None},
    }

    if spot is not None and vix is not None and 1 <= weekday <= 5:
        divisor = DEFAULT_DIVISORS.get(weekday, DEFAULT_DIVISORS[1])
        range_pts = spot * math.sqrt(vix * (DEFAULT_MULT_PCT / 100) / divisor)
        put_strike  = floor_to(spot - range_pts, STRIKE_STEP)
        call_strike = ceil_to(spot + range_pts, STRIKE_STEP)
        expiry = this_week_expiry(now).date()

        put_sym  = build_option_symbol("NIFTY", expiry, put_strike,  "PE")
        call_sym = build_option_symbol("NIFTY", expiry, call_strike, "CE")

        put_data  = fetch_option_chain_leg(session, "NIFTY", expiry, put_strike,  "PE")
        call_data = fetch_option_chain_leg(session, "NIFTY", expiry, call_strike, "CE")

        result["range"]  = round(range_pts)
        result["divisor"] = divisor
        result["expiry"] = expiry.isoformat()
        result["put"]    = {"symbol": put_sym,  "strike": put_strike,  **put_data}
        result["call"]   = {"symbol": call_sym, "strike": call_strike, **call_data}

    write_snapshot(result)


def write_snapshot(data):
    try:
        SNAPSHOT_PATH.write_text(json.dumps(data, indent=2))
        print(f"[options-snap] wrote {SNAPSHOT_PATH}: {json.dumps(data)[:200]}")
    except Exception as e:
        print(f"[options-snap] ERROR writing snapshot: {e}")


if __name__ == "__main__":
    main()
