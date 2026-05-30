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
    KITE_USER_ID      — Zerodha user ID  (e.g. RD1858)
    KITE_PASSWORD     — Kite login password
    KITE_TOTP_SECRET  — TOTP secret key from your 2FA setup
    BOT_PORT          — Flask port (5000 for RD1858, 5001 for PS5673)
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


def generate_totp() -> str:
    totp = pyotp.TOTP(TOTP_SECRET)
    code = totp.now()
    remaining = 30 - (int(time.time()) % 30)
    if remaining < 5:
        print(f"  → TOTP expires in {remaining}s — waiting for next window...")
        time.sleep(remaining + 1)
        code = pyotp.TOTP(TOTP_SECRET).now()
    return code


def save_enctoken(user_id: str, enctoken: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {"user_id": user_id, "enctoken": enctoken}
    ENCTOKEN_FILE.write_text(json.dumps(data, indent=2))
    print(f"  → Saved enctoken to {ENCTOKEN_FILE}")


def try_selector(page, selectors: list, timeout: int = 5000):
    """Return first matching locator, or None."""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except PWTimeout:
            continue
    return None


# ── Playwright login ──────────────────────────────────────────────────────────

def login_to_kite() -> str:
    """
    Open kite.zerodha.com in a headless Chromium browser, complete the
    2-step login (password + TOTP), and return the enctoken cookie value.
    """
    print("\n[1/3] Launching headless Chrome → kite.zerodha.com ...")

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
        page.goto("https://kite.zerodha.com", wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_load_state("networkidle", timeout=15_000)

        # Step 2: Fill user ID + password
        print(f"[2/3] Filling credentials for {USER_ID}...")

        uid_field = try_selector(page, [
            'input#userid', 'input[name="user_id"]',
            'input[type="text"]', 'input[placeholder*="User"]',
        ])
        if uid_field is None:
            page.screenshot(path="/tmp/kite_debug_userid.png")
            raise RuntimeError("Cannot find user ID field — screenshot saved to /tmp/kite_debug_userid.png")
        uid_field.fill(USER_ID)

        pwd_field = try_selector(page, [
            'input#password', 'input[name="password"]',
            'input[type="password"]',
        ])
        if pwd_field is None:
            raise RuntimeError("Cannot find password field")
        pwd_field.fill(PASSWORD)

        submit = try_selector(page, [
            'button[type="submit"]', 'button.button-orange',
            'button:has-text("Login")',
        ])
        if submit:
            submit.click()
        else:
            page.keyboard.press("Enter")

        # Step 3: Fill TOTP
        print("[3/3] Waiting for TOTP prompt...")
        totp_field = try_selector(page, [
            'input#totp', 'input[name="totp"]',
            'input[label*="TOTP"]', 'input[placeholder*="TOTP"]',
            'input[placeholder*="OTP"]', 'input[type="number"]',
        ], timeout=15_000)

        if totp_field is None:
            page.screenshot(path="/tmp/kite_debug_totp.png")
            raise RuntimeError("TOTP field not found — screenshot saved to /tmp/kite_debug_totp.png")

        otp = generate_totp()
        print(f"  → Generated TOTP: {otp}")
        totp_field.fill(otp)

        submit2 = try_selector(page, [
            'button[type="submit"]', 'button.button-orange',
            'button:has-text("Continue")',
        ])
        if submit2:
            submit2.click()
        else:
            page.keyboard.press("Enter")

        # Wait for dashboard
        print("  → Waiting for Kite dashboard to load...")
        try:
            page.wait_for_url("**/dashboard**", timeout=20_000)
        except PWTimeout:
            page.wait_for_load_state("networkidle", timeout=10_000)

        # Extract enctoken
        cookies = ctx.cookies("https://kite.zerodha.com")
        enctoken = next((c["value"] for c in cookies if c["name"] == "enctoken"), None)

        if not enctoken:
            page.screenshot(path="/tmp/kite_debug_postlogin.png")
            enctoken = page.evaluate("""() => {
                const match = document.cookie.match(/(?:^|; )enctoken=([^;]*)/);
                return match ? decodeURIComponent(match[1]) : null;
            }""")

        browser.close()

        if not enctoken:
            raise RuntimeError(
                "Login appeared to succeed but enctoken not found in cookies. "
                "Check /tmp/kite_debug_postlogin.png for the post-login state."
            )

        print(f"  → enctoken extracted ({len(enctoken)} chars) ✅")
        return enctoken


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    validate_env()

    print("=" * 60)
    print(f"  WealthAlgo Cloud Launcher — {USER_ID} (port {BOT_PORT})")
    print("=" * 60)

    max_attempts = 3
    enctoken = None

    for attempt in range(1, max_attempts + 1):
        try:
            enctoken = login_to_kite()
            break
        except Exception as e:
            print(f"\n  ❌ Login attempt {attempt}/{max_attempts} failed: {e}")
            if attempt < max_attempts:
                wait = attempt * 15
                print(f"  → Retrying in {wait}s...")
                time.sleep(wait)
            else:
                print("\n  ❌ All login attempts failed. Bot cannot start.")
                sys.exit(1)

    save_enctoken(USER_ID, enctoken)

    bot_script = Path(__file__).parent / "dashboard.py"
    if not bot_script.exists():
        print(f"ERROR: {bot_script} not found")
        sys.exit(1)

    print(f"\n✅ Authentication complete — launching bot on port {BOT_PORT}...\n")
    print("=" * 60)

    env = {**os.environ, "PORT": BOT_PORT}
    result = subprocess.run(
        [sys.executable, str(bot_script)],
        cwd=str(Path(__file__).parent),
        env=env,
    )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()