"""
telegram.py — WealthAlgo trade notifications
=============================================
Provides:
  _send(text)          — raw fire-and-forget message
  notify_buy(...)      — called by executor after a BUY is placed
  notify_sell(...)     — called by executor after a SELL is placed

Reads TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from environment variables
(set as GitHub Actions secrets: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID).

Both notify_buy and notify_sell silently do nothing if the env vars are absent
so dry-run / local dev never crashes.

Deploy this file to BOTH instances:
  PS5673/backend/utils/telegram.py
  RD1858/backend/utils/telegram.py
"""

import os
import json
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))


# ── Core send ─────────────────────────────────────────────────────────────────

def _send(text: str) -> bool:
    """
    Send a Telegram message. Returns True on success, False on any failure.
    Never raises — all exceptions are swallowed so trading is never interrupted.
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        # Env vars not configured — silently skip (local dev / dry-run)
        return False

    try:
        payload = json.dumps({
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "HTML",
        }).encode("utf-8")

        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data    = payload,
            headers = {"Content-Type": "application/json"},
            method  = "POST",
        )
        urllib.request.urlopen(req, timeout=8)
        return True

    except Exception:
        # Never crash the trading engine over a notification failure
        return False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _account() -> str:
    """Return the Kite user ID (e.g. PS5673 / RD1858) from env."""
    return os.environ.get("KITE_USER_ID", "BOT").strip()


def _now_ist() -> str:
    return datetime.now(IST).strftime("%H:%M IST")


def _fmt_inr(value: Optional[float]) -> str:
    if value is None:
        return "—"
    abs_val = abs(value)
    if abs_val >= 100_000:
        return f"₹{value/100_000:.2f}L"
    if abs_val >= 1_000:
        return f"₹{value/1_000:.1f}K"
    return f"₹{value:,.2f}"


# ── BUY notification ──────────────────────────────────────────────────────────

def notify_buy(
    symbol: str,
    qty: int,
    price: float,
    value: float,
    williams_r: Optional[float] = None,
    profit_target_pct: Optional[float] = None,
    dry_run: bool = False,
) -> bool:
    """
    Send a Telegram notification after a BUY order is executed.

    Called from executor.py:
        notify_buy(
            symbol=symbol,
            qty=etf_qty,
            price=etf_price,
            value=etf_qty * etf_price,
            williams_r=signal.get('williams_r'),
            profit_target_pct=self._get_profit_target(),
            dry_run=_Cfg.is_dry_run(),
        )
    """
    acct   = _account()
    mode   = "🧪 DRY RUN" if dry_run else "✅ LIVE"
    wr_str = f"\n📉 Williams %%R: <b>{williams_r:.1f}</b>" if williams_r is not None else ""
    pt_str = f"\n🎯 Profit target: <b>{profit_target_pct:.1f}%%</b>" if profit_target_pct is not None else ""

    msg = (
        f"📈 <b>BUY EXECUTED — {acct}</b>  {mode}\n"
        f"🏷 Symbol: <b>{symbol}</b>\n"
        f"🔢 Qty: <b>{qty}</b>  ×  ₹{price:,.2f}\n"
        f"💰 Value: <b>{_fmt_inr(value)}</b>"
        f"{wr_str}"
        f"{pt_str}\n"
        f"⏱ {_now_ist()}"
    )
    return _send(msg)


# ── SELL notification ─────────────────────────────────────────────────────────

def notify_sell(
    symbol: str,
    qty: int,
    sell_price: float,
    avg_buy_price: Optional[float] = None,
    dry_run: bool = False,
) -> bool:
    """
    Send a Telegram notification after a SELL order is executed.

    Called from executor.py:
        notify_sell(
            symbol=symbol,
            qty=etf_qty,
            sell_price=etf_price,
            avg_buy_price=self.portfolio.get_average_price(symbol),
            dry_run=_Cfg.is_dry_run(),
        )
    """
    acct = _account()
    mode = "🧪 DRY RUN" if dry_run else "✅ LIVE"

    pnl_str = ""
    if avg_buy_price and avg_buy_price > 0:
        pnl_amt = (sell_price - avg_buy_price) * qty
        pnl_pct = (sell_price - avg_buy_price) / avg_buy_price * 100
        sign    = "+" if pnl_amt >= 0 else ""
        emoji   = "🟢" if pnl_amt >= 0 else "🔴"
        pnl_str = (
            f"\n📊 Avg buy: ₹{avg_buy_price:,.2f}"
            f"\n{emoji} P&L: <b>{sign}{_fmt_inr(pnl_amt)}</b>  ({sign}{pnl_pct:.2f}%%)"
        )

    proceeds = sell_price * qty
    msg = (
        f"💰 <b>SELL EXECUTED — {acct}</b>  {mode}\n"
        f"🏷 Symbol: <b>{symbol}</b>\n"
        f"🔢 Qty: <b>{qty}</b>  ×  ₹{sell_price:,.2f}\n"
        f"💵 Proceeds: <b>{_fmt_inr(proceeds)}</b>"
        f"{pnl_str}\n"
        f"⏱ {_now_ist()}"
    )
    return _send(msg)
