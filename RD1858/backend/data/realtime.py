"""
Real-time Data Manager
Manages WebSocket connection to Zerodha for live market data
"""
import urllib.parse
import time
import threading
from typing import Dict, Optional, Callable
from datetime import datetime

_RECONNECT_DELAY_SECONDS = 10
# On Render, container evictions can happen at any time during market hours.
# We never give up reconnecting — _MAX_RECONNECT_ATTEMPTS=0 means unlimited.
import os as _os
_MAX_RECONNECT_ATTEMPTS = int(_os.environ.get("WS_MAX_RECONNECT", "0"))  # 0 = unlimited
_LTP_STALE_AFTER_SEC = 45  # cached tick older than this is untrusted; falls through to REST quote

from kiteconnect import KiteTicker
import pandas as pd
from io import StringIO

from pathlib import Path
import json

from backend.core.config import Config
from backend.core.constants import LIQUIDCASE_SYMBOL, MARKET_INDICES
from backend.utils.logger import get_logger

logger = get_logger(__name__)

# Persistent token cache — survives restarts outside market hours
_TOKEN_CACHE = Path(__file__).resolve().parent.parent.parent / 'config' / 'instrument_tokens.json'


def _save_token_cache(instrument_tokens: dict, live_data_keys: list):
    """Save instrument tokens to JSON so they survive restarts."""
    try:
        _TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
        cache = {
            'instrument_tokens': instrument_tokens,
            'live_data_keys': live_data_keys,
            'saved_at': datetime.now().isoformat(),
        }
        _TOKEN_CACHE.write_text(json.dumps(cache, indent=2))
        logger.info(f"✓ Token cache saved: {len(instrument_tokens)} tokens → {_TOKEN_CACHE.name}")
    except Exception as e:
        logger.warning(f"Could not save token cache: {e}")


def _load_token_cache() -> tuple:
    """Load instrument tokens from cache. Returns (instrument_tokens, live_data_keys) or (None, None)."""
    try:
        if not _TOKEN_CACHE.exists():
            return None, None
        cache = json.loads(_TOKEN_CACHE.read_text())
        tokens = cache.get('instrument_tokens', {})
        keys   = cache.get('live_data_keys', [])
        if not tokens:
            return None, None
        logger.info(f"✓ Loaded {len(tokens)} instrument tokens from cache (saved {cache.get('saved_at','?')[:19]})")
        return tokens, keys
    except Exception as e:
        logger.warning(f"Could not load token cache: {e}")
        return None, None


class RealtimeDataManager:
    """Manages real-time market data via WebSocket"""
    
    def __init__(self, auth_manager):
        self.auth = auth_manager
        self.kws: Optional[KiteTicker] = None
        self.is_connected = False
        self.instrument_tokens: Dict[str, Dict] = {}
        self.live_data: Dict[str, Dict] = {}
        self.callbacks = []
        self._lock = threading.Lock()
        self._reconnect_attempts = 0
        self._reconnect_thread: Optional[threading.Thread] = None
        self._last_close_reason: str = ''
    
    def initialize(self) -> bool:
        """Initialize WebSocket connection"""
        try:
            logger.info("Initializing WebSocket connection...")
            
            # Fetch instrument tokens
            if not self._fetch_instrument_tokens():
                logger.error("Failed to fetch instrument tokens")
                return False
            
            # Initialize KiteTicker
            access_token = f'&user_id={self.auth.user_id}&enctoken={urllib.parse.quote(self.auth.enctoken)}'
            
            # reconnect_max_tries=0 is critical here: KiteTicker's own
            # built-in auto-reconnect (the `reconnect` kwarg is a documented
            # no-op in kiteconnect 5.0.1 — it's accepted but never read; the
            # real lever is reconnect_max_tries, which sets the underlying
            # autobahn factory's maxRetries) otherwise races our custom
            # 403-aware reconnect loop, hammering the same stale enctoken on
            # its own internal exponential-backoff timer before
            # _refresh_token_and_rebuild_ticker() ever gets a chance to run.
            # Setting tries to 0 makes _on_close → _schedule_reconnect the
            # ONLY reconnect path, so a stale-token refresh actually lands
            # before the next connect attempt fires.
            self.kws = KiteTicker(
                api_key='kitefront',
                access_token=access_token,
                root=Config.ZERODHA_WS_URL,
                reconnect=False,
                reconnect_max_tries=0
            )
            
            # Set up callbacks
            self.kws.on_ticks = self._on_ticks
            self.kws.on_connect = self._on_connect
            self.kws.on_close = self._on_close
            self.kws.on_error = self._on_error
            
            logger.info("✓ WebSocket initialized")
            return True
            
        except Exception as e:
            logger.error(f"Error initializing WebSocket: {e}")
            return False
    
    def _fetch_instrument_tokens(self) -> bool:
        """
        Fetch instrument tokens for all symbols and market indices.

        URL strategy (tried in order):
          1. https://api.kite.trade/instruments  — public CSV, no auth, works anytime
          2. https://kite.zerodha.com/oms/instruments/NSE  — requires enctoken, market hours only

        On success: tokens are saved to config/instrument_tokens.json for future startups.
        On failure: falls back to the cached file from the last successful fetch.
        """
        import requests as _req

        df_all = None

        # URL list: (url, use_auth_session)
        URLS = [
            ("https://api.kite.trade/instruments", False),          # public, no auth needed
            (f"{Config.ZERODHA_API_BASE}/oms/instruments/NSE", True),  # OMS, needs enctoken
        ]

        for url, use_auth in URLS:
            try:
                session = self.auth.session if use_auth else _req.Session()
                # Public endpoint must NOT send Authorization header
                if not use_auth:
                    session.headers.update({'Accept': 'text/csv,*/*'})
                resp = session.get(url, timeout=12)
                if resp.status_code == 200 and resp.text.strip():
                    df_all = pd.read_csv(StringIO(resp.text), low_memory=False)
                    logger.info(f"✓ Instruments CSV from {url} ({len(df_all)} rows)")
                    break
                else:
                    logger.warning(f"Instruments fetch {url}: HTTP {resp.status_code}")
            except Exception as e:
                logger.warning(f"Instruments fetch {url}: {e}")

        if df_all is None:
            # Live fetch failed — try cache
            cached_tokens, cached_keys = _load_token_cache()
            if cached_tokens:
                self.instrument_tokens = cached_tokens
                for sym in (cached_keys or []):
                    if sym not in self.live_data:
                        self.live_data[sym] = {
                            'token': cached_tokens.get(next(
                                (t for t,i in cached_tokens.items() if i.get('symbol')==sym), None
                            ), {}).get('symbol'),
                            'last_price': None, 'open': None, 'high': None,
                            'low': None, 'close': None, 'volume': None, 'timestamp': None
                        }
                return True
            logger.error("Failed to fetch instrument tokens and no cache available")
            return False

        # Build token map
        SEGMENT_PRIORITY = ['NSE-EQ', 'NSE', 'NSE-INDICES', 'BSE-EQ', 'BSE']

        active_etfs      = Config.get_active_etfs()
        bnh_symbols      = Config.get_bnh_symbols()
        symbols_to_track = list(dict.fromkeys(active_etfs + bnh_symbols + [LIQUIDCASE_SYMBOL]))

        logger.info(f"Looking up tokens for: {', '.join(symbols_to_track)}")

        def _find(symbol, seg_hint=None):
            if seg_hint:
                m = df_all[(df_all['tradingsymbol'] == symbol) & (df_all['segment'] == seg_hint)]
                if not m.empty:
                    return m.iloc[0]
            for seg in SEGMENT_PRIORITY:
                m = df_all[(df_all['tradingsymbol'] == symbol) & (df_all['segment'] == seg)]
                if not m.empty:
                    return m.iloc[0]
            m = df_all[df_all['tradingsymbol'] == symbol]
            return m.iloc[0] if not m.empty else None

        # ETFs + LIQUIDCASE
        for symbol in symbols_to_track:
            row = _find(symbol)
            if row is not None:
                token = str(row['instrument_token'])
                self.instrument_tokens[token] = {
                    'symbol': symbol, 'name': str(row.get('name', symbol)),
                    'exchange': str(row.get('exchange', 'NSE'))
                }
                self.live_data[symbol] = {
                    'token': token, 'last_price': None, 'open': None,
                    'high': None, 'low': None, 'close': None,
                    'volume': None, 'timestamp': None
                }
                logger.info(f"✓ {symbol}: token {token} (segment={row.get('segment')})")
            else:
                logger.warning(f"✗ Token not found for {symbol}")

        # Market indices (NIFTY 50, NIFTY MIDCAP 150, INDIA VIX)
        for index_name, index_info in MARKET_INDICES.items():
            row = _find(index_name, seg_hint=index_info.get('segment'))
            if row is None:
                # Try name column
                m = df_all[df_all['name'] == index_name]
                row = m.iloc[0] if not m.empty else None
            if row is not None:
                token = str(row['instrument_token'])
                self.instrument_tokens[token] = {
                    'symbol': index_name, 'name': index_name,
                    'exchange': index_info.get('exchange', 'NSE')
                }
                self.live_data[index_name] = {
                    'token': token, 'last_price': None, 'open': None,
                    'high': None, 'low': None, 'close': None,
                    'volume': None, 'timestamp': None
                }
                logger.info(f"✓ Index {index_name}: token {token}")
            else:
                logger.warning(f"✗ Index token not found for {index_name}")

        if self.instrument_tokens:
            _save_token_cache(self.instrument_tokens, list(self.live_data.keys()))

        logger.info(f"✓ {len(self.instrument_tokens)} instrument tokens ready")
        return len(self.instrument_tokens) > 0
    
    def start(self) -> bool:
        """Start WebSocket connection"""
        try:
            if not self.kws:
                logger.error("WebSocket not initialized")
                return False
            
            logger.info("Starting WebSocket connection...")
            self.kws.connect(threaded=True)
            
            # Wait for connection
            max_wait = 10
            waited = 0
            while not self.is_connected and waited < max_wait:
                time.sleep(0.5)
                waited += 0.5
            
            if self.is_connected:
                logger.info("✓ WebSocket connected and subscribed")
                return True
            else:
                logger.error("WebSocket connection timeout")
                return False
                
        except Exception as e:
            logger.error(f"Error starting WebSocket: {e}")
            return False
    
    def stop(self):
        """Stop WebSocket connection"""
        if self.kws:
            try:
                self.kws.close()
                logger.info("WebSocket connection closed")
            except Exception as e:
                logger.error(f"Error closing WebSocket: {e}")
    
    def _on_connect(self, ws, response):
        """WebSocket connection callback"""
        logger.info("✓ WebSocket connected successfully")
        
        # Subscribe to all tokens
        tokens = [int(token) for token in self.instrument_tokens.keys()]
        logger.info(f"Subscribing to {len(tokens)} instruments...")
        
        try:
            ws.subscribe(tokens)
            logger.info(f"✅ Subscribe called successfully")
            
            ws.set_mode(ws.MODE_FULL, tokens)
            logger.info(f"✅ Set mode to FULL successfully")
            
            self.is_connected = True
            logger.info(f"✅ Subscribed to {len(tokens)} instruments - waiting for ticks...")
        except Exception as e:
            logger.error(f"❌ Error during subscription: {e}")
    
    def _on_ticks(self, ws, ticks):
        """WebSocket ticks callback"""
        try:
            # Quietly process ticks (only log errors)
            pass  # Remove all logging to reduce noise
            
            with self._lock:
                for tick in ticks:
                    token = str(tick['instrument_token'])
                    
                    if token in self.instrument_tokens:
                        symbol = self.instrument_tokens[token]['symbol']
                        
                        # Update live data (including depth from MODE_FULL)
                        depth = tick.get('depth', {})
                        buy_depth  = depth.get('buy',  [])
                        sell_depth = depth.get('sell', [])
                        top_bid = float(buy_depth[0]['price'])  if buy_depth  and buy_depth[0].get('price')  else None
                        top_ask = float(sell_depth[0]['price']) if sell_depth and sell_depth[0].get('price') else None

                        self.live_data[symbol].update({
                            'last_price': tick.get('last_price'),
                            'open':       tick.get('ohlc', {}).get('open'),
                            'high':       tick.get('ohlc', {}).get('high'),
                            'low':        tick.get('ohlc', {}).get('low'),
                            'close':      tick.get('ohlc', {}).get('close'),
                            'volume':     tick.get('volume_traded'),
                            'timestamp':  datetime.now(),
                            'top_bid':    top_bid,
                            'top_ask':    top_ask,
                            'buy_depth':  buy_depth[:5]  if buy_depth  else [],
                            'sell_depth': sell_depth[:5] if sell_depth else [],
                        })
            
            # Notify callbacks
            for callback in self.callbacks:
                try:
                    callback(ticks)
                except Exception as e:
                    logger.error(f"Error in callback: {e}")
                    
        except Exception as e:
            logger.error(f"Error processing ticks: {e}")
    
    def _on_close(self, ws, code, reason):
        """WebSocket close callback — triggers background reconnect"""
        logger.warning(f"🔴 WebSocket closed: {code} - {reason}")
        self.is_connected = False
        self._last_close_reason = str(reason or '')
        self._schedule_reconnect()
    
    def _on_error(self, ws, code, reason):
        """WebSocket error callback"""
        logger.error(f"❌ WebSocket error: {code} - {reason}")
        self._last_close_reason = str(reason or '')
    
    @staticmethod
    def _looks_like_auth_rejection(reason: str) -> bool:
        """
        Zerodha's WS handshake returns HTTP 403 when the enctoken is stale or
        expired. The underlying websocket-client lib surfaces this as a 1006
        abnormal-close with '403' / 'Forbidden' in the reason text — never a
        clean 401, since the rejection happens at the HTTP upgrade step before
        any WS-level close code exists. Reconnecting with the same dead token
        will repeat this forever, so we detect it here and refresh first.
        """
        r = (reason or '').lower()
        return '403' in r or 'forbidden' in r

    def _schedule_reconnect(self):
        """Spawn a background thread to attempt WebSocket reconnection."""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return  # Already reconnecting

        # _MAX_RECONNECT_ATTEMPTS == 0 means unlimited (recommended for Render)
        if _MAX_RECONNECT_ATTEMPTS > 0 and self._reconnect_attempts >= _MAX_RECONNECT_ATTEMPTS:
            logger.error(
                f"❌ WebSocket reconnect gave up after {_MAX_RECONNECT_ATTEMPTS} attempts. "
                "Restart the bot to restore live data."
            )
            return

        self._reconnect_thread = threading.Thread(
            target=self._reconnect_loop, daemon=True
        )
        self._reconnect_thread.start()

    def _refresh_token_and_rebuild_ticker(self) -> bool:
        """
        Pull a fresh enctoken and rebuild the KiteTicker with it. Returns
        True only if the token actually changed and the ticker was rebuilt.

        IMPORTANT: this method alone is not sufficient. KiteTicker has its
        own built-in auto-reconnect (the `reconnect` constructor kwarg is a
        documented no-op in kiteconnect 5.0.1 — accepted but never read; the
        real lever is `reconnect_max_tries`, which sets the underlying
        autobahn factory's maxRetries). If that isn't set to 0 at
        construction time, the library races this method: it reconnects
        with the same stale enctoken on its own internal backoff timer
        before this refresh logic ever runs, so the 403 just repeats no
        matter how correct the refresh-on-403 logic is. Both KiteTicker(...)
        call sites in this file MUST pass reconnect_max_tries=0 so that
        _on_close -> _schedule_reconnect -> this method is the ONLY
        reconnect path.

        Priority mirrors AuthManager.authenticate()'s own GH-Actions-aware
        shortcut:
          - On GitHub Actions there is never a browser, so CDP extraction
            inside handle_session_expiry() would burn up to ~37s (3 ports ×
            5 retries × 2.5s) before falling through anyway — skip straight
            to a TOTP credentials login, same as authenticate() does.
          - Everywhere else, try CDP first (handle_session_expiry) since it
            preserves the live browser session, then fall back to
            credentials login only if no browser is reachable at all.
        """
        if not self.auth:
            return False
        if getattr(self.auth, '_bot_paused', False):
            logger.debug("WS 403 re-auth: bot is PAUSED — re-auth suppressed to protect browser session")
            return False

        old_enctoken = getattr(self.auth, 'enctoken', None)
        refreshed = False

        import os as _os
        on_gh_actions = _os.environ.get('GITHUB_ACTIONS', '').lower() == 'true'

        if on_gh_actions:
            logger.info("WS 403 — GH Actions: no browser available, skipping CDP, using credentials login")
            if hasattr(self.auth, '_login_with_credentials') and Config.CREDENTIALS_FILE.exists():
                try:
                    refreshed = bool(self.auth._login_with_credentials())
                except Exception as e:
                    logger.error(f"TOTP re-login failed: {e}")
                    return False
            else:
                logger.warning("WS 403: GH Actions but no credentials.json — cannot refresh token")
                return False
        else:
            try:
                refreshed = bool(self.auth.handle_session_expiry())
            except Exception as e:
                logger.warning(f"CDP token refresh raised: {e}")

            new_enctoken = getattr(self.auth, 'enctoken', None)
            if not refreshed or new_enctoken == old_enctoken:
                # CDP didn't yield a fresh token (no reachable browser session) —
                # fall back to a TOTP credentials login, same as snapshot.py does
                # for OMS 403s.
                if hasattr(self.auth, '_login_with_credentials') and Config.CREDENTIALS_FILE.exists():
                    try:
                        logger.warning("WS 403: CDP unavailable — attempting fresh TOTP re-login...")
                        refreshed = bool(self.auth._login_with_credentials())
                    except Exception as e:
                        logger.error(f"TOTP re-login failed: {e}")
                        return False

        new_enctoken = getattr(self.auth, 'enctoken', None)
        if not refreshed or not new_enctoken or new_enctoken == old_enctoken:
            logger.warning("WS 403: token refresh did not produce a new enctoken")
            return False

        try:
            access_token = f'&user_id={self.auth.user_id}&enctoken={urllib.parse.quote(new_enctoken)}'
            self.kws = KiteTicker(
                api_key='kitefront',
                access_token=access_token,
                root=Config.ZERODHA_WS_URL,
                reconnect=False,
                reconnect_max_tries=0
            )
            self.kws.on_ticks   = self._on_ticks
            self.kws.on_connect = self._on_connect
            self.kws.on_close   = self._on_close
            self.kws.on_error   = self._on_error
            logger.info("✅ WebSocket ticker rebuilt with refreshed enctoken")
            return True
        except Exception as e:
            logger.error(f"Failed to rebuild KiteTicker with refreshed token: {e}")
            return False
    
    def _reconnect_loop(self):
        """
        Attempt to reconnect WebSocket with capped back-off.
        When _MAX_RECONNECT_ATTEMPTS == 0, retries indefinitely (Render default).
        Back-off is capped at 60 s so a Render container eviction during
        market hours recovers within one minute.
        """
        unlimited = (_MAX_RECONNECT_ATTEMPTS == 0)
        while (unlimited or self._reconnect_attempts < _MAX_RECONNECT_ATTEMPTS) and not self.is_connected:
            self._reconnect_attempts += 1
            # Cap back-off at 60 s
            wait = min(_RECONNECT_DELAY_SECONDS * self._reconnect_attempts, 60)
            attempt_str = str(self._reconnect_attempts) if not unlimited else f"{self._reconnect_attempts}(∞)"
            logger.info(
                f"🔄 WebSocket reconnect attempt {attempt_str} in {wait}s..."
            )
            time.sleep(wait)

            if self.is_connected:
                break

            # If the connection was rejected at the WS handshake with a 403,
            # the enctoken itself is stale/expired — retrying with the same
            # token will just 403 again forever (this is exactly the failure
            # mode that was happening before this check existed). Refresh the
            # token and rebuild the ticker BEFORE the next connect attempt.
            if self._looks_like_auth_rejection(self._last_close_reason):
                logger.warning(
                    "🔑 WS reconnect: last close looked like a 403/Forbidden "
                    "(stale enctoken) — refreshing token before retrying..."
                )
                if self._refresh_token_and_rebuild_ticker():
                    self._last_close_reason = ''  # consumed; don't re-refresh every loop
                else:
                    logger.warning(
                        "🔑 Token refresh did not succeed — will still retry with the "
                        "existing token in case this resolves itself (e.g. browser "
                        "login completes), but live data will stay down until either "
                        "the browser Kite session is refreshed or credentials login "
                        "succeeds."
                    )

            try:
                self.kws.connect(threaded=True)
                waited = 0
                while not self.is_connected and waited < 10:
                    time.sleep(0.5)
                    waited += 0.5

                if self.is_connected:
                    logger.info(f"✅ WebSocket reconnected (attempt {self._reconnect_attempts})")
                    self._reconnect_attempts = 0
                    return
            except Exception as e:
                logger.error(f"Reconnect attempt {self._reconnect_attempts} failed: {e}")

        if not self.is_connected and not unlimited:
            logger.error(
                f"❌ WebSocket reconnect failed after {_MAX_RECONNECT_ATTEMPTS} attempts. "
                "Restart the bot to restore live data."
            )

    def add_symbols(self, symbols: list) -> dict:
        """
        Dynamically subscribe to new symbols without restarting the WebSocket.
        Fetches instrument tokens for any symbol not already tracked, adds them
        to live_data, and subscribes via the active WebSocket connection.
        Returns {'added': [...], 'already_tracked': [...], 'not_found': [...]}.
        """
        already_tracked, added, not_found = [], [], []

        with self._lock:
            existing_syms = {info['symbol'] for info in self.instrument_tokens.values()}

        for symbol in symbols:
            symbol = symbol.strip().upper()
            if symbol in existing_syms:
                already_tracked.append(symbol)
                continue

            # Look up token — try public endpoint first (works anytime, no auth)
            try:
                import pandas as pd, requests as _req
                df_all = None
                for url, use_auth in [
                    ("https://api.kite.trade/instruments", False),
                    (f"{Config.ZERODHA_API_BASE}/oms/instruments/NSE", True),
                ]:
                    try:
                        sess = self.auth.session if use_auth else _req.Session()
                        r = sess.get(url, timeout=12)
                        if r.status_code == 200 and r.text.strip():
                            from io import StringIO as _SIO
                            df_all = pd.read_csv(_SIO(r.text))
                            break
                    except Exception:
                        pass

                if df_all is None:
                    not_found.append(symbol)
                    continue

                df_all.columns = [c.lower() for c in df_all.columns]
                df_sym = df_all[df_all['tradingsymbol'] == symbol]
                if df_sym.empty:
                    not_found.append(symbol)
                    logger.warning(f"add_symbols: token not found for {symbol}")
                    continue

                SEGMENT_PRIORITY = ['NSE-EQ', 'NSE', 'NSE-INDICES', 'BSE-EQ', 'BSE']
                row = None
                for seg in SEGMENT_PRIORITY:
                    m = df_sym[df_sym['segment'] == seg] if 'segment' in df_sym.columns else pd.DataFrame()
                    if not m.empty:
                        row = m.iloc[0]; break
                if row is None:
                    row = df_sym.iloc[0]

                token = str(int(row['instrument_token']))
                with self._lock:
                    self.instrument_tokens[token] = {
                        'symbol': symbol,
                        'name': str(row.get('name', symbol)),
                        'exchange': str(row.get('exchange', 'NSE')),
                    }
                    self.live_data[symbol] = {
                        'token': token, 'last_price': None, 'open': None,
                        'high': None, 'low': None, 'close': None,
                        'volume': None, 'timestamp': None,
                    }

                # Subscribe via active WebSocket
                if self.kws and self.is_connected:
                    try:
                        self.kws.subscribe([int(token)])
                        self.kws.set_mode(self.kws.MODE_FULL, [int(token)])
                        logger.info(f"add_symbols: subscribed {symbol} (token {token})")
                    except Exception as e:
                        logger.warning(f"add_symbols: WS subscribe error for {symbol}: {e}")

                added.append(symbol)
                existing_syms.add(symbol)

            except Exception as e:
                logger.error(f"add_symbols: error for {symbol}: {e}")
                not_found.append(symbol)

        if added:
            _save_token_cache(self.instrument_tokens, list(self.live_data.keys()))

        logger.info(f"add_symbols result — added:{added} already:{already_tracked} not_found:{not_found}")
        return {'added': added, 'already_tracked': already_tracked, 'not_found': not_found}

    def get_ltp(self, symbol: str) -> Optional[float]:
        """
        Get last traded price for a symbol.
        Primary: WebSocket live_data cache.
        Fallback: REST /oms/quote when WebSocket tick hasn't arrived yet.
        """
        with self._lock:
            if symbol in self.live_data:
                price = self.live_data[symbol].get('last_price')
                ts    = self.live_data[symbol].get('timestamp')
                if price is not None:
                    # A cached tick is only trustworthy if it's recent. During
                    # a WebSocket outage, live_data keeps whatever the last
                    # good tick was — potentially minutes/hours stale — and
                    # without this check get_ltp() would keep returning that
                    # frozen price forever instead of ever trying the REST
                    # fallback below, silently feeding stale prices into buy
                    # decisions during exactly the kind of outage that should
                    # trigger the fallback.
                    if ts is not None and (datetime.now() - ts).total_seconds() <= _LTP_STALE_AFTER_SEC:
                        return price

        # WebSocket tick not received yet — try REST quote as fallback
        try:
            # Find exchange for this symbol from instrument_tokens
            exchange = 'NSE'
            with self._lock:
                for info in self.instrument_tokens.values():
                    if info.get('symbol') == symbol:
                        exchange = info.get('exchange', 'NSE')
                        break

            instrument_key = f'{exchange}:{symbol}'
            resp = self.auth.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/quote",
                params={'i': instrument_key},
                timeout=5
            )
            if resp.status_code == 200:
                qd = resp.json().get('data', {}).get(instrument_key, {})
                ltp = qd.get('last_price')
                if ltp:
                    ltp = float(ltp)
                    # Cache it so next call is instant
                    with self._lock:
                        if symbol in self.live_data:
                            self.live_data[symbol]['last_price'] = ltp
                            prev = qd.get('ohlc', {})
                            if prev.get('close'):
                                self.live_data[symbol]['close'] = float(prev['close'])
                    return ltp
        except Exception as e:
            logger.debug(f"REST LTP fallback for {symbol}: {e}")

        return None
    
    def get_depth(self, symbol: str) -> Optional[Dict]:
        """Return top-5 market depth for a symbol (buy/sell sides)."""
        with self._lock:
            data = self.live_data.get(symbol, {})
            if not data:
                return None
            return {
                'top_bid':    data.get('top_bid'),
                'top_ask':    data.get('top_ask'),
                'buy_depth':  data.get('buy_depth',  []),
                'sell_depth': data.get('sell_depth', []),
            }

    def get_top_bid(self, symbol: str) -> Optional[float]:
        """Return the best (highest) bid price from market depth."""
        with self._lock:
            return self.live_data.get(symbol, {}).get('top_bid')

    def get_top_ask(self, symbol: str) -> Optional[float]:
        """Return the best (lowest) ask price from market depth."""
        with self._lock:
            return self.live_data.get(symbol, {}).get('top_ask')

    def get_total_ask_qty(self, symbol: str) -> int:
        """Return total quantity available on the ask (sell) side of the order book."""
        with self._lock:
            sell_depth = self.live_data.get(symbol, {}).get('sell_depth', [])
            return sum(int(level.get('quantity', 0)) for level in sell_depth)

    def get_ohlc(self, symbol: str) -> Optional[Dict]:
        """Get OHLC data for a symbol"""
        with self._lock:
            if symbol in self.live_data:
                return {
                    'open': self.live_data[symbol].get('open'),
                    'high': self.live_data[symbol].get('high'),
                    'low': self.live_data[symbol].get('low'),
                    'close': self.live_data[symbol].get('close'),
                    'volume': self.live_data[symbol].get('volume')
                }
        return None
    
    def add_callback(self, callback: Callable):
        """Add a callback for tick updates"""
        self.callbacks.append(callback)
