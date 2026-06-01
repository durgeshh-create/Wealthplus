# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    validate_env()

    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(IST)
    now_str = now_ist.strftime("%d %b %Y %H:%M IST")

    # Determine if this is the Morning or Afternoon runner session
    is_afternoon_session = now_ist.hour >= 12

    print("=" * 60)
    print(f"  WealthAlgo Cloud Launcher — {USER_ID} (port {BOT_PORT})")
    print(f"  Started: {now_str}")
    print(f"  Session: {'Afternoon Pickup' if is_afternoon_session else 'Morning Open'}")
    print("=" * 60)

    max_attempts = 3
    enctoken = None

    # ── CRITICAL: Check if existing saved token is still valid BEFORE logging in.
    # ─────────────────────────────────────────────────────────────────────────
    # Zerodha allows only ONE active session per account.  Doing a fresh
    # Playwright login when a valid token already exists creates a NEW session
    # which immediately invalidates the old one — logging you out of Kite in
    # your local browser.
    #
    # Rule: only login when the saved token is actually expired or missing.
    # ─────────────────────────────────────────────────────────────────────────
    print("\n  🔍 Checking existing token validity before login...")
    saved_token_valid = False
    try:
        from backend.auth.token_store import load_token
        import requests as _req
        saved = load_token()
        if saved and saved.get("enctoken"):
            _s = _req.Session()
            _s.headers.update({
                "Authorization": f"enctoken {saved['enctoken']}",
                "User-Agent":    "Mozilla/5.0",
                "Referer":       "https://kite.zerodha.com/",
            })
            _r = _s.get("https://kite.zerodha.com/oms/user/profile", timeout=8)
            if _r.status_code == 200 and _r.json().get("data", {}).get("user_name"):
                enctoken = saved["enctoken"]
                saved_token_valid = True
                print(f"  ✅ Saved token is still valid for {saved.get('user_id')} — skipping Playwright login.")
                print(f"     (No new session created, your Kite browser session is untouched)")
    except Exception as _e:
        print(f"  ⚠️  Token check error: {_e} — will attempt fresh login")

    if not saved_token_valid:
        print("  🔐 Saved token expired or missing — performing fresh TOTP login...")
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
                        f"📅 {now_str}\n"
                        f"🔴 All {max_attempts} login attempts failed.\n"
                        f"⚠️ Error: {str(e)[:200]}\n"
                        f"📋 Check GitHub Actions → Artifacts for screenshots."
                    )
                    telegram_notify(msg)
                    print("\n  ❌ All login attempts failed. Bot cannot start.")
                    sys.exit(1)

    save_enctoken(USER_ID, enctoken)

    bot_script = Path(__file__).parent / "dashboard.py"
    if not bot_script.exists():
        print(f"ERROR: {bot_script} not found")
        sys.exit(1)

    # ── Start briefing thread BEFORE dashboard subprocess ────────────────────
    print("\n  Starting briefing scheduler...")
    start_briefing_thread()

    # ── Notify: bot started successfully (Dynamic message depending on session) ─
    if is_afternoon_session:
        telegram_notify(
            f"✅ <b>WealthAlgo {USER_ID} — AFTERNOON SESSION STARTED</b>\n"
            f"📅 {now_str}\n"
            f"🟢 Kite login successful\n"
            f"🔄 Resuming tracking and execution\n"
            f"📩 EOD summary scheduled for 3:32 PM IST\n"
            f"⏰ Auto-shutdown at 3:30 PM IST"
        )
        # Afternoon session cutoff times
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
        # Morning session cutoff times (to handover to afternoon workflow)
        shutdown_env_time = "12:35"
        restart_cutoff_hour = 12
        restart_cutoff_minute = 25

    print(f"\n✅ Authentication complete — launching bot on port {BOT_PORT}...\n")
    print("=" * 60)

    env = {**os.environ, "PORT": BOT_PORT, "SHUTDOWN_TIME_IST": shutdown_env_time}
    MAX_RESTARTS  = 3    # restart up to 3 times on crash (not on clean exit)
    restart_count = 0

    while True:
        from datetime import datetime, timezone, timedelta
        IST     = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(IST)

        # Dynamic session safety boundary check
        if now_ist.hour > restart_cutoff_hour or (now_ist.hour == restart_cutoff_hour and now_ist.minute >= restart_cutoff_minute):
            print(f"\n  ⏰ Past {restart_cutoff_hour}:{restart_cutoff_minute} IST — not restarting bot after exit.")
            break

        print(f"\n  ▶ Starting bot (attempt {restart_count + 1})...")
        result    = subprocess.run(
            [sys.executable, str(bot_script)],
            cwd=str(Path(__file__).parent),
            env=env,
        )
        exit_code = result.returncode

        if exit_code == 0:
            # Clean shutdown (market close or session split watchdog fired)
            telegram_notify(
                f"⏹️ <b>WealthAlgo {USER_ID} — SESSION COMPLETED</b>\n"
                f"📅 {datetime.now(IST).strftime('%d %b %Y %H:%M IST')}\n"
                f"✅ Clean exit (Scheduled Session Break)"
            )
            break

        # Crash — notify and possibly restart
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

        # Re-authenticate before restart — token may have expired
        print("  🔐 Re-authenticating before restart...")
        try:
            new_token = login_to_kite()
            save_enctoken(USER_ID, new_token)
            print("  ✅ Re-authentication successful")
        except Exception as re_err:
            print(f"  ❌ Re-authentication failed: {re_err}")
            sys.exit(1)

        time.sleep(10)   # brief pause before restart


if __name__ == "__main__":
    main()