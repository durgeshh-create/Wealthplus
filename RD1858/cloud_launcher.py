#!/usr/bin/env python3
"""
WealthAlgo Cloud Launcher — RD1858
------------------------------------
Automates Kite login via Playwright headless Chrome, writes the enctoken
to config/enctoken.json, then launches dashboard.py.

Usage (GitHub Actions):
    python cloud_launcher.py

Environment variables (set as GitHub Secrets):
    KITE_USER_ID        — Zerodha user ID  (e.g. RD1858)
    KITE_PASSWORD       — Kite login password
    KITE_TOTP_SECRET    — TOTP secret key from your 2FA setup (base32, no spaces)
    BOT_PORT            — Flask port (5001 for RD1858)
    TELEGRAM_BOT_TOKEN  — Telegram bot token for notifications
    TELEGRAM_CHAT_ID    — Your Telegram chat ID

Features:
    - Headless Chrome login with 3-attempt retry
    - Session aware context handling (Morning vs. Afternoon split runner)
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
import urllib.request
from pathlib import Path

import pyotp


# ── Pause flag self-heal ───────────────────────────────────────────────────────
# The frontend no longer has any way to set pause_flag.json to true at all
# (the old PAUSE & STOP / RESUME flow was replaced with a plain Start/Stop
# toggle that never touches this file). _clear_pause_flag() below still runs
# unconditionally at the top of every fresh run as a safety net, in case the
# flag was ever left at paused=true by something else — it's cheap, harmless,
# and guarantees a fresh run can never be blocked by stale state from before.

_GH_OWNER = os.environ.get("GITHUB_REPOSITORY", "/").split("/")[0]
_GH_REPO  = os.environ.get("GITHUB_REPOSITORY", "/").split("/")[-1]
_GH_TOKEN = os.environ.get("GITHUB_TOKEN", "")


def _clear_pause_flag():
    """
    Write paused=false to gh-pages/pause_flag.json.
    Called on every fresh bot startup so a leftover PAUSE state from a previous
    run (e.g. after a RESTART without a prior RESUME) never blocks the new session.
    Retries up to 3× on 409 stale-SHA conflict (same pattern as push_snapshot.py).
    """
    if not _GH_TOKEN or not _GH_OWNER or not _GH_REPO:
        return
    try:
        import base64 as _b64, socket as _sock
        api_url = (
            f"https://api.github.com/repos/{_GH_OWNER}/{_GH_REPO}"
            f"/contents/pause_flag.json"
        )
        headers = {
            "Authorization": f"token {_GH_TOKEN}",
            "Accept":        "application/vnd.github.v3+json",
            "Content-Type":  "application/json",
        }
        content = _b64.b64encode(
            json.dumps({"paused": False, "updated": __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}).encode()
        ).decode()

        for _attempt in range(3):
            # Fetch current SHA
            sha = None
            _sock.setdefaulttimeout(8)
            try:
                req = urllib.request.Request(
                    api_url + "?ref=gh-pages", headers={
                        "Authorization": f"token {_GH_TOKEN}",
                        "Accept": "application/vnd.github.v3+json",
                    }
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    sha = json.loads(r.read()).get("sha")
            except Exception:
                pass
            finally:
                _sock.setdefaulttimeout(None)

            body = {"message": "resume: auto-cleared on bot startup", "content": content, "branch": "gh-pages"}
            if sha:
                body["sha"] = sha

            put_req = urllib.request.Request(
                api_url,
                data=json.dumps(body).encode(),
                headers=headers,
                method="PUT",
            )
            try:
                _sock.setdefaulttimeout(10)
                with urllib.request.urlopen(put_req, timeout=10) as r:
                    r.read()
                print("  → pause_flag.json cleared on gh-pages (paused=false)")
                return
            except urllib.error.HTTPError as e:
                if e.code == 409 and _attempt < 2:
                    time.sleep(0.5 * (_attempt + 1))
                    continue
                raise
            finally:
                _sock.setdefaulttimeout(None)
    except Exception as _e:
        print(f"  ⚠️  Could not clear pause_flag.json: {_e} (non-fatal)")


try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    print("ERROR: playwright not installed. Run: pip install playwright && playwright install chromium")
    sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────[...]

USER_ID     = os.environ.get("KITE_USER_ID", "").strip()
PASSWORD    = os.environ.get("KITE_PASSWORD", "").strip()
TOTP_SECRET = os.environ.get("KITE_TOTP_SECRET", "").strip()
BOT_PORT    = os.environ.get("BOT_PORT", "5000").strip()

CONFIG_DIR     = Path(__file__).parent / "config"
ENCTOKEN_FILE  = CONFIG_DIR / "enctoken.json"
SCREENSHOT_DIR = Path("/tmp")


# ── Telegram notifications ────────────────────────────────────────────────

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


# ── Helpers ─────────────────────────────────────────────────────────────[...]

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
    """Save a debug screenshot — only call on failures to avoid wasting time."""
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


# ── Playwright login ────────────────────────────────────────────────────────–[...]

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

        # Submit
        btn = try_selector(page, ['button[type="submit"]', 'button.button-orange', 'button:has-text("Login")'])
        if btn:
            btn.click()
        else:
            page.keyboard.press("Enter")

        time.sleep(2)

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
        totp_field.fill(generate_totp())

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


# ── Main ────────────────────────────────────────────────────────────–[...]

def main():
    validate_env()

    from datetime import datetime, timezone, timedelta
    IST     = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    now_str = now_ist.strftime("%d %b %Y %H:%M IST")

    # Determine if this runner is dealing with the morning open or afternoon session
    is_afternoon_session = now_ist.hour >= 12

    print("=" * 60)
    print(f"  WealthAlgo Cloud Launcher — {USER_ID} (port {BOT_PORT})")
    print(f"  Started: {now_str}")
    print(f"  Session: {'Afternoon Pickup' if is_afternoon_session else 'Morning Open'}")
    print("=" * 60)

    # ── Always clear any stale pause flag, unconditionally, before anything
    # else. Previously this only ran AFTER a successful login — but the old
    # wait_for_resume() gate (now removed) blocked execution BEFORE that
    # point whenever the flag was stuck at paused=true, so the self-heal
    # could never actually fire in the one scenario it existed to fix. The
    # frontend no longer ever writes paused=true at all, but clearing here
    # unconditionally means a stale flag from any other source can never
    # block a fresh run, by construction.
    print("  → Clearing pause_flag.json on startup...")
    _clear_pause_flag()

    enctoken      = None
    max_attempts  = 3

    # ── CRITICAL: Check saved token BEFORE doing a Playwright login ───────────
    # A fresh login creates a NEW Zerodha session, immediately invalidating the
    # old one and logging you out of Kite in your local browser.
    # Only perform a fresh login when the saved token is actually expired.
    #
    # ON GITHUB ACTIONS: skip the HTTP validity check entirely — network calls to
    # kite.zerodha.com from GH Actions runners hang silently (TCP SYN dropped,
    # no RST) even with connect timeouts. Instead, just load the cached token and
    # use it directly. If it's expired, the first real trading API call will get
    # a 403 and trigger a fresh TOTP login automatically.
    print("\n  🔍 Checking existing token validity before login...")
    saved_token_valid = False
    _on_gha = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
    try:
        from backend.auth.token_store import load_token
        saved = load_token()
        if saved and saved.get("enctoken"):
            if _on_gha:
                # On GH Actions: trust the cached token without a network round-trip.
                # Avoids the silent TCP-connect hang to kite.zerodha.com.
                enctoken = saved["enctoken"]
                saved_token_valid = True
                print(f"  ✅ Using cached token for {saved.get('user_id')} (GH Actions — skipping network check)")
            else:
                import requests as _req
                _s = _req.Session()
                _s.headers.update({
                    "Authorization": f"enctoken {saved['enctoken']}",
                    "User-Agent":    "Mozilla/5.0",
                    "Referer":       "https://kite.zerodha.com/",
                })
                for _attempt in range(2):
                    try:
                        _r = _s.get(
                            "https://kite.zerodha.com/oms/user/profile",
                            timeout=(4, 6),
                            allow_redirects=False,
                        )
                        if _r.status_code == 200 and _r.json().get("data", {}).get("user_name"):
                            enctoken = saved["enctoken"]
                            saved_token_valid = True
                            print(f"  ✅ Saved token valid for {saved.get('user_id')} — skipping login (browser session untouched)")
                        else:
                            print(f"  ℹ Saved token check: HTTP {_r.status_code} — will do fresh login")
                        break
                    except (_req.exceptions.Timeout, _req.exceptions.ConnectionError) as _te:
                        if _attempt == 0:
                            print(f"  ⚠️  Token check timeout (attempt 1) — retrying once...")
                            time.sleep(1)
                        else:
                            print(f"  ⚠️  Token check failed after retry ({type(_te).__name__}) — proceeding to fresh login")
    except Exception as _e:
        print(f"  ⚠️  Token check error: {_e} — will attempt fresh login")

    if not saved_token_valid:
        print("  🔐 Saved token expired — performing fresh TOTP login...")
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
                        f"📅 {now_str}\n"
                        f"🔴 All {max_attempts} login attempts failed.\n"
                        f"⚠️ Error: {str(e)[:200]}\n"
                        f"📋 Check GitHub Actions → Artifacts for screenshots."
                    )
                    print("\n  ❌ All login attempts failed.")
                    sys.exit(1)

    save_enctoken(USER_ID, enctoken)

    # ── Write credentials.json so dashboard.py's AuthManager can do a fresh
    # TOTP login if the saved enctoken is stale after a subprocess restart.
    # On GH Actions the env vars are always set; locally this is a no-op if
    # credentials.json already exists with the correct values.
    try:
        import re as _re
        if USER_ID and PASSWORD and TOTP_SECRET and _re.match(r'^[A-Z2-7]+=*$', TOTP_SECRET.upper()):
            _cred_file = CONFIG_DIR / "credentials.json"
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            _cred_file.write_text(json.dumps({
                "user_id":  USER_ID,
                "password": PASSWORD,
                "totp_key": TOTP_SECRET,
            }, indent=2))
            print(f"  → credentials.json written for dashboard subprocess")
    except Exception as _ce:
        print(f"  ⚠️  Could not write credentials.json: {_ce}")

    # Find dashboard script — supports both dashboard.py and frontend/app.py
    for candidate in ["dashboard.py", "frontend/app.py"]:
        bot_script = Path(__file__).parent / candidate
        if bot_script.exists():
            break
    else:
        print("ERROR: No dashboard.py or frontend/app.py found")
        sys.exit(1)

    # ── Start briefing thread BEFORE dashboard subprocess ────────────────────
    print("\n  Starting briefing scheduler...")
    start_briefing_thread()

    # NOTE: snapshot writer is started inside dashboard.py (the subprocess) where
    # portfolio_tracker, realtime_manager, historical_manager and order_manager
    # are all available. Starting it here would use an empty dict and write useless data.

    # ── Notify: bot started with dynamic session info ────────────────────────
    if is_afternoon_session:
        telegram_notify(
            f"✅ <b>WealthAlgo {USER_ID} — AFTERNOON SESSION STARTED</b>\n"
            f"📅 {now_str}\n"
            f"🟢 Kite login successful\n"
            f"🔄 Resuming tracking and execution matrix\n"
            f"📩 EOD summary scheduled for 3:32 PM IST\n"
            f"⏰ Auto-shutdown at 3:30 PM IST"
        )
        shutdown_env_time = "15:35"
        restart_cutoff_hour = 15
        restart_cutoff_minute = 30
    else:
        telegram_notify(
            f"✅ <b>WealthAlgo {USER_ID} — MORNING SESSION STARTED</b>\n"
            f"📅 {now_str}\n"
            f"🟢 Kite login successful\n"
            f"📊 Trading begins at market open (9:15 AM IST)\n"
            f"📩 Morning briefing scheduled for 9:05 AM IST\n"
            f"⏰ Session handover scheduled for 12:30 PM IST"
        )
        shutdown_env_time = "12:35"
        restart_cutoff_hour = 12
        restart_cutoff_minute = 30  # ✅ FIXED: was 25, now 30 to match 12:30 PM cutoff

    print(f"\n✅ Authentication complete — launching {bot_script.name} on port {BOT_PORT}...\n")
    print("=" * 60)

    env = {**os.environ, "PORT": BOT_PORT, "SHUTDOWN_TIME_IST": shutdown_env_time}
    MAX_RESTARTS  = 3
    restart_count = 0

    while True:
        from datetime import datetime, timezone, timedelta
        IST     = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(IST)

        # Dynamic validation check based on the current run segment boundaries
        if now_ist.hour > restart_cutoff_hour or (now_ist.hour == restart_cutoff_hour and now_ist.minute >= restart_cutoff_minute):
            print(f"\n  ⏰ Past {restart_cutoff_hour}:{restart_cutoff_minute} IST — not restarting bot after exit.")
            break

        print(f"\n  ▶ Starting bot (attempt {restart_count + 1})...")
        result = subprocess.run(
            [sys.executable, str(bot_script)],
            cwd=str(Path(__file__).parent),
            env=env,
        )
        exit_code = result.returncode

        if exit_code == 0:
            telegram_notify(
                f"⏹️ <b>WealthAlgo {USER_ID} — SESSION COMPLETED</b>\n"
                f"📅 {datetime.now(IST).strftime('%d %b %Y %H:%M IST')}\n"
                f"✅ Clean exit (Scheduled Session Break)"
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

        print("  🔐 Re-checking token before restart...")

        # ── CRITICAL: try saved token first — same as initial startup ─────────
        # A fresh TOTP login creates a NEW Zerodha session and immediately
        # invalidates the user's browser session. Only do it if the current
        # token is genuinely expired.
        reauth_ok = False
        try:
            from backend.auth.token_store import load_token
            saved = load_token()
            if saved and saved.get("enctoken"):
                if _on_gha:
                    print(f"  ✅ Using cached token (GH Actions — skipping network re-check)")
                    reauth_ok = True
                else:
                    import requests as _req
                    _s = _req.Session()
                    _s.headers.update({
                        "Authorization": f"enctoken {saved['enctoken']}",
                        "User-Agent":    "Mozilla/5.0",
                        "Referer":       "https://kite.zerodha.com/",
                    })
                    for _attempt in range(2):
                        try:
                            _r = _s.get(
                                "https://kite.zerodha.com/oms/user/profile",
                                timeout=(4, 6),
                                allow_redirects=False,
                            )
                            if _r.status_code == 200 and _r.json().get("data", {}).get("user_name"):
                                print(f"  ✅ Saved token still valid — skipping login (browser session untouched)")
                                reauth_ok = True
                            break
                        except (_req.exceptions.Timeout, _req.exceptions.ConnectionError):
                            if _attempt == 0:
                                time.sleep(1)
        except Exception as _ce:
            print(f"  ⚠️  Token re-check error: {_ce}")

        if not reauth_ok:
            print("  🔐 Token expired — performing fresh TOTP login...")
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
