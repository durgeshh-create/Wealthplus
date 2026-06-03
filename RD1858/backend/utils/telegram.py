"""
WealthAlgo Telegram notification utility.

Centralised, fire-and-forget — safe to call from any thread or module.
Never raises; a failed send is silently swallowed so the trading bot
is never disrupted by a Telegram outage.

Usage:
    from backend.utils.telegram import send_message, notify_buy, notify_sell
"""
import os
import json
import urllib.request
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> str:
    return datetime.now(IST).strftime("%H:%M:%S IST")


def _account() -> str:
    return os.environ.get("KITE_USER_ID", "BOT").strip()


def _credentials():
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return token, chat_id


# ── Deduplication guard: same message cannot fire more than once per 60s ──────
import time as _time
_last_sent: dict = {}   # text_hash → timestamp
_DEDUP_WINDOW = 60      # seconds

def send_message(text: str, parse_mode: str = "HTML", _bypass_dedup: bool = False) -> bool:
    """
    Send a plain Telegram message.
    Deduplicates: identical message text is suppressed if sent within 60s.
    Pass _bypass_dedup=True for trade notifications that must always fire.
    Returns True on success, False otherwise (including duplicate suppression).
    Never raises.
    """
    token, chat_id = _credentials()
    if not token or not chat_id:
        return False

    if not _bypass_dedup:
        # Use full text as dedup key (not just first 120 chars)
        key = text
        now = _time.time()
        if now - _last_sent.get(key, 0) < _DEDUP_WINDOW:
            return False   # suppressed duplicate
        _last_sent[key] = now

        # Prune old entries to avoid unbounded growth
        if len(_last_sent) > 200:
            cutoff = now - _DEDUP_WINDOW * 2
            for k in [k for k, t in _last_sent.items() if t < cutoff]:
                _last_sent.pop(k, None)

    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": parse_mode,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8)
        return True
    except Exception:
        return False


def notify_buy(
    symbol:            str,
    qty:               int,
    price:             float,
    value:             float,
    williams_r:        float  = None,
    profit_target_pct: float  = None,
    slot:              int    = None,
    max_slots:         int    = None,
    dry_run:           bool   = False,
):
    """Send a BUY execution notification. Silently skips LIQUIDCASE."""
    if symbol == "LIQUIDCASE":
        return
    acct = _account()
    wr_line     = f"\n📊 W%%R: {williams_r:.1f} (oversold)" if williams_r is not None else ""
    target_line = ""
    if profit_target_pct:
        target_price = round(price * (1 + profit_target_pct / 100), 2)
        target_line  = f"\n🎯 Target: +{profit_target_pct}%% → ₹{target_price:,.2f}"
    slot_line = f"  [slot {slot}/{max_slots}]" if slot and max_slots else ""
    dry_tag   = " <i>(DRY RUN)</i>" if dry_run else ""

    msg = (
        f"✅ <b>BUY EXECUTED — {acct}</b>{dry_tag}{slot_line}\n"
        f"📈 <b>{symbol}</b>: {qty:,} units @ ₹{price:,.2f}\n"
        f"💰 Deployed: ₹{value:,.0f}"
        f"{wr_line}"
        f"{target_line}\n"
        f"⏱ {_now_ist()}"
    )
    send_message(msg, _bypass_dedup=True)
    symbol:        str,
    qty:           int,
    sell_price:    float,
    avg_buy_price: float  = None,
    pnl_pct:       float  = None,
    pnl_amt:       float  = None,
    dry_run:       bool   = False,
):
    """Send a SELL execution notification. Silently skips LIQUIDCASE."""
    if symbol == "LIQUIDCASE":
        return
    acct = _account()

    if avg_buy_price and avg_buy_price > 0:
        if pnl_pct is None:
            pnl_pct = (sell_price - avg_buy_price) / avg_buy_price * 100
        if pnl_amt is None:
            pnl_amt = (sell_price - avg_buy_price) * qty

    pnl_emoji = "🟢" if (pnl_pct or 0) >= 0 else "🔴"
    sign      = "+" if (pnl_amt or 0) >= 0 else ""
    pnl_line  = (
        f"\n{pnl_emoji} P&L: {sign}₹{abs(pnl_amt or 0):,.0f} ({sign}{pnl_pct:.2f}%%)"
        if pnl_pct is not None else ""
    )
    avg_line  = f"\n📊 Avg buy: ₹{avg_buy_price:,.2f}" if avg_buy_price else ""
    dry_tag   = " <i>(DRY RUN)</i>" if dry_run else ""

    msg = (
        f"💰 <b>SELL EXECUTED — {acct}</b>{dry_tag}\n"
        f"📉 <b>{symbol}</b>: {qty:,} units @ ₹{sell_price:,.2f}"
        f"{avg_line}"
        f"{pnl_line}\n"
        f"⏱ {_now_ist()}"
    )
    send_message(msg, _bypass_dedup=True)


def notify_eod_summary(trades_today: list, total_deployed: float, positions_held: list):
    """Send an end-of-day portfolio summary. Excludes LIQUIDCASE."""
    acct  = _account()
    # Filter out LIQUIDCASE from all trade and position lists
    trades_today   = [t for t in trades_today   if t.get("symbol") != "LIQUIDCASE"]
    positions_held = [p for p in positions_held if p != "LIQUIDCASE"]
    buys  = [t for t in trades_today if t.get("action") == "BUY"]
    sells = [t for t in trades_today if t.get("action") == "SELL"]

    held_str = ", ".join(positions_held) if positions_held else "None"
    msg = (
        f"📊 <b>EOD Summary — {acct}</b>\n"
        f"📅 {datetime.now(IST).strftime('%a %d %b %Y')}\n\n"
        f"Trades today: {len(buys)} BUY, {len(sells)} SELL\n"
        f"Total deployed: ₹{total_deployed:,.0f}\n"
        f"Positions held: {held_str}\n"
        f"⏱ Market closed 15:30 IST"
    )
    send_message(msg, _bypass_dedup=True)
