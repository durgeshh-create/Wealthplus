"""
ETF Trading Dashboard Launcher — RD1858 (port 5000)
Integrates backend trading system with web dashboard.

FEATURES:
- Web-based visualization of portfolio, market data, and signals
- Automated trading loop that monitors signals and executes trades
- Start/Stop bot control via dashboard interface
- Real-time updates every 1 second
- Signal checking every 2 seconds when bot is active
- Portfolio sync every 60 seconds
- Telegram notifications for trades, start, stop, errors

ARCHITECTURE:
- Main thread: Runs Flask web server
- Background thread: Trading loop (monitors signals when bot_running=True)
- WebSocket thread: Real-time market data updates
- Watchdog thread: Auto-shutdown at 3:32 PM IST (cloud only)

The trading loop is ALWAYS running in the background, but only executes
signals when you click "Start Bot" in the dashboard.
"""

import sys
import os
import time
import signal
import threading
import socket
import subprocess
import platform
import webbrowser
import json
import urllib.request
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

# FIX: Forces engine to cleanly listen strictly on default trading port 5000
PORT = int(os.environ.get("PORT", 5000))
HOST = "0.0.0.0"
BASE_URL = f"http://localhost:{PORT}"

# ── Cloud auto-shutdown ───────────────────────────────────────────────────────
# Active ONLY on GitHub Actions (GITHUB_ACTIONS=true is set automatically).
# On your local PC this is always False — Ctrl+C works as usual.
IS_CLOUD = os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

# Shutdown at 3:32 PM IST — 2 min after market close.
# Override via env var: SHUTDOWN_TIME_IST=15:35
_shutdown_env = os.environ.get("SHUTDOWN_TIME_IST", "15:30").split(":")
SHUTDOWN_HOUR_IST   = int(_shutdown_env[0])
SHUTDOWN_MINUTE_IST = int(_shutdown_env[1])
# ─────────────────────────────────────────────────────────────────────────────

# Global trading thread control
trading_thread = None
trading_active = False


# ── Telegram notifications ────────────────────────────────────────────────────

def telegram_notify(message: str):
    """
    Send a Telegram message. Silently skips if credentials are not set.
    Never raises — notifications must never crash the trading bot.
    Uses stdlib urllib only — no extra dependency.
    """
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return
    try:
        payload = json.dumps({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Never crash the bot over a notification failure


def _ist_now() -> str:
    """Return current IST time as a readable string."""
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(IST).strftime("%d %b %Y %H:%M IST")

# ─────────────────────────────────────────────────────────────────────────────


def send_daily_summary(backend: dict):
    """
    Build and send a Telegram end-of-day P&L summary.
    Called at 3:28 PM IST — 2 minutes before shutdown.
    Only reports symbols managed by this bot (active_etfs + bnh_symbols).
    Never raises — a failed summary must not affect shutdown.
    """
    try:
        lines = [
            f"📊 <b>WealthAlgo RD1858 — Today's P&L Summary</b>",
            f"📅 {_ist_now()}",
            f"{'─' * 30}",
        ]

        try:
            portfolio = backend.get("portfolio")
            if portfolio:
                positions = portfolio.get_positions() if hasattr(portfolio, "get_positions") else []
                holdings  = portfolio.get_holdings()  if hasattr(portfolio, "get_holdings")  else []

                # Only report symbols defined in settings — skip unmanaged demat holdings (e.g. NIFTYBEES)
                system_symbols = set(Config.get_active_etfs()) | set(Config.get_bnh_symbols())

                total_pnl = 0.0
                trade_lines = []
                seen_symbols: set = set()   # guard against double-counting across positions + holdings

                realtime   = backend.get("realtime")
                historical = backend.get("historical")

                def _prev_close(sym):
                    """
                    Get previous session close for a symbol.
                    Priority:
                      1. WebSocket OHLC tick (ohlc.close = prev session close, only in MODE_FULL)
                      2. Historical daily data last candle close (always available, already cached)
                    """
                    # Try WebSocket ohlc.close first
                    if realtime:
                        ohlc = realtime.get_ohlc(sym)
                        if ohlc and ohlc.get("close") and float(ohlc["close"]) > 0:
                            return float(ohlc["close"])
                    # Fall back to last row of historical daily data (prev trading day close)
                    if historical:
                        try:
                            df = historical.get_daily_data(sym)
                            if df is not None and len(df) > 0:
                                return float(df.iloc[-1]["close"])
                        except Exception:
                            pass
                    return None

                for pos in (positions or []):
                    sym = pos.get("tradingsymbol", pos.get("symbol", "?"))
                    if sym not in system_symbols:
                        continue
                    qty = int(pos.get("quantity", pos.get("net_quantity", 0)))
                    ltp        = float(realtime.get_ltp(sym)) if realtime else None
                    prev_close = _prev_close(sym)
                    if ltp and prev_close and prev_close > 0:
                        pnl = round((ltp - prev_close) * qty, 2)
                    else:
                        pnl = float(pos.get("pnl", pos.get("unrealised", 0)))
                    total_pnl += pnl
                    if qty != 0:
                        icon = "🟢" if pnl >= 0 else "🔴"
                        trade_lines.append(f"  {icon} {sym}: qty={qty}, Today=₹{pnl:+.2f}")
                    seen_symbols.add(sym)

                for h in (holdings or []):
                    sym = h.get("tradingsymbol", h.get("symbol", "?"))
                    if sym not in system_symbols or sym in seen_symbols:
                        continue
                    qty = int(h.get("quantity", 0))
                    ltp        = float(realtime.get_ltp(sym)) if realtime else float(h.get("last_price", 0) or 0)
                    prev_close = _prev_close(sym)
                    if ltp and prev_close and prev_close > 0:
                        pnl = round((ltp - prev_close) * qty, 2)
                    else:
                        pnl = 0.0
                    total_pnl += pnl
                    if qty != 0:
                        icon = "🟢" if pnl >= 0 else "🔴"
                        trade_lines.append(f"  {icon} {sym}: qty={qty}, Today=₹{pnl:+.2f}")

                if trade_lines:
                    lines.append("💼 <b>Positions today:</b>")
                    lines.extend(trade_lines)
                    lines.append(f"{'─' * 30}")
                else:
                    lines.append("💼 No open positions today")

                pnl_icon = "🟢" if total_pnl >= 0 else "🔴"
                lines.append(f"{pnl_icon} <b>Total P&amp;L: ₹{total_pnl:+.2f}</b>")
        except Exception as _e:
            lines.append(f"⚠️ Portfolio data unavailable: {_e}")

        lines.append(f"{'─' * 30}")
        lines.append(f"⏹️ Bot shutting down at 3:30 PM IST")
        lines.append(f"🔁 Restarts automatically tomorrow at 9:00 AM IST")

        telegram_notify("\n".join(lines))
        logger.info("📊 Daily P&L summary sent to Telegram")

    except Exception as e:
        logger.warning(f"Daily summary failed (non-fatal): {e}")


def market_close_watchdog(backend: dict):
    """
    Cloud-only background watchdog — shuts the process down cleanly at
    SHUTDOWN_HOUR_IST:SHUTDOWN_MINUTE_IST IST every day.

    Steps:
      1. Send Telegram daily P&L summary at 3:28 PM.
      2. Send Telegram shutdown notification.
      3. Stop the trading loop.
      4. Stop the realtime data feed.
      5. Send SIGINT to main thread (same as Ctrl+C → clean Flask exit).
      6. Hard-exit fallback after 60s if Flask doesn't respond.

    Only starts when IS_CLOUD is True. Never fires on local PC.
    """
    from datetime import datetime, timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))

    logger.info(
        f"⏰ Market-close watchdog started — "
        f"auto-shutdown at {SHUTDOWN_HOUR_IST:02d}:{SHUTDOWN_MINUTE_IST:02d} IST"
    )

    summary_sent = False   # send only once per day

    while True:
        now_ist = datetime.now(IST)

        # ── P&L summary 2 min before shutdown (works for both sessions) ───────
        # Fires when within 2 minutes of SHUTDOWN_HOUR_IST:SHUTDOWN_MINUTE_IST
        summary_min = SHUTDOWN_MINUTE_IST - 2
        summary_hour = SHUTDOWN_HOUR_IST
        if summary_min < 0:
            summary_min += 60
            summary_hour -= 1
        if (not summary_sent and
                now_ist.hour == summary_hour and now_ist.minute >= summary_min):
            logger.info("📊 Sending P&L summary to Telegram...")
            send_daily_summary(backend)
            summary_sent = True

        if (now_ist.hour > SHUTDOWN_HOUR_IST or
                (now_ist.hour == SHUTDOWN_HOUR_IST and
                 now_ist.minute >= SHUTDOWN_MINUTE_IST)):

            shutdown_time = now_ist.strftime("%H:%M IST")
            logger.info(
                f"⏰ Market closed — {shutdown_time} — initiating shutdown..."
            )
            print(
                f"\n{'=' * 60}\n"
                f"  ⏰  MARKET CLOSE AUTO-SHUTDOWN ({shutdown_time})\n"
                f"{'=' * 60}\n"
            )

            # Step 1: Telegram notification
            telegram_notify(
                f"⏹️ <b>WealthAlgo RD1858 — MARKET CLOSE</b>\n"
                f"📅 {shutdown_time}\n"
                f"✅ Bot shutting down cleanly after market hours.\n"
                f"🔁 Will restart automatically next trading day at 9:10 AM IST."
            )

            # Step 2: Stop trading loop
            global trading_active
            trading_active = False
            logger.info("🛑 Trading loop stopped")

            # Step 3: Stop realtime data feed
            try:
                if backend and backend.get("realtime"):
                    backend["realtime"].stop()
                    logger.info("🛑 Realtime data feed stopped")
            except Exception as _e:
                logger.warning(f"Error stopping realtime feed: {_e}")

            # Step 4: Graceful Flask shutdown (same as Ctrl+C)
            logger.info("🛑 Sending shutdown signal to Flask server...")
            try:
                os.kill(os.getpid(), signal.SIGINT)
            except Exception as _e:
                logger.warning(f"SIGINT failed: {_e}")

            # Step 5: Hard-exit fallback if Flask doesn't exit in 60s
            time.sleep(60)
            logger.info("🛑 Hard-exit fallback triggered")
            os._exit(0)

        time.sleep(30)  # Check every 30 seconds


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
        from datetime import datetime as _dt, time as _dtime

        # ── Startup grace period (60s) ────────────────────────────────────────
        if _loop_started_at > 0 and (time.time() - _loop_started_at) < 60:
            logger.debug("⏳ Startup grace period — monitoring signals, no execution yet")
            return

        settings_path = Path(__file__).parent / "config" / "settings.json"
        profit_target = Config.PROFIT_TARGET_PCT
        max_qty = 0
        sell_exec_time = _dtime(15, 15)
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

        buy_signals  = signals.get("buy",  [])
        sell_signals = signals.get("sell", [])

        # ── Sell time gate ────────────────────────────────────────────────────
        if not anytime_mode:
            from datetime import timezone as _tz_d, timedelta as _td_d
            _IST_d = _tz_d(_td_d(hours=5, minutes=30))
            now_t = _dt.now(_IST_d).time()
            # Widened from 1 minute to 10 — same reasoning as get_due_buys()'s
            # window in signal_generator.py: a transient data outage spanning
            # the original single minute used to silently push a sell to
            # "never happens today" with no way to recover once the window
            # closed, even though nothing about the sell itself was wrong.
            sell_window_end = _dtime(sell_exec_time.hour,
                                     min(sell_exec_time.minute + 10, 59))
            in_sell_window  = sell_exec_time <= now_t <= sell_window_end

            if sell_signals and not in_sell_window:
                for sig in sell_signals:
                    logger.info(
                        f"  🔴 SELL PENDING (executes at {sell_exec_time.strftime('%H:%M')}): "
                        f"{sig['symbol']} | profit target met @ ₹{sig['price']:.2f}"
                    )
                sell_signals = []

        if buy_signals or sell_signals:
            logger.info("=" * 60)
            logger.info(f"📊 Executing signals — Buy: {len(buy_signals)}, Sell: {len(sell_signals)}")
            logger.info(
                f"⚙️  Profit Target={profit_target}%, "
                f"Max Qty={max_qty if max_qty > 0 else 'Unlimited'}"
            )

            for sig in buy_signals:
                logger.info(
                    f"  🟢 BUY: {sig['symbol']} | "
                    f"W%R={sig['williams_r']:.2f} | ₹{sig['price']:.2f}"
                )
            for sig in sell_signals:
                logger.info(f"  🔴 SELL: {sig['symbol']} | ₹{sig['price']:.2f}")

            logger.info("=" * 60)

            if Config.is_dry_run():
                logger.warning("⚠️  DRY RUN MODE: Signals detected but not executing")
                # Notify on dry run — but only ONCE per symbol until it clears
                # (signals repeat every 2s while oversold; without this guard
                #  a single oversold symbol generates hundreds of messages)
                if buy_signals or sell_signals:
                    new_syms = set(s['symbol'] for s in buy_signals + sell_signals)
                    already  = getattr(check_and_execute_signals, '_dry_notified', set())
                    to_notify = [s for s in buy_signals + sell_signals
                                 if s['symbol'] not in already
                                 and s['symbol'] != 'LIQUIDCASE']
                    if to_notify:
                        acct = os.environ.get("KITE_USER_ID", bot)
                        lines = [f"🧪 <b>WealthAlgo {acct} — DRY RUN SIGNALS</b>",
                                 f"📅 {_ist_now()}"]
                        for sig in [s for s in buy_signals if s['symbol'] not in already]:
                            lines.append(f"🟢 BUY (dry): {sig['symbol']} @ ₹{sig['price']:.2f} W%%R={sig.get('williams_r',0):.1f}")
                        for sig in [s for s in sell_signals if s['symbol'] not in already]:
                            lines.append(f"🔴 SELL (dry): {sig['symbol']} @ ₹{sig['price']:.2f}")
                        telegram_notify("\n".join(lines))
                    # Track notified symbols; clear ones no longer signalling
                    check_and_execute_signals._dry_notified = already | new_syms
                else:
                    # All signals cleared — reset guard so next signal fires fresh
                    check_and_execute_signals._dry_notified = set()
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
        import threading as _rt_threading
        _rt_ok = [False]
        def _do_init():
            _rt_ok[0] = realtime_manager.initialize()
        _t = _rt_threading.Thread(target=_do_init, daemon=True)
        _t.start()
        _t.join(timeout=15)

        ws_thread = threading.Thread(target=realtime_manager.start, daemon=True)
        ws_thread.start()
        time.sleep(2)
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
        # Rebuild buy counts from today's Zerodha order history on every startup
        signal_generator.rebuild_from_order_history()
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


def get_pid_using_port(port=5000):
    """Get PID of process using the specified port."""
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, check=True
            )
            for line in result.stdout.split("\n"):
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        return int(parts[-1])
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"], capture_output=True, text=True, check=True
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
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, check=True)
        else:
            subprocess.run(["kill", "-9", str(pid)], capture_output=True, check=True)
        return True
    except Exception as e:
        logger.error(f"Error killing process {pid}: {e}")
        return False


def check_and_clear_port(port=5000):
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
    print("🚀  ETF TRADING SYSTEM — RD1858  🚀".center(70))
    print("=" * 70)
    mode = "🟡 DRY RUN MODE" if Config.is_dry_run() else "🔴 LIVE TRADING MODE"
    print(mode.center(70))
    if IS_CLOUD:
        print(f"☁️  CLOUD MODE — auto-shutdown at {SHUTDOWN_HOUR_IST:02d}:{SHUTDOWN_MINUTE_IST:02d} IST".center(70))
    print("=" * 70 + "\n")


def print_startup_summary(backend):
    """Print clean startup summary."""
    print("\n" + "╔" + "═" * 68 + "╗")
    print("║" + "  ✅  SYSTEM READY — RD1858".center(68) + "║")
    print("╠" + "═" * 68 + "╣")
    print("║" + f"  🌐  Dashboard: {BASE_URL}".ljust(68) + "║")
    print("║" + f"  📊  Real-time: {'🟢 Connected' if backend['realtime'].is_connected else '🔴 Disconnected'}".ljust(68) + "║")
    print("║" + f"  👤  User: {backend['auth'].user_id}".ljust(68) + "║")
    print("║" + "  💼  Portfolio: Synced & Tracking".ljust(68) + "║")
    print("║" + "  🤖  Auto-Trading: Ready (Start from dashboard)".ljust(68) + "║")
    if IS_CLOUD:
        print("╠" + "═" * 68 + "╣")
        print("║" + f"  ⏰  Auto-shutdown: {SHUTDOWN_HOUR_IST:02d}:{SHUTDOWN_MINUTE_IST:02d} IST (market close)".ljust(68) + "║")
        tg = "✅ Active" if os.environ.get("TELEGRAM_BOT_TOKEN") else "⚠️  Not configured"
        print("║" + f"  📱  Telegram alerts: {tg}".ljust(68) + "║")
    print("╠" + "═" * 68 + "╣")
    print("║" + "  ⚠️   Press Ctrl+C to stop the server".ljust(68) + "║")
    print("╚" + "═" * 68 + "╝")
    print()


def _open_browser_when_ready(port: int = PORT, timeout: int = 30):
    """Wait until Flask is accepting connections, then open browser. Local only."""
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

    if not has_valid_auth:
        print("\n ⚠️ No saved session — attempting CDP auto-login from Kite browser tab...")
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

        if IS_CLOUD:
            telegram_notify(
                f"❌ <b>WealthAlgo RD1858 — AUTH FAILED</b>\n"
                f"📅 {_ist_now()}\n"
                f"⚠️ Bot could not authenticate. Check GitHub secrets."
            )

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
        if IS_CLOUD:
            telegram_notify(
                f"❌ <b>WealthAlgo RD1858 — INIT FAILED</b>\n"
                f"📅 {_ist_now()}\n"
                f"⚠️ Backend failed to initialize. Bot not trading."
            )
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

    # ── Snapshot writer — writes /tmp/status_rd1858.json every 5 min ─────────
    # GitHub Actions pushes this to gh-pages for the static dashboard.
    try:
        from backend.utils.snapshot import start_snapshot_thread, SNAPSHOT_PATH
        _snap_state = {
            "portfolio_tracker":  backend["portfolio"],
            "realtime_manager":   backend["realtime"],
            "historical_manager": backend["historical"],
            "order_manager":      backend["orders"],
            "signal_generator":   backend["signals"],  # ✅ needed for per-symbol buys_today
        }
        start_snapshot_thread(_snap_state)

        # ── Snapshot startup watchdog ────────────────────────────────────────
        # ✅ FIX: previously a silent failure anywhere between "Snapshot writer
        # started" being printed and its first successful write (e.g. a hang
        # or unhandled exception in a code path not wrapped by write_snapshot's
        # own try/except) could leave /tmp/status_rd1858.json never created.
        # The GitHub Actions pusher loop (`until [ -f status_rd1858.json ]`)
        # then waits forever with zero log output, and gh-pages keeps serving
        # whatever snapshot was last pushed — potentially from the prior day —
        # with no visible error anywhere (especially if Telegram is disabled).
        # This watchdog gives the snapshot up to 3 minutes to appear; if it
        # doesn't, it force-exits with a distinct code so cloud_launcher.py's
        # restart loop detects the crash and tries again automatically,
        # instead of requiring a manual workflow restart.
        def _snapshot_startup_watchdog():
            for _ in range(36):  # 36 × 5s = 180s
                time.sleep(5)
                if SNAPSHOT_PATH.exists():
                    return  # snapshot appeared — all good, watchdog exits quietly
            logger.error(
                "Snapshot file never appeared within 180s of startup — "
                "forcing exit so cloud_launcher.py restarts the bot."
            )
            try:
                telegram_notify(
                    f"⚠️ <b>WealthAlgo {os.environ.get('KITE_USER_ID', '')} — SNAPSHOT STARTUP FAILED</b>\n"
                    f"No snapshot file appeared within 180s of startup.\n"
                    f"Forcing restart — dashboard may have been showing stale data."
                )
            except Exception:
                pass
            os._exit(2)  # os._exit, not sys.exit — bypasses normal cleanup,
                         # guaranteed to actually terminate the process even
                         # if other threads are blocked

        threading.Thread(
            target=_snapshot_startup_watchdog,
            daemon=True,
            name="SnapshotStartupWatchdog",
        ).start()

    except Exception as _snap_err:
        logger.warning(f"Snapshot writer not started: {_snap_err}")

    trading_active = True
    trading_thread = threading.Thread(
        target=trading_loop,
        args=(backend["signals"], backend["executor"], backend["portfolio"]),
        daemon=True,
        name="TradingLoopThread",
    )
    trading_thread.start()
    logger.info("✅ Trading loop thread started (waiting for bot activation)")

    # ── Cloud: auto-start trading immediately (no manual dashboard button needed) ──
    if IS_CLOUD:
        dashboard_state["bot_running"] = True
        logger.info("☁️  Cloud mode — bot_running auto-set to True (no dashboard button needed)")

        # ✅ FIX: the dip-accumulator / weekday-systematic-buy engine
        # (IntradayEngine) was only ever started via the dashboard's
        # "▶ START BOT" button hitting POST /api/intraday/start — a route
        # that requires a human to click it. In cloud/GitHub-Actions mode
        # there is no one present to click anything, so the engine's
        # background loop (which contains the entire 15:15 IST buy-window
        # logic, including the Monday weekday-systematic buy) never ran at
        # all, every single cloud run, regardless of settings or W%R.
        # Mirror exactly what that button does so cloud runs get the same
        # behavior as a manually-started local session.
        engine = dashboard_state.get('intraday_engine')
        if engine:
            started = engine.start()
            logger.info(
                f"☁️  Cloud mode — intraday/dip-accumulator engine auto-start "
                f"{'succeeded' if started else 'skipped (already running)'}"
            )
        else:
            logger.warning(
                "☁️  Cloud mode — intraday_engine not found in dashboard_state; "
                "dip-accumulator buys (including the Monday weekday tranche) will NOT run this session."
            )

    # ── Cloud auto-shutdown watchdog ──────────────────────────────────────────
    if IS_CLOUD:
        watchdog_thread = threading.Thread(
            target=market_close_watchdog,
            args=(backend,),
            daemon=True,
            name="MarketCloseWatchdog",
        )
        watchdog_thread.start()
        logger.info(
            f"✅ Market-close watchdog started — "
            f"auto-shutdown at {SHUTDOWN_HOUR_IST:02d}:{SHUTDOWN_MINUTE_IST:02d} IST"
        )
    # ─────────────────────────────────────────────────────────────────────────

    # Hibernate + WS watchdogs
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

    # Browser is opened by algo.bat (Ulaa→5000) — no auto-open here

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

        logger.info("Dashboard stopped")
    except Exception as e:
        logger.error(f"Dashboard error: {e}", exc_info=True)
        if IS_CLOUD:
            telegram_notify(
                f"💥 <b>WealthAlgo RD1858 — CRASH</b>\n"
                f"📅 {_ist_now()}\n"
                f"❌ {str(e)[:200]}"
            )
        trading_active = False
        if backend and backend.get("realtime"):
            backend["realtime"].stop()


if __name__ == "__main__":
    main()
