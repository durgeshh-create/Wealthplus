"""
Dip Accumulator & Harvester Engine — multi-symbol
Each symbol in settings['bnh_symbols'] is tracked independently.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from backend.core.config import Config
from backend.core.constants import LIQUIDCASE_SYMBOL
from backend.indicators.calculator import calculate_daily_williams_r
from backend.utils.logger import get_logger

from datetime import timezone as _tz
_IST = _tz(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    """Return current datetime in IST (works on GitHub Actions UTC runners)."""
    return datetime.now(_IST).replace(tzinfo=None)


logger = get_logger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
WILLIAMS_R_PERIOD  = 14
OVERSOLD_THRESHOLD = -60.0
BUY_HOUR, BUY_MIN  = 15, 15        # 3:15 PM IST
BNH_MIN_DROP_PCT   = 0.5           # min % below avg before adding to position
BNH_SYMBOL         = 'MID150BEES'  # legacy alias kept for any external references

SETTINGS_PATH = Path(__file__).parent.parent.parent / 'config' / 'settings.json'
DATA_DIR      = SETTINGS_PATH.parent.parent / 'data'


# ── Per-symbol runtime state ──────────────────────────────────────────────────

@dataclass
class _SymState:
    symbol:                 str
    session_date:           Optional[date]  = None
    bought_today:           bool            = False
    buy_attempted_today:    bool            = False
    sold_today:             bool            = False
    buy_in_progress:        bool            = False
    tiers_fired_this_cycle: set             = field(default_factory=set)
    total_deployed:         float           = 0.0
    deployed_today:         float           = 0.0
    trade_log:              List[dict]      = field(default_factory=list)
    latest:                 dict            = field(default_factory=dict)
    # Weekday systematic-buy tranche — fires at most once per ISO week,
    # independent of the daily W%R tier reset.
    weekday_buy_attempted:  bool            = False
    weekday_buy_week_key:   Optional[str]   = None
    weekday_total_deployed: float           = 0.0

    def reset_session(self):
        today = _now_ist().date()
        if self.session_date == today:
            return
        self.session_date        = today
        self.bought_today        = False
        self.buy_attempted_today = False
        self.sold_today          = False
        self.deployed_today      = 0.0
        logger.info(f"[{self.symbol}] Session reset for {today}")

    def reset_weekday_buy_if_new_week(self):
        """Weekly latch resets on ISO year-week change, independent of daily session reset."""
        wk = "%d-%02d" % _now_ist().date().isocalendar()[:2]
        if self.weekday_buy_week_key != wk:
            self.weekday_buy_week_key  = wk
            self.weekday_buy_attempted = False


# ── Engine ───────────────────────────────────────────────────────────────────

class IntradayEngine:
    """
    Dip Accumulator & Harvester engine — multi-symbol.
    Each symbol in settings['bnh_symbols'] is tracked independently with its
    own state, tier-firing set and trade log.
    Named IntradayEngine so existing app.py / routes.py wiring is unchanged.
    """

    def __init__(self, realtime_manager, order_manager, portfolio_tracker,
                 historical_manager=None):
        self.realtime   = realtime_manager
        self.orders     = order_manager
        self.portfolio  = portfolio_tracker
        self.historical = historical_manager

        self._running  = False
        self._thread: Optional[threading.Thread] = None
        self._lock     = threading.Lock()

        # Per-symbol state — populated/refreshed on start and when settings change
        self._sym_states: Dict[str, _SymState] = {}

        # Legacy single-symbol attributes kept for any external consumers
        self.trade_log:      List[dict] = []
        self.total_deployed: float      = 0.0
        self.deployed_today: float      = 0.0
        self.latest:         dict       = {}

        self._init_sym_states()
        logger.info(f"Dip Accumulator engine initialised for {self._bnh_symbols()}")

    # ── Settings helpers ──────────────────────────────────────────────────────

    def _s(self, key, default=None):
        try:
            import json
            with open(SETTINGS_PATH, 'r') as f:
                return json.load(f).get(key, default)
        except Exception:
            return default

    def _bnh_symbols(self) -> List[str]:
        return self._s('bnh_symbols', ['MID150BEES']) or ['MID150BEES']

    def _max_cash_per_etf(self) -> float:
        try:    return float(self._s('bnh_max_cash_per_etf', 1_000_000))
        except: return 1_000_000.0

    def _max_cash_per_txn(self) -> float:
        try:    return float(self._s('bnh_max_cash_per_transaction', 20_000))
        except: return 20_000.0

    def _partial_profit_pct(self) -> float:
        try:    return float(self._s('bnh_partial_profit_pct', 5.0))
        except: return 5.0

    def _tiered_sizing(self) -> bool:
        return bool(self._s('bnh_tiered_sizing', True))

    def _order_type(self) -> str:
        return self._s('default_order_type', 'LIMIT')

    # ── Weekday systematic buy (supplementary DCA tranche) ────────────────────
    # Historical analysis (2020–2026 daily data, MID150BEES & MINDSPACE-RR):
    # Monday showed the most consistent (though modest, and partly decayed)
    # weakness vs other weekdays. This tranche is intentionally small — a
    # fraction of bnh_max_cash_per_etf comparable to roughly a third of the
    # lightest W%R tier (Tier 1 = 5%) — because the edge is mild, not a
    # high-conviction oversold signal. It exists purely to add disciplined
    # weekly accumulation on top of the W%R-driven dips, and is fully
    # toggleable since the day-of-week edge can fade further or shift.

    def _weekday_buy_enabled(self) -> bool:
        return bool(self._s('bnh_weekday_buy_enabled', False))

    def _weekday_buy_day(self) -> str:
        """Target weekday name, e.g. 'Thursday'. Case-insensitive match against %A."""
        return str(self._s('bnh_weekday_buy_day', 'Thursday')).strip().capitalize()

    def _weekday_buy_frac(self) -> float:
        """Fraction of bnh_max_cash_per_etf deployed by the weekday tranche per fire."""
        try:    return float(self._s('bnh_weekday_buy_frac', 0.02))
        except: return 0.02

    def _weekday_buy_max_share(self) -> float:
        """
        Lifetime cap on the weekday tranche, as a fraction of bnh_max_cash_per_etf.
        Without this, firing every single week (52x/yr) could out-deploy the
        oversold-tier system in quiet/low-volatility years and crowd out the
        higher-conviction W%R buys. Default 20% keeps it a true supplement —
        the tier system still owns the other 80%+ of the capital budget.
        """
        try:    return float(self._s('bnh_weekday_buy_max_share', 0.20))
        except: return 0.20

    # ── Symbol-state management ───────────────────────────────────────────────

    def _init_sym_states(self):
        for sym in self._bnh_symbols():
            if sym not in self._sym_states:
                self._sym_states[sym] = _SymState(symbol=sym)

    def _sync_sym_states(self):
        """Add new symbols from settings; keep existing state for symbols already tracked."""
        current = set(self._bnh_symbols())
        for sym in current:
            if sym not in self._sym_states:
                self._sym_states[sym] = _SymState(symbol=sym)
                logger.info(f"[{sym}] New BnH symbol added to engine")
        # Keep removed symbols in state (don't delete — preserve trade log)

    # ── Daily session reset ───────────────────────────────────────────────────

    def _reset_sessions(self):
        for st in self._sym_states.values():
            st.reset_session()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> bool:
        with self._lock:
            if self._running:
                logger.warning("Dip Accumulator engine already running")
                return False
            self._running = True
            self._thread = threading.Thread(
                target=self._loop, daemon=True, name='BnHEngine')
            self._thread.start()
            logger.info(f"Dip Accumulator engine STARTED — watching {self._bnh_symbols()}")
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self._running:
                return False
            self._running = False
            logger.info("Dip Accumulator engine STOPPED")
            return True

    # ── Data loaders ──────────────────────────────────────────────────────────

    def _load_csv(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        try:
            csv_path = DATA_DIR / timeframe / f'{symbol}.csv'
            if not csv_path.exists():
                return None
            df = pd.read_csv(csv_path, parse_dates=['date'])
            df.columns = [c.lower() for c in df.columns]
            df = df.sort_values('date').reset_index(drop=True)
            return df
        except Exception as e:
            logger.debug(f"[{symbol}] _load_csv({timeframe}) error: {e}")
            return None

    def _calc_daily_wr(self, symbol: str) -> Optional[float]:
        """Compute daily W%R(14) for a symbol, appending live price as today's candle."""
        try:
            # Try historical manager first
            if (self.historical and
                    hasattr(self.historical, 'cache') and
                    symbol in self.historical.cache and
                    'daily' in self.historical.cache[symbol]):
                df = self.historical.get_daily_data(symbol)
            else:
                df = self._load_csv(symbol, 'daily')

            if df is None or len(df) < WILLIAMS_R_PERIOD:
                return None

            live_price = self.realtime.get_ltp(symbol) if self.realtime else None
            ohlc = self.realtime.get_ohlc(symbol) if self.realtime else None
            return calculate_daily_williams_r(
                df,
                live_price=float(live_price) if live_price else None,
                live_high=float(ohlc['high']) if ohlc and ohlc.get('high') else None,
                live_low=float(ohlc['low'])  if ohlc and ohlc.get('low')  else None,
            )
        except Exception as e:
            logger.debug(f"[{symbol}] _calc_daily_wr error: {e}")
            return None

    # ── Enhancement 2: 5-Tier Exponential Sizing ─────────────────────────────

    TIERS = [
        # (wr_threshold,  fraction_of_max_etf)
        (-60,  0.05),
        (-70,  0.10),
        (-80,  0.20),
        (-90,  0.30),
        (-95,  0.35),
    ]

    def _current_tier(self, wr: float):
        """Return (tier_index, fraction) for the deepest applicable tier."""
        tier_idx  = None
        tier_frac = 0.0
        for idx, (threshold, frac) in enumerate(self.TIERS):
            if wr <= threshold:
                tier_idx  = idx
                tier_frac = frac
        return tier_idx, tier_frac

    def _tiered_budget(self, wr: float, max_etf: float, st: _SymState) -> float:
        """Return the budget for the current tier, or 0 if already fired."""
        if not self._tiered_sizing():
            txn = self._max_cash_per_txn()
            return txn if not st.tiers_fired_this_cycle else 0.0
        tier_idx, frac = self._current_tier(wr)
        if tier_idx is None:
            return 0.0
        if tier_idx in st.tiers_fired_this_cycle:
            return 0.0
        return frac * max_etf

    def _mark_tier_fired(self, wr: float, st: _SymState):
        tier_idx, _ = self._current_tier(wr)
        if tier_idx is not None:
            st.tiers_fired_this_cycle.add(tier_idx)
            logger.info(f"[{st.symbol}] Tier {tier_idx} fired (fired set: {sorted(st.tiers_fired_this_cycle)})")

    def _check_dip_cycle_reset(self, wr: Optional[float], st: _SymState):
        if wr is not None and wr > -40 and st.tiers_fired_this_cycle:
            logger.info(f"[{st.symbol}] W%R {wr:.1f} > -40 — dip cycle reset, tiers re-armed")
            st.tiers_fired_this_cycle.clear()

    # ── Enhancement 4: Partial profit taking ─────────────────────────────────

    def _check_partial_sell(self, now: datetime, st: _SymState):
        """Sell 50% of position if P&L ≥ harvest target, with 30-day cooldown."""
        symbol     = st.symbol
        target_pct = self._partial_profit_pct()
        try:
            qty = self.portfolio.get_quantity_held(symbol) if self.portfolio else 0
            if qty <= 0:
                return
            avg = self.portfolio.get_average_price(symbol) if self.portfolio else None
            if not avg or avg <= 0:
                return
            ltp = self.realtime.get_ltp(symbol) if self.realtime else None
            if not ltp or ltp <= 0:
                return
            pnl_pct = (ltp - avg) / avg * 100
            if pnl_pct < target_pct:
                return

            # 30-day cooldown
            sell_trades = [t for t in st.trade_log if t.get('action') == 'PARTIAL_SELL']
            if sell_trades:
                last_sell = datetime.strptime(
                    sell_trades[-1]['date'] + ' ' + sell_trades[-1]['time'],
                    '%Y-%m-%d %H:%M:%S')
                if (now - last_sell).days < 30:
                    logger.info(f"[{symbol}] Harvest cooldown active — last sell {last_sell.date()}")
                    return

            sell_qty = max(1, int(qty // 2))
            order_type  = self._order_type()
            limit_price = None
            try:
                top_ask = self.realtime.get_top_ask(symbol)
                if order_type == 'LIMIT' and top_ask and top_ask > 0:
                    limit_price = round(float(top_ask), 2)
            except Exception:
                pass

            success, _ = self.orders.smart_sell(
                symbol=symbol,
                quantity=sell_qty,
                portfolio_tracker=self.portfolio,
                sell_order_type=order_type,
                sell_limit_price=limit_price,
                sell_product='CNC',
            )
            if success:
                proceeds = sell_qty * (limit_price or float(ltp))
                self._log_trade(st, action='PARTIAL_SELL', price=limit_price or float(ltp),
                                qty=sell_qty, amount=round(proceeds, 2), wr=None,
                                funded_by='—',
                                reason=f"Harvest: P&L {pnl_pct:.1f}% ≥ {target_pct}% | {order_type}",
                                now=now)
                st.sold_today = True
                logger.info(f"[{symbol}] ✅ Partial sell {sell_qty} × ₹{ltp:.2f} = ₹{proceeds:.0f}")
        except Exception as e:
            logger.error(f"[{symbol}] _check_partial_sell error: {e}", exc_info=True)

    # ── Core buy logic ────────────────────────────────────────────────────────

    def _execute_buy(self, wr: float, now: datetime, st: _SymState):
        """Execute a buy for one symbol according to tier budget and guard rules."""
        symbol  = st.symbol
        max_etf = self._max_cash_per_etf()

        if st.total_deployed >= max_etf:
            logger.info(f"[{symbol}] Cumulative cap ₹{max_etf:.0f} reached — skipping")
            return

        raw_budget = self._tiered_budget(wr, max_etf, st)
        if raw_budget <= 0.0:
            return

        budget = min(raw_budget, max_etf - st.total_deployed)

        ltp = self.realtime.get_ltp(symbol) if self.realtime else None
        if not ltp or ltp <= 0:
            logger.warning(f"[{symbol}] Cannot get LTP — skipping")
            return

        # Avg-price drop guard (subsequent buys only)
        avg_buy_price = None
        qty_held = 0
        try:
            if self.portfolio:
                qty_held = self.portfolio.get_quantity_held(symbol)
                if qty_held > 0:
                    avg_buy_price = self.portfolio.get_average_price(symbol)
        except Exception:
            pass

        if qty_held > 0 and avg_buy_price and avg_buy_price > 0:
            drop_from_avg = (avg_buy_price - ltp) / avg_buy_price * 100
            if drop_from_avg < BNH_MIN_DROP_PCT:
                logger.info(f"[{symbol}] Skip — LTP ₹{ltp:.2f} only {drop_from_avg:.2f}% below avg ₹{avg_buy_price:.2f}")
                return

        qty = int(budget // float(ltp))
        if qty <= 0:
            logger.warning(f"[{symbol}] qty=0 at LTP ₹{ltp:.2f} budget ₹{budget:.0f} — skipping")
            return

        order_type  = self._order_type()
        limit_price = None
        try:
            top_ask = self.realtime.get_top_ask(symbol)
            if order_type == 'LIMIT' and top_ask and top_ask > 0:
                limit_price = round(float(top_ask), 2)
        except Exception:
            pass

        cash_reserve = Config.get_cash_reserve()
        try:
            success = self.orders.smart_buy(
                buy_symbol=symbol,
                buy_quantity=qty,
                buy_price_estimate=float(ltp),
                realtime_manager=self.realtime,
                portfolio_tracker=self.portfolio,
                buy_order_type=order_type,
                buy_limit_price=limit_price,
                buy_product='CNC',
                cash_reserve=cash_reserve,
            )
        except RuntimeError as e:
            logger.error(f"[{symbol}] smart_buy failed: {e}")
            return

        if not success:
            logger.error(f"[{symbol}] smart_buy returned False")
            return

        exec_price = limit_price if (order_type == 'LIMIT' and limit_price) else float(ltp)
        cost = round(exec_price * qty, 2)
        self._mark_tier_fired(wr, st)
        st.total_deployed += cost
        st.deployed_today += cost
        st.bought_today    = True
        # sync legacy totals
        self.total_deployed += cost
        self.deployed_today += cost

        avail_cash = 0.0
        try:    avail_cash = self.orders.get_available_cash()
        except: pass
        funded_by = 'Available Cash' if avail_cash >= cost else 'LIQUIDCASE + Cash'

        self._log_trade(st, action='BUY', price=exec_price, qty=qty, amount=cost,
                        wr=wr, funded_by=funded_by,
                        reason=(f"Daily W%R {wr:.1f} ≤ {OVERSOLD_THRESHOLD}"
                                + (f" | {(avg_buy_price - ltp) / avg_buy_price * 100:.2f}% below avg ₹{avg_buy_price:.2f}"
                                   if qty_held > 0 and avg_buy_price else " | first entry")
                                + f" | {order_type}"),
                        now=now)
        logger.info(f"[{symbol}] ✅ BUY {qty} × ₹{exec_price:.2f} = ₹{cost:.0f} | {order_type} | {funded_by}")

    # ── Weekday systematic buy (supplementary DCA tranche) ───────────────────

    def _execute_weekday_buy(self, now: datetime, st: _SymState) -> Optional[bool]:
        """
        Once-per-week, day-gated supplementary buy — independent of the W%R
        tier system AND independent of buy_execution_time. Always evaluated
        by the caller at the fixed 3:15 PM IST window (see BUY_HOUR/BUY_MIN),
        even when the tier system runs in "anytime" mode. Small fixed
        fraction of bnh_max_cash_per_etf, same cumulative cap and funding
        path as the tier engine. Skipped if a W%R tier already deployed
        capital for this symbol today (avoids double-dipping the same
        session) or if the cumulative ETF cap is hit.

        Return value tells the caller whether to latch the weekly attempt:
          True  — order reached the broker (success or rejection) OR a cap/
                  budget condition makes retrying later today pointless.
                  Latch so we don't re-check uselessly every 15s all day.
          False — transient pre-flight miss (LTP not ready yet). Don't latch;
                  retry later the same day instead of burning the whole week.
        """
        symbol  = st.symbol
        max_etf = self._max_cash_per_etf()

        if st.total_deployed >= max_etf:
            logger.debug(f"[{symbol}] weekday-buy: cumulative cap reached — skipping")
            return True   # permanent for the week — no point retrying
        if st.bought_today:
            logger.debug(f"[{symbol}] weekday-buy: W%R tier already bought today — skipping to avoid double-dip")
            return True   # today's window is closed either way; tomorrow isn't the target day anyway

        weekday_cap = self._weekday_buy_max_share() * max_etf
        if st.weekday_total_deployed >= weekday_cap:
            logger.debug(f"[{symbol}] weekday-buy: lifetime tranche cap ₹{weekday_cap:,.0f} reached — "
                         f"tier system now owns all remaining budget")
            return True   # permanent — tranche is fully spent, will never un-spend

        raw_budget = self._weekday_buy_frac() * max_etf
        budget     = min(raw_budget, self._max_cash_per_txn(),
                          max_etf - st.total_deployed,
                          weekday_cap - st.weekday_total_deployed)
        if budget <= 0:
            return True   # same as above — structurally zero budget, won't change today

        ltp = self.realtime.get_ltp(symbol) if self.realtime else None
        if not ltp or ltp <= 0:
            logger.debug(f"[{symbol}] weekday-buy: LTP not ready yet — will retry later today")
            return False  # transient — market data may not be ready yet

        qty = int(budget // float(ltp))
        if qty <= 0:
            logger.warning(f"[{symbol}] weekday-buy: qty=0 at LTP ₹{ltp:.2f} budget ₹{budget:.0f} — skipping")
            return True   # price/budget mismatch won't resolve itself intraday

        order_type  = self._order_type()
        limit_price = None
        try:
            top_ask = self.realtime.get_top_ask(symbol)
            if order_type == 'LIMIT' and top_ask and top_ask > 0:
                limit_price = round(float(top_ask), 2)
        except Exception:
            pass

        cash_reserve = Config.get_cash_reserve()
        try:
            success = self.orders.smart_buy(
                buy_symbol=symbol,
                buy_quantity=qty,
                buy_price_estimate=float(ltp),
                realtime_manager=self.realtime,
                portfolio_tracker=self.portfolio,
                buy_order_type=order_type,
                buy_limit_price=limit_price,
                buy_product='CNC',
                cash_reserve=cash_reserve,
            )
        except RuntimeError as e:
            logger.error(f"[{symbol}] weekday-buy smart_buy failed: {e}")
            # Order never reached the broker — treat as transient, allow retry today.
            return False

        if not success:
            logger.error(f"[{symbol}] weekday-buy smart_buy returned False")
            # Broker rejected or order outcome uncertain — do NOT retry again
            # this week; avoids duplicate orders if the broker silently
            # accepted it.
            return True

        exec_price = limit_price if (order_type == 'LIMIT' and limit_price) else float(ltp)
        cost = round(exec_price * qty, 2)
        st.total_deployed         += cost
        st.deployed_today         += cost
        st.weekday_total_deployed += cost
        st.bought_today    = True
        self.total_deployed += cost
        self.deployed_today += cost

        avail_cash = 0.0
        try:    avail_cash = self.orders.get_available_cash()
        except: pass
        funded_by = 'Available Cash' if avail_cash >= cost else 'LIQUIDCASE + Cash'

        self._log_trade(st, action='BUY', price=exec_price, qty=qty, amount=cost,
                        wr=None, funded_by=funded_by,
                        reason=(f"Weekly systematic buy ({self._weekday_buy_day()}) — "
                                f"{self._weekday_buy_frac()*100:.1f}% of Max Cash/ETF | {order_type}"),
                        now=now)
        logger.info(f"[{symbol}] ✅ WEEKDAY BUY {qty} × ₹{exec_price:.2f} = ₹{cost:.0f} | {order_type} | {funded_by}")
        return True

    def force_buy_now(self) -> dict:
        """Immediately execute buys for all BnH symbols bypassing the time gate."""
        results = []
        for sym, st in self._sym_states.items():
            if sym not in self._bnh_symbols():
                continue
            if st.buy_in_progress or st.buy_attempted_today or st.bought_today:
                results.append({'symbol': sym, 'success': False, 'reason': 'Already bought today or in progress'})
                continue
            wr = self._calc_daily_wr(sym)
            if wr is None:
                results.append({'symbol': sym, 'success': False, 'reason': 'W%R data not available'})
                continue
            if wr > OVERSOLD_THRESHOLD:
                results.append({'symbol': sym, 'success': False, 'reason': f'W%R {wr:.1f} not oversold'})
                continue
            now = _now_ist()
            st.buy_attempted_today = True
            st.buy_in_progress     = True
            try:
                self._execute_buy(wr, now, st)
                results.append({'symbol': sym, 'success': st.bought_today, 'wr': wr})
            except Exception as e:
                results.append({'symbol': sym, 'success': False, 'reason': str(e)})
            finally:
                st.buy_in_progress = False

        overall = any(r['success'] for r in results)
        # Legacy compat: return first result shape for existing callers
        if results:
            return {'success': overall, 'symbol': results[0].get('symbol', BNH_SYMBOL),
                    'wr': results[0].get('wr'), 'results': results}
        return {'success': False, 'reason': 'No BnH symbols configured'}

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _loop(self):
        logger.info("Dip Accumulator engine loop started")
        while self._running:
            try:
                self._reset_sessions()
                self._sync_sym_states()
                now  = _now_ist()
                t    = now.time()

                exec_val = self._s('buy_execution_time', '15:15')
                anytime  = (exec_val == 'anytime')
                at_trigger = anytime or (dtime(BUY_HOUR, BUY_MIN) <= t <= dtime(BUY_HOUR, BUY_MIN + 1))

                for sym in self._bnh_symbols():
                    st = self._sym_states.get(sym)
                    if not st:
                        continue
                    try:
                        wr = self._calc_daily_wr(sym)
                        self._refresh_latest_sym(now, wr, st)
                        self._check_dip_cycle_reset(wr, st)

                        if at_trigger and not st.buy_in_progress:
                            if not st.sold_today:
                                self._check_partial_sell(now, st)

                            if (not st.buy_attempted_today
                                    and not st.bought_today
                                    and wr is not None
                                    and wr <= OVERSOLD_THRESHOLD):
                                st.buy_attempted_today = True
                                st.buy_in_progress     = True
                                trigger_label = 'anytime' if anytime else '3:15 PM'
                                logger.info(f"[{sym}] ⏰ {trigger_label} — W%R={wr:.1f}: executing buy")
                                try:
                                    self._execute_buy(wr, now, st)
                                finally:
                                    st.buy_in_progress = False

                        # Weekday systematic buy — runs independently of the W%R
                        # tier system AND independently of buy_execution_time.
                        # Always fires at the fixed 3:15 PM IST window (BUY_HOUR/
                        # BUY_MIN), even when the tier system is set to "anytime" —
                        # this is a scheduled weekly DCA tranche, not an oversold
                        # signal, so it deliberately stays anchored to one
                        # predictable time of day rather than firing as soon as
                        # the weekday starts. Evaluated outside the at_trigger
                        # block above on purpose: in "anytime" mode the tier buy
                        # can fire well before 3:15 PM, and we don't want that
                        # early tier fire to look like a same-day collision for
                        # a tranche that hasn't had its 3:15 PM window yet.
                        # Fires at most once per ISO week, only on the configured
                        # weekday, and skips if a tier already bought this symbol
                        # today (see _execute_weekday_buy). The weekly latch is
                        # only set if the attempt genuinely reached the broker —
                        # a pre-flight skip (e.g. LTP not ready yet at 3:15 PM)
                        # retries on the next loop tick within the same
                        # 3:15–3:16 PM window instead of burning the week's attempt.
                        if not st.buy_in_progress:
                            # Widened from a hard 15:15-15:16 window: a transient
                            # data outage (e.g. WebSocket down) spanning that
                            # single minute used to cost the whole week's
                            # systematic buy with no way to recover same-day.
                            # Now retries for 10 minutes past the nominal 3:15 PM
                            # trigger before giving up for the week.
                            at_weekday_trigger = (dtime(BUY_HOUR, BUY_MIN) <= t <= dtime(BUY_HOUR, BUY_MIN + 10))
                            st.reset_weekday_buy_if_new_week()

                            # ── Thursday → Friday holiday fallback ───────────────────────
                            # NSE holidays where markets are closed (updated annually).
                            # If the configured day is Thursday and today is Thursday but
                            # it's an NSE holiday, fire on Friday instead.
                            _NSE_HOLIDAYS_2025_2026 = {
                                # 2025
                                "2025-01-26", "2025-02-26", "2025-03-14", "2025-03-31",
                                "2025-04-10", "2025-04-14", "2025-04-18", "2025-05-01",
                                "2025-08-15", "2025-08-27", "2025-10-02", "2025-10-02",
                                "2025-10-21", "2025-10-22", "2025-10-24", "2025-11-05",
                                "2025-11-24", "2025-12-25",
                                # 2026
                                "2026-01-26", "2026-03-02", "2026-03-25", "2026-04-02",
                                "2026-04-10", "2026-04-14", "2026-04-30", "2026-05-01",
                                "2026-06-01", "2026-08-15", "2026-08-27", "2026-10-02",
                                "2026-10-21", "2026-10-22", "2026-11-05", "2026-11-24",
                                "2026-12-25",
                            }
                            _target_day = self._weekday_buy_day()   # e.g. "Thursday"
                            _today_str  = now.strftime('%Y-%m-%d')
                            _today_name = now.strftime('%A')         # e.g. "Thursday"
                            _effective_day = _target_day

                            if (_target_day == 'Thursday'
                                    and _today_name == 'Thursday'
                                    and _today_str in _NSE_HOLIDAYS_2025_2026):
                                # Thursday is a market holiday — shift to Friday
                                _effective_day = 'Friday'
                                logger.info(
                                    f"Weekday buy: Thursday {_today_str} is NSE holiday "
                                    f"— shifting systematic buy to Friday"
                                )
                            # ─────────────────────────────────────────────────────────────

                            if (self._weekday_buy_enabled()
                                    and not st.weekday_buy_attempted
                                    and _today_name == _effective_day
                                    and at_weekday_trigger):
                                st.buy_in_progress = True
                                try:
                                    should_latch = self._execute_weekday_buy(now, st)
                                    if should_latch:
                                        st.weekday_buy_attempted = True
                                finally:
                                    st.buy_in_progress = False
                    except Exception as e:
                        logger.error(f"[{sym}] loop tick error: {e}", exc_info=True)

                # Refresh legacy single-symbol latest for backward compat
                first_sym = self._bnh_symbols()[0] if self._bnh_symbols() else None
                if first_sym and first_sym in self._sym_states:
                    self.latest = self._sym_states[first_sym].latest

                time.sleep(15)

            except Exception as e:
                logger.error(f"Dip Accumulator loop error: {e}", exc_info=True)
                time.sleep(30)

        logger.info("Dip Accumulator engine loop exited")

    # ── Dashboard helpers ─────────────────────────────────────────────────────

    def _log_trade(self, st: _SymState, action: str, price: float, qty: int,
                   amount: float, wr: Optional[float], funded_by: str,
                   reason: str, now: datetime):
        entry = {
            'date':      now.strftime('%Y-%m-%d'),
            'time':      now.strftime('%H:%M:%S'),
            'action':    action,
            'price':     round(float(price), 2),
            'qty':       int(qty),
            'amount':    round(float(amount), 2),
            'wr':        wr,
            'funded_by': funded_by,
            'reason':    reason,
            'symbol':    st.symbol,
        }
        st.trade_log.append(entry)
        self.trade_log.append(entry)  # legacy combined log

    def _bnh_holdings(self, symbol: str) -> dict:
        try:
            if not self.portfolio:
                raise ValueError("no portfolio")
            qty_from_holdings = 0
            ltp_fallback      = 0.0
            for h in (self.portfolio.holdings or []):
                sym = h.get('tradingsymbol') or h.get('symbol', '')
                if sym == symbol:
                    free_qty    = int(h.get('quantity',            0) or 0)
                    pledged_qty = int(h.get('collateral_quantity', 0) or 0)
                    t1_qty      = int(h.get('t1_quantity',         0) or 0)
                    qty_from_holdings = free_qty + pledged_qty + t1_qty
                    ltp_fallback = float(h.get('last_price', 0) or 0)
                    break
            qty_from_positions = 0
            for pos in (self.portfolio.positions.get('net', []) or []):
                sym = pos.get('tradingsymbol') or pos.get('symbol', '')
                if sym == symbol:
                    qty_from_positions = int(pos.get('quantity', 0) or 0)
                    break
            total_qty = qty_from_holdings + qty_from_positions
            if total_qty <= 0:
                return {'qty': 0, 'avg_price': None, 'current_value': None,
                        'pnl_amt': None, 'pnl_pct': None}
            avg = self.portfolio.get_average_price(symbol)
            ltp = None
            try:
                raw = self.realtime.get_ltp(symbol) if self.realtime else None
                ltp = float(raw) if raw else ltp_fallback or None
            except Exception:
                ltp = ltp_fallback or None
            cur_val = round(total_qty * ltp, 2)         if ltp else None
            cost    = round(total_qty * avg, 2)         if avg else None
            pnl_amt = round(cur_val  - cost, 2)         if (cur_val and cost) else None
            pnl_pct = round((ltp - avg) / avg * 100, 2) if (ltp and avg) else None
            return {'qty': total_qty, 'avg_price': avg, 'current_value': cur_val,
                    'pnl_amt': pnl_amt, 'pnl_pct': pnl_pct}
        except Exception as e:
            logger.debug(f"[{symbol}] _bnh_holdings error: {e}")
            return {'qty': 0, 'avg_price': None, 'current_value': None,
                    'pnl_amt': None, 'pnl_pct': None}

    def _liquidcase_qty(self) -> int:
        try:
            for h in (self.portfolio.holdings or []):
                if (h.get('tradingsymbol') or h.get('symbol', '')) == LIQUIDCASE_SYMBOL:
                    return int(h.get('quantity', 0) or 0)
        except Exception:
            pass
        return 0

    def _liquidcase_value(self) -> Optional[float]:
        try:
            qty   = self._liquidcase_qty()
            price = self.realtime.get_ltp(LIQUIDCASE_SYMBOL) if self.realtime else None
            if qty and price:
                return round(qty * float(price), 2)
        except Exception:
            pass
        return None

    def _refresh_latest_sym(self, now: datetime, wr: Optional[float], st: _SymState):
        symbol = st.symbol
        price  = None
        try:
            ltp = self.realtime.get_ltp(symbol) if self.realtime else None
            price = round(float(ltp), 2) if ltp else None
        except Exception:
            pass

        order_type = self._order_type()

        if wr is None:
            signal   = '⏳ Loading W%R data...'
            wr_state = 'LOADING'
        elif wr <= OVERSOLD_THRESHOLD:
            signal   = f'🟢 BUY SIGNAL — W%R {wr:.1f} ≤ {OVERSOLD_THRESHOLD} ({order_type})'
            wr_state = 'OVERSOLD'
        else:
            signal   = f'😐 No signal — W%R {wr:.1f} (need ≤ {OVERSOLD_THRESHOLD})'
            wr_state = 'NEUTRAL'

        exec_val_r = self._s('buy_execution_time', '15:15')
        anytime_r  = (exec_val_r == 'anytime')
        time_label = 'anytime' if anytime_r else 'at 3:15 PM'

        oversold_today = wr is not None and wr <= OVERSOLD_THRESHOLD
        if oversold_today and not st.bought_today:
            next_buy = f'Today {time_label}'
        elif oversold_today and st.bought_today:
            next_buy = f'Tomorrow {time_label} (signal active)'
        else:
            next_buy = 'No signal — W%R above threshold'

        st.latest = {
            'symbol':           symbol,
            'time':             now.strftime('%H:%M:%S'),
            'price':            price,
            'wr':               wr,
            'wr_state':         wr_state,
            'signal':           signal,
            'order_type':       order_type,
            'next_buy':         next_buy,
            'deployed_today':   round(st.deployed_today, 2),
            'total_deployed':   round(st.total_deployed, 2),
            'max_cash_per_etf': self._max_cash_per_etf(),
            'max_cash_per_txn': self._max_cash_per_txn(),
            'holdings':         self._bnh_holdings(symbol),
            'liquidcase_qty':   self._liquidcase_qty(),
            'liquidcase_val':   self._liquidcase_value(),
            'bought_today':     st.bought_today,
            'tiers_fired':      sorted(st.tiers_fired_this_cycle),
            'weekday_buy_enabled':   self._weekday_buy_enabled(),
            'weekday_buy_day':       self._weekday_buy_day(),
            'weekday_buy_frac':      self._weekday_buy_frac(),
            'weekday_buy_max_share': self._weekday_buy_max_share(),
            'weekday_total_deployed': round(st.weekday_total_deployed, 2),
            'weekday_buy_attempted_this_week': st.weekday_buy_attempted,
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        now  = _now_ist()
        # Sync first: ensures newly added symbols have a _SymState immediately
        # (the _loop also syncs every 15s, but status may be polled sooner)
        self._sync_sym_states()
        syms = self._bnh_symbols()

        # Refresh all symbols
        for sym in syms:
            st = self._sym_states.get(sym)
            if not st:
                continue
            wr = self._calc_daily_wr(sym)
            self._refresh_latest_sym(now, wr, st)

        # Build per-symbol payload for the dashboard table
        symbols_status = {}
        for sym in syms:
            st = self._sym_states.get(sym)
            if st:
                symbols_status[sym] = st.latest

        # Legacy single-symbol compat (first symbol)
        first_sym = syms[0] if syms else None
        first_st  = self._sym_states.get(first_sym) if first_sym else None
        legacy_lat = first_st.latest if first_st else {}
        if first_st:
            self.latest = legacy_lat

        today_str = now.date().strftime('%Y-%m-%d')
        # Scope trade log to current symbols only (exclude removed symbols)
        current_syms_set = set(syms)
        combined_log = []
        for sym, st in self._sym_states.items():
            if sym in current_syms_set:
                combined_log.extend(st.trade_log)
        combined_log.sort(key=lambda t: t.get('date','') + t.get('time',''))

        return {
            'running':            self._running,
            'available':          True,
            'strategy':           'dip_accumulator',
            'symbol':             first_sym or BNH_SYMBOL,   # legacy
            'symbols':            syms,
            'symbols_status':     symbols_status,             # NEW — per-symbol table data
            'latest':             legacy_lat,                 # legacy single-symbol
            'trade_log':          combined_log[-100:],
            'total_deployed':     round(sum(st.total_deployed for sym,st in self._sym_states.items() if sym in current_syms_set), 2),
            'deployed_today':     round(sum(st.deployed_today  for sym,st in self._sym_states.items() if sym in current_syms_set), 2),
            'max_cash_per_etf':   self._max_cash_per_etf(),
            'max_cash_per_txn':   self._max_cash_per_txn(),
            'candle_minutes':     1440,
            'wr_period':          WILLIAMS_R_PERIOD,
            'oversold':           OVERSOLD_THRESHOLD,
            'max_attempts':       1,
            'cycles_today':       sum(1 for t in combined_log
                                      if t.get('action') == 'BUY' and t.get('date') == today_str),
            'partial_profit_pct': self._partial_profit_pct(),
            'liquidcase_qty':     self._liquidcase_qty(),
            'liquidcase_val':     self._liquidcase_value(),
            'position':           None,
            'weekday_buy_enabled': self._weekday_buy_enabled(),
            'weekday_buy_day':      self._weekday_buy_day(),
            'weekday_buy_frac':     self._weekday_buy_frac(),
            'weekday_buy_max_share': self._weekday_buy_max_share(),
        }
