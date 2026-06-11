"""
Order Manager
Handles order placement, monitoring, and execution
"""
import time
from typing import Optional, Dict, Tuple
from datetime import datetime

from backend.core.config import Config
from backend.core.constants import (
    TRANSACTION_BUY, TRANSACTION_SELL,
    ORDER_STATUS_COMPLETE, ORDER_STATUS_CANCELLED,
    ORDER_STATUS_REJECTED, ORDER_STATUS_OPEN,
    LIQUIDCASE_SYMBOL,      # ✅ FIX: was missing — caused NameError in smart_buy
)
from backend.utils.logger import get_logger

logger = get_logger(__name__)


class PostSellError(RuntimeError):
    """
    Raised when the ETF buy fails AFTER LIQUIDCASE has already been sold.
    Executor must NOT allow_retry=True — LIQUIDCASE is gone, cash is in account
    and will fund the next natural execution cycle without re-selling LIQUIDCASE.
    """


class OrderManager:
    """Manages order placement and monitoring"""
    
    def __init__(self, auth_manager):
        self.auth = auth_manager
    
    def place_order(
        self,
        symbol: str,
        transaction_type: str,
        quantity: int,
        order_type: str = None,
        price: Optional[float] = None,
        product: str = None,         # 'CNC' for delivery, 'MIS' for intraday
        variety: str = None,         # 'regular' default
        trigger_price: Optional[float] = None,  # for SL orders
    ) -> Tuple[Optional[str], str]:
        """
        Place an order with Zerodha.

        Args:
            symbol:           Trading symbol
            transaction_type: BUY or SELL
            quantity:         Quantity to trade
            order_type:       MARKET / LIMIT / SL / SL-M
            price:            Limit price (required for LIMIT/SL)
            product:          CNC (delivery) or MIS (intraday margin). Defaults to Config.PRODUCT_TYPE
            variety:          regular / amo / co. Defaults to Config.ORDER_VARIETY
            trigger_price:    Trigger price for SL orders

        Returns:
            Tuple of (order_id, message)
        """
        if order_type is None:
            order_type = Config.ORDER_TYPE
        if product is None:
            product = Config.PRODUCT_TYPE
        if variety is None:
            variety = Config.ORDER_VARIETY
        
        try:
            # Check dry run mode
            if Config.is_dry_run():
                logger.warning(f"DRY RUN: Would place {transaction_type} order: {quantity} x {symbol}")
                return "DRY_RUN_ORDER_ID", "Dry run - order not placed"
            
            # Prepare order payload
            payload = {
                'exchange':        Config.EXCHANGE,
                'tradingsymbol':   symbol,
                'transaction_type':transaction_type,
                'quantity':        quantity,
                'product':         product,
                'order_type':      order_type,
                'validity':        'DAY',
                'tag':             f'etf_bot_{int(time.time())}'
            }
            if price and order_type in ['LIMIT', 'SL']:
                payload['price'] = round(float(price), 2)
            if trigger_price and order_type in ['SL', 'SL-M']:
                payload['trigger_price'] = round(float(trigger_price), 2)
            
            # Place order
            logger.info(f"Placing {order_type} {transaction_type} order: {quantity} x {symbol}")
            
            # Zerodha web OMS requires form-encoded body with explicit Content-Type.
            # Without it the requests Session may send multipart/form-data which triggers
            # the "Incorrect api_key or access_token" rejection even with a valid enctoken.
            response = self.auth.session.post(
                f"{Config.ZERODHA_API_BASE}/oms/orders/{variety}",
                data=payload,
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('status') == 'success':
                    order_id = result.get('data', {}).get('order_id')
                    logger.info(f"✓ Order placed. ID: {order_id}")
                    return str(order_id), "Success"
                else:
                    # Zerodha returns error in 'message' field
                    error_msg = result.get('message') or result.get('error_type') or 'Order rejected by Zerodha'
                    logger.error(f"✗ Order rejected: {error_msg} | Full response: {result}")

                    # ── AMO auto-retry ─────────────────────────────────────────────
                    # Outside market hours, variety='regular' orders fail with
                    # "could not be converted to After Market Order (AMO)".
                    # Automatically retry as variety='amo' so the order queues for
                    # the next session without requiring user intervention.
                    if variety != 'amo' and 'after market order' in error_msg.lower():
                        logger.info(f"↩ Retrying as AMO for {payload.get('tradingsymbol')} ...")
                        amo_response = self.auth.session.post(
                            f"{Config.ZERODHA_API_BASE}/oms/orders/amo",
                            data=payload,
                            headers={'Content-Type': 'application/x-www-form-urlencoded'},
                            timeout=10
                        )
                        if amo_response.status_code == 200:
                            amo_result = amo_response.json()
                            if amo_result.get('status') == 'success':
                                order_id = amo_result.get('data', {}).get('order_id')
                                logger.info(f"✓ AMO order placed. ID: {order_id}")
                                return str(order_id), "AMO"
                            else:
                                amo_err = amo_result.get('message') or 'AMO retry also failed'
                                logger.error(f"✗ AMO retry rejected: {amo_err}")
                                return None, amo_err
                        else:
                            logger.error(f"✗ AMO retry HTTP {amo_response.status_code}")
                    # ── end AMO auto-retry ─────────────────────────────────────────

                    return None, error_msg
            else:
                # Non-200: try to extract Zerodha's error body
                try:
                    err_body  = response.json()
                    error_msg = err_body.get('message') or err_body.get('error') or f"HTTP {response.status_code}"
                except Exception:
                    error_msg = f"HTTP {response.status_code}: {response.text[:200]}"
                logger.error(f"✗ Order placement HTTP error: {error_msg}")
                return None, error_msg
                
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            return None, str(e)
    
    def place_buy_order(self, symbol: str, quantity: int) -> Tuple[Optional[str], str]:
        """Place a BUY order"""
        return self.place_order(symbol, TRANSACTION_BUY, quantity)
    
    def place_sell_order(self, symbol: str, quantity: int) -> Tuple[Optional[str], str]:
        """Place a SELL order"""
        return self.place_order(symbol, TRANSACTION_SELL, quantity)
    
    def get_order_status(self, order_id: str) -> Optional[Dict]:
        """
        Get status of an order
        
        Args:
            order_id: Order ID
            
        Returns:
            Order status dict or None
        """
        try:
            if Config.is_dry_run() and order_id == "DRY_RUN_ORDER_ID":
                return {
                    'order_id': order_id,
                    'status': ORDER_STATUS_COMPLETE,
                    'message': 'Dry run order'
                }
            
            response = self.auth.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/orders",
                timeout=10
            )
            
            if response.status_code == 200:
                orders = response.json().get('data', [])
                
                for order in orders:
                    if str(order.get('order_id')) == str(order_id):
                        return {
                            'order_id': order_id,
                            'status': order.get('status'),
                            'symbol': order.get('tradingsymbol'),
                            'transaction_type': order.get('transaction_type'),
                            'quantity': order.get('quantity'),
                            'filled_quantity': order.get('filled_quantity', 0),
                            'pending_quantity': order.get('pending_quantity', 0),
                            'average_price': order.get('average_price'),
                            'order_timestamp': order.get('order_timestamp'),
                            'exchange_timestamp': order.get('exchange_timestamp'),
                            'status_message': order.get('status_message', '')
                        }
                
                logger.warning(f"Order {order_id} not found")
                return None
            else:
                logger.error(f"Failed to fetch orders: HTTP {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Error fetching order status: {e}")
            return None
    
    def monitor_order(self, order_id: str, max_wait: int = 30) -> bool:
        """
        Monitor an order until completion
        
        Args:
            order_id: Order ID to monitor
            max_wait: Maximum seconds to wait
            
        Returns:
            True if order completed successfully, False otherwise
        """
        logger.info(f"Monitoring order {order_id}...")
        
        if Config.is_dry_run() and order_id == "DRY_RUN_ORDER_ID":
            logger.info("✓ Dry run order completed")
            return True
        
        waited = 0
        while waited < max_wait:
            order_status = self.get_order_status(order_id)
            
            if order_status:
                status = order_status.get('status')
                filled_qty = order_status.get('filled_quantity', 0)
                pending_qty = order_status.get('pending_quantity', 0)
                
                logger.debug(f"Order {order_id}: {status} | Filled: {filled_qty} | Pending: {pending_qty}")
                
                if status == ORDER_STATUS_COMPLETE:
                    avg_price = order_status.get('average_price', 0)
                    symbol = order_status.get('symbol', '')
                    logger.info(f"✓ Order COMPLETE: {symbol} @ ₹{avg_price}")
                    return True
                elif status in [ORDER_STATUS_CANCELLED, ORDER_STATUS_REJECTED]:
                    msg = order_status.get('status_message', '')
                    logger.error(f"✗ Order {status}: {msg}")
                    return False
            
            time.sleep(1)
            waited += 1
        
        logger.warning(f"Order monitoring timeout after {max_wait}s — doing final status check")
        # Order may have completed just after the polling window closed.
        # One extra check before declaring failure to avoid false orphan errors.
        for _ in range(3):
            time.sleep(2)
            final_status = self.get_order_status(order_id)
            if final_status:
                status = final_status.get('status')
                if status == ORDER_STATUS_COMPLETE:
                    avg_price = final_status.get('average_price', 0)
                    symbol    = final_status.get('symbol', '')
                    logger.info(f"✓ Order COMPLETE (caught after timeout): {symbol} @ ₹{avg_price}")
                    return True
                elif status in [ORDER_STATUS_CANCELLED, ORDER_STATUS_REJECTED]:
                    msg = final_status.get('status_message', '')
                    logger.error(f"✗ Order {status} (confirmed after timeout): {msg}")
                    return False
        logger.error(f"Order {order_id} still not complete after {max_wait + 6}s — giving up")
        return False
    
    def get_open_buy_orders(self) -> list:
        """Fetch all open/pending BUY orders blocking margin."""
        try:
            r = self.auth.session.get(f"{Config.ZERODHA_API_BASE}/oms/orders", timeout=10)
            if r.status_code != 200:
                return []
            return [o for o in r.json().get('data', [])
                    if o.get('transaction_type') == 'BUY'
                    and o.get('status') in ('OPEN', 'TRIGGER PENDING', 'AMO REQ RECEIVED')]
        except Exception as e:
            logger.warning(f"Could not fetch open orders: {e}")
            return []

    def cancel_open_buy_orders(self) -> int:
        """Cancel all open BUY orders to free blocked margin."""
        cancelled = 0
        for o in self.get_open_buy_orders():
            oid = o.get('order_id')
            sym = o.get('tradingsymbol')
            variety = o.get('variety', Config.ORDER_VARIETY)
            try:
                r = self.auth.session.delete(
                    f"{Config.ZERODHA_API_BASE}/oms/orders/{variety}/{oid}", timeout=10)
                if r.status_code == 200:
                    logger.info(f"[smart_buy] Cancelled open BUY {oid} ({sym}) to free margin")
                    cancelled += 1
            except Exception as e:
                logger.warning(f"[smart_buy] Error cancelling {oid}: {e}")
        return cancelled

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order"""
        try:
            if Config.is_dry_run():
                logger.warning(f"DRY RUN: Would cancel order {order_id}")
                return True
            
            response = self.auth.session.delete(
                f"{Config.ZERODHA_API_BASE}/oms/orders/{Config.ORDER_VARIETY}/{order_id}",
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                if result.get('status') == 'success':
                    logger.info(f"✓ Order {order_id} cancelled")
                    return True
            
            logger.error(f"✗ Failed to cancel order {order_id}")
            return False
            
        except Exception as e:
            logger.error(f"Error cancelling order: {e}")
            return False
    
    def get_available_cash(self) -> float:
        """
        Fetch live available cash balance from Zerodha margins API.

        Returns 0.0 on error so callers always get a safe numeric value.

        ✅ FIX: raises RuntimeError with a clear message on HTTP 403 (market
        closed / token issue) so callers can surface it to the user instead of
        silently treating it as zero cash and attempting a LIQUIDCASE sell.
        """
        try:
            if Config.is_dry_run():
                return 999999.0   # unlimited in dry-run

            response = self.auth.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/user/margins",
                timeout=10
            )
            if response.status_code == 403:
                # ✅ FIX: surface 403 explicitly — happens when market is closed
                # or the enctoken has expired, rather than returning 0 and
                # letting the bot try (and fail) to sell LIQUIDCASE.
                try:
                    msg = response.json().get('message') or 'Session expired or market closed'
                except Exception:
                    msg = 'Session expired or market closed'
                raise RuntimeError(f"Zerodha rejected margin request (HTTP 403): {msg}")

            if response.status_code != 200:
                logger.warning(f"get_available_cash: HTTP {response.status_code}")
                return 0.0

            margins  = response.json()
            equity   = margins.get('data', {}).get('equity', {})
            avail    = equity.get('available', {})
            live_bal    = float(avail.get('live_balance', 0))
            net_bal     = float(avail.get('cash', 0))
            adhoc_bal   = float(avail.get('adhoc_margin', 0))
            opening_bal = float(avail.get('opening_balance', 0))

            # ── Which field to use ────────────────────────────────────────────
            # live_balance: includes pledged collateral/securities margin.
            #               Suitable for F&O/MIS but NOT for CNC delivery buys —
            #               Zerodha will NOT allow collateral to fund fresh CNC
            #               purchases, causing "Insufficient funds" rejections even
            #               when live_balance appears high enough.
            #
            # cash (net_bal): settled cash balance only — this is the true liquid
            #                 cash available for CNC (delivery) purchases.
            #                 May be 0 before market open until recalculated.
            #
            # Rule: prefer cash (net_bal) for CNC accuracy.
            # Fall back to live_balance, then opening_balance if both are zero.
            if net_bal > 0:
                cash = net_bal
            elif live_bal > 0:
                cash = live_bal
            else:
                cash = opening_bal

            logger.info(
                f"Available cash: live=₹{live_bal:,.2f} net(cash)=₹{net_bal:,.2f} "
                f"opening=₹{opening_bal:,.2f} adhoc=₹{adhoc_bal:,.2f} → using ₹{cash:,.2f}"
            )
            return cash
        except RuntimeError:
            raise   # let the caller (smart_buy) propagate this to the UI
        except Exception as e:
            logger.error(f"Error fetching available cash: {e}")
            return 0.0

    def smart_buy(
        self,
        buy_symbol: str,
        buy_quantity: int,
        buy_price_estimate: float,
        realtime_manager,
        portfolio_tracker,
        buy_order_type: str = 'MARKET',
        buy_limit_price: float = None,
        buy_product: str = None,
        cash_reserve: float = 0.0,
    ) -> bool:
        """
        Smart buy: use available cash first; only sell LIQUIDCASE for the shortfall.

        Flow:
          1. Get live available cash from Zerodha.
          2. If cash >= full cost  → direct buy (no LIQUIDCASE sell).
          3. If 0 < cash < cost   → sell only enough LIQUIDCASE to cover the gap,
                                     then buy the ETF.
          4. If cash = 0          → sell full LIQUIDCASE amount first, then buy.

        Args:
            buy_symbol:         ETF/stock to buy.
            buy_quantity:       Number of units.
            buy_price_estimate: Estimated price (used for cost calculation).
            realtime_manager:   For LIQUIDCASE LTP lookup.
            portfolio_tracker:  For LIQUIDCASE qty lookup.
            buy_order_type:     MARKET or LIMIT.
            buy_limit_price:    Limit price (if LIMIT).
            buy_product:        CNC / MIS. None = Config default.
        """
        import math

        exec_price = buy_limit_price if (buy_order_type == 'LIMIT' and buy_limit_price) else buy_price_estimate
        total_cost = exec_price * buy_quantity

        # ── Step 1: check available cash ──────────────────────────────────
        # ✅ FIX: get_available_cash now raises RuntimeError on 403 — let it
        # propagate so the UI shows "Zerodha rejected margin request: market closed"
        # instead of silently falling through to a LIQUIDCASE sell that also fails.
        avail_cash_raw = self.get_available_cash()
        avail_cash = max(0.0, avail_cash_raw - cash_reserve)
        logger.info(
            f"smart_buy {buy_quantity} × {buy_symbol} @ ₹{exec_price:.2f} "
            f"(cost ₹{total_cost:,.2f}) | Available cash: ₹{avail_cash_raw:,.2f} "
            f"(spendable after ₹{cash_reserve:.0f} reserve: ₹{avail_cash:,.2f})"
        )

        # ── Step 2: cash fully covers cost → direct buy ───────────────────
        if avail_cash >= total_cost:
            logger.info(
                f"✅ Cash sufficient (₹{avail_cash:,.2f} >= ₹{total_cost:,.2f}) — "
                f"buying {buy_quantity} × {buy_symbol} directly, no LIQUIDCASE sell needed"
            )
            try:
                order_id, msg = self.place_order(
                    buy_symbol, 'BUY', buy_quantity,
                    order_type=buy_order_type,
                    price=buy_limit_price,
                    product=buy_product,
                )
            except RuntimeError as e:
                raise
            if order_id:
                if self.monitor_order(order_id):
                    logger.info(f"✓ Direct buy complete: {buy_quantity} × {buy_symbol}")
                    portfolio_tracker.sync()
                    return True
                else:
                    raise RuntimeError(f"Buy order {order_id} did not complete")
            else:
                raise RuntimeError(f"Buy order placement failed: {msg}")

        # ── Step 3: partial cash — sell only the shortfall from LIQUIDCASE ──
        shortfall = total_cost - avail_cash
        liq_price = realtime_manager.get_ltp(LIQUIDCASE_SYMBOL) if realtime_manager else None
        if not liq_price or liq_price <= 0:
            raise RuntimeError("Cannot get LIQUIDCASE price for shortfall calculation")

        # ✅ FIX: add 2% buffer to shortfall to account for:
        # 1. LIQUIDCASE MARKET sell executing slightly below LTP
        # 2. MON100/ETF price moving up between calculation and order placement
        # 3. Zerodha margins API lag causing available cash to appear lower
        liq_to_sell = math.ceil((shortfall * 1.08) / liq_price)
        liq_held    = portfolio_tracker.liquidcase_quantity

        if liq_to_sell > liq_held:
            raise RuntimeError(
                f"Insufficient LIQUIDCASE: shortfall ₹{shortfall:,.2f} needs "
                f"{liq_to_sell} units but only {liq_held} held"
            )

        if avail_cash > 0:
            logger.info(
                f"💡 Partial cash (₹{avail_cash:,.2f}) + LIQUIDCASE shortfall "
                f"(₹{shortfall:,.2f} → {liq_to_sell} units)"
            )
        else:
            logger.info(
                f"💡 No cash — selling {liq_to_sell} LIQUIDCASE (₹{liq_to_sell*liq_price:,.2f}) "
                f"to fund full buy of ₹{total_cost:,.2f}"
            )

        # Cancel open BUY orders to free blocked margin before LIQUIDCASE sell
        _ncancelled = self.cancel_open_buy_orders()
        if _ncancelled:
            logger.info(f"[smart_buy] Cancelled {_ncancelled} open BUY order(s) to free margin")
            import time as _t; _t.sleep(2)

        # Sell LIQUIDCASE for the shortfall (CNC — delivery sell)
        liq_oid, liq_msg = self.place_order(
            LIQUIDCASE_SYMBOL, 'SELL', liq_to_sell,
            order_type='MARKET',   # always market for LIQUIDCASE sell
            product='CNC',
        )
        if not liq_oid:
            raise RuntimeError(f"LIQUIDCASE sell failed: {liq_msg}")

        liq_status = self.get_order_status(liq_oid)
        if not self.monitor_order(liq_oid):
            sm = liq_status.get('status_message', '') if liq_status else ''
            raise RuntimeError(f"LIQUIDCASE sell did not complete: {sm}")

        logger.info(f"✓ LIQUIDCASE sell complete: {liq_to_sell} units @ ₹{liq_price:.2f}")

        # ── Wait for Zerodha to credit sale proceeds into available cash ──────
        # Zerodha does NOT update available margin instantly after a CNC sell.
        # Placing the ETF buy too quickly causes "Insufficient funds" even though
        # the LIQUIDCASE was sold successfully.  Poll until cash reflects the
        # full cost of the ETF buy (up to 45s), then add a small safety pause.
        #
        # FIX: compare against total_cost (what the buy actually needs) rather
        # than a computed floor from the pre-sale balance — the old floor could
        # be satisfied before Zerodha's OMS had actually credited the margin,
        # because the margins API and the OMS margin engine update independently.
        import time as _time
        # ── Poll live_balance (not net_bal/cash) ─────────────────────────────
        # After a CNC sell, Zerodha updates live_balance intraday to reflect
        # sale proceeds — but net_bal/cash only updates at T+1 settlement.
        # get_available_cash() uses net_bal so it never sees intraday proceeds.
        # We poll live_balance directly here so the poll actually converges.
        # Threshold: live_balance must cover total_cost + cash_reserve (raw).
        _poll_wait  = 0
        _poll_limit = 90
        _last_cash  = avail_cash
        _need       = total_cost + cash_reserve   # raw cash needed (no reserve deducted)

        def _fetch_live_balance():
            try:
                r = self.auth.session.get(
                    f"{Config.ZERODHA_API_BASE}/oms/user/margins", timeout=10
                )
                if r.status_code != 200:
                    return None
                avail = r.json().get("data", {}).get("equity", {}).get("available", {})
                lb = float(avail.get("live_balance", 0) or 0)
                nb = float(avail.get("cash", 0) or 0)
                # live_balance reflects intraday CNC proceeds; cash does not.
                return lb if lb > 0 else nb
            except Exception:
                return None

        while _poll_wait < _poll_limit:
            _time.sleep(2)
            _poll_wait += 2
            refreshed = _fetch_live_balance()
            if refreshed is not None:
                _last_cash = refreshed
                logger.debug(
                    f"[smart_buy] Margin poll {_poll_wait}/{_poll_limit}s: "
                    f"live_balance=₹{refreshed:,.2f} (need ≥ ₹{_need:,.2f})"
                )
                if refreshed >= _need:
                    logger.info(
                        f"✅ Margin updated after {_poll_wait}s: "
                        f"₹{refreshed:,.2f} ≥ ₹{_need:,.2f} — proceeding to buy"
                    )
                    break
        else:
            # Poll timed out — one final check
            refreshed = _fetch_live_balance()
            if refreshed is not None:
                _last_cash = refreshed
            if _last_cash < _need:
                raise PostSellError(
                    f"Margin not credited after {_poll_limit}s: "
                    f"live_balance ₹{_last_cash:,.2f} < required ₹{_need:,.2f}. "
                    f"LIQUIDCASE was sold ({liq_to_sell} units) — cash should credit shortly."
                )
            logger.warning(
                f"⚠️ Margin poll timed out at {_poll_limit}s but live_balance "
                f"₹{_last_cash:,.2f} now covers ₹{_need:,.2f} — proceeding to buy"
            )
        # ─────────────────────────────────────────────────────────────────────
        # After LIQUIDCASE sell + margin poll (up to 90s), the limit price
        # captured before the poll is stale — market has moved. Force MARKET
        # so the fill is guaranteed and we don't orphan the cash.
        # Recalculate qty using fresh LTP after 90s poll — price may have moved
        _fresh_ltp = realtime_manager.get_ltp(buy_symbol) if realtime_manager else None
        if _fresh_ltp and _fresh_ltp > 0 and _fresh_ltp != exec_price:
            _safe_cash = max(0.0, (_last_cash or total_cost) - cash_reserve)
            _fresh_qty = max(1, int(_safe_cash / _fresh_ltp))
            if _fresh_qty != buy_quantity:
                logger.info(f"[smart_buy] Price moved ₹{exec_price:.2f}→₹{_fresh_ltp:.2f} — qty {buy_quantity}→{_fresh_qty}")
                buy_quantity = _fresh_qty

        _actual_order_type  = 'MARKET'
        _actual_limit_price = None
        if buy_order_type != 'MARKET':
            logger.info(
                f"[smart_buy] Overriding {buy_order_type} → MARKET after LIQUIDCASE sell "
                f"(limit price ₹{buy_limit_price} is stale after margin poll delay)"
            )

        buy_oid, buy_msg = self.place_order(
            buy_symbol, 'BUY', buy_quantity,
            order_type=_actual_order_type,
            price=_actual_limit_price,
            product=buy_product,
        )
        if not buy_oid:
            logger.error(
                f"🚨 ORPHAN: LIQUIDCASE sold but {buy_symbol} BUY failed: {buy_msg}"
            )
            # LIQUIDCASE already sold — raise PostSellError so executor does NOT retry
            raise PostSellError(f"Buy failed after LIQUIDCASE sell: {buy_msg}")

        buy_status = self.get_order_status(buy_oid)
        if not self.monitor_order(buy_oid, max_wait=60):
            sm = buy_status.get('status_message', '') if buy_status else ''
            logger.error(f"🚨 ORPHAN: {buy_symbol} buy incomplete after LIQUIDCASE sell")
            # LIQUIDCASE already sold — raise PostSellError so executor does NOT retry
            raise PostSellError(f"Buy order did not complete after LIQUIDCASE sell: {sm}")

        logger.info(f"✓ smart_buy complete: {buy_quantity} × {buy_symbol} @ ₹{exec_price:.2f}")
        portfolio_tracker.sync()
        return True

    def check_margin_availability(self, symbol: str, quantity: int, price: float) -> tuple[bool, str]:
        """
        Check if there's enough margin to execute a BUY order for CNC
        
        Args:
            symbol: Symbol to buy
            quantity: Quantity to buy
            price: Estimated price per unit
            
        Returns:
            Tuple of (is_available: bool, message: str)
        """
        try:
            # CNC (delivery) orders require 100% of order value — no leverage
            required_value = quantity * price
            
            response = self.auth.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/user/margins",
                timeout=10
            )
            
            if response.status_code != 200:
                return False, f"Failed to fetch margin data: HTTP {response.status_code}"
            
            margins_data = response.json()
            avail_data = margins_data.get('data', {}).get('equity', {}).get('available', {})
            net_bal     = float(avail_data.get('cash', 0))
            live_bal    = float(avail_data.get('live_balance', 0))
            opening_bal = float(avail_data.get('opening_balance', 0))
            available_margin = net_bal if net_bal > 0 else (live_bal if live_bal > 0 else opening_bal)
            
            if available_margin < required_value:
                msg = f"Insufficient funds: Need ₹{required_value:.2f}, have ₹{available_margin:.2f}"
                logger.warning(msg)
                return False, msg
            
            logger.info(f"✓ Funds check passed: ₹{available_margin:.2f} available, ₹{required_value:.2f} needed")
            return True, "OK"
            
        except Exception as e:
            error_msg = f"Error checking margin: {e}"
            logger.error(error_msg)
            return False, error_msg
    
    def execute_swap(
        self,
        sell_symbol: str,
        sell_quantity: int,
        buy_symbol: str,
        buy_quantity: int,
        buy_price_estimate: float = None,
        sell_price_estimate: float = None,
        buy_order_type: str = 'MARKET',
        buy_limit_price: float = None,
        sell_order_type: str = 'MARKET',
        sell_limit_price: float = None,
        buy_product: str = None,     # None = use Config default (CNC). Set 'MIS' for intraday
        sell_product: str = None,    # None = use Config default (CNC). Set 'MIS' for intraday
    ) -> bool:
        """
        Execute atomic swap: Sell -> Buy

        CRITICAL: Pre-flight check is SWAP-DIRECTION AWARE
        - SELL→BUY (ETF→LIQUIDCASE): Sell proceeds fund buy, minimal margin needed
        - BUY→SELL (LIQUIDCASE→ETF): Need upfront margin for buy

        Args:
            sell_symbol:      Symbol to sell
            sell_quantity:    Quantity to sell
            buy_symbol:       Symbol to buy
            buy_quantity:     Quantity to buy
            buy_price_estimate:  Estimated buy price (for pre-flight)
            sell_price_estimate: Estimated sell price (for pre-flight)
            buy_order_type:   'MARKET' or 'LIMIT' for the buy leg
            buy_limit_price:  Limit price for buy leg (required if LIMIT)
            sell_order_type:  'MARKET' or 'LIMIT' for the sell leg
            sell_limit_price: Limit price for sell leg (required if LIMIT)

        Returns:
            True if swap successful, False otherwise
        """
        logger.info(f"Executing SWAP: Sell {sell_quantity} {sell_symbol} -> Buy {buy_quantity} {buy_symbol}")
        
        # Pre-flight check: Swap-direction aware margin validation
        if buy_price_estimate and buy_price_estimate > 0:
            buy_value = buy_quantity * buy_price_estimate
            
            if sell_symbol != LIQUIDCASE_SYMBOL and buy_symbol == LIQUIDCASE_SYMBOL:
                # SELL→BUY: ETF to LIQUIDCASE (sell proceeds fund the buy)
                logger.info(f"[SWAP PRE-FLIGHT] ETF→LIQUIDCASE swap detected")
                
                if sell_price_estimate and sell_price_estimate > 0:
                    sell_value = sell_quantity * sell_price_estimate
                    
                    if sell_value >= buy_value * 0.95:
                        logger.info(f"✓ Swap feasible: Sell proceeds ₹{sell_value:.2f} covers buy ₹{buy_value:.2f}")
                    else:
                        logger.error(f"✗ SWAP ABORTED: Sell proceeds ₹{sell_value:.2f} insufficient for buy ₹{buy_value:.2f}")
                        logger.error(f"⚠️  Pre-flight check failed. NO orders placed.")
                        return False
                else:
                    logger.warning(f"⚠️  Sell price not provided, proceeding without exact pre-flight check")
                    
            elif sell_symbol == LIQUIDCASE_SYMBOL and buy_symbol != LIQUIDCASE_SYMBOL:
                # LIQUIDCASE→ETF swap: LIQUIDCASE sell proceeds fund the ETF buy
                logger.info(f"[SWAP PRE-FLIGHT] LIQUIDCASE→ETF swap detected")
                
                if sell_price_estimate and sell_price_estimate > 0:
                    sell_value = sell_quantity * sell_price_estimate
                    
                    if sell_value >= buy_value * 0.95:
                        logger.info(f"✓ Swap feasible: LIQUIDCASE proceeds ₹{sell_value:.2f} covers ETF buy ₹{buy_value:.2f}")
                    else:
                        logger.error(f"✗ SWAP ABORTED: LIQUIDCASE proceeds ₹{sell_value:.2f} insufficient for ETF buy ₹{buy_value:.2f}")
                        logger.error(f"⚠️  Pre-flight check failed. NO orders placed.")
                        return False
                else:
                    logger.warning(f"⚠️  Sell price not provided, proceeding without pre-flight check")
            else:
                logger.warning(f"⚠️  Non-standard swap ({sell_symbol}→{buy_symbol}), proceeding with caution")
        
        # Step 1: Sell
        logger.info(f"[SWAP STEP 1/4] Placing {sell_order_type} SELL order: {sell_quantity} {sell_symbol}")
        sell_order_id, sell_msg = self.place_order(sell_symbol, TRANSACTION_SELL, sell_quantity,
            order_type=sell_order_type, price=sell_limit_price, product=sell_product)

        if sell_order_id is None:
            err = f"Sell order rejected by Zerodha: {sell_msg}"
            logger.error(f"✗ SWAP FAILED at Step 1: {err}")
            raise RuntimeError(err)

        logger.info(f"[SWAP STEP 2/4] SELL order placed, ID: {sell_order_id}. Monitoring...")

        # Monitor sell order
        sell_status = self.get_order_status(sell_order_id)
        if not self.monitor_order(sell_order_id):
            status_msg = sell_status.get('status_message', '') if sell_status else ''
            err = f"Sell order did not complete: {status_msg}" if status_msg else f"Sell order {sell_order_id} timed out"
            logger.error(f"✗ SWAP FAILED at Step 2: {err}")
            raise RuntimeError(err)
        
        logger.info(f"✓ [SWAP STEP 2/4] Sell leg completed: {sell_order_id}")
        
        # Brief delay between sell and buy (helps with margin update)
        time.sleep(0.5)
        
        # Step 2: Buy (with one retry — sell is already done, must not leave cash unparked)
        logger.info(f"[SWAP STEP 3/4] Placing {buy_order_type} BUY order: {buy_quantity} {buy_symbol}")
        buy_order_id, buy_msg = self.place_order(buy_symbol, TRANSACTION_BUY, buy_quantity,
            order_type=buy_order_type, price=buy_limit_price, product=buy_product)

        if buy_order_id is None:
            logger.warning(f"⚠️  BUY placement failed ({buy_msg}), retrying once in 3s...")
            time.sleep(3)
            buy_order_id, buy_msg = self.place_order(buy_symbol, TRANSACTION_BUY, buy_quantity,
                order_type=buy_order_type, price=buy_limit_price, product=buy_product)

        if buy_order_id is None:
            err = f"Buy order rejected by Zerodha: {buy_msg}"
            logger.error(
                f"🚨 ORPHAN SWAP: {sell_quantity} {sell_symbol} SOLD (id={sell_order_id}) "
                f"but {buy_quantity} {buy_symbol} BUY FAILED. Cash is unparked. Manual intervention required."
            )
            raise RuntimeError(err)

        logger.info(f"[SWAP STEP 4/4] BUY order placed, ID: {buy_order_id}. Monitoring...")

        # Monitor buy order
        buy_status = self.get_order_status(buy_order_id)
        if not self.monitor_order(buy_order_id):
            status_msg = buy_status.get('status_message', '') if buy_status else ''
            err = f"Buy order not completed: {status_msg}" if status_msg else f"Buy order {buy_order_id} timed out"
            logger.error(
                f"🚨 ORPHAN SWAP: {sell_quantity} {sell_symbol} SOLD (id={sell_order_id}) "
                f"but {buy_quantity} {buy_symbol} BUY INCOMPLETE (id={buy_order_id}). Manual intervention required."
            )
            raise RuntimeError(err)
        
        logger.info(f"✓ [SWAP STEP 4/4] Buy leg completed: {buy_order_id}")
        logger.info(f"✓ SWAP COMPLETE: {sell_symbol} -> {buy_symbol}")
        return True
