"""
Authentication Manager for Zerodha
Handles login, token management, and session verification
"""
import json
import requests
import pyotp
from typing import Optional, Dict
from pathlib import Path

from backend.core.config import Config
from backend.utils.logger import get_logger
from backend.auth.token_store import save_token, load_token, delete_token

logger = get_logger(__name__)


class AuthManager:
    """Manages Zerodha authentication"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            'Referer':    'https://kite.zerodha.com/',
            'Origin':     'https://kite.zerodha.com',
            'Accept':     'application/json, text/plain, */*',
            # Do NOT add X-Kite-Version: 3 — that belongs to api.kite.trade only
            # and causes the web OMS to demand api_key + access_token.
        })

        self.user_id:          Optional[str] = None
        self.enctoken:         Optional[str] = None
        self.is_authenticated: bool          = False

        # Set to True the moment any debug-port browser is reachable, even if
        # it has no Kite tab yet. Prevents credentials login from firing while
        # any browser window is open.
        self._cdp_found_tab:       bool  = False
        self._last_reauth_attempt: float = 0.0

        Config.ensure_directories()

    # ══════════════════════════════════════════════════════════════════════════
    # Public API
    # ══════════════════════════════════════════════════════════════════════════

    def authenticate(self) -> bool:
        """
        Authenticate with Zerodha.

        Priority — browser session is NEVER disturbed:

          1. Saved enctoken  — instant, used on every bot restart.
          2. CDP extraction  — reads the live enctoken from the open browser tab
                               without any new login. Browser session unchanged.
          3. Credentials login (TOTP) — ONLY when CDP confirms no debug-port
                               browser is reachable at all (headless mode).
                               A TOTP login creates a new enctoken and invalidates
                               every existing browser session, so it must NEVER
                               fire while a browser window is open.
        """
        logger.info("Starting authentication...")

        # Step 1 — saved token
        if self._load_saved_enctoken():
            logger.info("✓ Authenticated using saved enctoken")
            return True

        # Step 2 — extract from open browser (safe, no new login)
        logger.info("Saved token expired — trying CDP browser extraction (preserves browser session)...")
        self._cdp_found_tab = False
        if self._load_enctoken_from_browser(expected_uid=self._expected_user_id()):
            logger.info("✓ Authenticated using browser Kite session (CDP)")
            return True

        # Step 3 — credentials login ONLY when no browser was found
        if self._cdp_found_tab:
            # A browser IS open but we could not read a Kite token.
            # Doing a credentials login now would log that browser out.
            # Wait for the user to finish loading Kite and restart the bot.
            logger.warning(
                "CDP: browser reachable but no valid Kite session found. "
                "Credentials login BLOCKED — this would log out the open browser. "
                "Log into kite.zerodha.com in the browser, then restart the bot."
            )
            return False

        if Config.CREDENTIALS_FILE.exists():
            logger.info("No browser found — using credentials login (headless mode only)")
            if self._login_with_credentials():
                logger.info("✓ Authenticated using credentials login")
                return True
            logger.warning("Credentials login failed")
            return False

        logger.info("No saved token, no browser, no credentials — waiting for manual login.")
        return False

    def handle_session_expiry(self) -> bool:
        """
        Called when any OMS endpoint returns 403 / TokenException mid-session.

        ONLY does CDP extraction — never a credentials login — because a TOTP
        login mid-session would create a new enctoken and log every browser out.

        Debounce: 10 s for near-instant recovery after a browser token rotation.
        """
        import time
        now = time.time()
        if now - self._last_reauth_attempt < 10:
            return False

        # Never re-auth while paused — the user paused to log into Kite in
        # the browser without interference. A credentials login here would
        # immediately log the browser out.
        if getattr(self, '_bot_paused', False):
            logger.debug("OMS 403 received but bot is PAUSED — re-auth suppressed to protect browser session")
            return False

        self._last_reauth_attempt = now
        logger.warning("OMS 403 — token stale, extracting fresh token from browser via CDP...")
        self.is_authenticated = False

        try:
            self._cdp_found_tab = False
            if self._load_enctoken_from_browser(expected_uid=self._expected_user_id()):
                self.is_authenticated = True
                logger.info("✅ Token refreshed from browser via CDP — session restored, browser untouched")
                return True

            if self._cdp_found_tab:
                # Browser open but Kite tab not readable yet — do NOT fall back to credentials
                logger.warning(
                    "CDP: browser reachable but Kite tab not extractable. "
                    "Credentials login BLOCKED to protect browser. Will retry on next 403."
                )
                return False

            # No browser at all — credentials only as last resort
            if Config.CREDENTIALS_FILE.exists() and self._login_with_credentials():
                self.is_authenticated = True
                logger.info("✅ Re-auth via credentials (headless fallback)")
                return True

            logger.error("❌ Re-auth failed — manual login required")
            return False

        except Exception as e:
            logger.error(f"Re-authentication error: {e}")
            return False

    def get_profile(self) -> Optional[Dict]:
        """Get user profile information"""
        try:
            response = self.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/user/profile", timeout=10
            )
            if response.status_code == 200:
                return response.json().get('data', {})
            return None
        except Exception as e:
            logger.error(f"Error fetching profile: {e}")
            return None

    # ══════════════════════════════════════════════════════════════════════════
    # Internal helpers
    # ══════════════════════════════════════════════════════════════════════════

    def _expected_user_id(self) -> str:
        """Read user_id from credentials.json; return '' if absent."""
        try:
            if Config.CREDENTIALS_FILE.exists():
                return json.loads(
                    Config.CREDENTIALS_FILE.read_text()
                ).get('user_id', '').strip().upper()
        except Exception:
            pass
        return ''

    def _load_saved_enctoken(self) -> bool:
        """Load and verify the persisted enctoken."""
        try:
            auth_data = load_token()
            if not auth_data:
                logger.debug("No saved enctoken found")
                return False

            self.user_id  = auth_data.get('user_id')
            self.enctoken = auth_data.get('enctoken')

            if not self.user_id or not self.enctoken:
                logger.warning("Invalid enctoken data in store")
                return False

            self._update_session_headers()

            if self._verify_session():
                self.is_authenticated = True
                logger.info(f"Loaded saved enctoken for user {self.user_id}")
                return True
            else:
                logger.warning("Saved enctoken is expired")
                return False

        except Exception as e:
            logger.error(f"Error loading saved enctoken: {e}")
            return False

    def _load_enctoken_from_browser(self, expected_uid: str = '') -> bool:
        """
        Extract the live enctoken from an open Kite tab via Chrome DevTools
        Protocol (CDP) without performing any new login.

        Sets self._cdp_found_tab = True as soon as ANY debug-port browser
        responds — even if it has no kite.zerodha.com tab open. This prevents
        the caller from falling through to credentials login while a browser
        window is running (which would log it out).
        """
        import time as _time

        self._cdp_found_tab = False

        CDP_PORTS   = [9222, 9223, 9224]
        MAX_RETRIES = getattr(self, '_cdp_max_retries_override', None) or 5
        RETRY_DELAY = 2.5 if MAX_RETRIES > 1 else 0

        for attempt in range(1, MAX_RETRIES + 1):
            kite_tab = None
            ws_url   = None

            for port in CDP_PORTS:
                try:
                    tabs_resp = requests.get(f"http://localhost:{port}/json", timeout=2)
                    if tabs_resp.status_code != 200:
                        continue
                    all_tabs = tabs_resp.json()
                    # Any valid CDP response → browser is open → block credentials login
                    if isinstance(all_tabs, list):
                        self._cdp_found_tab = True
                    for tab in (all_tabs or []):
                        if "kite.zerodha.com" in tab.get("url", "") and tab.get("type") == "page":
                            kite_tab = tab
                            ws_url   = tab.get("webSocketDebuggerUrl")
                            break
                    if kite_tab:
                        break
                except Exception:
                    continue

            if not kite_tab:
                if self._cdp_found_tab:
                    if attempt < MAX_RETRIES:
                        logger.debug(
                            f"CDP attempt {attempt}/{MAX_RETRIES}: browser reachable "
                            "but no kite.zerodha.com tab yet — retrying..."
                        )
                        _time.sleep(RETRY_DELAY)
                        continue
                    logger.info(
                        "CDP: browser open but no kite.zerodha.com tab. "
                        "Credentials login blocked. Open Kite in the browser."
                    )
                    return False
                if attempt < MAX_RETRIES:
                    logger.debug(f"CDP attempt {attempt}/{MAX_RETRIES}: no debug-port browser — retrying...")
                    _time.sleep(RETRY_DELAY)
                    continue
                logger.debug("No CDP-enabled browser on ports 9222-9224")
                return False

            if not ws_url:
                logger.debug("Kite tab has no WebSocket debugger URL")
                return False

            logger.info(f"Found Kite tab via CDP (attempt {attempt}): {kite_tab.get('url', '')[:60]}")

            JS = """
(function() {
    var token = '';
    var cookieList = document.cookie.split(';');
    for (var i = 0; i < cookieList.length; i++) {
        var c = cookieList[i].trim();
        if (c.indexOf('enctoken=') === 0) {
            token = decodeURIComponent(c.substring('enctoken='.length));
            break;
        }
    }
    var uid = '';
    try { var d = JSON.parse(localStorage.getItem('user_data') || '{}'); uid = d.user_id || d.userId || ''; } catch(e) {}
    if (!uid) { try { var s = JSON.parse(localStorage.getItem('kf_session') || '{}'); uid = s.user_id || s.userId || ''; } catch(e) {} }
    if (!uid) { var el = document.querySelector('.user-id') || document.querySelector('.avatar span'); if (el) uid = el.textContent.trim(); }
    return JSON.stringify({enctoken: token, user_id: uid});
})();
"""
            try:
                import websocket
                import threading

                result_json = None

                def _on_open(ws):
                    ws.send(json.dumps({
                        "id": 1,
                        "method": "Runtime.evaluate",
                        "params": {"expression": JS, "returnByValue": True},
                    }))

                def _on_message(ws, message):
                    nonlocal result_json
                    msg = json.loads(message)
                    if msg.get("id") == 1:
                        result_json = msg.get("result", {}).get("result", {}).get("value")
                        ws.close()

                def _on_error(ws, error):
                    logger.debug(f"CDP WebSocket error: {error}")
                    ws.close()

                ws_app = websocket.WebSocketApp(ws_url, on_open=_on_open,
                                                on_message=_on_message, on_error=_on_error)
                t = threading.Thread(target=ws_app.run_forever, daemon=True)
                t.start()
                t.join(timeout=8)

            except ImportError:
                logger.debug("websocket-client not installed — run: pip install websocket-client")
                return False
            except Exception as e:
                logger.debug(f"CDP JS execution failed: {e}")
                return False

            if not result_json:
                if attempt < MAX_RETRIES:
                    _time.sleep(RETRY_DELAY)
                    continue
                logger.debug("CDP returned no result from Kite tab")
                return False

            try:
                data    = json.loads(result_json)
                token   = (data.get("enctoken") or "").strip()
                user_id = (data.get("user_id")  or "").strip().upper()
            except Exception as e:
                logger.debug(f"Could not parse CDP result: {e}")
                return False

            if not token:
                if attempt < MAX_RETRIES:
                    _time.sleep(RETRY_DELAY)
                    continue
                logger.debug("Kite tab found but enctoken empty — user may not be logged in")
                return False

            if expected_uid and user_id and user_id != expected_uid:
                logger.warning(
                    f"CDP tab logged in as {user_id} but bot expects {expected_uid} "
                    "— token rejected. Browser session untouched."
                )
                return False

            break  # success

        else:
            return False

        # Verify with Zerodha
        try:
            test_session = requests.Session()
            test_session.headers.update({
                'Authorization': f'enctoken {token}',
                'User-Agent':    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer':       'https://kite.zerodha.com/',
                'Origin':        'https://kite.zerodha.com',
                'Accept':        'application/json',
            })
            resp = test_session.get(f'{Config.ZERODHA_API_BASE}/oms/user/profile', timeout=8)
            if resp.status_code != 200:
                logger.debug(f"CDP token rejected by Zerodha (HTTP {resp.status_code})")
                return False
            profile = resp.json().get('data', {})
            if not user_id:
                user_id = (profile.get('user_id') or '').upper()
            if not user_id:
                logger.debug("Could not determine user_id from profile")
                return False
        except Exception as e:
            logger.debug(f"Zerodha verification failed: {e}")
            return False

        self.enctoken = token
        self.user_id  = user_id
        self._update_session_headers()
        self._save_enctoken()
        self.is_authenticated = True
        logger.info(f"✓ Enctoken extracted from browser via CDP for {self.user_id}")
        return True

    def _login_with_credentials(self) -> bool:
        """
        Fresh TOTP login — ONLY called when no debug-port browser is reachable.
        Creates a new enctoken which invalidates all existing browser sessions.
        Must NEVER be called when _cdp_found_tab is True.
        """
        try:
            with open(Config.CREDENTIALS_FILE, 'r') as f:
                creds = json.load(f)

            user_id  = creds.get('user_id',  '').strip()
            password = creds.get('password', '').strip()
            totp_key = creds.get('totp_key', '').strip()

            if not user_id or not password or not totp_key:
                logger.debug("Credentials file incomplete")
                return False

            import re
            if not re.match(r'^[A-Z2-7]+=*$', totp_key.upper()):
                logger.debug("TOTP key appears invalid")
                return False

            logger.info("Performing fresh credentials login (no browser detected)...")

            resp = self.session.post(
                f'{Config.ZERODHA_API_BASE}/api/login',
                data={'user_id': user_id, 'password': password}
            )
            if resp.status_code != 200:
                logger.error(f"Login failed: {resp.text}")
                return False

            totp = pyotp.TOTP(totp_key)
            resp = self.session.post(
                f'{Config.ZERODHA_API_BASE}/api/twofa',
                data={
                    'user_id':     user_id,
                    'request_id':  resp.json()['data']['request_id'],
                    'twofa_value': totp.now(),
                }
            )
            if resp.status_code != 200:
                logger.error(f"2FA failed: {resp.text}")
                return False

            if 'enctoken' not in self.session.cookies:
                logger.error("Enctoken not in response cookies")
                return False

            self.enctoken = self.session.cookies['enctoken']
            self.user_id  = user_id
            self._update_session_headers()
            self._save_enctoken()
            self.is_authenticated = True
            logger.info(f"Fresh login successful for {user_id}")
            return True

        except Exception as e:
            logger.error(f"Credentials login error: {e}")
            return False

    def _update_session_headers(self):
        if self.enctoken:
            self.session.headers.update({'Authorization': f'enctoken {self.enctoken}'})

    def _verify_session(self) -> bool:
        try:
            resp = self.session.get(f"{Config.ZERODHA_API_BASE}/oms/user/profile", timeout=10)
            return resp.status_code == 200 and bool(resp.json().get('data', {}).get('user_name'))
        except Exception as e:
            logger.error(f"Session verification failed: {e}")
            return False

    def _save_enctoken(self):
        try:
            save_token(self.user_id, self.enctoken)
        except Exception as e:
            logger.error(f"Error saving enctoken: {e}")
