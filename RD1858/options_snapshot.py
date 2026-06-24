#!/usr/bin/env python3
"""
options_snapshot.py — Server-side relay for the Nifty Options tracker page.

WHY THIS EXISTS
----------------
options.html (a static GitHub Pages file) cannot call Kite directly from
the browser:
  - api.kite.trade does not send CORS headers and rejects browser-origin
    requests outright (confirmed via Kite Connect's own developer forum),
    regardless of login state.
  - There is also no Kite Connect API to read a user's actual Marketwatch
    list (confirmed via the same forum) — so symbols can't be
    auto-discovered either; they're entered manually and saved to
    RD1858/config/options_watch.json via the GitHub Contents API (same
    pattern as settings-editor/marketwatch.json).

This script runs server-side, inside the bot's already-authenticated
GitHub Actions session. It reads whatever Put/Call symbols are currently
saved in options_watch.json, fetches LTP + delta for each from Kite, and
writes the result to a small JSON file. That file gets pushed to
gh-pages (same mechanism as status_rd1858.json), and options.html just
fetches it as a static file — a same-origin GET with zero CORS issues.

Usage (called from the workflow's background loop):
    python3 options_snapshot.py
"""

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).parent))

IST = timezone(timedelta(hours=5, minutes=30))
SNAPSHOT_PATH = Path("/tmp/options_snapshot_rd1858.json")
WATCH_FILE = Path(__file__).parent / "config" / "options_watch.json"

API_BASE = "https://kite.zerodha.com/oms"


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


def load_watch_symbols():
    """Reads the manually-saved Put/Call symbols. Returns (put, call), each
    either a non-empty uppercase string or None."""
    try:
        data = json.loads(WATCH_FILE.read_text())
        put = (data.get("put") or "").strip().upper() or None
        call = (data.get("call") or "").strip().upper() or None
        return put, call
    except Exception as e:
        print(f"[options-snap] no options_watch.json yet (or unreadable): {e}")
        return None, None


# Mirrors backend/utils/option_greeks.py's parser — kept here as a plain
# function (no Flask/auth-object dependency) since this script runs
# standalone, outside the Flask app, with its own requests.Session.
import re

# Weekly format:  NIFTY2627025200CE  -> YY + single-char-month + DD + strike + CE/PE
_OPT_SYM_WEEKLY_RE = re.compile(r'^([A-Z]+)(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$')
# Monthly format: NIFTY26JUN25200CE  -> YY + 3-letter-month + strike + CE/PE (no day)
_OPT_SYM_MONTHLY_RE = re.compile(r'^([A-Z]+)(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d+)(CE|PE)$')

_MONTH_CODE_TO_NUM = {c: i + 1 for i, c in enumerate("123456789")}
_MONTH_CODE_TO_NUM.update({'O': 10, 'N': 11, 'D': 12})
_MONTHS_3LETTER = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]


def parse_option_symbol(sym):
    # Try monthly first (3-letter month, e.g. NIFTY26JUN25200PE)
    m = _OPT_SYM_MONTHLY_RE.match(sym)
    if m:
        underlying, yy, month_str, strike, opt_type = m.groups()
        # Kite chain API accepts "MMMYY" expiry for monthly contracts
        expiry_str = f"{month_str}{yy}"  # e.g. "JUN26"
        return {
            "underlying": underlying,
            "expiry_str": expiry_str,
            "strike": int(strike),
            "opt_type": opt_type,
            "is_monthly": True,
        }

    # Try weekly format (single-char month code, e.g. NIFTY2627025200PE)
    m = _OPT_SYM_WEEKLY_RE.match(sym)
    if not m:
        return None
    underlying, yy, month_code, dd, strike, opt_type = m.groups()
    month_num = _MONTH_CODE_TO_NUM.get(month_code)
    if not month_num:
        return None
    return {
        "underlying": underlying,
        "expiry_str": f"{dd}{_MONTHS_3LETTER[month_num-1]}{yy}",  # e.g. "27JUN26"
        "strike": int(strike),
        "opt_type": opt_type,
        "is_monthly": False,
    }

def fetch_leg_data(session, sym):
    """Fetch LTP + delta + OI + IV for one option tradingsymbol via Kite's
    authenticated option-chain endpoint. Returns a dict; all fields None
    on any failure (never raises)."""
    info = parse_option_symbol(sym)
    if not info:
        return {"ltp": None, "delta": None, "oi": None, "iv": None, "error": "unparseable_symbol"}
    try:
        url = f"https://api.kite.trade/oi/chain/{info['underlying']}"
        r = session.get(url, params={"expiry": info["expiry_str"]}, timeout=10)
        if r.status_code != 200:
            return {"ltp": None, "delta": None, "oi": None, "iv": None, "error": f"http_{r.status_code}"}
        chain = r.json().get("data", [])
        entry = next((e for e in chain if e.get("strike_price") == info["strike"]), None)
        if not entry:
            return {"ltp": None, "delta": None, "oi": None, "iv": None, "error": "strike_not_found"}
        leg_key = "call_options" if info["opt_type"] == "CE" else "put_options"
        leg = entry.get(leg_key) or {}
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
        print(f"[options-snap] fetch_leg_data({sym}): {e}")
        return {"ltp": None, "delta": None, "oi": None, "iv": None, "error": str(e)}


def main():
    now = datetime.now(IST)
    put, call = load_watch_symbols()

    if not put and not call:
        write_snapshot({"updated_at": now.isoformat(), "legs": {}})
        print("[options-snap] no symbols saved in options_watch.json — nothing to fetch")
        return

    enctoken = load_enctoken()
    if not enctoken:
        write_snapshot({"error": "no_enctoken", "updated_at": now.isoformat(), "legs": {}})
        return

    session = make_session(enctoken)
    legs = {}
    for sym in filter(None, [put, call]):
        legs[sym] = fetch_leg_data(session, sym)
        print(f"[options-snap] {sym}: {legs[sym]}")

    write_snapshot({"updated_at": now.isoformat(), "put": put, "call": call, "legs": legs})


def write_snapshot(data):
    try:
        SNAPSHOT_PATH.write_text(json.dumps(data, indent=2))
        print(f"[options-snap] wrote {SNAPSHOT_PATH}")
    except Exception as e:
        print(f"[options-snap] ERROR writing snapshot: {e}")


if __name__ == "__main__":
    main()
