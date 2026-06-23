#!/usr/bin/env python3
"""
seed_scanner_oncall.py — On-demand seeder for the Scanner page's full
ETF/NIFTY200/NIFTY-Midcap150 universe (the ~575-symbol set seed_csvs.py
refreshes).

WHY THIS EXISTS
----------------
seed_csvs.py refreshes every CSV already sitting in data/daily/ — which,
over time, accumulated ~575 symbols from past Scanner runs. The main
trading-bot.yml workflow used to run that full refresh on every single
scheduled session (twice daily), even though the bot itself only trades
the handful of symbols in bnh_symbols/active_etfs. That made every bot
run slower for no trading benefit — the Scanner only actually needs fresh
data when someone opens it and clicks "Run Scanner".

This script does the same seeding work, but is meant to be triggered
on demand (via the Scanner page's "⬇ Seed Scanner Data" button, which
dispatches the seed-scanner.yml workflow) instead of running automatically
every session.

LOGIN
-----
Reuses cloud_launcher.py's exact login path: checks for an already-valid
saved token first (no browser needed, ~1s), and only falls back to a full
Playwright + TOTP login if that token is missing or expired. This is the
same logic the main bot already relies on, so behavior here is identical
to what the bot would do.

Usage (GitHub Actions):
    python seed_scanner_oncall.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# Reuse cloud_launcher's env validation, TOTP, and Playwright login path
# instead of duplicating ~150 lines of login logic.
import cloud_launcher as cl


def get_valid_enctoken() -> str:
    """
    Mirrors the token-reuse check in cloud_launcher.main(): verify any
    saved token against Kite's live API first, and only perform a fresh
    Playwright login if that check fails. Avoids invalidating a session
    that's already active elsewhere (e.g. your own browser, or a bot run
    that logged in earlier today) by skipping login whenever possible.
    """
    print("🔍 Checking existing token validity before login...")
    try:
        from backend.auth.token_store import load_token
        import requests as _req
        saved = load_token()
        if saved and saved.get("enctoken"):
            s = _req.Session()
            s.headers.update({
                "Authorization": f"enctoken {saved['enctoken']}",
                "User-Agent":    "Mozilla/5.0",
                "Referer":       "https://kite.zerodha.com/",
            })
            r = s.get("https://kite.zerodha.com/oms/user/profile", timeout=8)
            if r.status_code == 200 and r.json().get("data", {}).get("user_name"):
                print(f"✅ Saved token still valid — skipping Playwright login")
                return saved["enctoken"]
    except Exception as e:
        print(f"⚠️  Token check error: {e} — will attempt fresh login")

    cl.validate_env()
    print("🔐 No valid saved token — performing fresh TOTP login...")
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            print(f"\n{'─'*60}  LOGIN ATTEMPT {attempt}/{max_attempts}  {'─'*60}")
            enctoken = cl.login_to_kite()
            cl.save_enctoken(cl.USER_ID, enctoken)
            return enctoken
        except Exception as e:
            print(f"❌ Attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                import time
                wait = attempt * 15
                print(f"  → Retrying in {wait}s...")
                time.sleep(wait)
            else:
                raise RuntimeError(f"All {max_attempts} login attempts failed: {e}")


def main():
    print("=" * 60)
    print("  Scanner Universe — On-Demand Seed")
    print("  (full ETF/NIFTY200/Midcap150 refresh, decoupled from the")
    print("   twice-daily bot workflow)")
    print("=" * 60)

    get_valid_enctoken()   # writes/refreshes config/enctoken.json as needed

    # Hand off to the existing seeder — same retry/backoff logic, same
    # data/daily/ output path, same instrument-resolution behavior.
    import seed_csvs
    seed_csvs.main()


if __name__ == "__main__":
    main()
