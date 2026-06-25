"""
Portfolio Tracker
Manages portfolio state by syncing with Zerodha
Source of truth: Zerodha Holdings and Positions
"""
from typing import Dict, List, Optional
from datetime import datetime

from backend.core.config import Config
from backend.core.constants import LIQUIDCASE_SYMBOL
from backend.utils.logger import get_logger

logger = get_logger(__name__)


class PortfolioTracker:
    """Tracks portfolio state from Zerodha"""
    
    def __init__(self, auth_manager):
        self.auth = auth_manager
        self.holdings: List[Dict] = []
        self.positions: Dict[str, List[Dict]] = {'day': [], 'net': []}
        self.liquidcase_quantity = 0
        self.liquidcase_free_quantity = 0  # unpledged qty available to sell
        self.liquidcase_value = 0.0
        self.available_slots = Config.SLOTS_COUNT
        self.locked_symbols: set = set()
        # ✅ FIX: the snapshot writer (backend/utils/snapshot.py) runs on its
        # own independent timer and just reads whatever is currently in
        # self.positions/self.holdings — it never itself triggers a fresh
        # Kite fetch. If sync() below stops succeeding (e.g. the main
        # trading loop that normally calls it dies or stalls), the snapshot
        # writer keeps stamping a fresh "timestamp" every cycle regardless,
        # wrapped around silently frozen position/holdings data — the
        # dashboard then shows "Live ✓" with data that's actually hours old.
        # Tracking the real last-successful-sync time here lets the
        # snapshot carry a SEPARATE, honest "data last confirmed fresh"
        # timestamp the frontend can check, instead of only trusting
        # "when did the snapshot file get written."
        self.last_synced_at: Optional[datetime] = None
        self.last_sync_error: Optional[str] = None
    
    def sync(self) -> bool:
        """
        Sync portfolio state from Zerodha
        This is the source of truth - called on startup and periodically
        
        Returns:
            True if sync successful, False otherwise
        """
        logger.debug("Syncing portfolio with Zerodha...")
        
        try:
            # Fetch holdings
            holdings = self._fetch_holdings()
            if holdings is None:
                logger.debug("Failed to fetch holdings — retaining cached state")
                self.last_sync_error = "holdings fetch failed"
                return False
            
            # Fetch positions
            positions = self._fetch_positions()
            if positions is None:
                logger.debug("Failed to fetch positions — retaining cached state")
                self.last_sync_error = "positions fetch failed"
                return False
            
            # Update state
            self.holdings = holdings
            self.positions = positions
            
            # Process holdings
            self._process_holdings()
            
            # Process positions
            self._process_positions()
            
            # Calculate locked symbols and available slots
            self._update_slot_availability()
            
            logger.info("✓ Portfolio synced successfully")
            self._log_portfolio_summary()
            
            self.last_synced_at = datetime.now()
            self.last_sync_error = None
            return True
            
        except Exception as e:
            logger.error(f"Error syncing portfolio: {e}")
            self.last_sync_error = str(e)[:200]
            return False
    
    def _fetch_holdings(self) -> Optional[List[Dict]]:
        """Fetch holdings from Zerodha"""
        try:
            response = self.auth.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/portfolio/holdings",
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                return data.get('data', [])
            else:
                if response.status_code == 403:
                    logger.debug(f"Holdings temporarily unavailable (403) - Zerodha post-trade delay, retaining cached data")
                else:
                    logger.error(f"Holdings fetch failed: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Error fetching holdings: {e}")
            return None
    
    def _fetch_positions(self) -> Optional[Dict[str, List]]:
        """Fetch positions from Zerodha"""
        try:
            response = self.auth.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/portfolio/positions",
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                positions_data = data.get('data', {})
                return {
                    'day': positions_data.get('day', []),
                    'net': positions_data.get('net', [])
                }
            else:
                logger.error(f"Positions fetch failed: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            return None
    
    def _process_holdings(self):
        """
        Process holdings to extract LIQUIDCASE quantity and value.
        
        Zerodha behaviour (verified):
        - Holdings API updates INTRADAY immediately after a sell.
        - Positions (net) records today's transactions as signed deltas.

        Logic (mirrors get_quantity_held):
        - Sold today (positions < 0): Holdings already reflects the sale → use holdings only.
          DO NOT subtract positions — that double-counts the sell.
        - Bought today (positions > 0): Holdings may not reflect the buy yet → add both.
        - No activity today: Use holdings only.
        """
        qty_from_holdings = 0
        free_qty_holdings = 0   # unpledged qty from holdings
        qty_from_positions = 0
        liquidcase_avg_price = 0.0
        
        # Holdings (reflects intraday sells immediately)
        for holding in self.holdings:
            symbol = holding.get('tradingsymbol', '')
            if symbol == LIQUIDCASE_SYMBOL:
                free_qty    = int(holding.get('quantity', 0))
                pledged_qty = int(holding.get('collateral_quantity', 0))
                t1_qty      = int(holding.get('t1_quantity', 0))
                qty_from_holdings = free_qty + pledged_qty + t1_qty
                free_qty_holdings = free_qty  # only unpledged, T+0 deliverable
                liquidcase_avg_price = float(holding.get('last_price', 0))
                logger.info(
                    f"LIQUIDCASE (Holdings): {free_qty} free"
                    f" + {pledged_qty} pledged + {t1_qty} T1 = {qty_from_holdings} units"
                    f" @ ₹{liquidcase_avg_price:.2f}"
                )
                break
        
        # Positions — today's net activity
        for position in self.positions.get('net', []):
            symbol = position.get('tradingsymbol', '')
            if symbol == LIQUIDCASE_SYMBOL:
                qty_from_positions = int(position.get('quantity', 0))
                position_price = float(position.get('last_price', 0))
                logger.info(f"LIQUIDCASE (Positions): {qty_from_positions:+d} units @ ₹{position_price:.2f}")
                if position_price > 0:
                    liquidcase_avg_price = position_price
                break
        
        # Apply same logic as get_quantity_held
        if qty_from_positions > 0:
            # Bought LIQUIDCASE today — Holdings may lag, add both
            self.liquidcase_quantity = qty_from_holdings + qty_from_positions
            self.liquidcase_free_quantity = free_qty_holdings + qty_from_positions
            logger.info(f"LIQUIDCASE Total: {qty_from_holdings} (Holdings) + {qty_from_positions} (Bought) = {self.liquidcase_quantity} units")
        elif qty_from_positions < 0:
            # Sold LIQUIDCASE today — Holdings already updated intraday, use as-is
            self.liquidcase_quantity = qty_from_holdings
            self.liquidcase_free_quantity = free_qty_holdings
            logger.info(f"LIQUIDCASE Total: {qty_from_holdings} units (Holdings updated | {qty_from_positions} sold today)")
        else:
            self.liquidcase_quantity = qty_from_holdings
            self.liquidcase_free_quantity = free_qty_holdings
        
        # Calculate value using latest price
        self.liquidcase_value = self.liquidcase_quantity * liquidcase_avg_price
        
        if self.liquidcase_quantity > 0:
            logger.info(f"LIQUIDCASE Total: {self.liquidcase_quantity} units (free: {self.liquidcase_free_quantity}) = ₹{self.liquidcase_value:.2f}")
    
    def _process_positions(self):
        """Process positions to identify held ETFs"""
        # Process both day and net positions
        pass  # Positions are already stored, no additional processing needed
    
    def _update_slot_availability(self):
        """Calculate locked symbols and available slots (only for active ETFs)"""
        import json
        from pathlib import Path
        
        # Get active ETFs from settings.json
        settings_path = Path(__file__).parent.parent.parent / 'config' / 'settings.json'
        active_etfs = ['MID150BEES', 'MON100', 'GOLDBEES', 'SILVERBEES', 'MINDSPACE-RR', 'EMBASSY-RR']  # Default
        
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f:
                    settings = json.load(f)
                    active_etfs = settings.get('active_etfs', active_etfs)
            except Exception as e:
                logger.warning(f"Failed to read active_etfs from settings: {e}")
        
        self.locked_symbols = set()
        
        # Check holdings for ACTIVE ETFs only
        for holding in self.holdings:
            symbol = holding.get('tradingsymbol', '')
            quantity = int(holding.get('quantity', 0))
            
            if symbol in active_etfs and quantity > 0:
                self.locked_symbols.add(symbol)
                logger.debug(f"Slot locked: {symbol} (Holdings)")
        
        # Check net positions for ACTIVE ETFs only
        for position in self.positions['net']:
            symbol = position.get('tradingsymbol', '')
            quantity = int(position.get('quantity', 0))
            
            if symbol in active_etfs and quantity != 0:
                self.locked_symbols.add(symbol)
                logger.debug(f"Slot locked: {symbol} (Positions)")
        
        # Calculate available slots based on active ETFs
        total_slots = len(active_etfs)
        self.available_slots = total_slots - len(self.locked_symbols)
        
        logger.info(f"Active ETFs: {', '.join(active_etfs)}")
        logger.info(f"Slots: {self.available_slots} available, {len(self.locked_symbols)} locked ({', '.join(self.locked_symbols) if self.locked_symbols else 'None'})")
    
    def _log_portfolio_summary(self):
        """Log portfolio summary"""
        import json
        from pathlib import Path
        
        # Get active ETFs count
        settings_path = Path(__file__).parent.parent.parent / 'config' / 'settings.json'
        active_etfs = ['MID150BEES', 'MON100', 'GOLDBEES', 'SILVERBEES', 'MINDSPACE-RR', 'EMBASSY-RR']
        if settings_path.exists():
            try:
                with open(settings_path, 'r') as f:
                    settings = json.load(f)
                    active_etfs = settings.get('active_etfs', active_etfs)
            except:
                pass
        
        total_slots = len(active_etfs)
        
        logger.info("-" * 60)
        logger.info("PORTFOLIO SUMMARY")
        logger.info("-" * 60)
        logger.info(f"LIQUIDCASE: {self.liquidcase_quantity} units (₹{self.liquidcase_value:.2f})")
        logger.info(f"Active ETFs: {', '.join(active_etfs)}")
        logger.info(f"Held ETFs ({len(self.locked_symbols)}): {', '.join(self.locked_symbols) if self.locked_symbols else 'None'}")
        logger.info(f"Available slots: {self.available_slots}/{total_slots}")
        logger.info("-" * 60)
    
    def is_symbol_held(self, symbol: str) -> bool:
        """Check if a symbol is currently held"""
        return symbol in self.locked_symbols
    
    def get_held_symbols(self) -> List[str]:
        """Get list of currently held ETF symbols"""
        return list(self.locked_symbols)

    def get_holdings(self) -> List[Dict]:
        """Return the raw holdings list fetched from Zerodha.
        
        This is a convenience accessor for self.holdings — the same data
        synced by the last successful call to sync().  Callers should
        ensure sync() has been called before relying on this data.
        """
        return self.holdings or []

    def get_average_price(self, symbol: str) -> Optional[float]:
        """
        Return the TRUE blended average buy price for a symbol.

        The critical scenario this fixes:
          - You hold 549 units bought weeks ago at ₹200 (from holdings)
          - Today you bought 50 more at ₹190 (from day/net positions)
          - get_quantity_held() correctly returns 599
          - BUT the old code returned the DAY position avg (₹190) as the
            overall avg — making a losing position look like a winner.

        Correct approach — weighted average:
          blended_avg = (holdings_qty * holdings_avg + bought_today_qty * today_avg)
                        / (holdings_qty + bought_today_qty)

        PRIORITY / CASE table:
          A. Holdings exist + bought more today (positive net position):
             → Weighted blend of holdings avg + day/net position avg
          B. Holdings exist, nothing bought today (no / zero / negative net position):
             → Holdings avg (source of truth for settled positions)
          C. No holdings, but net position > 0 (pure intraday / T1 buy, not yet in holdings):
             → Net / day position avg
          D. Nothing found → None
        """
        # ── Gather holdings data ──────────────────────────────────────────────
        holdings_qty = 0
        holdings_avg = 0.0
        for h in (self.holdings or []):
            if h.get('tradingsymbol') == symbol:
                free_qty    = int(h.get('quantity',            0) or 0)
                pledged_qty = int(h.get('collateral_quantity', 0) or 0)
                t1_qty      = int(h.get('t1_quantity',         0) or 0)
                holdings_qty = free_qty + pledged_qty + t1_qty
                holdings_avg = float(h.get('average_price',   0) or 0)
                break

        # ── Gather today's net position data ──────────────────────────────────
        net_qty = 0
        net_avg = 0.0
        for pos in (self.positions.get('net', []) or []):
            if pos.get('tradingsymbol') == symbol:
                net_qty = int(pos.get('quantity',      0) or 0)
                net_avg = float(pos.get('average_price', 0) or 0)
                break

        # ── Also check day positions in case net is missing ───────────────────
        day_avg = 0.0
        for pos in (self.positions.get('day', []) or []):
            if pos.get('tradingsymbol') == symbol:
                day_avg = float(pos.get('average_price', 0) or 0)
                break

        # ── Case A: Holdings + bought more today → weighted blend ─────────────
        if holdings_qty > 0 and net_qty > 0 and holdings_avg > 0:
            today_avg = net_avg if net_avg > 0 else day_avg
            if today_avg > 0:
                total_qty   = holdings_qty + net_qty
                blended_avg = (holdings_qty * holdings_avg + net_qty * today_avg) / total_qty
                logger.debug(
                    f"✓ {symbol} blended avg: "
                    f"({holdings_qty}×₹{holdings_avg:.2f} + {net_qty}×₹{today_avg:.2f})"
                    f" / {total_qty} = ₹{blended_avg:.2f}"
                )
                return round(blended_avg, 4)
            # today_avg unavailable — fall back to holdings avg
            logger.debug(f"✓ {symbol} avg from HOLDINGS (today avg unavailable): ₹{holdings_avg:.2f}")
            return holdings_avg

        # ── Case B: Holdings only (nothing bought today, or sold today) ───────
        if holdings_qty > 0 and holdings_avg > 0:
            logger.debug(f"✓ {symbol} avg from HOLDINGS: ₹{holdings_avg:.2f}")
            return holdings_avg

        # ── Case C: No holdings yet — pure intraday / T1 buy ─────────────────
        if net_qty > 0 and net_avg > 0:
            logger.debug(f"✓ {symbol} avg from NET positions: ₹{net_avg:.2f}")
            return net_avg
        if day_avg > 0:
            logger.debug(f"✓ {symbol} avg from DAY positions: ₹{day_avg:.2f}")
            return day_avg

        # ── Case D: Nothing found ─────────────────────────────────────────────
        logger.warning(f"⚠️ No avg_price found for {symbol} in any source")
        return None
    
    def get_quantity_held(self, symbol: str) -> int:
        """
        Get quantity of a symbol held
        
        Important: Zerodha's Holdings API updates intraday after sales, so:
        - Holdings: Current quantity (already reflects today's sales)
        - Positions: Today's transactions (positive for buys, negative for sells)
        
        Logic:
        - If positions POSITIVE (bought today): Add to holdings (Holdings + Positions)
        - If positions NEGATIVE (sold today): Use holdings only (already updated)
        - If no positions: Use holdings only
        
        Example 1 - Buy more today:
          Holdings: 549, Positions: +50 → Returns: 599 ✅
        
        Example 2 - Sold today:
          Holdings: 500 (already updated), Positions: -513 (sale) → Returns: 500 ✅
          (NOT 500 + (-513) = -13, because holdings already reflects the sale)
        """
        qty_from_holdings = 0
        free_qty_holdings = 0   # unpledged qty from holdings
        qty_from_positions = 0
        
        # Check holdings (current quantity, updates intraday)
        for holding in self.holdings:
            holding_symbol = holding.get('tradingsymbol')
            if holding_symbol == symbol:
                # Free (settled) qty
                free_qty = int(holding.get('quantity', 0))
                # Pledged qty — shown in Zerodha Kite but excluded from 'quantity'
                pledged_qty = int(holding.get('collateral_quantity', 0))
                # T1 qty — bought today/yesterday, not yet settled into demat
                t1_qty = int(holding.get('t1_quantity', 0))
                qty_from_holdings = free_qty + pledged_qty + t1_qty
                logger.debug(
                    f"✓ Found {symbol} in Holdings: {free_qty} free"
                    f" + {pledged_qty} pledged + {t1_qty} T1 = {qty_from_holdings} units"
                )
                break
        
        # Check net positions (today's transactions)
        for position in self.positions.get('net', []):
            position_symbol = position.get('tradingsymbol')
            if position_symbol == symbol:
                qty_from_positions = int(position.get('quantity', 0))
                logger.debug(f"✓ Found {symbol} in NET Positions: {qty_from_positions:+d} units")
                break
        
        # Calculate total:
        # - If bought today (positive positions): Add to holdings
        # - If sold today (negative positions): Use holdings as-is (already updated intraday)
        # 
        # IMPORTANT: Zerodha's Holdings API updates INTRADAY after sells!
        # We tested this: After selling units, Holdings API immediately reflects new quantity.
        # So we should ALWAYS use holdings quantity for held positions (don't subtract positions).
        if qty_from_positions > 0:
            # Bought today: add to holdings (holdings might not reflect new buys yet)
            total_qty = qty_from_holdings + qty_from_positions
            logger.debug(f"📊 {symbol}: {qty_from_holdings} (Holdings) + {qty_from_positions} (Bought) = {total_qty} units")
        elif qty_from_positions < 0:
            # Sold today: Holdings already updated intraday, use as-is
            # Don't subtract positions - that would be double-counting the sale!
            total_qty = qty_from_holdings
            logger.debug(f"📊 {symbol}: {qty_from_holdings} units (Holdings updated | {qty_from_positions} sold today)")
        else:
            # No positions today: use holdings only
            total_qty = qty_from_holdings
            if total_qty > 0:
                logger.debug(f"📊 {symbol}: {qty_from_holdings} units (Holdings only)")
        
        return total_qty
    
    def calculate_profit_percent(self, symbol: str, current_price: float) -> Optional[float]:
        """
        Calculate profit percentage for a held symbol
        
        Args:
            symbol: ETF symbol
            current_price: Current market price
            
        Returns:
            Profit percentage or None if symbol not held
        """
        avg_price = self.get_average_price(symbol)
        
        if avg_price is None or avg_price == 0:
            return None
        
        profit_pct = ((current_price - avg_price) / avg_price) * 100
        return round(profit_pct, 2)
