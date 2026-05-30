#!/usr/bin/env python3
"""
WealthAlgo Cloud Launcher
--------------------------
Automates Kite login via Playwright headless Chrome, writes the enctoken
to config/enctoken.json, then launches dashboard.py — no browser window,
no CDP extraction, no proxy needed.

Usage (GitHub Actions):
    python cloud_launcher.py

Environment variables (set as GitHub Secrets):
    KITE_USER_ID        — Zerodha user ID  (e.g. RD1858)
    KITE_PASSWORD       — Kite login password
    KITE_TOTP_SECRET    — TOTP secret key from your 2FA setup (base32, no spaces)
    BOT_PORT            — Flask port (5000 for RD1858, 5001 for PS5673)
    TELEGRAM_BOT_TOKEN  — (optional) Telegram bot token for notifications
    TELEGRAM_CHAT_ID    — (optional) Your Telegram chat ID
"""

import os
import sys
import json
import time
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
BOT_PORT    = os.environ.get("BOT_PORT", "5000").strip()

CONFIG_DIR    = Path(__file__).parent / "config"
ENCTOKEN_FILE = CONFIG_DIR / "enctoken.json"

# All debug screenshots go here — uploaded as GitHub Actions artifacts on failure
SCREENSHOT_DIR = Path("/tmp")


# ── Telegram notifications ────────────────────────────────────────────────────

def telegram_notify(message: str):
    """
    Send a Telegram message. Silently skips if credentials are not set.
    Never raises — notifications must never crash the trading bot.
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        import urllib.request
        payload = json.dumps({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def validate_env():
    missing = [k for k, v in {
        "KITE_USER_ID": USER_ID,
        "KITE_PASSWORD": PASSWORD,
        "KITE_TOTP_SECRET": TOTP_SECRET,
    }.items() if not v]
    if missing:
        print(f"ERROR: Missing environment variables: {', '.join(missing)}")
        sys.exit(1)

    # Validate TOTP secret is valid base32
    try:
        totp = pyotp.TOTP(TOTP_SECRET)
        code = totp.now()
        print(f"  → TOTP secret valid — test code: {code}")
    except Exception as e:
        print(f"ERROR: KITE_TOTP_SECRET is invalid: {e}")
        print("  → It must be a base32 string like: JBSWY3DPEHPK3PXP")
        print("  → Remove any spaces from the secret if present")
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
    data = {"user_id": user_id, "enctoken": enctoken}
    ENCTOKEN_FILE.write_text(json.dumps(data, indent=2))
    print(f"  → Saved enctoken to {ENCTOKEN_FILE}")


def screenshot(page, name: str):
    """Save a debug screenshot. Always runs — shows exactly what Kite displayed."""
    path = SCREENSHOT_DIR / f"kite_debug_{name}.png"
    try:
        page.screenshot(path=str(path))
        print(f"  📸 Screenshot: {path}")
    except Exception as e:
        print(f"  ⚠️  Screenshot failed ({name}): {e}")


def try_selector(page, selectors: list, timeout: int = 5000):
    """Return first matching locator, or None."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            loc.wait_for(state="visible", timeout=timeout)
            print(f"  → Found selector: {sel}")
            return loc
        except PWTimeout:
            print(f"  → Not found: {sel}")
            continue
    return None


# ── Playwright login ──────────────────────────────────────────────────────────

def login_to_kite() -> str:
    """
    Open kite.zerodha.com in a headless Chromium browser, complete the
    2-step login (password + TOTP), and return the enctoken cookie value.
    """
    print("\n[1/4] Launching headless Chrome → kite.zerodha.com ...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
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

        # Step 1: Load login page
        print("[1/4] Loading kite.zerodha.com ...")
        page.goto("https://kite.zerodha.com", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=15_000)
        screenshot(page, "01_login_page")
        print(f"  → Page title: {page.title()}")
        print(f"  → Page URL:   {page.url}")

        # Step 2: Fill user ID
        print(f"\n[2/4] Filling user ID ({USER_ID})...")
        uid_field = try_selector(page, [
            'input#userid',
            'input[name="user_id"]',
            'input[type="text"]',
            'input[placeholder*="User"]',
            'input[placeholder*="user"]',
        ])
        if uid_field is None:
            screenshot(page, "02_userid_field_not_found")
            raise RuntimeError(
                f"Cannot find user ID field on page: {page.url}\n"
                f"Page title: {page.title()}\n"
                f"Check screenshot: kite_debug_02_userid_field_not_found.png"
            )
        uid_field.fill(USER_ID)

        # Step 3: Fill password
        print(f"[2/4] Filling password...")
        pwd_field = try_selector(page, [
            'input#password',
            'input[name="password"]',
            'input[type="password"]',
        ])
        if pwd_field is None:
            screenshot(page, "02_password_field_not_found")
            raise RuntimeError("Cannot find password field")
        pwd_field.fill(PASSWORD)
        screenshot(page, "02_credentials_filled")

        # Click Login
        print("[2/4] Clicking login button...")
        submit = try_selector(page, [
            'button[type="submit"]',
            'button.button-orange',
            'button:has-text("Login")',
        ])
        if submit:
            submit.click()
        else:
            page.keyboard.press("Enter")

        # Wait briefly for page to react
        time.sleep(2)
        screenshot(page, "03_after_login_click")
        print(f"  → URL after login click: {page.url}")

        # Step 4: Fill TOTP
        print("\n[3/4] Waiting for TOTP prompt...")
        totp_field = try_selector(page, [
            'input#totp',
            'input[name="totp"]',
            'input[label*="TOTP"]',
            'input[placeholder*="TOTP"]',
            'input[placeholder*="OTP"]',
            'input[placeholder*="otp"]',
            'input[type="number"]',
            'input[maxlength="6"]',
        ], timeout=15_000)

        if totp_field is None:
            screenshot(page, "03_totp_field_not_found")
            # Check for error messages on the page
            page_text = page.inner_text("body")
            print(f"  → Page text snippet: {page_text[:500]}")
            raise RuntimeError(
                f"TOTP field not found.\n"
                f"URL: {page.url}\n"
                f"This usually means the password was wrong or login was blocked.\n"
                f"Check screenshot: kite_debug_03_totp_field_not_found.png"
            )

        screenshot(page, "03_totp_page")
        otp = generate_totp()
        totp_field.fill(otp)
        screenshot(page, "03_totp_filled")

        # Submit TOTP
        print("[3/4] Submitting TOTP...")
        submit2 = try_selector(page, [
            'button[type="submit"]',
            'button.button-orange',
            'button:has-text("Continue")',
        ])
        if submit2:
            submit2.click()
        else:
            page.keyboard.press("Enter")

        # Step 5: Wait for dashboard
        print("\n[4/4] Waiting for Kite dashboard...")
        try:
            page.wait_for_url("**/dashboard**", timeout=20_000)
            print(f"  → Reached dashboard: {page.url}")
        except PWTimeout:
            page.wait_for_load_state("networkidle", timeout=10_000)
            print(f"  → Post-TOTP URL: {page.url}")

        screenshot(page, "04_post_login")

        # Extract enctoken
        cookies = ctx.cookies("https://kite.zerodha.com")
        print(f"  → Cookies found: {[c['name'] for c in cookies]}")
        enctoken = next((c["value"] for c in cookies if c["name"] == "enctoken"), None)

        if not enctoken:
            enctoken = page.evaluate("""() => {
                const match = document.cookie.match(/(?:^|; )enctoken=([^;]*)/);
                return match ? decodeURIComponent(match[1]) : null;
            }""")

        browser.close()

        if not enctoken:
            raise RuntimeError(
                "Login appeared to succeed but enctoken not found in cookies.\n"
                "Check screenshot: kite_debug_04_post_login.png"
            )

        print(f"  → enctoken extracted ({len(enctoken)} chars) ✅")
        return enctoken


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    validate_env()

    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST).strftime("%d %b %Y %H:%M IST")

    print("=" * 60)
    print(f"  WealthAlgo Cloud Launcher — {USER_ID} (port {BOT_PORT})")
    print(f"  Started: {now_ist}")
    print("=" * 60)

    max_attempts = 3
    enctoken = None

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"\n{'─' * 60}")
            print(f"  LOGIN ATTEMPT {attempt}/{max_attempts}")
            print(f"{'─' * 60}")
            enctoken = login_to_kite()
            break
        except Exception as e:
            print(f"\n  ❌ Attempt {attempt}/{max_attempts} failed:")
            print(f"     {e}")
            if attempt < max_attempts:
                wait = attempt * 15
                print(f"  → Retrying in {wait}s...")
                time.sleep(wait)
            else:
                msg = (
                    f"❌ <b>WealthAlgo {USER_ID} — LOGIN FAILED</b>\n"
                    f"📅 {now_ist}\n"
                    f"🔴 All {max_attempts} login attempts failed.\n"
                    f"⚠️ Error: {str(e)[:200]}\n"
                    f"📋 Check GitHub Actions → Artifacts for screenshots."
                )
                telegram_notify(msg)
                print("\n  ❌ All login attempts failed. Bot cannot start.")
                print("  → Check the uploaded screenshots in GitHub Actions Artifacts")
                print("  → Go to: Actions → your run → scroll down → Artifacts section")
                sys.exit(1)

    save_enctoken(USER_ID, enctoken)

    bot_script = Path(__file__).parent / "dashboard.py"
    if not bot_script.exists():
        print(f"ERROR: {bot_script} not found")
        sys.exit(1)

    # ── Notify: bot started successfully ─────────────────────────────────────
    telegram_notify(
        f"✅ <b>WealthAlgo {USER_ID} — BOT STARTED</b>\n"
        f"📅 {now_ist}\n"
        f"🟢 Kite login successful\n"
        f"📊 Trading begins at market open (9:15 AM IST)\n"
        f"⏰ Auto-shutdown at 3:30 PM IST"
    )

    print(f"\n✅ Authentication complete — launching bot on port {BOT_PORT}...\n")
    print("=" * 60)

    env = {**os.environ, "PORT": BOT_PORT}
    result = subprocess.run(
        [sys.executable, str(bot_script)],
        cwd=str(Path(__file__).parent),
        env=env,
    )

    # ── Notify: bot exited ────────────────────────────────────────────────────
    exit_code = result.returncode
    if exit_code == 0:
        telegram_notify(
            f"⏹️ <b>WealthAlgo {USER_ID} — BOT STOPPED</b>\n"
            f"📅 {datetime.now(IST).strftime('%d %b %Y %H:%M IST')}\n"
            f"✅ Clean exit (market close)"
        )
    else:
        telegram_notify(
            f"💥 <b>WealthAlgo {USER_ID} — BOT CRASHED</b>\n"
            f"📅 {datetime.now(IST).strftime('%d %b %Y %H:%M IST')}\n"
            f"❌ Exit code: {exit_code}\n"
            f"⚠️ Check GitHub Actions logs for details."
        )

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
