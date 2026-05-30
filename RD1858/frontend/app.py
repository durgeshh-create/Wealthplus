"""
Flask application for ETF Trading Dashboard.
Simple web server serving HTML templates with real-time updates.
"""

from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import sys
import os
import time
from pathlib import Path

# Add parent directory to path for backend imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.config import Config
from backend.utils.logger import get_logger

logger = get_logger(__name__)

# When running as a PyInstaller exe, Flask cannot auto-discover templates/static
# because __file__ points inside the bundled archive. We pass the paths explicitly.
if getattr(sys, 'frozen', False):
    _ui_base = Path(sys._MEIPASS) / 'frontend'
else:
    _ui_base = Path(__file__).resolve().parent

app = Flask(
    __name__,
    template_folder=str(_ui_base / 'templates'),
    static_folder=str(_ui_base / 'static'),
)
CORS(app)

# Configuration
app.config['SECRET_KEY'] = 'etf-trading-dashboard-2026'
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # Disable static file caching in development

# Build version for cache-busting static assets (changes every server restart)
_STATIC_VERSION = str(int(time.time()))

# Store reference to backend instances (will be set by launcher)
dashboard_state = {
    'auth_manager': None,
    'portfolio_tracker': None,
    'realtime_manager': None,
    'historical_manager': None,
    'signal_generator': None,
    'executor': None,           # ✅ FIX: added so Buy Now / Sell Now routes can find it
    'order_manager': None,
    'bot_running': False,
    'intraday_engine': None,
    'auth_ready': False,      # True once login completed and backend booted
    'boot_status': None,      # 'booting' | 'ready' | 'error'
    'boot_message': '',
}


def initialize_dashboard(auth_mgr, portfolio, realtime, signal_gen, order_mgr, historical=None, executor=None):
    """Initialize dashboard with backend instances."""
    from backend.strategy.intraday_engine import IntradayEngine
    dashboard_state['auth_manager']      = auth_mgr
    dashboard_state['portfolio_tracker'] = portfolio
    dashboard_state['realtime_manager']  = realtime
    dashboard_state['historical_manager']= historical
    dashboard_state['signal_generator']  = signal_gen
    dashboard_state['order_manager']     = order_mgr
    dashboard_state['executor']          = executor   # ✅ FIX: store executor so routes can access it
    dashboard_state['intraday_engine']   = IntradayEngine(realtime, order_mgr, portfolio, historical)
    logger.info("Dashboard initialized with backend instances")


@app.route('/')
def index():
    """Serve login page if not authenticated, else dashboard."""
    if not dashboard_state.get('auth_ready'):
        return render_template('login.html', v=_STATIC_VERSION)
    return render_template('dashboard.html', v=_STATIC_VERSION)


@app.route('/dashboard')
def dashboard():
    """Direct link to dashboard (redirects to login if not authed)."""
    if not dashboard_state.get('auth_ready'):
        return render_template('login.html', v=_STATIC_VERSION)
    return render_template('dashboard.html', v=_STATIC_VERSION)


@app.route('/api/status')
def get_status():
    """Get system status."""
    try:
        return jsonify({
            'status': 'online',
            'bot_running': dashboard_state['bot_running'],
            'mode': 'DRY_RUN' if Config.is_dry_run() else 'LIVE',
            'market_connected': dashboard_state['realtime_manager'] is not None
        })
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/ping')
def ping():
    """
    Lightweight liveness endpoint for health checks.
    Must return 200 instantly — no backend calls.
    """
    from datetime import datetime
    import pytz
    IST = pytz.timezone("Asia/Kolkata")
    return jsonify({"status": "ok", "time": datetime.now(IST).strftime("%H:%M:%S IST")}), 200


@app.route('/api/config')
def get_config():
    """Get current configuration."""
    try:
        import json
        settings_path = Config.SETTINGS_FILE

        if settings_path.exists():
            with open(settings_path, 'r') as f:
                settings = json.load(f)
        else:
            settings = {
                'active_etfs': ['MID150BEES', 'MON100', 'GOLDBEES', 'SILVERBEES', 'MINDSPACE-RR', 'EMBASSY-RR'],
                'slots_count': Config.SLOTS_COUNT,
                'profit_target_pct': Config.PROFIT_TARGET_PCT,
                'williams_r_threshold': Config.WILLIAMS_R_THRESHOLD,
                'test_quantity': 0,
                'max_cash_per_stock': Config.MAX_CASH_PER_STOCK,
                'max_cash_per_transaction': Config.MAX_CASH_PER_TRANSACTION,
            }

        if 'test_quantity' not in settings:
            settings['test_quantity'] = 0
        if 'default_test_quantity' not in settings:
            settings['default_test_quantity'] = settings.get('test_quantity', 0)
        if 'max_cash_per_stock' not in settings:
            settings['max_cash_per_stock'] = Config.MAX_CASH_PER_STOCK
        if 'max_cash_per_transaction' not in settings:
            settings['max_cash_per_transaction'] = Config.MAX_CASH_PER_TRANSACTION
        if 'min_price_drop_pct' not in settings:
            settings['min_price_drop_pct'] = 1.0
        if 'buy_execution_time' not in settings:
            settings['buy_execution_time'] = '15:15'
        if 'default_order_type' not in settings:
            settings['default_order_type'] = 'MARKET'
        if 'cash_reserve' not in settings:
            settings['cash_reserve'] = Config.CASH_RESERVE
        if 'intraday_capital' not in settings:
            settings['intraday_capital'] = 50000

        settings['mode'] = 'DRY_RUN' if Config.is_dry_run() else 'LIVE'
        
        return jsonify(settings)
    except Exception as e:
        logger.error(f"Error getting config: {e}")
        return jsonify({'error': str(e)}), 500


# Import routes after app initialization
from frontend.routes import register_routes
register_routes(app, dashboard_state)


# ═══════════════════════════════════════════════════════════════
# AUTH ROUTES  (defined here so they're available before backend boots)
# ═══════════════════════════════════════════════════════════════

@app.route('/api/auth/status')
def auth_status():
    return jsonify({
        'auth_ready':   dashboard_state.get('auth_ready', False),
        'boot_status':  dashboard_state.get('boot_status'),
        'boot_message': dashboard_state.get('boot_message', ''),
        'user_id':      dashboard_state['auth_manager'].user_id
                        if dashboard_state.get('auth_manager') else None,
    })


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    """Accept userid+enctoken from login page, verify, save, boot backend."""
    import threading as _threading
    import json as _json
    from datetime import datetime as _dt
    from pathlib import Path as _P
    from flask import request as _req

    data     = request.get_json(force=True, silent=True) or {}
    user_id  = (data.get('user_id')  or '').strip()
    enctoken = (data.get('enctoken') or '').strip()

    if not user_id or not enctoken:
        return jsonify({'success': False, 'error': 'user_id and enctoken are required'}), 400

    # Verify token against Zerodha before saving
    try:
        import requests as _r
        s = _r.Session()
        s.headers.update({
            'Authorization': f'enctoken {enctoken}',
            'Accept': 'application/json',
        })
        resp = s.get(f'{Config.ZERODHA_API_BASE}/oms/user/profile', timeout=8)
        if resp.status_code != 200:
            return jsonify({
                'success': False,
                'error': f'Zerodha rejected this token (HTTP {resp.status_code}). '
                         'Please copy a fresh enctoken from kite.zerodha.com and try again.'
            }), 401
    except Exception as e:
        return jsonify({'success': False, 'error': f'Could not verify token: {e}'}), 500

    # Save enctoken.json
    try:
        enc_path = _P(__file__).parent.parent / 'config' / 'enctoken.json'
        enc_path.parent.mkdir(parents=True, exist_ok=True)
        with open(enc_path, 'w') as f:
            _json.dump({
                'user_id':   user_id,
                'enctoken':  enctoken,
                'timestamp': _dt.now().strftime('%Y-%m-%d %H:%M:%S'),
            }, f, indent=4)
    except Exception as e:
        return jsonify({'success': False, 'error': f'Could not save token: {e}'}), 500

    dashboard_state['boot_status']  = 'booting'
    dashboard_state['boot_message'] = 'Starting up...'

    def _boot():
        import sys, time
        import threading as _t
        sys.path.insert(0, str(_P(__file__).parent.parent))

        from backend.auth.manager              import AuthManager
        from backend.data.historical           import HistoricalDataManager
        from backend.data.realtime             import RealtimeDataManager
        from backend.portfolio.tracker         import PortfolioTracker
        from backend.orders.manager            import OrderManager
        from backend.strategy.signal_generator import SignalGenerator
        from backend.strategy.executor         import StrategyExecutor

        try:
            dashboard_state['boot_message'] = 'Authenticating with Zerodha...'
            auth = AuthManager()
            if not auth.authenticate():
                dashboard_state['boot_status']  = 'error'
                dashboard_state['boot_message'] = 'Authentication failed. Please try again.'
                return

            dashboard_state['boot_message'] = 'Loading historical data (may take 30s for new symbols)...'
            historical = HistoricalDataManager(auth)

            dashboard_state['boot_message'] = 'Connecting real-time market feed...'
            realtime = RealtimeDataManager(auth)
            realtime.initialize()
            _t.Thread(target=realtime.start, daemon=True).start()
            time.sleep(4)

            dashboard_state['boot_message'] = 'Syncing portfolio...'
            portfolio = PortfolioTracker(auth)
            portfolio.sync()

            # ── Hibernate + WebSocket watchdogs ────────────────────────────────
            # Called AFTER portfolio.sync() so auth_manager and portfolio_tracker
            # can be passed in for full post-hibernate re-auth + resync.
            try:
                from backend.utils.keep_alive import start_keep_alive
                start_keep_alive(realtime, auth_manager=auth, portfolio_tracker=portfolio)
                logger.info("✓ Hibernate + WebSocket watchdogs started")
            except Exception as _ka_err:
                logger.warning(f"keep_alive start failed (non-fatal): {_ka_err}")
            # ──────────────────────────────────────────────────────────────────

            dashboard_state['boot_message'] = 'Starting order manager...'
            orders = OrderManager(auth)

            dashboard_state['boot_message'] = 'Initialising signal engine...'
            signals  = SignalGenerator(historical, realtime, portfolio)
            executor = StrategyExecutor(orders, portfolio, realtime, signals)

            # ✅ FIX: pass executor so initialize_dashboard stores it in dashboard_state
            initialize_dashboard(auth, portfolio, realtime, signals, orders, historical, executor=executor)

            # Start trading loop
            import dashboard as _dash
            _dash.trading_active = True
            _t.Thread(
                target=_dash.trading_loop,
                args=(signals, executor, portfolio),
                daemon=True, name='TradingLoopThread'
            ).start()

            dashboard_state['auth_ready']   = True
            dashboard_state['boot_status']  = 'ready'
            dashboard_state['boot_message'] = 'System ready'
            logger.info('✅ Backend booted after web login')

        except Exception as e:
            logger.error(f'Boot failed: {e}', exc_info=True)
            dashboard_state['boot_status']  = 'error'
            dashboard_state['boot_message'] = str(e)

    _threading.Thread(target=_boot, daemon=True, name='BootThread').start()
    return jsonify({'success': True, 'message': 'Token accepted — booting system...'})


@app.route('/api/auth/try-cdp', methods=['POST'])
def auth_try_cdp():
    """
    Called by the login page polling loop.
    Attempts CDP auto-extraction from the live Kite browser tab.
    Returns {success: true} and triggers backend boot if token found.
    """
    import threading as _threading

    if dashboard_state.get('auth_ready'):
        return jsonify({'success': True, 'already': True})

    try:
        from backend.auth.manager import AuthManager
        auth = AuthManager()
        if not auth._load_enctoken_from_browser():
            return jsonify({'success': False, 'reason': 'no_kite_tab'})
    except Exception as e:
        return jsonify({'success': False, 'reason': str(e)})

    # Token found — boot the backend exactly like auth_login does
    dashboard_state['boot_status']  = 'booting'
    dashboard_state['boot_message'] = 'Auto-login via Kite session...'

    def _boot():
        import sys, time
        import threading as _t
        from pathlib import Path as _P
        sys.path.insert(0, str(_P(__file__).parent.parent))

        from backend.auth.manager              import AuthManager
        from backend.data.historical           import HistoricalDataManager
        from backend.data.realtime             import RealtimeDataManager
        from backend.portfolio.tracker         import PortfolioTracker
        from backend.orders.manager            import OrderManager
        from backend.strategy.signal_generator import SignalGenerator
        from backend.strategy.executor         import StrategyExecutor

        try:
            dashboard_state['boot_message'] = 'Authenticating with Zerodha...'
            boot_auth = AuthManager()
            if not boot_auth.authenticate():
                dashboard_state['boot_status']  = 'error'
                dashboard_state['boot_message'] = 'Authentication failed after CDP extract.'
                return

            dashboard_state['boot_message'] = 'Loading historical data...'
            historical = HistoricalDataManager(boot_auth)

            dashboard_state['boot_message'] = 'Connecting real-time market feed...'
            realtime = RealtimeDataManager(boot_auth)
            realtime.initialize()
            _t.Thread(target=realtime.start, daemon=True).start()
            time.sleep(4)

            dashboard_state['boot_message'] = 'Syncing portfolio...'
            portfolio = PortfolioTracker(boot_auth)
            portfolio.sync()

            try:
                from backend.utils.keep_alive import start_keep_alive
                start_keep_alive(realtime, auth_manager=boot_auth, portfolio_tracker=portfolio)
            except Exception as _ka_err:
                logger.warning(f'keep_alive start failed (non-fatal): {_ka_err}')

            dashboard_state['boot_message'] = 'Starting order manager...'
            orders = OrderManager(boot_auth)

            dashboard_state['boot_message'] = 'Initialising signal engine...'
            signals  = SignalGenerator(historical, realtime, portfolio)
            executor = StrategyExecutor(orders, portfolio, realtime, signals)

            initialize_dashboard(boot_auth, portfolio, realtime, signals, orders, historical, executor=executor)

            import dashboard as _dash
            _dash.trading_active = True
            _t.Thread(
                target=_dash.trading_loop,
                args=(signals, executor, portfolio),
                daemon=True, name='TradingLoopThread'
            ).start()

            dashboard_state['auth_ready']   = True
            dashboard_state['boot_status']  = 'ready'
            dashboard_state['boot_message'] = 'System ready'
            logger.info('✅ Backend booted via CDP auto-login')

        except Exception as e:
            logger.error(f'CDP boot failed: {e}', exc_info=True)
            dashboard_state['boot_status']  = 'error'
            dashboard_state['boot_message'] = str(e)

    _threading.Thread(target=_boot, daemon=True, name='CDPBootThread').start()
    return jsonify({'success': True, 'message': 'CDP token found — booting system...'})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    """Clear saved enctoken and reset state."""
    from backend.auth.token_store import delete_token as _del_token
    _del_token()   # removes local enctoken file
    dashboard_state.update({
        'auth_ready': False, 'boot_status': None,
        'boot_message': '', 'bot_running': False
    })
    return jsonify({'success': True})


def run_dashboard(host='0.0.0.0', port=5000, debug=False):
    """Run the dashboard server."""
    import logging, sys

    # Suppress per-request werkzeug lines (GET /api/... 200) — keep startup messages
    class _StartupFilter(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            # Keep "Running on" startup line, suppress per-request lines
            if 'Running on' in msg or 'Press CTRL' in msg:
                return True
            return False

    wz = logging.getLogger('werkzeug')
    wz.setLevel(logging.INFO)
    for h in wz.handlers:
        h.addFilter(_StartupFilter())

    print(f"\n  🌐  Dashboard server starting on http://localhost:{port}", flush=True)
    print(f"  ⚡  Open http://localhost:{port} in your browser if it doesn't open automatically\n", flush=True)

    app.run(host=host, port=port, debug=debug, use_reloader=False)


if __name__ == '__main__':
    run_dashboard(debug=True)
