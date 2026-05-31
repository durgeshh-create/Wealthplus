#!/usr/bin/env python3
"""
WealthAlgo Cloud Launcher — PS5673
------------------------------------
Automates Kite login via Playwright headless Chrome, writes the enctoken
to config/enctoken.json, then launches dashboard.py.

Usage (GitHub Actions):
    python cloud_launcher.py

Environment variables (set as GitHub Secrets):
    KITE_USER_ID        — Zerodha user ID  (e.g. PS5673)
    KITE_PASSWORD       — Kite login password
    KITE_TOTP_SECRET    — TOTP secret key from your 2FA setup (base32, no spaces)
    BOT_PORT            — Flask port (5001 for PS5673)
    TELEGRAM_BOT_TOKEN  — Telegram bot token for notifications
    TELEGRAM_CHAT_ID    — Your Telegram chat ID

Features:
    - Headless Chrome login with 3-attempt retry
    - Morning briefing at 09:05 IST (watchlist + W%%R)
    - Opening prices at 09:16 IST
    - End-of-day summary at 15:32 IST
    - Trade notifications on every BUY/SELL (requires executor.py patch)
"""

import os
import sys
import json
import time
import threading
import subprocess
from pathlib import Path

import pyotp

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────

USER_ID     = os.environ.get("KITE_USER_ID", "").strip()
PASSWORD    = os.environ.get("KITE_PASSWORD", "").strip()
TOTP_SECRET = os.environ.get("KITE_TOTP_SECRET", "").strip()
BOT_PORT    = os.environ.get("BOT_PORT", "5001").strip()

CONFIG_DIR     = Path(__file__).parent / "config"
ENCTOKEN_FILE  = CONFIG_DIR / "enctoken.json"
SCREENSHOT_DIR = Path("/tmp")


# ── Telegram notifications ────────────────────────────────────────────────────

def telegram_notify(message: str):
    """Send a Telegram message. Never raises."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        import urllib.request
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       message,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def validate_env():
    missing = [k for k, v in {
        "KITE_USER_ID":     USER_ID,
        "KITE_PASSWORD":    PASSWORD,
        "KITE_TOTP_SECRET": TOTP_SECRET,
    }.items() if not v]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)
    try:
        code = pyotp.TOTP(TOTP_SECRET).now()
        print(f"  → TOTP secret valid — test code: {code}")
    except Exception as e:
        print(f"ERROR: KITE_TOTP_SECRET is invalid: {e}")
        sys.exit(1)


def generate_totp() -> str:
    totp = pyotp.TOTP(TOTP_SECRET)
    code = totp.now()
    remaining = 30 - (int(time.time()) % 30)
    print(f"  → TOTP: {code} (expires in {remaining}s)")
    if remaining < 5:
        print(f"  → Too close to expiry — waiting {remaining + 1}s for fresh code...")
        time.sleep(remaining + 1)
        code = pyotp.TOTP(TOTP_SECRET).now()
        print(f"  → Fresh TOTP: {code}")
    return code


def save_enctoken(user_id: str, enctoken: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    ENCTOKEN_FILE.write_text(json.dumps({"user_id": user_id, "enctoken": enctoken}, indent=2))
    print(f"  → Saved enctoken to {ENCTOKEN_FILE}")


def screenshot(page, name: str):
    for path, method in [
        (SCREENSHOT_DIR / f"kite_debug_{name}.png",  lambda p: page.screenshot(path=str(p), full_page=True)),
        (SCREENSHOT_DIR / f"kite_debug_{name}.html", lambda p: p.write_text(page.content(), encoding="utf-8")),
    ]:
        try:
            method(path)
            print(f"  📸 {path}")
        except Exception as e:
            print(f"  ⚠️  {name} capture failed: {e}")


def try_selector(page, selectors: list, timeout: int = 5000):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            loc.wait_for(state="visible", timeout=timeout)
            print(f"  → Found: {sel}")
            return loc
        except PWTimeout:
            continue
    return None


# ── Playwright login ──────────────────────────────────────────────────────────

def login_to_kite() -> str:
    print("\n[1/4] Launching headless Chrome → kite.zerodha.com ...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        page.goto("https://kite.zerodha.com", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=15_000)
        screenshot(page, "01_login_page")

        # User ID
        uid = try_selector(page, [
            'input#userid', 'input[name="user_id"]',
            'input[type="text"]', 'input[placeholder*="User"]',
        ])
        if uid is None:
            screenshot(page, "02_no_userid")
            raise RuntimeError(f"Cannot find user ID field on page: {page.url}")
        uid.fill(USER_ID)

        # Password
        pwd = try_selector(page, [
            'input#password', 'input[name="password"]', 'input[type="password"]',
        ])
        if pwd is None:
            screenshot(page, "02_no_password")
            raise RuntimeError("Cannot find password field")
        pwd.fill(PASSWORD)
        screenshot(page, "02_credentials")

        # Submit
        btn = try_selector(page, ['button[type="submit"]', 'button.button-orange', 'button:has-text("Login")'])
        if btn:
            btn.click()
        else:
            page.keyboard.press("Enter")

        time.sleep(2)
        screenshot(page, "03_after_login")

        # TOTP
        totp_field = try_selector(page, [
            'input#totp', 'input[name="totp"]', 'input[placeholder*="TOTP"]',
            'input[placeholder*="OTP"]', 'input[type="number"]', 'input[maxlength="6"]',
        ], timeout=15_000)
        if totp_field is None:
            screenshot(page, "03_no_totp")
            raise RuntimeError(
                f"TOTP field not found. URL: {page.url}\n"
                "Password may be wrong or login was blocked."
            )
        screenshot(page, "03_totp_page")
        totp_field.fill(generate_totp())
        screenshot(page, "03_totp_filled")

        try:
            s2 = page.locator('button[type="submit"], button.button-orange')
            s2.first.wait_for(state="visible", timeout=3_000)
            s2.first.click(force=True, timeout=3_000)
        except Exception:
            pass  # Kite auto-submits on 6 digits

        # Wait for dashboard
        try:
            page.wait_for_url("**/dashboard**", timeout=25_000)
            print(f"  → Reached dashboard ✅")
        except PWTimeout:
            if "kite.zerodha.com" in page.url:
                print("  → On Kite ✅")
            else:
                page.wait_for_load_state("networkidle", timeout=10_000)

        screenshot(page, "04_post_login")

        cookies  = ctx.cookies("https://kite.zerodha.com")
        enctoken = next((c["value"] for c in cookies if c["name"] == "enctoken"), None)
        if not enctoken:
            enctoken = page.evaluate("""() => {
                const m = document.cookie.match(/(?:^|; )enctoken=([^;]*)/);
                return m ? decodeURIComponent(m[1]) : null;
            }""")

        browser.close()

        if not enctoken:
            raise RuntimeError("Login appeared to succeed but enctoken not found.")
        print(f"  → enctoken extracted ({len(enctoken)} chars) ✅")
        return enctoken


# ── Morning briefing thread ───────────────────────────────────────────────────

def start_briefing_thread():
    """Start the morning briefing background thread (09:05 / 09:16 / 15:32 IST)."""
    try:
        project_root = str(Path(__file__).parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)

        from backend.notifications.briefing import run_briefing_thread
        t = threading.Thread(target=run_briefing_thread, daemon=True, name="WealthAlgoBriefing")
        t.start()
        print("  → Morning briefing thread started ✅")
    except Exception as e:
        print(f"  ⚠️  Could not start briefing thread: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    validate_env()

    from datetime import datetime, timezone, timedelta
    IST     = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST).strftime("%d %b %Y %H:%M IST")

    print("=" * 60)
    print(f"  WealthAlgo Cloud Launcher — {USER_ID} (port {BOT_PORT})")
    print(f"  Started: {now_ist}")
    print("=" * 60)

    enctoken      = None
    max_attempts  = 3

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"\n{'─' * 60}  LOGIN ATTEMPT {attempt}/{max_attempts}  {'─' * 60}")
            enctoken = login_to_kite()
            break
        except Exception as e:
            print(f"\n  ❌ Attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                wait = attempt * 15
                print(f"  → Retrying in {wait}s...")
                time.sleep(wait)
            else:
                telegram_notify(
                    f"❌ <b>WealthAlgo {USER_ID} — LOGIN FAILED</b>\n"
                    f"📅 {now_ist}\n"
                    f"🔴 All {max_attempts} login attempts failed.\n"
                    f"⚠️ Error: {str(e)[:200]}\n"
                    f"📋 Check GitHub Actions → Artifacts for screenshots."
                )
                print("\n  ❌ All login attempts failed.")
                sys.exit(1)

    save_enctoken(USER_ID, enctoken)

    # Find dashboard script — supports both dashboard.py and frontend/app.py
    for candidate in ["dashboard.py", "frontend/app.py"]:
        bot_script = Path(__file__).parent / candidate
        if bot_script.exists():
            break
    else:
        print("ERROR: No dashboard.py or frontend/app.py found")
        sys.exit(1)

    # ── Start briefing thread BEFORE dashboard subprocess ────────────────────
    print("\n  Starting morning briefing scheduler...")
    start_briefing_thread()

    # ── Notify: bot started ───────────────────────────────────────────────────
    telegram_notify(
        f"✅ <b>WealthAlgo {USER_ID} — BOT STARTED</b>\n"
        f"📅 {now_ist}\n"
        f"🟢 Kite login successful\n"
        f"📊 Trading begins at market open (9:15 AM IST)\n"
        f"📩 Morning briefing scheduled for 9:05 AM IST\n"
        f"⏰ Auto-shutdown at 3:30 PM IST"
    )

    print(f"\n✅ Authentication complete — launching {bot_script.name} on port {BOT_PORT}...\n")
    print("=" * 60)

    env = {**os.environ, "PORT": BOT_PORT, "SHUTDOWN_TIME_IST": "15:35"}
    MAX_RESTARTS  = 3
    restart_count = 0

    while True:
        from datetime import datetime, timezone, timedelta
        IST     = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(IST)

        if now_ist.hour > 15 or (now_ist.hour == 15 and now_ist.minute >= 30):
            print("\n  ⏰ Past 15:30 IST — not restarting bot after exit.")
            break

        print(f"\n  ▶ Starting bot (attempt {restart_count + 1})...")
        result    = subprocess.run(
            [sys.executable, str(bot_script)],
            cwd=str(Path(__file__).parent),
            env=env,
        )
        exit_code = result.returncode

        if exit_code == 0:
            telegram_notify(
                f"⏹️ <b>WealthAlgo {USER_ID} — BOT STOPPED</b>\n"
                f"📅 {datetime.now(IST).strftime('%d %b %Y %H:%M IST')}\n"
                f"✅ Clean exit (market close)"
            )
            break

        restart_count += 1
        print(f"  ❌ Bot crashed (exit {exit_code}), restart {restart_count}/{MAX_RESTARTS}")
        telegram_notify(
            f"⚠️ <b>WealthAlgo {USER_ID} — BOT CRASHED (restart {restart_count}/{MAX_RESTARTS})</b>\n"
            f"📅 {datetime.now(IST).strftime('%d %b %Y %H:%M IST')}\n"
            f"❌ Exit code: {exit_code}"
        )

        if restart_count >= MAX_RESTARTS:
            telegram_notify(
                f"💥 <b>WealthAlgo {USER_ID} — BOT STOPPED (max restarts reached)</b>\n"
                f"📅 {datetime.now(IST).strftime('%d %b %Y %H:%M IST')}\n"
                f"⚠️ Check GitHub Actions logs."
            )
            sys.exit(exit_code)

        print("  🔐 Re-authenticating before restart...")
        try:
            new_token = login_to_kite()
            save_enctoken(USER_ID, new_token)
            print("  ✅ Re-authentication successful")
        except Exception as re_err:
            print(f"  ❌ Re-authentication failed: {re_err}")
            sys.exit(1)

        time.sleep(10)


if __name__ == "__main__":
    main()
