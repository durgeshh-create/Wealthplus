"""
WealthAlgo Morning Briefing
----------------------------
Runs in a background thread inside cloud_launcher.py.

Timeline (all IST):
  09:05 — Pre-market briefing: watchlist + yesterday's close + W%%R readings
  09:16 — Opening prices: live quotes fetched via Zerodha REST after market opens
  11:00 — Heartbeat ping: mid-morning alive check
  13:30 — Heartbeat ping: afternoon alive check (post session-handover)
  15:32 — End-of-day summary: trades done today, positions held, cash deployed

The thread is started BEFORE dashboard.py is launched so it's alive for the
full session.  It never raises — any error is silently logged to stdout.
"""
import os
import sys
import json
import time
import threading
import urllib.request
import urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

# ── paths (resolved relative to this file: backend/notifications/briefing.py)
_BOT_DIR     = Path(__file__).resolve().parent.parent.parent   # e.g. RD1858/
_SETTINGS    = _BOT_DIR / "config" / "settings.json"
_ENCTOKEN    = _BOT_DIR / "config" / "enctoken.json"
_DAILY_DIR   = _BOT_DIR / "data" / "daily"

ZERODHA_API  = "https://kite.zerodha.com"


# ── helpers ───────────────────────────────────────────────────────────────────

def _account() -> str:
    return os.environ.get("KITE_USER_ID", "BOT").strip()


def _send(text: str):
    """Fire-and-forget Telegram send — same implementation as utils/telegram.py
    but self-contained so briefing.py has zero import dependencies at startup."""
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
    except Exception:
        pass


def _load_settings() -> dict:
    try:
        if _SETTINGS.exists():
            return json.loads(_SETTINGS.read_text())
    except Exception:
        pass
    return {}


def _load_enctoken() -> Optional[str]:
    try:
        if _ENCTOKEN.exists():
            data = json.loads(_ENCTOKEN.read_text())
            return data.get("enctoken")
    except Exception:
        pass
    return None


def _wait_until_ist(hour: int, minute: int, second: int = 0):
    """Block until HH:MM:SS IST today.  Returns immediately if already past."""
    now    = datetime.now(IST)
    target = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
    delta  = (target - now).total_seconds()
    if delta > 0:
        time.sleep(delta)


# ── W%%R calculation (mirrors backend/indicators/calculator.py) ───────────────

def _calc_wr(csv_path: Path, period: int = 14) -> Optional[float]:
    """
    Calculate Williams %%R from the local daily CSV (yesterday's close as last candle).
    Returns None if data is insufficient.
    """
    try:
        import csv as _csv
        rows = []
        with open(csv_path, newline="") as f:
            reader = _csv.DictReader(f)
            for row in reader:
                try:
                    rows.append({
                        "high":  float(row.get("high", 0) or 0),
                        "low":   float(row.get("low",  0) or 0),
                        "close": float(row.get("close", 0) or 0),
                    })
                except (ValueError, TypeError):
                    pass

        if len(rows) < period:
            return None

        window      = rows[-period:]
        highest_h   = max(r["high"]  for r in window)
        lowest_l    = min(r["low"]   for r in window)
        last_close  = rows[-1]["close"]

        if highest_h == lowest_l:
            return -50.0

        wr = ((highest_h - last_close) / (highest_h - lowest_l)) * -100
        return round(max(-100.0, min(0.0, wr)), 1)

    except Exception:
        return None


def _prev_close(csv_path: Path) -> Optional[float]:
    """Return the last close price from the daily CSV."""
    try:
        import csv as _csv
        last = None
        with open(csv_path, newline="") as f:
            for row in _csv.DictReader(f):
                try:
                    v = float(row.get("close", 0) or 0)
                    if v > 0:
                        last = v
                except (ValueError, TypeError):
                    pass
        return last
    except Exception:
        return None


# ── 09:05 Pre-market briefing ─────────────────────────────────────────────────

def send_premarket_briefing():
    """Send watchlist + yesterday's W%%R + prev-close prices."""
    try:
        settings   = _load_settings()
        symbols    = settings.get("active_etfs", [])
        bnh_syms   = settings.get("bnh_symbols", [])
        wr_period  = int(settings.get("williams_r_period", 14))
        wr_thresh  = float(settings.get("williams_r_threshold", -80))
        all_syms   = list(dict.fromkeys(symbols + bnh_syms))   # dedup, preserve order
        acct       = _account()
        date_str   = datetime.now(IST).strftime("%a %d %b %Y")

        if not all_syms:
            _send(
                f"🌅 <b>WealthAlgo {acct} — Morning Briefing</b>\n"
                f"📅 {date_str}\n\n"
                f"⚠️ No symbols configured in settings.json"
            )
            return

        rows           = []
        oversold_syms  = []
        near_syms      = []        # W%%R between -60 and -80 (watch zone)

        for sym in all_syms:
            csv_path = _DAILY_DIR / f"{sym}.csv"
            wr    = _calc_wr(csv_path, period=wr_period) if csv_path.exists() else None
            close = _prev_close(csv_path)                if csv_path.exists() else None

            if wr is not None and wr <= wr_thresh:
                status    = "🔴 OVERSOLD"
                oversold_syms.append(sym)
            elif wr is not None and wr <= -60:
                status    = "🟡 Watch"
                near_syms.append(sym)
            elif wr is not None:
                status    = "⚪ Neutral"
            else:
                status    = "❓ No data"

            wr_str    = f"{wr:.1f}" if wr is not None else "N/A"
            close_str = f"₹{close:,.2f}" if close else "N/A"
            rows.append(f"  <b>{sym}</b>  {close_str}  W%%R {wr_str}  {status}")

        rows_text = "\n".join(rows)

        signal_section = ""
        if oversold_syms:
            signal_section = (
                f"\n\n🔴 <b>BUY signals active</b> (W%%R ≤ {wr_thresh:.0f}):\n"
                + "\n".join(f"  • {s}" for s in oversold_syms)
            )
        if near_syms:
            signal_section += (
                f"\n🟡 <b>Watch zone</b> (W%%R −60 to {wr_thresh:.0f}):\n"
                + "\n".join(f"  • {s}" for s in near_syms)
            )

        msg = (
            f"🌅 <b>WealthAlgo {acct} — Morning Briefing</b>\n"
            f"📅 {date_str} | W%%R period: {wr_period}d | Threshold: {wr_thresh:.0f}\n\n"
            f"<b>Watchlist ({len(all_syms)} symbols) — yesterday's close:</b>\n"
            f"{rows_text}"
            f"{signal_section}\n\n"
            f"⏰ Market opens 09:15 IST — opening prices to follow"
        )
        _send(msg)
        print(f"[briefing] ✅ Pre-market briefing sent ({len(all_syms)} symbols)", flush=True)

    except Exception as e:
        print(f"[briefing] ⚠️ Pre-market briefing failed: {e}", flush=True)


# ── 09:16 Opening prices ──────────────────────────────────────────────────────

def send_opening_prices():
    """Fetch live opening prices via Zerodha REST and send them."""
    try:
        enctoken = _load_enctoken()
        if not enctoken:
            print("[briefing] ⚠️ No enctoken for opening prices fetch", flush=True)
            return

        settings   = _load_settings()
        symbols    = settings.get("active_etfs", [])
        bnh_syms   = settings.get("bnh_symbols", [])
        all_syms   = list(dict.fromkeys(symbols + bnh_syms))
        acct       = _account()

        if not all_syms:
            return

        # Build instrument keys: NSE:SYMBOL for each
        inst_keys = [f"NSE:{s}" for s in all_syms]
        params    = urllib.parse.urlencode([("i", k) for k in inst_keys])

        req = urllib.request.Request(
            f"{ZERODHA_API}/oms/quote?{params}",
            headers={
                "Authorization": f"enctoken {enctoken}",

            },
            method="GET",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read()).get("data", {})

        rows   = []
        movers = []   # symbols up/down > 1%%

        for sym in all_syms:
            key  = f"NSE:{sym}"
            q    = data.get(key, {})
            ltp  = q.get("last_price")
            ohlc = q.get("ohlc", {})
            open_p = ohlc.get("open")
            prev_c = ohlc.get("close")   # yesterday's close from Zerodha

            if open_p and prev_c and prev_c > 0:
                chg_pct = (open_p - prev_c) / prev_c * 100
                arrow   = "▲" if chg_pct >= 0 else "▼"
                sign    = "+" if chg_pct >= 0 else ""
                chg_str = f"{sign}{chg_pct:.2f}%% {arrow}"
                if abs(chg_pct) >= 1:
                    movers.append((sym, chg_pct, open_p))
            elif open_p:
                chg_str = "N/A"
            else:
                chg_str = "—"

            open_str = f"₹{open_p:,.2f}" if open_p else "N/A"
            rows.append(f"  <b>{sym}</b>  {open_str}  {chg_str}")

        rows_text = "\n".join(rows)

        movers_section = ""
        if movers:
            movers.sort(key=lambda x: abs(x[1]), reverse=True)
            movers_section = "\n\n📌 <b>Notable opens (>1%% move):</b>\n"
            for sym, chg, price in movers[:5]:
                sign = "+" if chg >= 0 else ""
                movers_section += f"  {sym}: ₹{price:,.2f}  ({sign}{chg:.2f}%%)\n"

        msg = (
            f"📊 <b>Opening Prices — {acct}</b>\n"
            f"⏱ {datetime.now(IST).strftime('%H:%M IST')}\n\n"
            f"{rows_text}"
            f"{movers_section}"
        )
        _send(msg)
        print(f"[briefing] ✅ Opening prices sent ({len(all_syms)} symbols)", flush=True)

    except Exception as e:
        print(f"[briefing] ⚠️ Opening prices failed: {e}", flush=True)



# ── Heartbeat pings ───────────────────────────────────────────────────────────

def send_heartbeat(label: str):
    """Send a brief 'still alive' ping at scheduled intervals."""
    try:
        acct = _account()
        now  = datetime.now(IST)

        # Count today's trades from log for a live status line
        log_file = _BOT_DIR / "logs" / "trading.log"
        today    = now.strftime("%Y-%m-%d")
        buys_today, sells_today = 0, 0

        if log_file.exists():
            for line in log_file.read_text(errors="replace").splitlines():
                if today not in line:
                    continue
                if "✓ BUY SUCCESS" in line or "BUY EXECUTED" in line:
                    buys_today += 1
                elif "✓ SELL SUCCESS" in line or "SELL EXECUTED" in line:
                    sells_today += 1

        trade_str = (
            f"📈 Trades so far: {buys_today} BUY, {sells_today} SELL"
            if (buys_today or sells_today)
            else "📋 No trades executed yet today"
        )

        msg = (
            f"💓 <b>WealthAlgo {acct} — Heartbeat</b>\n"
            f"⏱ {now.strftime('%H:%M IST')} | {label}\n"
            f"🟢 Bot running normally\n"
            f"{trade_str}"
        )
        _send(msg)
        print(f"[briefing] 💓 Heartbeat sent ({label})", flush=True)

    except Exception as e:
        print(f"[briefing] ⚠️ Heartbeat failed ({label}): {e}", flush=True)


# ── 15:32 End-of-day summary ──────────────────────────────────────────────────

def send_eod_summary():
    """
    Send an end-of-day summary.  Reads the trade log from the bot's log file.
    Falls back gracefully if parsing fails.
    """
    try:
        acct     = _account()
        settings = _load_settings()
        today    = datetime.now(IST).strftime("%Y-%m-%d")

        # Try to parse today's trades from trading.log
        log_file  = _BOT_DIR / "logs" / "trading.log"
        buys, sells = [], []

        if log_file.exists():
            for line in log_file.read_text(errors="replace").splitlines():
                if today not in line:
                    continue
                if "✓ BUY SUCCESS" in line or "BUY EXECUTED" in line:
                    buys.append(line.strip())
                elif "✓ SELL SUCCESS" in line or "SELL EXECUTED" in line:
                    sells.append(line.strip())

        trade_lines = ""
        if buys or sells:
            trade_lines = "\n\n<b>Today's trades:</b>"
            for l in sells[:10]:
                trade_lines += f"\n  💰 SELL — {l[-80:]}"
            for l in buys[:10]:
                trade_lines += f"\n  📈 BUY  — {l[-80:]}"
        else:
            trade_lines = "\n\nNo trades executed today."

        msg = (
            f"🔔 <b>Market Closed — {acct}</b>\n"
            f"📅 {datetime.now(IST).strftime('%a %d %b %Y')}\n"
            f"🔴 NSE closed 15:30 IST"
            f"{trade_lines}\n\n"
            f"⏹️ Bot will shut down at end of GitHub Actions session"
        )
        _send(msg)
        print("[briefing] ✅ EOD summary sent", flush=True)

    except Exception as e:
        print(f"[briefing] ⚠️ EOD summary failed: {e}", flush=True)


# ── Main thread entry ─────────────────────────────────────────────────────────

def run_briefing_thread():
    """
    Entry point — call this from cloud_launcher.py in a daemon thread.

        import threading
        from backend.notifications.briefing import run_briefing_thread
        t = threading.Thread(target=run_briefing_thread, daemon=True)
        t.start()
    """
    try:
        now = datetime.now(IST)
        print(
            f"[briefing] Thread started at {now.strftime('%H:%M IST')} — "
            f"waiting for 09:05",
            flush=True,
        )

        _wait_until_ist(9, 5)
        send_premarket_briefing()

        _wait_until_ist(9, 16)
        send_opening_prices()

        _wait_until_ist(11, 0)
        send_heartbeat("Mid-morning check")

        _wait_until_ist(13, 30)
        send_heartbeat("Afternoon check")

        _wait_until_ist(15, 32)
        send_eod_summary()

        print("[briefing] Thread complete — all scheduled messages sent", flush=True)

    except Exception as e:
        print(f"[briefing] ⚠️ Thread crashed: {e}", flush=True)
