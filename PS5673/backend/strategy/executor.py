"""
Strategy Executor
Executes validated trading signals.

BUY sizing:
  - Sell LIQUIDCASE worth max_cash_per_transaction
  - Buy as many ETF units as that cash covers
  - Respects max_qty_per_trade (unit cap) if set

SELL sizing:
  - Sell ALL held units of the ETF
  - Buy LIQUIDCASE with proceeds
"""
from typing import Dict
import json
from pathlib import Path

from backend.core.config import Config
from backend.core.constants import SIGNAL_BUY, SIGNAL_SELL, LIQUIDCASE_SYMBOL
from backend.orders.manager import PostSellError
from backend.utils.logger import get_logger, log_separator
# ── Telegram trade notifications ──────────────────────────────────────────────
try:
    from backend.utils.telegram import notify_buy, notify_sell
    _TELEGRAM_OK = True
except Exception:
    _TELEGRAM_OK = False
    def notify_buy(*a, **kw): pass
    def notify_sell(*a, **kw): pass


logger = get_logger(__name__)


def _load_settings() -> dict:
    try:
        p = Path(__file__).parent.parent.parent / 'config' / 'settings.json'
        if p.exists():
            with open(p, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.debug(f"Could not read settings.json: {e}")
    return {}


class StrategyExecutor:
    """Executes trading strategy"""

    def __init__(self, order_manager, portfolio_tracker, realtime_manager, signal_generator=None):
        self.orders    = order_manager
        self.portfolio = portfolio_tracker
        self.realtime  = realtime_manager
        self.signal_generator = signal_generator

    # ──────────────────────────────────────────────────────────────
    # Settings readers
    # ──────────────────────────────────────────────────────────────

    def _get_test_quantity(self) -> int:
        return int(_load_settings().get('test_quantity', 0))

    def _is_bnh_symbol(self, symbol: str) -> bool:
        """Return True if symbol is managed by the Dip Accumulator (BnH) engine."""
        bnh = _load_settings().get('bnh_symbols', ['MID150BEES'])
        return symbol in (bnh if isinstance(bnh, list) else [bnh])

    def _get_profit_target(self, symbol: str = None) -> float:
        """Return the correct profit target: bnh_partial_profit_pct for BnH symbols,
        profit_target_pct for active-strategy symbols."""
        s = _load_settings()
        if symbol and self._is_bnh_symbol(symbol):
            return float(s.get('bnh_partial_profit_pct', 7.0))
        return float(s.get('profit_target_pct', Config.PROFIT_TARGET_PCT))

    def _get_max_cash_per_transaction(self) -> float:
        return float(_load_settings().get('max_cash_per_transaction',
                                          getattr(Config, 'MAX_CASH_PER_TRANSACTION', 0)))

    def _get_max_cash_per_stock(self) -> float:
        return float(_load_settings().get('max_cash_per_stock',
                                          getattr(Config, 'MAX_CASH_PER_STOCK', 0)))

    def _get_slots_count(self) -> int:
        try:
            return len(Config.get_all_monitored_symbols())
        except Exception:
            return Config.SLOTS_COUNT

    def _get_order_type(self) -> str:
        """MARKET or LIMIT — from settings. Default MARKET for scheduled, LIMIT when price provided."""
        return _load_settings().get('default_order_type', 'MARKET').upper()

    # ──────────────────────────────────────────────────────────────
    # Settings reset (after sell)
    # ──────────────────────────────────────────────────────────────

    def _reset_to_default_settings(self):
        StrategyExecutor.reset_to_default_settings()

    @staticmethod
    def reset_to_default_settings():
        """Reset profit_target_pct and test_quantity to their saved defaults."""
        try:
            p = Path(__file__).parent.parent.parent / 'config' / 'settings.json'
            if not p.exists():
                return
            with open(p, 'r') as f:
                s = json.load(f)

            default_profit = s.get('default_profit_target_pct', Config.PROFIT_TARGET_PCT)
            default_qty    = s.get('default_test_quantity', 0)
            changed = False

            if s.get('profit_target_pct') != default_profit:
                s['profit_target_pct'] = default_profit
                changed = True
            if s.get('test_quantity') != default_qty:
                s['test_quantity'] = default_qty
                changed = True

            if changed:
                with open(p, 'w') as f:
                    json.dump(s, f, indent=2)
                logger.info(f"🔄 Reset: profit→{default_profit}%, qty→{default_qty}")
        except Exception as e:
            logger.error(f"Failed to reset settings: {e}")

    # ──────────────────────────────────────────────────────────────
    # BUY execution
    # ──────────────────────────────────────────────────────────────

    def execute_buy_signal(self, signal: Dict, is_automated: bool = True) -> bool:
        """
        Execute a buy signal.

        Sizing:
          1. Use max_cash_per_transaction as the spend budget for this buy.
             Falls back to (total_liquidcase_value / slots_count) if not set.
          2. Quantity = floor(budget / etf_price)
          3. If max_qty_per_trade is set, further cap the quantity.
          4. Recalculate LIQUIDCASE units to sell = ceil(etf_qty * etf_price / liq_price)
             so proceeds always cover the cost.

        Allows multiple buys into the same stock as long as:
          - Total deployed < max_cash_per_stock
          - Entry count < max_entries_per_stock
        (These guards are enforced in SignalGenerator._check_buy_signal)
        """
        import math
        symbol = signal['symbol']

        if self.signal_generator and is_automated:
            self.signal_generator.lock_symbol_for_execution(symbol, 'BUY')

        log_separator(logger, f"EXECUTING {'AUTOMATED' if is_automated else 'MANUAL'} BUY: {symbol}")

        try:
            # ── prices ──────────────────────────────────────────
            liquidcase_price = self.realtime.get_ltp(LIQUIDCASE_SYMBOL)
            if not liquidcase_price:
                logger.error("Cannot get LIQUIDCASE price")
                # BUG FIX: this early return used to skip unlock_symbol()
                # entirely, leaving executing_symbols[symbol] set until the
                # 60s stale-lock sweep in generate_signals() force-cleared
                # it — but that sweep does NOT clear _attempted_today, so
                # the symbol stayed latched out of buying for the rest of
                # the session even though the price gap was transient
                # (e.g. WebSocket not yet populated right at market open).
                if self.signal_generator and is_automated:
                    self.signal_generator.unlock_symbol(symbol, success=False, allow_retry=True)
                return False

            etf_price = self.realtime.get_ltp(symbol)
            if not etf_price:
                logger.error(f"Cannot get price for {symbol}")
                # Same lock-leak fix as above.
                if self.signal_generator and is_automated:
                    self.signal_generator.unlock_symbol(symbol, success=False, allow_retry=True)
                return False

            # ── budget for this transaction ──────────────────────
            max_tx = self._get_max_cash_per_transaction()
            if max_tx > 0:
                budget = max_tx
                logger.info(f"Budget: ₹{budget:.2f} (max_cash_per_transaction)")
            else:
                # Fallback: 1 slot worth of LIQUIDCASE
                liq_qty   = self.portfolio.liquidcase_quantity
                liq_value = liq_qty * liquidcase_price
                slots     = self._get_slots_count()
                budget    = liq_value / slots if slots > 0 else liq_value
                logger.info(f"Budget: ₹{budget:.2f} (1/{slots} of LIQUIDCASE value — fallback)")

            # ── safety: don't exceed remaining capacity for this stock ──
            max_cash_stock = self._get_max_cash_per_stock()
            if max_cash_stock > 0:
                deployed = self._get_cash_deployed(symbol, etf_price)
                remaining_capacity = max_cash_stock - deployed
                if remaining_capacity <= 0:
                    logger.warning(f"⚠️ {symbol}: max_cash_per_stock already reached (₹{deployed:.0f})")
                    # Lock-leak fix: release the execution lock immediately
                    # instead of letting it sit until the 60s stale-lock
                    # sweep. Not a transient/data issue, so no retry this
                    # session — matches the pre-existing latched-skip
                    # behavior _check_buy_signal already applies for this
                    # same condition.
                    if self.signal_generator and is_automated:
                        self.signal_generator.unlock_symbol(symbol, success=False, allow_retry=False)
                    return False
                budget = min(budget, remaining_capacity)
                logger.info(f"Capacity remaining for {symbol}: ₹{remaining_capacity:.2f} → budget capped to ₹{budget:.2f}")

            # ── quantities ──────────────────────────────────────
            etf_qty = int(budget / etf_price)
            if etf_qty <= 0:
                # Distinguish: was this a per-stock cap hit, or a genuine budget problem?
                if max_cash_stock > 0 and budget < etf_price:
                    deployed = self._get_cash_deployed(symbol, etf_price)
                    logger.warning(
                        f"⚠️ {symbol}: per-stock cap effectively reached — "
                        f"remaining capacity ₹{budget:.2f} < 1 unit @ ₹{etf_price:.2f} "
                        f"(deployed ₹{deployed:.0f} of ₹{max_cash_stock:.0f} max)"
                    )
                else:
                    logger.error(f"Budget ₹{budget:.2f} too small for 1 unit of {symbol} @ ₹{etf_price:.2f}")
                # Lock-leak fix — same reasoning as the max_cash_per_stock
                # branch above: not transient, so unlock without retry.
                if self.signal_generator and is_automated:
                    self.signal_generator.unlock_symbol(symbol, success=False, allow_retry=False)
                return False

            # Apply max_qty_per_trade cap
            max_qty = self._get_test_quantity()
            if max_qty > 0 and etf_qty > max_qty:
                logger.warning(f"⚠️ Qty capped: {etf_qty} → {max_qty} (max_qty_per_trade)")
                etf_qty = max_qty

            # ── Ask-side liquidity check ───────────────────────────────────
            # ETFs like MON100 can have only buyers and no sellers — placing a
            # MARKET BUY into an empty ask side causes the order to hang open
            # indefinitely or fill at a terrible stale price.
            # Skip the buy if there is no ask quantity in the order book.
            if hasattr(self.realtime, 'get_total_ask_qty'):
                ask_qty = self.realtime.get_total_ask_qty(symbol)
                if ask_qty == 0:
                    logger.warning(
                        f"⚠️ Skipping BUY {symbol}: no sellers in order book (ask qty = 0). "
                        f"Buy skipped for this session."
                    )
                    if self.signal_generator and is_automated:
                        self.signal_generator.unlock_symbol(symbol, success=False, allow_retry=False)
                    return False, "No ask-side liquidity"

            etf_cost   = etf_qty * etf_price
            avail_cash = self.orders.get_available_cash()

            # ── Cash reserve: never deploy below the configured floor ──────
            cash_reserve  = Config.get_cash_reserve()
            spendable_cash = max(0.0, avail_cash - cash_reserve)
            if spendable_cash < etf_cost:
                logger.info(
                    f"💵 Spendable cash ₹{spendable_cash:.2f} "
                    f"(total ₹{avail_cash:.2f} − reserve ₹{cash_reserve:.2f}) "
                    f"< cost ₹{etf_cost:.2f} — will sell LIQUIDCASE for shortfall"
                )

            # LIQUIDCASE needed = only the shortfall after using spendable cash
            # ceil() so proceeds always cover the gap
            shortfall   = max(0.0, etf_cost - spendable_cash)
            liq_to_sell = math.ceil(shortfall / liquidcase_price) if shortfall > 0 else 0

            # Verify we have enough LIQUIDCASE for the shortfall
            liq_held       = self.portfolio.liquidcase_quantity
            liq_held_value = liq_held * liquidcase_price
            if liq_to_sell > liq_held:
                logger.error(
                    f"Insufficient funds: spendable cash ₹{spendable_cash:.2f} "
                    f"(total ₹{avail_cash:.2f} − reserve ₹{cash_reserve:.2f}) + "
                    f"LIQUIDCASE ₹{liq_held_value:.2f} < ETF cost ₹{etf_cost:.2f}"
                )
                # Lock-leak fix: unlock_symbol's own docstring already names
                # "insufficient funds" as the canonical allow_retry=True
                # example — this return path just never actually called it.
                if self.signal_generator and is_automated:
                    self.signal_generator.unlock_symbol(symbol, success=False, allow_retry=True)
                return False, f"Insufficient funds: cash ₹{spendable_cash:.0f} + LIQUIDCASE ₹{liq_held_value:.0f} < cost ₹{etf_cost:.0f}"

            if liq_to_sell > 0:
                logger.info(
                    f"💱 SWAP PLAN: spendable cash ₹{spendable_cash:.2f} + sell {liq_to_sell} LIQUIDCASE "
                    f"× ₹{liquidcase_price:.2f} = ₹{liq_to_sell*liquidcase_price:.2f} (shortfall) → "
                    f"Buy {etf_qty} {symbol} × ₹{etf_price:.2f} = ₹{etf_cost:.2f}"
                )
            else:
                logger.info(
                    f"✅ Spendable cash ₹{spendable_cash:.2f} sufficient — buying {etf_qty} × {symbol} "
                    f"@ ₹{etf_price:.2f} directly"
                )

            # ── resolve order type and limit price ──────────────
            order_type = self._get_order_type()

            if order_type == 'LIMIT':
                # Use top bid (best buy price in market depth) for buy LIMIT orders
                top_bid = None
                if hasattr(self.realtime, 'get_top_bid'):
                    top_bid = self.realtime.get_top_bid(symbol)
                limit_price = float(top_bid) if top_bid and top_bid > 0 else etf_price
                logger.info(
                    f"LIMIT BUY: using top_bid ₹{limit_price:.2f} for {symbol} "                    f"(LTP was ₹{etf_price:.2f}, {'top_bid from depth' if top_bid else 'fell back to LTP'})"                )
            else:
                limit_price = None
                logger.info(f"MARKET BUY: {symbol} @ ₹{etf_price:.2f}")

            # ── PRE-REGISTER in pending_exec BEFORE smart_buy ───────────
            # This makes _get_avg_buy_price and _get_buys_today accurate
            # immediately, so the signal loop won't re-queue this symbol
            # during the 30s order monitoring window.
            if self.signal_generator and is_automated and hasattr(self.signal_generator, 'record_buy_executed'):
                self.signal_generator.record_buy_executed(
                    symbol, price=float(limit_price if limit_price else etf_price), qty=etf_qty
                )

            # ── Smart buy: use available cash first, LIQUIDCASE only for shortfall ──
            try:
                success = self.orders.smart_buy(
                    buy_symbol=symbol,
                    buy_quantity=etf_qty,
                    buy_price_estimate=etf_price,
                    realtime_manager=self.realtime,
                    portfolio_tracker=self.portfolio,
                    buy_order_type=order_type,
                    buy_limit_price=limit_price,
                    buy_product='CNC',
                    cash_reserve=cash_reserve,   # keep executor & smart_buy in sync
                )
            except PostSellError as e:
                # LIQUIDCASE was already sold — cash is now in account.
                # Rollback the pre-registered buy so _buys_today stays accurate
                # (the ETF was never actually bought). The next cycle will see
                # the freed cash and retry the buy directly without LIQUIDCASE sell.
                # Do NOT allow_retry: unlock_symbol keeps _attempted_today latched
                # and sets a 5-min cooldown to prevent the hammer loop.
                logger.error(f"✗ BUY FAILED after LIQUIDCASE sell [{symbol}]: {e}")
                if self.signal_generator and is_automated:
                    if hasattr(self.signal_generator, 'rollback_buy_executed'):
                        self.signal_generator.rollback_buy_executed(symbol)
                    self.signal_generator.unlock_symbol(symbol, success=False, allow_retry=False)
                return False, str(e)
            except RuntimeError as e:
                logger.error(f"✗ BUY FAILED [{symbol}]: {e}")
                # Pre-flight failure — order never reached broker; allow retry
                if self.signal_generator and is_automated:
                    self.signal_generator.rollback_buy_executed(symbol)
                    self.signal_generator.unlock_symbol(symbol, success=False, allow_retry=True)
                return False, str(e)

            if success:
                logger.info(f"✓ BUY SUCCESS: {symbol} x{etf_qty} @ ₹{etf_price:.2f}")
                # ── Telegram BUY notification ──────────────────────────────
                if _TELEGRAM_OK:
                    try:
                        from backend.core.config import Config as _Cfg
                        notify_buy(
                            symbol=symbol,
                            qty=etf_qty,
                            price=etf_price,
                            value=etf_qty * etf_price,
                            williams_r=signal.get('williams_r'),
                            profit_target_pct=self._get_profit_target(),
                            dry_run=_Cfg.is_dry_run(),
                        )
                    except Exception:
                        pass
                # ──────────────────────────────────────────────────────────
                if self.signal_generator and is_automated:
                    # success=True keeps recently_bought cooldown active
                    self.signal_generator.unlock_symbol(symbol, success=True)
                return True
            else:
                logger.error(f"✗ BUY FAILED: {symbol}")
                if self.signal_generator and is_automated:
                    self.signal_generator.rollback_buy_executed(symbol)
                    self.signal_generator.unlock_symbol(symbol, success=False)
                return False, "Order did not complete"

        except Exception as e:
            err_str = str(e)
            logger.error(f"Error executing buy for {symbol}: {e}", exc_info=True)
            if self.signal_generator and is_automated:
                # ── Auth/token failure (HTTP 403) ─────────────────────────────
                # The access_token is dead for the rest of this session.
                # unlock_symbol(success=False) would clear _attempted_today and
                # allow a retry every 2s — causing hundreds of logged errors.
                # Instead treat it like a permanent failure: keep _attempted_today
                # latched so this symbol is skipped for the rest of the session.
                if '403' in err_str or 'access_token' in err_str.lower() or 'api_key' in err_str.lower():
                    logger.critical(
                        f"🔐 AUTH FAILURE detected during BUY {symbol} — "
                        f"signalling launcher to re-authenticate (exit 2)."
                    )
                    self.signal_generator.rollback_buy_executed(symbol)
                    self.signal_generator.executing_symbols.pop(symbol, None)
                    self.signal_generator._attempted_today.add(symbol)
                    if _TELEGRAM_OK:
                        try:
                            from backend.utils.telegram import _send as _tg_send
                            from backend.core.config import Config as _Cfg
                            _tg_send(
                                f"🔐 <b>WealthAlgo {_Cfg.USER_ID if hasattr(_Cfg, 'USER_ID') else ''} — AUTH ERROR</b>\n"
                                f"❌ Zerodha 403: access_token expired.\n"
                                f"🔄 Bot will re-authenticate and resume automatically."
                            )
                        except Exception:
                            pass
                    import sys as _sys
                    _sys.exit(2)   # exit code 2 = auth failure → launcher re-logins
                else:
                    self.signal_generator.rollback_buy_executed(symbol)
                    self.signal_generator.unlock_symbol(symbol, success=False)
            return False, err_str

    def _get_cash_deployed(self, symbol: str, current_price: float) -> float:
        """Current market value deployed in a symbol (qty * avg_price)."""
        qty = self.portfolio.get_quantity_held(symbol)
        if qty <= 0:
            return 0.0
        avg = self.portfolio.get_average_price(symbol)
        return float(qty * avg) if avg else 0.0

    # ──────────────────────────────────────────────────────────────
    # SELL execution (unchanged logic, cleaned up)
    # ──────────────────────────────────────────────────────────────

    def execute_sell_signal(self, signal: Dict, is_automated: bool = True) -> bool:
        """
        Execute a sell signal: sell held ETF units (capped by max_qty_per_trade /
        test_quantity if set — otherwise the entire position), buy LIQUIDCASE
        with proceeds.
        """
        import time
        symbol = signal['symbol']

        if self.signal_generator and is_automated:
            self.signal_generator.lock_symbol_for_execution(symbol, 'SELL')

        log_separator(logger, f"EXECUTING {'AUTOMATED' if is_automated else 'MANUAL'} SELL: {symbol}")

        try:
            # ✅ FIX Bug-3: soft sell gate — mirrors the buy gate in get_due_buys().
            # Sells placed after 15:25 result in ETF proceeds arriving T+0 but the
            # LIQUIDCASE buy leg queuing as AMO (next session), leaving cash unparked
            # overnight.  We allow manual (is_automated=False) sells through always.
            if is_automated:
                from datetime import time as _dtime
                from backend.strategy.signal_generator import _now_ist as _now_sell
                _now_t = _now_sell().time()
                _SELL_CLOSE = _dtime(15, 25)
                _SELL_OPEN  = _dtime(9, 15)
                if not (_SELL_OPEN <= _now_t <= _SELL_CLOSE):
                    logger.info(
                        f"⏸ Automated SELL {symbol} blocked — outside 09:15–15:25 IST "
                        f"({_now_t.strftime('%H:%M')}). Will retry next cycle."
                    )
                    if self.signal_generator:
                        self.signal_generator.unlock_symbol(symbol, success=False, allow_retry=True)
                    return False

            etf_qty = self.portfolio.get_quantity_held(symbol)
            if etf_qty <= 0:
                logger.error(f"No {symbol} held to sell")
                if self.signal_generator and is_automated:
                    self.signal_generator.unlock_symbol(symbol)
                return False

            etf_qty_full_holding = etf_qty  # capture before any test_quantity cap below

            max_qty = self._get_test_quantity()
            if max_qty > 0 and etf_qty > max_qty:
                logger.warning(f"⚠️ Sell qty capped: {etf_qty} → {max_qty} (max_qty_per_trade)")
                etf_qty = max_qty

            # Get ETF price (with retry)
            etf_price = self._get_price_with_retry(symbol)
            if not etf_price:
                logger.error(f"❌ Cannot get valid price for {symbol} — sell aborted")
                if self.signal_generator and is_automated:
                    self.signal_generator.unlock_symbol(symbol)
                return False

            # Get LIQUIDCASE price (with retry)
            liq_price = self._get_price_with_retry(LIQUIDCASE_SYMBOL)
            if not liq_price:
                logger.error("❌ Cannot get LIQUIDCASE price — sell aborted")
                if self.signal_generator and is_automated:
                    self.signal_generator.unlock_symbol(symbol)
                return False

            # Use top ask (best offer) for LIQUIDCASE buy limit price — ensures
            # the limit order fills immediately against the lowest ask in the book.
            # top_bid would sit below market and may not fill quickly.
            liq_top_ask = None
            if hasattr(self.realtime, 'get_top_ask'):
                liq_top_ask = self.realtime.get_top_ask(LIQUIDCASE_SYMBOL)
            liq_limit_price = float(liq_top_ask) if liq_top_ask and liq_top_ask > 0 else liq_price
            logger.info(
                f"LIQUIDCASE buy limit price: ₹{liq_limit_price:.2f} "
                f"({'top ask (offer) from depth' if liq_top_ask else 'fell back to LTP ₹' + str(round(liq_price, 2))})"
            )

            proceeds    = etf_qty * etf_price
            liq_to_buy  = int(proceeds / liq_limit_price)

            logger.info(
                f"SWAP: Sell {etf_qty} {symbol} @ ₹{etf_price:.2f} = ₹{proceeds:.2f} → "
                f"Buy {liq_to_buy} LIQUIDCASE LIMIT @ ₹{liq_limit_price:.2f}"
            )

            sell_order_type = self._get_order_type()

            if sell_order_type == 'LIMIT':
                # Use top ask (best offer price in market depth) for sell LIMIT orders
                top_ask = None
                if hasattr(self.realtime, 'get_top_ask'):
                    top_ask = self.realtime.get_top_ask(symbol)
                sell_limit_price = float(top_ask) if top_ask and top_ask > 0 else etf_price
                logger.info(
                    f"LIMIT SELL: using top_ask ₹{sell_limit_price:.2f} for {symbol} "
                    f"(LTP was ₹{etf_price:.2f}, {'top_ask from depth' if top_ask else 'fell back to LTP'})"
                )
            else:
                sell_limit_price = None
                logger.info(f"MARKET SELL: {symbol} @ ₹{etf_price:.2f}")

            success = self.orders.execute_swap(
                sell_symbol=symbol,
                sell_quantity=etf_qty,
                buy_symbol=LIQUIDCASE_SYMBOL,
                buy_quantity=liq_to_buy,
                buy_price_estimate=liq_limit_price,
                sell_price_estimate=etf_price,
                sell_order_type=sell_order_type,
                sell_limit_price=sell_limit_price,
                buy_order_type='LIMIT',
                buy_limit_price=liq_limit_price,
            )

            if success:
                avg_price = self.portfolio.get_average_price(symbol)
                if avg_price:
                    pnl_pct = (etf_price - avg_price) / avg_price * 100
                    pnl_amt = (etf_price - avg_price) * etf_qty
                    logger.info(
                        f"✓ PROFIT: {pnl_pct:.2f}% ₹{pnl_amt:.2f} | "
                        f"Target was {self._get_profit_target(symbol)}%"
                    )
                logger.info(f"✓ SELL SUCCESS: {symbol}")
                # ── Telegram SELL notification ─────────────────────────────
                if _TELEGRAM_OK:
                    try:
                        from backend.core.config import Config as _Cfg
                        notify_sell(
                            symbol=symbol,
                            qty=etf_qty,
                            sell_price=etf_price,
                            avg_buy_price=self.portfolio.get_average_price(symbol),
                            dry_run=_Cfg.is_dry_run(),
                        )
                    except Exception:
                        pass
                # ──────────────────────────────────────────────────────────

                if is_automated:
                    self._reset_to_default_settings()

                # Only clear the Slot Matrix tranche count if this sell disposed
                # of the ENTIRE held quantity. A max_qty_per_trade (test_quantity)
                # cap above can make etf_qty less than the original full holding —
                # in that case the position is still open and the count must be
                # preserved, not zeroed.
                if self.signal_generator:
                    if etf_qty >= etf_qty_full_holding:
                        self.signal_generator.reset_position_slots(symbol)
                    else:
                        logger.info(
                            f"📌 {symbol}: partial sell ({etf_qty}/{etf_qty_full_holding} qty, "
                            f"test_quantity cap active) — position still open, slot count preserved"
                        )

                self.portfolio.sync()
                if self.signal_generator and is_automated:
                    self.signal_generator.unlock_symbol(symbol)
                return True
            else:
                logger.error(f"✗ SELL FAILED: {symbol}")
                if self.signal_generator and is_automated:
                    self.signal_generator.unlock_symbol(symbol)
                return False

        except Exception as e:
            err_str = str(e)
            logger.error(f"Error executing sell for {symbol}: {e}", exc_info=True)
            if self.signal_generator and is_automated:
                if '403' in err_str or 'access_token' in err_str.lower() or 'api_key' in err_str.lower():
                    logger.critical(
                        f"🔐 AUTH FAILURE detected during SELL {symbol} — "
                        f"signalling launcher to re-authenticate (exit 2)."
                    )
                    self.signal_generator.executing_symbols.pop(symbol, None)
                    if _TELEGRAM_OK:
                        try:
                            from backend.utils.telegram import _send as _tg_send
                            from backend.core.config import Config as _Cfg
                            _tg_send(
                                f"🔐 <b>WealthAlgo — AUTH ERROR (SELL)</b>\n"
                                f"❌ Zerodha 403: access_token expired.\n"
                                f"🔄 Bot will re-authenticate and resume automatically."
                            )
                        except Exception:
                            pass
                    import sys as _sys
                    _sys.exit(2)   # exit code 2 = auth failure → launcher re-logins
                else:
                    self.signal_generator.unlock_symbol(symbol)
            return False

    def _get_price_with_retry(self, symbol: str, retries: int = 3) -> float:
        import time
        price = self.realtime.get_ltp(symbol)
        for i in range(retries):
            if price and price > 0:
                return price
            logger.warning(f"⚠️ {symbol} price unavailable (attempt {i+1}/{retries})")
            time.sleep(0.5)
            price = self.realtime.get_ltp(symbol)
        logger.error(f"❌ Could not get valid price for {symbol} after {retries} retries")
        return None

    # ──────────────────────────────────────────────────────────────
    # Batch execution
    # ──────────────────────────────────────────────────────────────

    def execute_signals(self, signals: Dict) -> Dict[str, bool]:
        """Execute all active signals. Sells first, then buys."""
        results = {}
        for signal in signals.get('sell', []):
            results[f"SELL_{signal['symbol']}"] = self.execute_sell_signal(signal)
        for signal in signals.get('buy', []):
            outcome = self.execute_buy_signal(signal)
            # execute_buy_signal returns (success, reason) tuple or plain bool
            if isinstance(outcome, tuple):
                results[f"BUY_{signal['symbol']}"] = outcome[0]
                if not outcome[0] and len(outcome) > 1:
                    results[f"BUY_{signal['symbol']}__reason"] = outcome[1]
            else:
                results[f"BUY_{signal['symbol']}"] = outcome
        return results
