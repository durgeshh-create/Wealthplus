"""
Trade notification patch for executor.py
-----------------------------------------
This file shows EXACTLY what to add to backend/strategy/executor.py in
BOTH RD1858 and PS5673 to get Telegram alerts on every BUY and SELL.

HOW TO APPLY:
1. Open backend/strategy/executor.py
2. At the very top, after the existing imports, add the import block below.
3. Find the two insertion points and add the notification calls.

See CHANGES.md for the full search strings.
"""

# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — Add this import near the top of executor.py (after existing imports)
# ══════════════════════════════════════════════════════════════════════════════

IMPORT_ADDITION = """
# ── Telegram trade notifications ──────────────────────────────────────────────
try:
    from backend.utils.telegram import notify_buy, notify_sell
    _TELEGRAM_OK = True
except Exception:
    _TELEGRAM_OK = False
    def notify_buy(*a, **kw): pass
    def notify_sell(*a, **kw): pass
"""


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — BUY notification
# In execute_buy_signal(), find the line:
#
#     logger.info(f"✓ BUY SUCCESS: {symbol}")
#
# Immediately AFTER that line, add:
# ══════════════════════════════════════════════════════════════════════════════

BUY_NOTIFICATION_CODE = """
                # ── Telegram BUY notification ──────────────────────────────
                if _TELEGRAM_OK:
                    try:
                        from backend.core.config import Config as _Cfg
                        _dry = _Cfg.is_dry_run()
                        notify_buy(
                            symbol=symbol,
                            qty=etf_qty,
                            price=etf_price,
                            value=etf_qty * etf_price,
                            williams_r=signal.get('williams_r'),
                            profit_target_pct=self._get_profit_target(),
                            dry_run=_dry,
                        )
                    except Exception:
                        pass
                # ──────────────────────────────────────────────────────────
"""


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — SELL notification
# In execute_sell_signal(), find the line:
#
#     logger.info(f"✓ SELL SUCCESS: {symbol}")
#
# Immediately AFTER that line (and BEFORE `if is_automated:`), add:
# ══════════════════════════════════════════════════════════════════════════════

SELL_NOTIFICATION_CODE = """
                # ── Telegram SELL notification ─────────────────────────────
                if _TELEGRAM_OK:
                    try:
                        from backend.core.config import Config as _Cfg
                        _avg  = self.portfolio.get_average_price(symbol)
                        _dry  = _Cfg.is_dry_run()
                        notify_sell(
                            symbol=symbol,
                            qty=etf_qty,
                            sell_price=etf_price,
                            avg_buy_price=_avg,
                            dry_run=_dry,
                        )
                    except Exception:
                        pass
                # ──────────────────────────────────────────────────────────
"""


# ══════════════════════════════════════════════════════════════════════════════
# STEP 4 (OPTIONAL) — BNH / Dip Accumulator buy notification
# PS5673 has a separate dip-accumulator path. In intraday_engine.py or the
# relevant buy method, add the same notify_buy() call after a confirmed buy.
# ══════════════════════════════════════════════════════════════════════════════
