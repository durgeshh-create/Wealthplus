"""
Historical Data Manager
Loads and manages historical OHLC data from CSV files.
Auto-refreshes stale data from Zerodha API on startup when auth is available.
"""
import pandas as pd
from io import StringIO
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime, timedelta

from backend.core.config import Config
from backend.utils.logger import get_logger

logger = get_logger(__name__)


class HistoricalDataManager:
    """Manages historical OHLC data"""
    
    def __init__(self, auth_manager=None):
        self.cache: Dict[str, Dict[str, pd.DataFrame]] = {}
        self.auth_manager = auth_manager
        self._instrument_tokens: Dict[str, str] = {}
        self._load_all_data()
    
    def _load_all_data(self):
        """Load historical data for all monitored symbols (active_etfs + bnh_symbols)."""
        logger.info("Loading historical data...")
        
        active_etfs = Config.get_active_etfs()
        bnh_symbols = Config.get_bnh_symbols()
        seen, symbols_to_load = set(), []
        for s in active_etfs + bnh_symbols:
            if s not in seen:
                seen.add(s)
                symbols_to_load.append(s)
        
        logger.info(f"Loading data for {len(symbols_to_load)} symbols: {chr(39)}{chr(39).join(symbols_to_load)}{chr(39)}")
        
        # Pre-fetch instrument tokens in background — avoids blocking startup
        # when Zerodha's instrument endpoint is slow or outside market hours.
        if self.auth_manager:
            import threading
            t = threading.Thread(target=self._fetch_instrument_tokens, daemon=True,
                                 name='InstrumentTokenFetch')
            t.start()
            t.join(timeout=12)  # wait up to 12s then continue regardless
        
        for symbol in symbols_to_load:
            self.cache[symbol] = {}
            
            # Load daily data from CSV
            daily_data = self._load_csv(symbol, 'daily')
            
            # Auto-refresh stale data when auth is available
            if daily_data is not None and self.auth_manager:
                daily_data = self._refresh_if_stale(symbol, daily_data)
            elif daily_data is None and self.auth_manager:
                # No CSV at all — bootstrap a full history fetch from Zerodha
                daily_data = self._bootstrap_historical(symbol)

            if daily_data is not None:
                self.cache[symbol]['daily'] = daily_data
                logger.debug(f"Loaded {len(daily_data)} daily candles for {symbol}")
            else:
                logger.warning(f"⚠️ No daily data found for {symbol} - W%R unavailable until CSV is seeded")
        
        logger.info(f"✓ Historical data loaded for {len(self.cache)} symbols (active + BnH)")
    
    def _fetch_instrument_tokens(self):
        """
        Fetch instrument tokens for all active ETFs/REITs from Zerodha.

        Segment lookup order (handles ETFs, REITs, and regular equities):
          1. NSE-EQ   — most NSE equities and ETFs
          2. NSE      — some ETFs appear under plain NSE
          3. BSE-EQ   — BSE equities
          4. BSE      — plain BSE segment
          5. Any      — last resort: tradingsymbol match regardless of segment
        """
        try:
            import requests as _req
            # Try public URL first (no auth needed, works anytime)
            # then fall back to OMS endpoint (needs enctoken, market hours only)
            df = None
            for url, use_auth in [
                ("https://api.kite.trade/instruments", False),
                (f"{Config.ZERODHA_API_BASE}/oms/instruments/NSE", True),
            ]:
                try:
                    session = self.auth_manager.session if use_auth else _req.Session()
                    resp = session.get(url, timeout=12)
                    if resp.status_code == 200 and resp.text.strip():
                        df = pd.read_csv(StringIO(resp.text))
                        logger.info(f"✓ Instruments CSV for historical from {url} ({len(df)} rows)")
                        break
                    else:
                        logger.warning(f"Could not fetch instrument tokens: HTTP {resp.status_code} — skipping auto-refresh (CSV data will be used as-is)")
                except Exception as _e:
                    logger.debug(f"Instruments fetch {url}: {_e}")

            if df is None:
                return

            logger.debug(f"Instruments CSV loaded: {len(df)} rows, segments: {df['segment'].unique().tolist()}")

            SEGMENT_PRIORITY = ['NSE-EQ', 'NSE', 'BSE-EQ', 'BSE']

            for symbol in list(dict.fromkeys(Config.get_active_etfs() + Config.get_bnh_symbols())):
                match = pd.DataFrame()

                # Try segments in priority order
                for seg in SEGMENT_PRIORITY:
                    match = df[(df['tradingsymbol'] == symbol) & (df['segment'] == seg)]
                    if not match.empty:
                        logger.debug(f"{symbol}: found in segment '{seg}'")
                        break

                # Last resort — any segment (catches REITs in unusual segments)
                if match.empty:
                    match = df[df['tradingsymbol'] == symbol]
                    if not match.empty:
                        seg_found = match.iloc[0]['segment']
                        logger.info(f"{symbol}: not in standard segments — found in '{seg_found}' (fallback)")

                if not match.empty:
                    token = str(match.iloc[0]['instrument_token'])
                    self._instrument_tokens[symbol] = token
                    logger.info(f"✓ Instrument token for {symbol}: {token}")
                    print(f"         ✓ {symbol}: token={token}", flush=True)
                else:
                    logger.warning(f"✗ No instrument token found for {symbol} — W%R will be unavailable")
                    print(f"         ✗ {symbol}: NOT found in Zerodha instruments list", flush=True)

        except Exception as e:
            logger.error(f"Error fetching instrument tokens: {e}")
    
    def _is_data_stale(self, df: pd.DataFrame) -> bool:
        """
        Returns True if yesterday's data is not yet in the CSV.

        Threshold: >= 2 calendar days behind today.
        - 1 day old (last_date = yesterday) → fresh, skip fetch
        - 2+ days old                       → stale, fetch missing bars

        Zerodha only returns candles for actual trading days, so passing a
        date range that includes weekends or holidays is perfectly safe —
        the API simply returns nothing for non-trading days.
        """
        if df is None or len(df) == 0:
            return True
        last_date = df['timestamp'].max().date()
        today = datetime.now().date()
        return (today - last_date).days >= 2
    
    def _refresh_if_stale(self, symbol: str, df: pd.DataFrame) -> pd.DataFrame:
        """
        If the CSV data is stale, fetch missing daily candles from Zerodha and
        merge them in.  The updated CSV is saved to disk so the next startup is
        fast.  Returns the refreshed (or original) DataFrame with a 'timestamp'
        column, filtered to exclude today (live data covers today).
        """
        if not self._is_data_stale(df):
            return df
        
        token = self._instrument_tokens.get(symbol)
        if not token:
            print(f"         ⚠️  {symbol}: no instrument token — skipping auto-refresh", flush=True)
            logger.warning(f"No instrument token for {symbol} — cannot auto-refresh data")
            return df
        
        try:
            last_date = df['timestamp'].max().date()
            today = datetime.now().date()
            # Fetch from the day after the last bar up to yesterday (today = live data)
            fetch_from = last_date + timedelta(days=1)
            fetch_to = today - timedelta(days=1)
            
            if fetch_from > fetch_to:
                return df
            
            print(
                f"         📥 {symbol}: data stale (last={last_date}) — "
                f"fetching {fetch_from} → {fetch_to}...",
                flush=True
            )
            logger.info(
                f"📥 {symbol}: CSV data stale (last={last_date}). "
                f"Fetching {fetch_from} → {fetch_to}..."
            )
            
            url = f"{Config.ZERODHA_API_BASE}/oms/instruments/historical/{token}/day"
            params = {
                'from': fetch_from.strftime('%Y-%m-%d'),
                'to': fetch_to.strftime('%Y-%m-%d'),
                'continuous': 0,
                'oi': 1
            }
            
            response = self.auth_manager.session.get(url, params=params, timeout=30)
            if response.status_code != 200:
                logger.error(
                    f"Failed to fetch historical data for {symbol}: "
                    f"HTTP {response.status_code}"
                )
                return df
            
            candles = response.json().get('data', {}).get('candles', [])
            if not candles:
                logger.info(f"No new candles returned for {symbol}")
                return df
            
            # Build new-data DataFrame
            new_df = pd.DataFrame(
                candles,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi']
            )
            new_df['timestamp'] = pd.to_datetime(new_df['timestamp'])
            # Strip timezone so it matches the existing (timezone-naive) data
            if new_df['timestamp'].dt.tz is not None:
                new_df['timestamp'] = new_df['timestamp'].dt.tz_localize(None)
            
            # Merge, deduplicate on the date portion, sort
            combined = pd.concat([df, new_df], ignore_index=True)
            combined['_date'] = combined['timestamp'].dt.date
            combined = combined.drop_duplicates(subset=['_date']).drop(columns=['_date'])
            combined = combined.sort_values('timestamp').reset_index(drop=True)
            
            # Persist to CSV — use a simple YYYY-MM-DD 'date' column to match
            # the original file format so the existing _load_csv rename logic
            # continues to work on subsequent startups.
            save_df = combined.copy()
            save_df.insert(0, 'date', save_df['timestamp'].dt.strftime('%Y-%m-%d'))
            save_df = save_df.drop(columns=['timestamp'])
            for col in ['oi']:
                if col not in save_df.columns:
                    save_df[col] = 0
            
            file_path = Config.DAILY_DATA_DIR / f"{symbol}.csv"
            save_df.to_csv(file_path, index=False)
            
            print(
                f"         ✅ {symbol}: +{len(new_df)} new candles "
                f"(CSV now {len(combined)} bars total)",
                flush=True
            )
            logger.info(
                f"✅ {symbol}: added {len(new_df)} new candles "
                f"(CSV now has {len(combined)} bars)"
            )
            
            # Return combined but filtered to exclude today (live covers today)
            today = datetime.now().date()
            combined = combined[combined['timestamp'].dt.date < today].reset_index(drop=True)
            return combined
        
        except Exception as e:
            print(f"         ❌ {symbol}: auto-refresh failed — {e}", flush=True)
            logger.error(f"Error auto-refreshing historical data for {symbol}: {e}")
            return df
    

    def _bootstrap_historical(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        Fetch ~1 year of daily OHLC from Zerodha for a symbol that has no local CSV.
        Saves to data/daily/<SYMBOL>.csv so subsequent startups are fast.
        Called automatically on first run for any new ETF that lacks a CSV file.
        """
        token = self._instrument_tokens.get(symbol)
        if not token:
            print(f"         ⚠️  {symbol}: no instrument token — cannot bootstrap data", flush=True)
            logger.warning(
                f"No instrument token for {symbol} — bootstrap skipped. "
                f"Known tokens: {list(self._instrument_tokens.keys())}"
            )
            return None

        try:
            today = datetime.now().date()
            fetch_from = today - timedelta(days=400)   # ~1 year + buffer
            fetch_to   = today - timedelta(days=1)     # exclude today (live covers today)

            print(
                f"         📥 {symbol}: no CSV found — bootstrapping from "
                f"{fetch_from} → {fetch_to} (token={token})...",
                flush=True
            )
            logger.info(f"📥 {symbol}: bootstrapping historical data {fetch_from} → {fetch_to} (token={token})")

            url = f"{Config.ZERODHA_API_BASE}/oms/instruments/historical/{token}/day"
            params = {
                'from':       fetch_from.strftime('%Y-%m-%d'),
                'to':         fetch_to.strftime('%Y-%m-%d'),
                'continuous': 0,
                'oi':         1
            }

            response = self.auth_manager.session.get(url, params=params, timeout=30)
            if response.status_code != 200:
                # Fallback: some accounts route through KITE_API_BASE instead
                alt_url = f"{Config.KITE_API_BASE}/oms/instruments/historical/{token}/day"
                logger.warning(
                    f"Bootstrap fetch failed for {symbol} via primary URL "
                    f"(HTTP {response.status_code}), trying alt URL..."
                )
                response = self.auth_manager.session.get(alt_url, params=params, timeout=30)
                if response.status_code != 200:
                    logger.error(
                        f"Bootstrap fetch failed for {symbol} on both URLs: "
                        f"HTTP {response.status_code}"
                    )
                    return None

            candles = response.json().get('data', {}).get('candles', [])
            if not candles:
                logger.warning(f"Zerodha returned no candles for {symbol} during bootstrap")
                return None

            df = pd.DataFrame(
                candles,
                columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi']
            )
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            if df['timestamp'].dt.tz is not None:
                df['timestamp'] = df['timestamp'].dt.tz_localize(None)

            df = df.sort_values('timestamp').reset_index(drop=True)

            # Persist to CSV
            Config.DAILY_DATA_DIR.mkdir(parents=True, exist_ok=True)
            save_df = df.copy()
            save_df.insert(0, 'date', save_df['timestamp'].dt.strftime('%Y-%m-%d'))
            save_df = save_df.drop(columns=['timestamp'])
            file_path = Config.DAILY_DATA_DIR / f"{symbol}.csv"
            save_df.to_csv(file_path, index=False)

            print(
                f"         ✅ {symbol}: bootstrapped {len(df)} candles → saved to {file_path.name}",
                flush=True
            )
            logger.info(f"✅ {symbol}: bootstrapped {len(df)} candles, saved to {file_path}")

            # Filter out today before returning
            df = df[df['timestamp'].dt.date < today].reset_index(drop=True)
            return df

        except Exception as e:
            print(f"         ❌ {symbol}: bootstrap failed — {e}", flush=True)
            logger.error(f"Error bootstrapping historical data for {symbol}: {e}")
            return None

    def _load_csv(self, symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
        """
        Load CSV file for a symbol
        
        Args:
            symbol: ETF symbol
            timeframe: 'daily' or 'weekly'
            
        Returns:
            DataFrame with OHLC data or None if file doesn't exist
        """
        try:
            if timeframe == 'daily':
                file_path = Config.DAILY_DATA_DIR / f"{symbol}.csv"
            else:
                file_path = Config.WEEKLY_DATA_DIR / f"{symbol}.csv"
            
            if not file_path.exists():
                return None
            
            # Read CSV
            df = pd.read_csv(file_path)
            
            # Standardize column names
            if 'date' in df.columns:
                df.rename(columns={'date': 'timestamp'}, inplace=True)
            
            # Convert timestamp to datetime
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            
            # Filter out today's data (we'll use live data for today)
            today = datetime.now().date()
            df = df[df['timestamp'].dt.date < today]
            
            # Sort by timestamp
            df = df.sort_values('timestamp').reset_index(drop=True)
            
            return df
            
        except Exception as e:
            logger.error(f"Error loading {timeframe} data for {symbol}: {e}")
            return None
    
    def ensure_symbol_loaded(self, symbol: str) -> bool:
        """
        Ensure a symbol's historical data is loaded and up to date.
        Called when a new symbol is added via Manage Symbols.

        Steps:
          1. If already cached and fresh → return immediately.
          2. Fetch instrument token for this symbol if not already known
             (tokens are normally fetched at startup; new symbols need this).
          3. Try loading from CSV (data/daily/SYMBOL.csv).
          4. If CSV missing or stale, bootstrap from Zerodha OMS API.
        """
        if symbol in self.cache and 'daily' in self.cache[symbol]:
            df = self.cache[symbol]['daily']
            if not self._is_data_stale(df):
                return True
            # Stale — refresh
            if self.auth_manager:
                refreshed = self._refresh_if_stale(symbol, df)
                if refreshed is not None:
                    self.cache[symbol]['daily'] = refreshed
            return True

        # ── Fetch instrument token for this symbol if missing ─────────────────
        # _bootstrap_historical and _refresh_if_stale both need the token.
        # Normally tokens are fetched at startup for the configured symbol list.
        # When a new symbol is added dynamically we fetch its token on demand.
        if symbol not in self._instrument_tokens and self.auth_manager:
            self._fetch_token_for_symbol(symbol)

        # ── Load from CSV or bootstrap from Zerodha ───────────────────────────
        self.cache.setdefault(symbol, {})
        daily_data = self._load_csv(symbol, 'daily')

        if daily_data is not None:
            if self._is_data_stale(daily_data) and self.auth_manager:
                refreshed = self._refresh_if_stale(symbol, daily_data)
                if refreshed is not None:
                    daily_data = refreshed
        elif self.auth_manager:
            daily_data = self._bootstrap_historical(symbol)

        if daily_data is not None:
            self.cache[symbol]['daily'] = daily_data
            logger.info(f"✓ ensure_symbol_loaded: {symbol} loaded ({len(daily_data)} candles)")
            return True

        logger.warning(f"ensure_symbol_loaded: no data available for {symbol} "
                       f"(token={'found' if symbol in self._instrument_tokens else 'MISSING'})")
        return False

    def _fetch_token_for_symbol(self, symbol: str) -> bool:
        """
        Fetch the Zerodha instrument token for a single symbol.
        Uses the cached instrument CSV from realtime manager if available,
        otherwise fetches fresh from the public Kite instruments endpoint.
        Returns True if token was found and stored.
        """
        try:
            import requests as _req

            # ── Try to reuse the realtime manager's already-fetched instrument list ──
            rt = None
            try:
                # Import here to avoid circular imports
                from frontend.app import dashboard_state as _ds
                rt = _ds.get('realtime_manager')
            except Exception:
                pass

            if rt and hasattr(rt, 'instrument_tokens') and rt.instrument_tokens:
                for tok, info in rt.instrument_tokens.items():
                    if info.get('symbol') == symbol:
                        self._instrument_tokens[symbol] = tok
                        logger.info(f"✓ Token for {symbol} from realtime cache: {tok}")
                        return True

            # ── Fetch from public instruments CSV ─────────────────────────────
            df = None
            for url, use_auth in [
                ("https://api.kite.trade/instruments", False),
                (f"{Config.ZERODHA_API_BASE}/oms/instruments/NSE", True),
            ]:
                try:
                    session = self.auth_manager.session if use_auth else _req.Session()
                    resp = session.get(url, timeout=12)
                    if resp.status_code == 200 and resp.text.strip():
                        df = pd.read_csv(StringIO(resp.text))
                        break
                except Exception:
                    pass

            if df is None:
                logger.warning(f"_fetch_token_for_symbol({symbol}): could not fetch instruments CSV")
                return False

            SEGMENT_PRIORITY = ['NSE-EQ', 'NSE', 'NSE-INDICES', 'BSE-EQ', 'BSE']
            match = pd.DataFrame()
            for seg in SEGMENT_PRIORITY:
                match = df[(df['tradingsymbol'] == symbol) & (df['segment'] == seg)]
                if not match.empty:
                    break
            if match.empty:
                match = df[df['tradingsymbol'] == symbol]

            if not match.empty:
                token = str(match.iloc[0]['instrument_token'])
                self._instrument_tokens[symbol] = token
                logger.info(f"✓ Token fetched for new symbol {symbol}: {token} "
                            f"(segment={match.iloc[0]['segment']})")
                return True
            else:
                logger.warning(f"_fetch_token_for_symbol: {symbol} not found in instruments list")
                return False

        except Exception as e:
            logger.error(f"_fetch_token_for_symbol({symbol}) error: {e}")
            return False

    def get_daily_data(self, symbol: str, days: int = None) -> Optional[pd.DataFrame]:
        """
        Get daily data for a symbol
        
        Args:
            symbol: ETF symbol
            days: Number of recent days to return (None for all)
            
        Returns:
            DataFrame with daily OHLC data
        """
        if symbol not in self.cache or 'daily' not in self.cache[symbol]:
            logger.warning(f"No daily data cached for {symbol}")
            return None
        
        df = self.cache[symbol]['daily'].copy()
        
        if days is not None and len(df) > days:
            df = df.tail(days).reset_index(drop=True)
        
        return df
    
    def get_latest_close(self, symbol: str) -> Optional[float]:
        """Get the latest closing price from historical data"""
        df = self.get_daily_data(symbol)
        if df is not None and len(df) > 0:
            return float(df.iloc[-1]['close'])
        return None
    
    def get_symbol_list(self) -> list:
        """Get list of symbols with cached data"""
        return list(self.cache.keys())
