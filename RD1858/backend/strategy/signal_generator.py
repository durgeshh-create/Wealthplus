"""
Signal Generator
================
BUY TRIGGERS (either fires a buy):
  Trigger A: W%R <= -80   (deep oversold)
  Trigger B: W%R <= -60  AND  price drop >= min_price_drop_pct% from prev close

SLOT RULES (per symbol, slot counter resets daily):
  Slot 1: Fires when Trigger A or B is first met and symbol is not already held.
  Slot 2+: Fires only when ALL of the following are true:
           - A prior buy already exists for this symbol (could be from a previous day)
           - Live price has fallen >= min_price_drop_pct% BELOW the current avg buy price
             (flat threshold — same % required for every additional slot)
           - Trigger A or B is still active (market still oversold)
  Max buys per day = slots_count setting (default 5). Counter resets at midnight.
  avg_buy_price is blended across ALL prior buys (current day + previous days),
  so the drop-from-avg check works correctly when accumulating over multiple sessions.

SELL TRIGGER:
  Profit >= profit_target_pct (per stock/ETF, default 5%)

SCHEDULED EXECUTION:
  Buy  → top bid  price for LIMIT orders
  Sell → top ask  price for LIMIT orders
"""
import json
import threading
from collections import defaultdict
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Dict, List, Optional

from backend.core.config import Config
from backend.core.constants import (
    SIGNAL_BUY, SIGNAL_SELL, SIGNAL_NONE, LIQUIDCASE_SYMBOL
)
from backend.indicators.calculator import calculate_daily_williams_r, get_signal_status
from backend.utils.logger import get_logger

logger = get_logger(__name__)


def _load_settings() -> dict:
    try:
        p = Path(__file__).parent.parent.parent / 'config' / 'settings.json'
        if p.exists():
            with open(p) as f:
                return json.load(f)
    except Exception as e:
        logger.debug(f"Could not read settings.json: {e}")
    return {}


class SignalGenerator:

    def __init__(self, historical_manager, realtime_manager, portfolio_tracker):
        self.historical = historical_manager
        self.realtime   = realtime_manager
        self.portfolio  = portfolio_tracker

        self.recently_sold:     Dict[str, datetime] = {}
        self.recently_bought:   Dict[str, datetime] = {}   # cooldown after buy fires
        self.executing_symbols: Dict[str, dict]     = {}
        self.pending_buys:      Dict[str, dict]     = {}

        # Per-symbol buy count today {symbol: int} — resets at midnight
        self._buys_today:    Dict[str, int]  = defaultdict(int)
        # Symbols for which a buy was attempted today — latched at queue time,
        # prevents duplicate triggers in the 2s window before executing_symbols is set
        self._attempted_today: set           = set()
        self._session_date: Optional[object] = None
        self._lock = threading.Lock()

        # In-memory record of executed buys not yet reflected in portfolio sync.
        # {symbol: {'qty': int, 'avg_price': float, 'cost': float}}
        self._pending_exec: Dict[str, dict] = {}

    # ── Settings helpers ──────────────────────────────────────────────────────

    def _get_profit_target(self) -> float:
        return float(_load_settings().get('profit_target_pct', Config.PROFIT_TARGET_PCT))

    def _get_max_cash_per_stock(self) -> float:
        return float(_load_settings().get('max_cash_per_stock',
                                          getattr(Config, 'MAX_CASH_PER_STOCK', 0)))

    def _get_max_cash_per_transaction(self) -> float:
        return float(_load_settings().get('max_cash_per_transaction',
                                          getattr(Config, 'MAX_CASH_PER_TRANSACTION', 0)))

    def _get_max_slots(self) -> int:
        """Total allowed buy slots per symbol (default 5)."""
        return int(_load_settings().get('slots_count', Config.SLOTS_COUNT))

    def _get_min_price_drop_pct(self) -> float:
        return float(_load_settings().get('min_price_drop_pct', 1.0))

    def _get_buy_execution_time(self):
        """Return a dtime for scheduled execution, or None if 'anytime' (no gate)."""
        s = _load_settings()
        val = s.get('buy_execution_time', '15:15')
        if val == 'anytime':
            return None   # sentinel: no time gate
        try:
            parts = val.split(':')
            return dtime(int(parts[0]), int(parts[1]))
        except Exception:
            return dtime(15, 15)

    # ── Daily session reset ───────────────────────────────────────────────────

    def _reset_daily_if_needed(self):
        today = datetime.now().date()
        with self._lock:
            if self._session_date != today:
                self._session_date = today
                self._buys_today.clear()
                self._attempted_today.clear()
                logger.info(f"📅 Signal generator daily reset for {today}")

    # ── Portfolio helpers ─────────────────────────────────────────────────────

    def _get_cash_deployed(self, symbol: str) -> float:
        qty = self.portfolio.get_quantity_held(symbol)
        if qty <= 0:
            return 0.0
        avg = self.portfolio.get_average_price(symbol)
        return float(qty * avg) if avg else 0.0

    def _get_avg_buy_price(self, symbol: str) -> Optional[float]:
        """Effective avg buy price: merges portfolio data with any
        executed-but-not-yet-synced buys from _pending_exec."""
        port_qty = self.portfolio.get_quantity_held(symbol)
        port_avg = self.portfolio.get_average_price(symbol) if port_qty > 0 else None
        pending  = self._pending_exec.get(symbol)

        if pending and pending['qty'] > 0:
            if port_avg and port_qty > 0:
                total_qty = port_qty + pending['qty']
                blended   = ((port_avg * port_qty) +
                             (pending['avg_price'] * pending['qty'])) / total_qty
                return round(blended, 2)
            else:
                return pending['avg_price']

        if port_qty > 0:
            return port_avg
        return None

    def _get_buys_today(self, symbol: str) -> int:
        # _buys_today is incremented by record_buy_executed.
        # _pending_exec may have a buy in-flight before that fires.
        # Take the max so we never undercount.
        recorded      = self._buys_today.get(symbol, 0)
        pending_count = 1 if symbol in self._pending_exec else 0
        return max(recorded, pending_count)

    def record_buy_executed(self, symbol: str, price: float = 0, qty: int = 0):
        """Increment buy counter and store execution in _pending_exec
        so slot guards work correctly before the next portfolio.sync()."""
        with self._lock:
            self._buys_today[symbol] = self._buys_today.get(symbol, 0) + 1
            if price > 0 and qty > 0:
                existing = self._pending_exec.get(symbol)
                if existing and existing['qty'] > 0:
                    total_qty = existing['qty'] + qty
                    blended   = ((existing['avg_price'] * existing['qty']) +
                                 (price * qty)) / total_qty
                    self._pending_exec[symbol] = {
                        'qty': total_qty, 'avg_price': round(blended, 2),
                        'cost': existing['cost'] + price * qty,
                    }
                else:
                    self._pending_exec[symbol] = {
                        'qty': qty, 'avg_price': price, 'cost': price * qty,
                    }
        logger.info(
            f"📌 {symbol}: buy #{self._buys_today[symbol]} recorded today"
            + (f" @ ₹{price:.2f} x{qty} (pending sync)" if price > 0 else "")
        )

    def clear_pending_exec(self, symbol: str):
        """Called after portfolio.sync() to clear the in-memory pending record."""
        with self._lock:
            if symbol in self._pending_exec:
                del self._pending_exec[symbol]
                logger.debug(f"📋 {symbol}: pending_exec cleared after portfolio sync")

    # ── Main signal generation ────────────────────────────────────────────────

    def generate_signals(self) -> Dict[str, dict]:
        self._reset_daily_if_needed()
        signals = {}

        # Clean stale execution locks
        stale = [s for s, info in self.executing_symbols.items()
                 if datetime.now() - info['timestamp'] > timedelta(seconds=60)]
        for s in stale:
            del self.executing_symbols[s]
            logger.warning(f"⚠️ Removed stale lock: {s}")

        for symbol in Config.get_active_etfs():
            if symbol == LIQUIDCASE_SYMBOL:
                continue
            if symbol in self.executing_symbols:
                continue
            sig = self._generate_signal(symbol)
            if sig:
                signals[symbol] = sig
        return signals

    def _generate_signal(self, symbol: str) -> Optional[dict]:
        try:
            daily_data = self.historical.get_daily_data(symbol)
            if daily_data is None or len(daily_data) == 0:
                return None

            live_price = self.realtime.get_ltp(symbol)
            live_ohlc  = self.realtime.get_ohlc(symbol)
            if live_price is None:
                live_price = float(daily_data.iloc[-1]['close'])
                live_ohlc  = {'high': float(daily_data.iloc[-1]['high']),
                              'low':  float(daily_data.iloc[-1]['low'])}

            williams_r = calculate_daily_williams_r(
                daily_data,
                live_price=live_price,
                live_high=live_ohlc.get('high'),
                live_low=live_ohlc.get('low'),
            )
            if williams_r is None:
                return None

            prev_close  = float(daily_data.iloc[-1]['close'])
            buy_signal  = self._check_buy_signal(symbol, williams_r, live_price, prev_close)
            sell_signal = self._check_sell_signal(symbol, live_price)

            if buy_signal:
                signal_type = SIGNAL_BUY
            elif sell_signal:
                signal_type = SIGNAL_SELL
            else:
                signal_type = SIGNAL_NONE

            return {
                'symbol':     symbol,
                'signal':     signal_type,
                'williams_r': williams_r,
                'price':      live_price,
                'prev_close': prev_close,
                'status':     get_signal_status(williams_r),
                'buy_ready':  buy_signal,
                'sell_ready': sell_signal,
                'timestamp':  datetime.now(),
            }
        except Exception as e:
            logger.error(f"Error generating signal for {symbol}: {e}")
            return None

    # ── Buy signal — slot rules ───────────────────────────────────────────────

    def _check_buy_signal(self, symbol: str, williams_r: float,
                          live_price: float, prev_close: float) -> bool:
        """
        TRIGGER CONDITIONS (either fires):
          Trigger A: W%R <= -80
          Trigger B: W%R <= -60 AND price dropped >= min_drop% from prev_close

        SLOT RULES:
          Slot 1 (first buy): Trigger A or B fires and no existing holding.
          Slot 2+ (additional buys): Requires BOTH:
            - Symbol already held (slot 1 already executed, possibly on a prior day)
            - Live price is >= min_price_drop_pct% BELOW current avg_buy_price
            - Trigger A or B must still be met (market still oversold)
          The drop check uses the CURRENT avg_buy_price (blended across all prior
          buys), so the rule works correctly whether slot 1 was today or weeks ago.
          Each slot requires the same flat min_drop% below avg — not cumulative.
          Slots are counted across the current day only (_buys_today resets at
          midnight) but avg_buy_price persists across days via portfolio sync.
        """
        wr_val   = williams_r if williams_r is not None else 0.0
        min_drop = self._get_min_price_drop_pct()

        drop_from_prev = ((prev_close - live_price) / prev_close * 100) if prev_close > 0 else 0.0

        trigger_a = wr_val <= Config.WILLIAMS_R_THRESHOLD          # W%R <= -80
        trigger_b = (wr_val <= -60) and (drop_from_prev >= min_drop)

        if not trigger_a and not trigger_b:
            logger.debug(
                f"No trigger {symbol}: W%R={wr_val:.1f} "
                f"(A<={Config.WILLIAMS_R_THRESHOLD}, "
                f"B<=-60+drop>={min_drop}%), drop={drop_from_prev:.2f}%"
            )
            return False

        trigger_str = []
        if trigger_a: trigger_str.append(f"A(W%R={wr_val:.1f})")
        if trigger_b: trigger_str.append(f"B(W%R={wr_val:.1f}+drop={drop_from_prev:.2f}%)")

        max_slots     = self._get_max_slots()
        buys_today    = self._get_buys_today(symbol)
        avg_buy_price = self._get_avg_buy_price(symbol)
        is_held       = avg_buy_price is not None

        # ── Slot 1: first entry ───────────────────────────────────────────
        if not is_held:
            if buys_today >= 1:
                logger.debug(
                    f"Skip {symbol}: slot-1 already used today "
                    f"(buys_today={buys_today}, not held — sold and not re-entering)"
                )
                return False
            # Falls through to common guards below

        # ── Slot 2+: averaging down ───────────────────────────────────────
        else:
            slots_used = buys_today   # each buy = 1 slot
            if slots_used >= max_slots:
                logger.debug(
                    f"Skip {symbol}: max slots reached "
                    f"({slots_used}/{max_slots})"
                )
                return False

            # Additional slot requires price to be >= min_drop% below the
            # CURRENT avg_buy_price (flat threshold, same for every slot).
            # avg_buy_price is blended across ALL prior buys (including those
            # from previous days), so this check works across multiple sessions.
            drop_from_avg = ((avg_buy_price - live_price) / avg_buy_price * 100)

            if drop_from_avg < min_drop:
                logger.debug(
                    f"Skip {symbol} slot-{slots_used+1}: "
                    f"price ₹{live_price:.2f} only {drop_from_avg:.2f}% below avg "
                    f"₹{avg_buy_price:.2f} (need >= {min_drop:.1f}% for next slot)"
                )
                return False

            logger.info(
                f"📉 {symbol}: slot-{slots_used+1} averaging down — "
                f"₹{live_price:.2f} is {drop_from_avg:.2f}% below avg ₹{avg_buy_price:.2f} "
                f"(threshold {min_drop:.1f}%)"
            )

        # ── Common guards ─────────────────────────────────────────────────

        # Max cash per stock
        max_cash_stock = self._get_max_cash_per_stock()
        if max_cash_stock > 0:
            deployed = self._get_cash_deployed(symbol)
            if deployed >= max_cash_stock:
                logger.debug(
                    f"Skip {symbol}: deployed ₹{deployed:.0f} >= limit ₹{max_cash_stock:.0f}"
                )
                return False

        # Sufficient LIQUIDCASE
        max_tx = self._get_max_cash_per_transaction()
        liq_price = self.realtime.get_ltp(LIQUIDCASE_SYMBOL)
        if max_tx > 0 and liq_price and liq_price > 0:
            liq_value = self.portfolio.liquidcase_quantity * liq_price
            if liq_value < max_tx:
                logger.debug(
                    f"Skip {symbol}: LIQUIDCASE ₹{liq_value:.0f} < tx ₹{max_tx:.0f}"
                )
                return False

        # Not already queued
        if symbol in self.pending_buys:
            logger.debug(f"Skip {symbol}: already queued")
            return False

        # Already attempted this session (latched at queue time — covers the
        # 2s window between get_due_buys() clearing pending_buys and the
        # executor setting executing_symbols)
        if symbol in self._attempted_today:
            logger.debug(f"Skip {symbol}: already attempted today")
            return False

        # Within post-buy cooldown (5 min after a confirmed buy)
        if symbol in self.recently_bought:
            elapsed = (datetime.now() - self.recently_bought[symbol]).total_seconds()
            if elapsed < 300:
                logger.debug(
                    f"Skip {symbol}: within post-buy cooldown ({elapsed:.0f}s / 300s)"
                )
                return False

        logger.info(
            f"✅ BUY SIGNAL [{', '.join(trigger_str)}]: {symbol} | "
            f"Price=₹{live_price:.2f} | PrevClose=₹{prev_close:.2f} | "
            f"AvgBuy={'₹'+str(round(avg_buy_price,2)) if avg_buy_price else 'None'} | "
            f"Slot {(self._get_buys_today(symbol))+1}/{max_slots}"
        )
        return True

    # ── Sell signal ───────────────────────────────────────────────────────────

    def _check_sell_signal(self, symbol: str, current_price: float) -> bool:
        # Cooldown: don't re-signal within 10s of last signal (prevents duplicate orders)
        if symbol in self.recently_sold:
            if datetime.now() - self.recently_sold[symbol] < timedelta(seconds=10):
                return False
            del self.recently_sold[symbol]

        # Use get_quantity_held directly — does NOT depend on the locked_symbols cache
        # which can be stale between portfolio.sync() calls.
        qty_held = self.portfolio.get_quantity_held(symbol)
        if qty_held <= 0:
            logger.debug(f"No sell signal {symbol}: qty_held={qty_held}")
            return False

        avg_price = self.portfolio.get_average_price(symbol)
        if avg_price is None or avg_price <= 0:
            logger.warning(f"No sell signal {symbol}: avg_price unavailable")
            return False

        profit_pct = ((current_price - avg_price) / avg_price) * 100
        target = self._get_profit_target()

        if profit_pct >= target:
            logger.info(
                f"✅ SELL SIGNAL: {symbol} | "
                f"LTP=₹{current_price:.2f} Avg=₹{avg_price:.2f} "
                f"Profit={profit_pct:.2f}% (Target={target}%)"
            )
            self.recently_sold[symbol] = datetime.now()
            return True

        logger.debug(f"No sell signal {symbol}: profit={profit_pct:.2f}% < target={target}%")
        return False

    def force_execute_all(self) -> List[dict]:
        """
        Return all signals currently meeting buy conditions immediately,
        bypassing the scheduled execution time gate.
        Used by the 'Trade All Now' button in the Controls tab.
        """
        now = datetime.now()
        forced = []
        # Get fresh signals (re-evaluates W%R etc.)
        all_signals = self.generate_signals()
        for symbol, signal in all_signals.items():
            if signal.get('signal') != 'BUY':
                continue
            if symbol in self.executing_symbols:
                continue
            if symbol in self.recently_bought:
                elapsed = (now - self.recently_bought[symbol]).total_seconds()
                if elapsed < 300:
                    continue
            # Remove from pending queue if already there
            # NOTE: recently_bought is set by executor after successful order placement,
            # NOT here — pre-marking caused the Activity tab BUY signal to persist
            # while force_buy/execute skipped the symbol due to false cooldown.
            self.pending_buys.pop(symbol, None)
            forced.append(signal)
            logger.info(f"⚡ FORCE EXECUTE: {symbol} (W%R={signal.get('williams_r','N/A')})")
        return forced

    # ── Scheduled buy queue ───────────────────────────────────────────────────

    def queue_pending_buy(self, symbol: str, signal: dict):
        # Don't re-queue if already queued
        if symbol in self.pending_buys:
            return
        # Don't re-queue within 5 minutes of a buy firing (execution cooldown)
        if symbol in self.recently_bought:
            elapsed = (datetime.now() - self.recently_bought[symbol]).total_seconds()
            if elapsed < 300:
                logger.debug(
                    f"Skip re-queue {symbol}: bought {elapsed:.0f}s ago "
                    f"(cooldown 300s)"
                )
                return
            else:
                del self.recently_bought[symbol]
        # Don't re-queue while execution lock is held
        if symbol in self.executing_symbols:
            return
        exec_time = self._get_buy_execution_time()   # None = anytime
        self.pending_buys[symbol] = {
            'scheduled_time': exec_time,   # None signals no time gate
            'signal':         signal,
            'queued_at':      datetime.now(),
        }
        if exec_time is None:
            logger.info(
                f"⚡ QUEUED (anytime): {symbol} — executes on next signal check "
                f"(W%R={signal.get('williams_r','N/A')})"
            )
        else:
            logger.info(
                f"⏰ QUEUED: {symbol} @ {exec_time.strftime('%H:%M')} "
                f"(W%R={signal.get('williams_r','N/A')})"
            )

    def get_due_buys(self) -> List[dict]:
        now = datetime.now()
        due, remove = [], []
        for symbol, entry in self.pending_buys.items():
            sched = entry['scheduled_time']   # None = anytime, dtime = scheduled

            if sched is None:
                # Anytime mode — execute immediately whenever conditions are met
                due.append(entry['signal'])
                remove.append(symbol)
                self.recently_bought[symbol] = now
                self._attempted_today.add(symbol)   # latch — blocks re-trigger this session
                logger.info(f"⚡ Buy due (anytime): {symbol}")
            else:
                sched_dt   = now.replace(hour=sched.hour, minute=sched.minute,
                                         second=0, microsecond=0)
                window_end = sched_dt + timedelta(minutes=1)
                if sched_dt <= now < window_end:
                    due.append(entry['signal'])
                    remove.append(symbol)
                    self.recently_bought[symbol] = now
                    self._attempted_today.add(symbol)   # latch — blocks re-trigger this session
                    logger.info(f"⏰ Buy due: {symbol}")
        for s in remove:
            del self.pending_buys[s]
        return due

    def expire_stale_pending_buys(self):
        stale = []
        now = datetime.now()
        for symbol, entry in self.pending_buys.items():
            sched = entry['scheduled_time']
            if sched is None:
                continue   # anytime entries never expire — signal_generator handles cooldown
            sched_dt = now.replace(hour=sched.hour, minute=sched.minute, second=0, microsecond=0)
            if now > sched_dt + timedelta(minutes=5):
                stale.append(symbol)
                logger.warning(f"🗑️ Expired stale pending buy: {symbol}")
        for s in stale:
            del self.pending_buys[s]

    # ── Execution locking ─────────────────────────────────────────────────────

    def lock_symbol_for_execution(self, symbol: str, signal_type: str):
        self.executing_symbols[symbol] = {
            'type': signal_type, 'timestamp': datetime.now()
        }

    def unlock_symbol(self, symbol: str, success: bool = False):
        """
        Release execution lock.
        success=False (default): clear recently_bought and _attempted_today so a retry is allowed.
        success=True           : SET recently_bought now (order placed) and keep _attempted_today —
                                 recently_bought enforces 5-min cooldown,
                                 _attempted_today prevents any further buy this session.
        """
        if symbol in self.executing_symbols:
            self.executing_symbols.pop(symbol)
        if success:
            # Order placed — set cooldown NOW (was not pre-set; only set after confirmed execution)
            self.recently_bought[symbol] = datetime.now()
        else:
            # Failed execution — clear guards so next cycle can retry
            if symbol in self.recently_bought:
                del self.recently_bought[symbol]
            self._attempted_today.discard(symbol)
        # On success: both guards stay set — no second buy this session for this symbol.

    # ── Public interface ──────────────────────────────────────────────────────


    def get_buy_signals_direct(self) -> List[dict]:
        """
        Check buy conditions using live W%R where available, falling back to
        cached historical W%R if live data is temporarily unavailable.
        Used by Force Buy Now so a transient W%R miss doesn't silently block orders.
        Standard slot/cooldown/cash guards still apply.
        """
        buy_signals = []
        for symbol in Config.get_active_etfs():
            if symbol == LIQUIDCASE_SYMBOL:
                continue
            if symbol in self.executing_symbols:
                continue
            if symbol in self.recently_bought:
                elapsed = (datetime.now() - self.recently_bought[symbol]).total_seconds()
                if elapsed < 300:
                    continue
            try:
                live_price = self.realtime.get_ltp(symbol)
                if live_price is None:
                    logger.warning(f"force_buy: no LTP for {symbol}, skipping")
                    continue

                # Try live W%R first; fall back to last cached value
                daily_data = self.historical.get_daily_data(symbol)
                williams_r = None
                if daily_data is not None and len(daily_data) > 0:
                    live_ohlc = self.realtime.get_ohlc(symbol) or {}
                    williams_r = calculate_daily_williams_r(
                        daily_data,
                        live_price=live_price,
                        live_high=live_ohlc.get('high'),
                        live_low=live_ohlc.get('low'),
                    )

                if williams_r is None:
                    logger.warning(f"force_buy: W%R unavailable for {symbol}, skipping")
                    continue

                prev_close = float(daily_data.iloc[-1]['close']) if daily_data is not None and len(daily_data) > 0 else live_price
                if self._check_buy_signal(symbol, williams_r, live_price, prev_close):
                    signal = {
                        'symbol':     symbol,
                        'signal':     SIGNAL_BUY,
                        'williams_r': williams_r,
                        'price':      live_price,
                        'prev_close': prev_close,
                        'timestamp':  datetime.now(),
                    }
                    # NOTE: Do NOT set recently_bought here — that must only happen
                    # after the order is successfully placed (executor handles it).
                    # Pre-marking here caused the Activity tab to show BUY while
                    # force_buy silently skipped the symbol due to cooldown.
                    self.pending_buys.pop(symbol, None)
                    buy_signals.append(signal)
                    logger.info(f"⚡ FORCE BUY: {symbol} W%R={williams_r:.1f}")
            except Exception as e:
                logger.error(f"get_buy_signals_direct error for {symbol}: {e}")
        return buy_signals

    def get_sell_signals_direct(self) -> List[dict]:
        """
        Check sell conditions for all active ETFs directly, without requiring
        W%R or historical data. Used by Force Sell Now so a symbol meeting the
        profit target is never silently skipped due to a missing W%R value.
        """
        sell_signals = []
        for symbol in Config.get_active_etfs():
            if symbol == LIQUIDCASE_SYMBOL:
                continue
            if symbol in self.executing_symbols:
                continue
            try:
                live_price = self.realtime.get_ltp(symbol)
                if live_price is None:
                    logger.warning(f"force_sell: no LTP for {symbol}, skipping")
                    continue
                if self._check_sell_signal(symbol, live_price):
                    sell_signals.append({
                        'symbol':    symbol,
                        'signal':    SIGNAL_SELL,
                        'price':     live_price,
                        'timestamp': datetime.now(),
                    })
            except Exception as e:
                logger.error(f"get_sell_signals_direct error for {symbol}: {e}")
        return sell_signals

    def get_active_signals(self) -> Dict[str, List[dict]]:
        all_signals  = self.generate_signals()
        buy_signals  = []
        sell_signals = []

        for symbol, signal in all_signals.items():
            if signal['signal'] == SIGNAL_BUY:
                self.queue_pending_buy(symbol, signal)
                buy_signals.append(signal)
            elif signal['signal'] == SIGNAL_SELL:
                sell_signals.append(signal)

        due_buys = self.get_due_buys()
        self.expire_stale_pending_buys()

        return {
            'buy':    due_buys,
            'sell':   sell_signals,
            'queued': buy_signals,
        }
