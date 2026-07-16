"""
System Watchdog — Post-Hibernate & WebSocket Recovery
======================================================

TWO complementary watchdogs run as daemon threads:

1. HIBERNATE WATCHDOG  (every 15 s, always active)
   Detects that the PC was woken from sleep/hibernate by checking
   monotonic-clock drift — `time.monotonic()` pauses during suspend
   while `time.time()` (wall clock) keeps advancing.  When a gap of
   >60 s is detected between the two clocks the watchdog runs a full
   recovery sequence:

       a) Wait for network (up to 90 s, pinging 8.8.8.8:53)
       b) Re-verify Zerodha session; re-authenticate via saved enctoken
          if needed (saves the fresh token back to disk)
       c) Re-sync portfolio (holdings / positions)
       d) Hard-reset the WebSocket:
            - close existing KWS object
            - call realtime.initialize()  (re-fetches instrument tokens)
            - call realtime.start()       (new WS thread + subscription)
       e) Reset watchdog heartbeat so WS-watchdog doesn't immediately
          fire again

2. WS WATCHDOG  (every 30 s, market hours only  08:00-16:00 IST)
   Same stale-tick logic as before.  Fires if no tick arrives in 90 s
   during market hours.  After a hibernate recovery the heartbeat is
   reset so this watchdog won't double-trigger.

Usage (unchanged - called once from dashboard.py):

    from backend.utils.keep_alive import start_keep_alive
    start_keep_alive(realtime_manager, auth_manager, portfolio_tracker)

`auth_manager` and `portfolio_tracker` are optional for backward
compatibility; hibernate recovery is skipped when they are absent.
"""

import socket
import time
import threading
from datetime import datetime, time as dtime
from typing import Optional

import pytz

from backend.utils.logger import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Market-hours window
_WATCH_START = dtime(8, 0)
_WATCH_END   = dtime(16, 0)

# Hibernate watchdog tuning
_HIBER_CHECK_EVERY  = 15   # s  how often hibernate watchdog polls
_HIBER_GAP_TRIGGER  = 60   # s  wall-clock vs monotonic drift that signals a wake
_NETWORK_WAIT_MAX   = 90   # s  max time to wait for network after resume
_NETWORK_CHECK_HOST = "8.8.8.8"
_NETWORK_CHECK_PORT = 53   # DNS port - always open when internet is up

# WS watchdog tuning
_WS_CHECK_EVERY = 30   # s  how often WS watchdog polls (market hours only)
_STALE_AFTER    = 90   # s  no-tick threshold before forced WS reconnect

# Shared heartbeat state
_last_tick_ts   = {"t": time.monotonic()}   # updated by _on_ticks
_last_monotonic = {"t": time.monotonic()}   # updated by hibernate loop
_last_wall      = {"t": time.time()}


# ── Public helpers ─────────────────────────────────────────────────────────────

def record_tick() -> None:
    """Call this from _on_ticks to update the WS-watchdog heartbeat."""
    _last_tick_ts["t"] = time.monotonic()


def _in_market_window() -> bool:
    now = datetime.now(IST).time()
    return _WATCH_START <= now <= _WATCH_END


# ── Network helper ─────────────────────────────────────────────────────────────

def _wait_for_network(timeout: int = _NETWORK_WAIT_MAX) -> bool:
    """
    Block until TCP connection to 8.8.8.8:53 succeeds.
    Returns True when network is up, False on timeout.
    """
    deadline = time.monotonic() + timeout
    attempt  = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            with socket.create_connection(
                (_NETWORK_CHECK_HOST, _NETWORK_CHECK_PORT), timeout=3
            ):
                logger.info(f"Network reachable after {attempt} attempt(s)")
                return True
        except OSError:
            remaining = int(deadline - time.monotonic())
            logger.debug(
                f"Network not yet reachable (attempt {attempt}, {remaining}s left)..."
            )
            time.sleep(3)
    logger.error("Network did not come up within %ds after hibernate resume", timeout)
    return False


# ── Heartbeat injection ────────────────────────────────────────────────────────

def _inject_heartbeat(realtime_manager) -> None:
    """
    Wrap _on_ticks so every incoming tick also calls record_tick().
    Idempotent - uses a flag to avoid double-wrapping.
    """
    if getattr(realtime_manager._on_ticks, "_heartbeat_injected", False):
        return  # already wrapped

    original = realtime_manager._on_ticks

    def _patched_on_ticks(ws, ticks):
        record_tick()
        original(ws, ticks)

    _patched_on_ticks._heartbeat_injected = True
    realtime_manager._on_ticks = _patched_on_ticks

    # Propagate to already-live KWS object if it exists
    if realtime_manager.kws:
        realtime_manager.kws.on_ticks = _patched_on_ticks


# ── Full post-hibernate recovery ───────────────────────────────────────────────

def _full_recovery(realtime_manager, auth_manager=None, portfolio_tracker=None) -> None:
    """
    Run a complete reconnect sequence after the PC resumes from sleep/hibernate.

    Steps:
      1. Wait for network
      2. Re-verify / re-authenticate Zerodha session
      3. Re-sync portfolio
      4. Hard-reset WebSocket (close -> initialize -> start)
      5. Reset heartbeat timestamps
    """
    logger.warning("Hibernate/sleep resume detected - starting full recovery...")

    # Step 1: Network
    if not _wait_for_network():
        logger.error("Recovery aborted: no network after resume")
        return

    # Step 2: Re-authenticate if session is stale
    # IMPORTANT: skip re-auth when bot is paused — the user paused specifically
    # to log into Kite without interference. Calling authenticate() here while
    # paused could trigger a credentials login that logs the browser out.
    if auth_manager is not None:
        paused = getattr(auth_manager, '_bot_paused', False)
        if paused:
            logger.info("Hibernate recovery: bot is PAUSED — skipping re-auth to protect browser session")
        else:
            try:
                if not auth_manager._verify_session():
                    logger.warning("Session expired after resume - re-authenticating...")
                    ok = auth_manager.authenticate()
                    if ok:
                        logger.info("Re-authenticated with saved enctoken after resume")
                        realtime_manager.auth = auth_manager
                    else:
                        logger.error("Re-authentication failed - live data may be stale")
                else:
                    logger.info("Zerodha session still valid after resume")
            except Exception as exc:
                logger.error(f"Auth re-check error after resume: {exc}")

    # Step 3: Portfolio resync
    if portfolio_tracker is not None:
        try:
            portfolio_tracker.sync()
            logger.info("Portfolio re-synced after resume")
        except Exception as exc:
            logger.error(f"Portfolio resync error after resume: {exc}")

    # Step 4: Hard-reset WebSocket
    try:
        if realtime_manager.kws:
            try:
                realtime_manager.kws.close()
            except Exception:
                pass
        realtime_manager.is_connected       = False
        realtime_manager._reconnect_attempts = 0

        if realtime_manager.initialize():
            _inject_heartbeat(realtime_manager)
            t = threading.Thread(
                target=realtime_manager.start,
                daemon=True,
                name="HibernateResumeWS",
            )
            t.start()
            time.sleep(3)
            if realtime_manager.is_connected:
                logger.info("WebSocket reconnected after hibernate resume")
            else:
                logger.warning(
                    "WebSocket not yet confirmed - "
                    "WS watchdog will retry if ticks stay stale"
                )
        else:
            logger.error("realtime.initialize() failed after hibernate resume")

    except Exception as exc:
        logger.error(f"WebSocket reset error after resume: {exc}")

    # Step 5: Reset heartbeats so WS-watchdog doesn't immediately fire
    now_mono = time.monotonic()
    _last_tick_ts["t"]   = now_mono
    _last_monotonic["t"] = now_mono
    _last_wall["t"]      = time.time()

    logger.info("Post-hibernate recovery complete")


# ── Hibernate watchdog loop ────────────────────────────────────────────────────

def _hibernate_watchdog_loop(
    realtime_manager,
    auth_manager=None,
    portfolio_tracker=None,
) -> None:
    """
    Detect PC resume from sleep by comparing wall-clock vs monotonic advancement.

    During normal operation both clocks advance at the same rate.
    After suspend/hibernate, time.time() jumps forward while
    time.monotonic() does not — the gap reveals the sleep duration.
    """
    logger.info("Hibernate watchdog started")
    _last_monotonic["t"] = time.monotonic()
    _last_wall["t"]      = time.time()

    while True:
        time.sleep(_HIBER_CHECK_EVERY)

        now_mono = time.monotonic()
        now_wall = time.time()

        mono_delta = now_mono - _last_monotonic["t"]
        wall_delta = now_wall - _last_wall["t"]
        gap = wall_delta - mono_delta

        if gap > _HIBER_GAP_TRIGGER:
            logger.warning(
                f"Suspend detected: wall advanced {wall_delta:.0f}s "
                f"but monotonic only {mono_delta:.0f}s (gap={gap:.0f}s)"
            )
            _full_recovery(realtime_manager, auth_manager, portfolio_tracker)
        
        # Always update reference points after each check
        _last_monotonic["t"] = now_mono
        _last_wall["t"]      = now_wall


# ── WS watchdog loop ───────────────────────────────────────────────────────────

def _ws_watchdog_loop(realtime_manager) -> None:
    """
    Force-reconnect KiteTicker if ticks go stale during market hours.
    This handles ordinary WS drops that are not caused by hibernate.
    """
    logger.info(f"WebSocket watchdog started (stale threshold: {_STALE_AFTER}s)")

    while True:
        time.sleep(_WS_CHECK_EVERY)

        if not _in_market_window():
            continue

        stale_for = time.monotonic() - _last_tick_ts["t"]

        if stale_for > _STALE_AFTER:
            # realtime.py's own _on_close -> _schedule_reconnect -> _reconnect_loop
            # is likely already handling this exact disconnect with its own
            # growing backoff (capped at 60s). Forcing ANOTHER reconnect on top
            # of that — full initialize() + a brand new start() thread — races
            # it: two attempts to connect() the same session in overlapping
            # windows is a plausible cause of repeated 1006 "peer dropped the
            # connection" closes, which then never lets either mechanism
            # actually stabilize (this is exactly what produced an all-day
            # outage on 2026-07-16 instead of a brief blip).
            #
            # So: only step in here if that reconnect thread isn't actually
            # alive/working right now. If it is, this cycle just logs and
            # waits for the next check instead of piling on.
            reconnect_thread = getattr(realtime_manager, '_reconnect_thread', None)
            if reconnect_thread is not None and reconnect_thread.is_alive():
                logger.info(
                    f"No ticks for {stale_for:.0f}s, but realtime's own reconnect "
                    f"thread is already active — not forcing a competing one"
                )
                continue

            logger.warning(f"No ticks for {stale_for:.0f}s - forcing WebSocket reconnect")
            try:
                if realtime_manager.kws:
                    try:
                        realtime_manager.kws.close()
                    except Exception:
                        pass
                realtime_manager.is_connected       = False
                realtime_manager._reconnect_attempts = 0

                if realtime_manager.initialize():
                    _inject_heartbeat(realtime_manager)
                    t = threading.Thread(
                        target=realtime_manager.start,
                        daemon=True,
                        name="WSWatchdog-Reconnect",
                    )
                    t.start()
                    _last_tick_ts["t"] = time.monotonic()
                    logger.info("WS watchdog triggered reconnect")
                else:
                    logger.error("WS watchdog: initialize() failed")
            except Exception as exc:
                logger.error(f"WS watchdog reconnect error: {exc}")


# ── Public entry point ─────────────────────────────────────────────────────────

def start_keep_alive(
    realtime_manager,
    auth_manager=None,
    portfolio_tracker=None,
) -> None:
    """
    Start both watchdog threads. Safe to call multiple times.
    Threads are daemonised so they die with the main process.

    Args:
        realtime_manager:  RealtimeDataManager instance (required)
        auth_manager:      AuthManager - enables session re-auth after hibernate
        portfolio_tracker: PortfolioTracker - enables portfolio resync after hibernate
    """
    _inject_heartbeat(realtime_manager)

    # Hibernate watchdog - always running, detects wake from sleep
    threading.Thread(
        target=_hibernate_watchdog_loop,
        args=(realtime_manager, auth_manager, portfolio_tracker),
        daemon=True,
        name="HibernateWatchdog",
    ).start()

    # WS watchdog - market hours only, catches ordinary WS drops
    threading.Thread(
        target=_ws_watchdog_loop,
        args=(realtime_manager,),
        daemon=True,
        name="WSWatchdog",
    ).start()

    logger.info("Hibernate watchdog + WS watchdog threads started")
