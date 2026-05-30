"""
ETF Trading Dashboard Launcher
Integrates backend trading system with web dashboard.

FEATURES:
- Web-based visualization of portfolio, market data, and signals
- Automated trading loop that monitors signals and executes trades
- Start/Stop bot control via dashboard interface
- Real-time updates every 1 second
- Signal checking every 2 seconds when bot is active
- Portfolio sync every 60 seconds

ARCHITECTURE:
- Main thread: Runs Flask web server
- Background thread: Trading loop (monitors signals when bot_running=True)
- WebSocket thread: Real-time market data updates

The trading loop is ALWAYS running in the background, but only executes
signals when you click "Start Bot" in the dashboard.
"""

import sys
import os
import time
import threading
import socket
import subprocess
import platform
import webbrowser
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent))

from backend.auth.manager import AuthManager
from backend.data.historical import HistoricalDataManager
from backend.data.realtime import RealtimeDataManager
from backend.portfolio.tracker import PortfolioTracker
from backend.orders.manager import OrderManager
from backend.strategy.signal_generator import SignalGenerator
from backend.strategy.executor import StrategyExecutor
from backend.core.config import Config
from backend.utils.logger import get_logger
from frontend.app import run_dashboard, initialize_dashboard, dashboard_state

logger = get_logger(__name__)

# FIX: Forces engine to cleanly listen strictly on default trading port 5001
PORT = 5001
HOST = "0.0.0.0"
BASE_URL = f"http://localhost:{PORT}"

# Global trading thread control
trading_thread = None
trading_active = False


def trading_loop(signal_generator, executor, portfolio_tracker):
    """
    Background trading loop - monitors signals and executes trades.
    Runs when bot_running is True.
    """
    global trading_active, _loop_started_at

    _loop_started_at = time.time()
    logger.info("🤖 Trading loop thread started (60s startup grace period — monitoring only)")
    last_portfolio_sync = time.time()
    last_signal_check = time.time()

    try:
        while trading_active:
            current_time = time.time()

            if dashboard_state.get("bot_running", False):
                if current_time - last_portfolio_sync >= Config.PORTFOLIO_REFRESH_INTERVAL:
                    logger.debug("🔄 Auto-syncing portfolio...")
                    portfolio_tracker.sync()
                    last_portfolio_sync = current_time
                    if signal_generator and hasattr(signal_generator, '_pending_exec'):
                        for sym in list(signal_generator._pending_exec.keys()):
                            signal_generator.clear_pending_exec(sym)

                if current_time - last_signal_check >= Config.MARKET_DATA_REFRESH_INTERVAL:
                    check_and_execute_signals(signal_generator, executor)
                    last_signal_check = current_time

            time.sleep(0.5)

    except Exception as e:
        logger.error(f"❌ Error in trading loop: {e}", exc_info=True)
    finally:
        logger.info("🛑 Trading loop thread stopped")


# Track when the trading loop started — used for startup grace period
_loop_started_at: float = 0.0


def check_and_execute_signals(signal_generator, executor):
    """
    Check for active signals and execute if conditions are fully met.

    BUY  execution: gated by scheduled time window (default 3:15 PM) via
                    get_due_buys() in signal_generator — never fires early.
    SELL execution: gated by the SAME scheduled time window so sells don't
                    fire the moment the bot starts (e.g. at 9:15 AM when
                    a position is already in profit from a prior session).

    Startup grace period: 60 seconds after the loop starts, only logging
    occurs — no orders — so the portfolio has time to sync and stale
    pending-buy queues are cleared before the first execution cycle.
    """
    global _loop_started_at
    try:
        import json
        from datetime import datetime as _dt, time as _dtime

        # ── Startup grace period (60s) ────────────────────────────────────────
        if _loop_started_at > 0 and (time.time() - _loop_started_at) < 60:
            logger.debug("⏳ Startup grace period — monitoring signals, no execution yet")
            return

        settings_path = Path(__file__).parent / "config" / "settings.json"
        profit_target = Config.PROFIT_TARGET_PCT
        max_qty = 0
        sell_exec_time = _dtime(15, 15)   # default: same window as buy
        anytime_mode   = False

        if settings_path.exists():
            try:
                with open(settings_path, "r") as f:
                    settings = json.load(f)
                    profit_target = float(settings.get("profit_target_pct", Config.PROFIT_TARGET_PCT))
                    max_qty       = int(settings.get("test_quantity", 0))
                    exec_val      = settings.get("buy_execution_time", "15:15")
                    if exec_val == "anytime":
                        anytime_mode = True
                    else:
                        parts = exec_val.split(":")
                        sell_exec_time = _dtime(int(parts[0]), int(parts[1]))
            except Exception:
                pass

        signals = signal_generator.get_active_signals()

        buy_signals  = signals.get("buy",  [])   # already time-gated by get_due_buys()
        sell_signals = signals.get("sell", [])

        # ── Sell time gate ────────────────────────────────────────────────────
        # Anytime mode: sell immediately whenever profit target is met.
        # Scheduled mode: sell only within the 1-minute execution window.
        if not anytime_mode:
            now_t = _dt.now().time()
            sell_window_end = _dtime(sell_exec_time.hour,
                                     min(sell_exec_time.minute + 1, 59))
            in_sell_window  = sell_exec_time <= now_t <= sell_window_end

            if sell_signals and not in_sell_window:
                for signal in sell_signals:
                    logger.info(
                        f"  🔴 SELL PENDING (executes at {sell_exec_time.strftime('%H:%M')}): "
                        f"{signal['symbol']} | profit target met @ ₹{signal['price']:.2f}"
                    )
                sell_signals = []   # suppress execution until window opens

        if buy_signals or sell_signals:
            logger.info("=" * 60)
            logger.info(f"📊 Executing signals — Buy: {len(buy_signals)}, Sell: {len(sell_signals)}")
            logger.info(
                f"⚙️  Profit Target={profit_target}%, "
                f"Max Qty={max_qty if max_qty > 0 else 'Unlimited'}"
            )

            for signal in buy_signals:
                logger.info(
                    f"  🟢 BUY: {signal['symbol']} | "
                    f"W%R={signal['williams_r']:.2f} | ₹{signal['price']:.2f}"
                )
            for signal in sell_signals:
                logger.info(f"  🔴 SELL: {signal['symbol']} | ₹{signal['price']:.2f}")

            logger.info("=" * 60)

            if Config.is_dry_run():
                logger.warning("⚠️  DRY RUN MODE: Signals detected but not executing")
            else:
                logger.info("⚡ Executing signals...")
                results = executor.execute_signals({'buy': buy_signals, 'sell': sell_signals})
                for action, success in results.items():
                    status = "✅" if success else "❌"
                    logger.info(f"{status} {action}")

    except Exception as e:
        logger.error(f"❌ Error checking signals: {e}", exc_info=True)


def initialize_backend(auth_manager=None):
    """Initialize all backend components. Accepts pre-authenticated AuthManager."""
    print("⏳ Initializing system...\n")

    try:
        if auth_manager is None:
            print("   [1/7] 🔐 Authenticating with Zerodha...", end=" ", flush=True)
            auth_manager = AuthManager()
            if not auth_manager.authenticate():
                print("❌")
                logger.error("Authentication failed")
                return None
            print("✅")
        else:
            print("   [1/7] 🔐 Using pre-authenticated session ✅")

        print("   [2/7] 📈 Loading & refreshing historical data...", flush=True)
        historical_manager = HistoricalDataManager(auth_manager)
        print("   [2/7] ✅ Historical data ready")

        print("   [3/7] 📡 Connecting to real-time data...", end=" ", flush=True)
        realtime_manager = RealtimeDataManager(auth_manager)
        # initialize() fetches instrument tokens (up to 13s total with timeouts).
        # Run it in a thread so we can cap its wait time and move on.
        import threading as _rt_threading
        _rt_ok = [False]
        def _do_init():
            _rt_ok[0] = realtime_manager.initialize()
        _t = _rt_threading.Thread(target=_do_init, daemon=True)
        _t.start()
        _t.join(timeout=15)   # max 15s wait for WS init (token fetch = 8+5s max)

        ws_thread = threading.Thread(target=realtime_manager.start, daemon=True)
        ws_thread.start()
        time.sleep(2)   # reduced from 5s — WS connect is fast once tokens are ready
        print("✅" if realtime_manager.is_connected else "⚠️  (offline — live data unavailable)")

        print("   [4/7] 💼 Syncing portfolio...", end=" ", flush=True)
        portfolio_tracker = PortfolioTracker(auth_manager)
        portfolio_tracker.sync()
        print("✅")

        print("   [5/7] 📝 Initializing order manager...", end=" ", flush=True)
        order_manager = OrderManager(auth_manager)
        print("✅")

        print("   [6/7] 🎯 Initializing signal generator...", end=" ", flush=True)
        signal_generator = SignalGenerator(
            historical_manager,
            realtime_manager,
            portfolio_tracker
        )
        print("✅")

        print("   [7/7] ⚡ Initializing strategy executor...", end=" ", flush=True)
        strategy_executor = StrategyExecutor(
            order_manager,
            portfolio_tracker,
            realtime_manager,
            signal_generator
        )
        print("✅\n")

        return {
            "auth": auth_manager,
            "historical": historical_manager,
            "realtime": realtime_manager,
            "portfolio": portfolio_tracker,
            "orders": order_manager,
            "signals": signal_generator,
            "executor": strategy_executor,
        }

    except Exception as e:
        logger.error(f"Backend initialization failed: {e}", exc_info=True)
        return None


def get_pid_using_port(port=5001):
    """Get PID of process using the specified port."""
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                check=True
            )
            for line in result.stdout.split("\n"):
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        return int(parts[-1])
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True,
                text=True,
                check=True
            )
            if result.stdout.strip():
                return int(result.stdout.strip().split()[0])
    except Exception as e:
        logger.debug(f"Error getting PID for port {port}: {e}")
    return None


def kill_process(pid):
    """Kill process by PID."""
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True,
                check=True
            )
        else:
            subprocess.run(
                ["kill", "-9", str(pid)],
                capture_output=True,
                check=True
            )
        return True
    except Exception as e:
        logger.error(f"Error killing process {pid}: {e}")
        return False


def check_and_clear_port(port=5001):
    """Check if port is in use and kill existing process if needed."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(("localhost", port))
    sock.close()

    if result == 0:
        print(f"\n⚠️  Port {port} is already in use. Checking for existing dashboard...")

        pid = get_pid_using_port(port)
        if pid:
            print(f"🔄 Found existing dashboard instance (PID: {pid})")
            print("🔄 Stopping old instance...")

            if kill_process(pid):
                print("✅ Stopped old instance successfully")
                time.sleep(2)

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                result = sock.connect_ex(("localhost", port))
                sock.close()

                if result != 0:
                    print(f"✅ Port {port} is now available\n")
                    return True
                else:
                    print(f"\n❌ ERROR: Port {port} is still in use after stopping process")
                    print(f"Please manually close any applications using port {port}\n")
                    sys.exit(1)
            else:
                print("\n❌ ERROR: Failed to stop existing instance")
                print(f"Please manually close the dashboard at http://localhost:{port}\n")
                sys.exit(1)
        else:
            print(f"\n❌ ERROR: Could not identify process using port {port}")
            print(f"Please manually close any applications using port {port}\n")
            sys.exit(1)

    return True


def print_startup_banner():
    """Print professional startup banner."""
    print("\n" + "=" * 70)
    print("🚀  ETF TRADING SYSTEM  🚀".center(70))
    print("=" * 70)
    mode = "🟡 DRY RUN MODE" if Config.is_dry_run() else "🔴 LIVE TRADING MODE"
    print(mode.center(70))
    print("=" * 70 + "\n")


def print_startup_summary(backend):
    """Print clean startup summary."""
    print("\n" + "╔" + "═" * 68 + "╗")
    print("║" + "  ✅  SYSTEM READY".center(68) + "║")
    print("╠" + "═" * 68 + "╣")
    print("║" + f" 🌐 Dashboard: {BASE_URL}".ljust(68) + "║")
    print(
        "║"
        + f"  📊  Real-time Data: {'🟢 Connected' if backend['realtime'].is_connected else '🔴 Disconnected'}".ljust(68)
        + "║"
    )
    print("║" + f"  👤  User: {backend['auth'].user_id}".ljust(68) + "║")
    print("║" + "  💼  Portfolio: Synced & Tracking".ljust(68) + "║")
    print("║" + "  🤖  Auto-Trading: Ready (Start from dashboard)".ljust(68) + "║")
    print("╠" + "═" * 68 + "╣")
    print("║" + "  ⚠️   Press Ctrl+C to stop the server".ljust(68) + "║")
    print("╚" + "═" * 68 + "╝")
    print()


def _open_browser_when_ready(port: int = PORT, timeout: int = 30):
    """
    Wait until Flask is accepting connections on port, then open the browser.
    Local-only convenience helper.
    """
    url = f"http://localhost:{port}"
    deadline = time.time() + timeout

    while time.time() < deadline:
        for host in ("127.0.0.1", "localhost"):
            try:
                with socket.create_connection((host, port), timeout=1):
                    print(f"  🌐  Opening browser at {url}", flush=True)
                    webbrowser.open(url)
                    return
            except OSError:
                pass
        time.sleep(0.4)


def main():
    """Main entry point."""
    global trading_thread, trading_active

    check_and_clear_port(PORT)

    print_startup_banner()

    auth_manager = AuthManager()
    has_valid_auth = auth_manager.authenticate()

    # == FIX: LOCAL LOGIN HOOK RECOVERY REMOVED ==
    # Disables old background port 5050 capture scripts entirely

    if not has_valid_auth:
        print("\n ⚠️ No saved session — attempting CDP auto-login from Kite browser tab...")

        # Try CDP extraction directly at startup (before Flask loads the login page).
        # This covers the first-ever boot where enctoken.json doesn't exist yet but
        # the user already has kite.zerodha.com open in Chrome (launched by algo.bat).
        try:
            has_valid_auth = auth_manager._load_enctoken_from_browser()
            if has_valid_auth:
                logger.info("✓ CDP startup auto-login succeeded — bypassing login page")
                print(" ✓ CDP auto-login successful — starting dashboard directly\n")
        except Exception as _cdp_err:
            logger.debug(f"CDP startup attempt failed (non-fatal): {_cdp_err}")

    if not has_valid_auth:
        print(f"\n ⚠️ Authentication session payload missing or expired.")
        print(f" 📋 Standby Login interface: {BASE_URL}")
        print(" ℹ️ Booting system infrastructure into manual capture listening mode.\n")

        # == FIX: INTERNALLY SPAWNED AUTO-OPEN THREAD REMOVED ==
        # Bypasses the auto-browser thread to avoid spawning phantom tabs

        try:
            sys.stdout.flush()
            run_dashboard(host=HOST, port=PORT, debug=False)
        except KeyboardInterrupt:
            print("\n\nShutting down.")
        return

    dashboard_state["auth_ready"] = True
    dashboard_state["boot_status"] = "booting"
    dashboard_state["boot_message"] = "Initialising..."

    backend = initialize_backend(auth_manager=auth_manager)

    if not backend:
        logger.error("Failed to initialize backend. Exiting.")
        return

    dashboard_state["boot_status"] = "ready"
    dashboard_state["boot_message"] = "System ready"

    initialize_dashboard(
        backend["auth"],
        backend["portfolio"],
        backend["realtime"],
        backend["signals"],
        backend["orders"],
        backend["historical"],
        executor=backend["executor"],
    )

    trading_active = True
    trading_thread = threading.Thread(
        target=trading_loop,
        args=(backend["signals"], backend["executor"], backend["portfolio"]),
        daemon=True,
        name="TradingLoopThread",
    )
    trading_thread.start()
    logger.info("✅ Trading loop thread started (waiting for bot activation)")

    # Hibernate + WS watchdogs — must start after all backend objects are ready
    try:
        from backend.utils.keep_alive import start_keep_alive
        start_keep_alive(
            backend["realtime"],
            auth_manager=backend["auth"],
            portfolio_tracker=backend["portfolio"],
        )
        logger.info("✅ Hibernate + WebSocket watchdogs started")
    except Exception as _ka_err:
        logger.warning(f"keep_alive start failed (non-fatal): {_ka_err}")

    print_startup_summary(backend)

    # Browser is opened by algo.bat (Ulaa→5001, Chrome→5001) — no auto-open here

    try:
        run_dashboard(host=HOST, port=PORT, debug=False)
    except KeyboardInterrupt:
        print("\n\n" + "╔" + "═" * 68 + "╗")
        print("║" + "  🛑  SHUTTING DOWN GRACEFULLY".center(68) + "║")
        print("╚" + "═" * 68 + "╝")

        trading_active = False
        if trading_thread and trading_thread.is_alive():
            logger.info("Stopping trading thread...")
            trading_thread.join(timeout=5)

        if backend["realtime"]:
            backend["realtime"].stop()

        logger.info("Dashboard stopped by user")
    except Exception as e:
        logger.error(f"Dashboard error: {e}", exc_info=True)
        trading_active = False
        if backend and backend.get("realtime"):
            backend["realtime"].stop()


if __name__ == "__main__":
    main()