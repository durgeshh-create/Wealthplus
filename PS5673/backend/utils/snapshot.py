"""
snapshot.py — PS5673
=====================
Writes a JSON status file to /tmp/status_ps5673.json every 2 minutes.
GitHub Actions pushes this file to the gh-pages branch so the static
GitHub Pages dashboard can read it without any server or tunnel.

Started from cloud_launcher.py:
    from backend.utils.snapshot import start_snapshot_thread
    start_snapshot_thread(dashboard_state)
"""

import json
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST           = timezone(timedelta(hours=5, minutes=30))
SNAPSHOT_PATH = Path("/tmp/status_ps5673.json")
ACCOUNT       = "PS5673"
INTERVAL_SEC  = 60    # write every 60 s — pairs with Contents API pusher every 60 s


def _load_settings() -> dict:
    settings_path = Path(__file__).parent.parent.parent / "config" / "settings.json"
    if settings_path.exists():
        # ✅ FIX: a single failed read here used to fall straight back to {}
        # (and downstream to hardcoded default symbol lists), which could
        # make a just-moved symbol (e.g. MINDSPACE-RR moved from Active to
        # Dip Accumulator) appear to silently revert on the dashboard if this
        # read happened to land mid-write of settings.json by routes.py.
        # routes.py writes are now atomic (temp file + os.replace), which
        # should eliminate that race entirely — but a brief retry here is a
        # cheap second layer of defense against any other future writer that
        # doesn't go through the atomic helper, and the explicit log line
        # below means a fallback is never silent again.
        for _attempt in range(2):
            try:
                with open(settings_path) as f:
                    return json.load(f)
            except Exception as _e:
                if _attempt == 0:
                    import time as _time
                    _time.sleep(0.05)
                    continue
                import sys as _sys
                print(f"[snapshot] _load_settings FAILED after retry — "
                      f"falling back to hardcoded defaults: {_e}", file=_sys.stderr)
    return {}



def _safe_position_slots_used(signal_gen, sym, bnh_symbols):
    """Safely read per-symbol slot usage for the CURRENT open position —
    never raises, returns None on any error.

    This intentionally reads the persistent PositionSlotTracker (via
    get_position_slots_used), NOT SignalGenerator._get_buys_today(). The
    latter is a daily trading-permission counter that resets at midnight —
    correct for gating new buys, but wrong for the dashboard, where a
    position built up over multiple days should keep showing its true slot
    usage instead of dropping back to 0 every night.
    """
    if signal_gen is None or sym in bnh_symbols:
        return None
    try:
        signal_gen.ensure_position_slots_seeded(sym)
        return signal_gen.get_position_slots_used(sym)
    except Exception:
        return None

def write_snapshot(dashboard_state: dict):
    """Build and write the full status snapshot. Never raises."""
    try:
        # Guard: don't overwrite good snapshot with empty data.
        # portfolio.holdings is initialised to [] (not None), so we must check
        # for BOTH None AND empty-list to detect "not yet populated".
        import json as _jg
        _port = dashboard_state.get("portfolio_tracker")
        _holdings_attr = getattr(_port, "holdings", None) if _port else None
        _holdings_empty = _holdings_attr is None or (isinstance(_holdings_attr, list) and len(_holdings_attr) == 0 and _port is not None)
        if _holdings_empty:
            try:
                if SNAPSHOT_PATH.exists():
                    _prev = _jg.loads(SNAPSHOT_PATH.read_text())
                    if _prev.get("total_value"):
                        import sys as _sg; print("[snapshot] Portfolio empty — keeping last good snapshot", file=_sg.stderr)
                        return
            except Exception:
                pass
        from backend.core.constants import LIQUIDCASE_SYMBOL
        from backend.indicators.calculator import calculate_daily_williams_r

        portfolio    = dashboard_state.get("portfolio_tracker")
        realtime     = dashboard_state.get("realtime_manager")
        historical   = dashboard_state.get("historical_manager")
        signal_gen   = dashboard_state.get("signal_generator")

        settings    = _load_settings()
        active_etfs = settings.get("active_etfs", [
            "GOLDBEES", "SILVERBEES", "JUNIORBEES",
            "MINDSPACE-RR", "EMBASSY-RR", "BANKBEES",
        ])
        bnh_symbols    = settings.get("bnh_symbols", ["MID150BEES"])
        profit_target  = float(settings.get("profit_target_pct", 3))
        wr_threshold   = float(settings.get("williams_r_threshold", -75))
        slots_count    = int(settings.get("slots_count", 5))

        # ── Holdings ──────────────────────────────────────────────────────────
        holdings   = []
        total_value = 0.0
        today_pnl  = 0.0
        held_set   = set()

        if portfolio:
            for h in (portfolio.holdings or []):
                sym = h.get("tradingsymbol", "")
                qty = int(h.get("quantity", 0)) + int(h.get("t1_quantity", 0))
                if qty <= 0 or sym == LIQUIDCASE_SYMBOL:
                    continue
                if sym not in active_etfs and sym not in bnh_symbols:
                    continue
                avg = float(h.get("average_price", 0))
                ltp = (realtime.get_ltp(sym) if realtime else None) or float(h.get("last_price", avg))
                val = qty * ltp

                # today_move = (ltp - prev_close) * qty  — today's P&L only
                prev_close = None
                prev_close_src = "none"
                if realtime:
                    ohlc = realtime.get_ohlc(sym)
                    if ohlc and ohlc.get("close") and float(ohlc["close"]) > 0:
                        prev_close = float(ohlc["close"])
                        prev_close_src = "ohlc"
                if prev_close is None:
                    # Use get_latest_close — always returns yesterday's close from cached CSV
                    if historical:
                        try:
                            pc = historical.get_latest_close(sym)
                            if pc and pc > 0:
                                prev_close = pc
                                prev_close_src = "historical"
                        except Exception:
                            pass
                import sys as _sys
                today_move = round((ltp - prev_close) * qty, 2) if prev_close and prev_close > 0 else 0.0
                today_move_pct = round((ltp - prev_close) / prev_close * 100, 2) if prev_close and prev_close > 0 else 0.0

                # Unrealised P&L — use Zerodha's pnl field directly (matches Kite)
                _z_pnl = h.get("pnl")
                if _z_pnl is not None:
                    unrealised_pnl     = round(float(_z_pnl), 2)
                    cost_basis         = avg * qty
                    unrealised_pnl_pct = round(unrealised_pnl / cost_basis * 100, 2) if cost_basis > 0 else 0.0
                else:
                    unrealised_pnl     = round((ltp - avg) * qty, 2) if avg > 0 else 0.0
                    unrealised_pnl_pct = round((ltp - avg) / avg * 100, 2) if avg > 0 else 0.0

                total_value += val
                held_set.add(sym)
                holdings.append({
                    "symbol":   sym,
                    "quantity": qty,
                    "avg":      round(avg, 2),
                    "ltp":      round(ltp, 2),
                    "value":    round(val, 2),
                    "pnl":      today_move,
                    "pnl_pct":  today_move_pct,
                    "unrealised_pnl":     unrealised_pnl,
                    "unrealised_pnl_pct": unrealised_pnl_pct,
                    "strategy":   "bnh" if sym in bnh_symbols else "active",
                    "buys_today": _safe_position_slots_used(signal_gen, sym, bnh_symbols) or 0,
                    "max_slots":  int(settings.get("slots_count", slots_count)),
                })

            # LIQUIDCASE
            liq_qty   = getattr(portfolio, "liquidcase_quantity", 0)
            liq_free  = getattr(portfolio, "liquidcase_free_quantity", liq_qty)
            liq_price = (realtime.get_ltp(LIQUIDCASE_SYMBOL) if realtime else None) or 0
            liq_val   = liq_qty * liq_price
            total_value += liq_val

            # Today's P&L — sum of per-holding today_move already computed above
            today_pnl = sum(h["pnl"] for h in holdings)

        else:
            liq_qty = liq_price = liq_val = 0

        # ── Williams %R + market data ─────────────────────────────────────────
        # FIX: include both active_etfs AND bnh_symbols in WR loop
        wr_data = []
        signals = []

        all_tracked = list(active_etfs) + [s for s in bnh_symbols if s not in active_etfs]

        for sym in all_tracked:
            is_bnh = sym in bnh_symbols
            ltp = (realtime.get_ltp(sym) if realtime else None)
            ohlc = (realtime.get_ohlc(sym) if realtime else None) or {}
            prev_close = ohlc.get("close", 0)
            chg_pct = ((ltp - prev_close) / prev_close * 100) if ltp and prev_close else None

            wr = None
            try:
                hist = historical.get_daily_data(sym) if historical else None
                if hist is not None and len(hist) > 0:
                    wr = calculate_daily_williams_r(
                        hist,
                        live_price=ltp,
                        live_high=ohlc.get("high") if ohlc else None,
                        live_low=ohlc.get("low") if ohlc else None,
                    )
            except Exception:
                pass

            is_held = sym in held_set
            avg_price = None
            if is_held and portfolio:
                avg_price = portfolio.get_average_price(sym)

            wr_data.append({
                "symbol":     sym,
                "ltp":        round(ltp, 2) if ltp else None,
                "change_pct": round(chg_pct, 2) if chg_pct is not None else None,
                "williams_r": round(wr, 2) if wr is not None else None,
                "is_held":    is_held,
                "avg_price":  round(avg_price, 2) if avg_price else None,
                "strategy":   "bnh" if is_bnh else "active",
            })

            # Generate signals for active ETFs only (BNH is long-term hold)
            if not is_bnh:
                if is_held and avg_price and ltp:
                    profit_pct = (ltp - avg_price) / avg_price * 100
                    if profit_pct >= profit_target:
                        signals.append({
                            "type":    "SELL",
                            "symbol":  sym,
                            "reason":  f"Profit target {profit_pct:.1f}% ≥ {profit_target}%",
                            "ltp":     round(ltp, 2),
                            "avg":     round(avg_price, 2),
                            "profit_pct": round(profit_pct, 2),
                        })
                elif not is_held and wr is not None and wr <= wr_threshold:
                    signals.append({
                        "type":      "BUY",
                        "symbol":    sym,
                        "reason":    f"W%R {wr:.1f} ≤ {wr_threshold}",
                        "ltp":       round(ltp, 2) if ltp else None,
                        "williams_r": round(wr, 2),
                    })

        # ── Slot summary ──────────────────────────────────────────────────────
        # FIX: count slots used across active ETFs only (BNH is separate strategy)
        slots_used = len([s for s in active_etfs if s in held_set])
        bnh_held   = len([s for s in bnh_symbols if s in held_set])

        # ── Today's orders ────────────────────────────────────────────────────
        # FIX: "kite" key is never set in dashboard_state — use order_manager's
        # auth session to hit /oms/orders directly (same endpoint get_order_status uses)
        orders = []
        try:
            order_mgr = dashboard_state.get("order_manager")
            if order_mgr and hasattr(order_mgr, "auth") and order_mgr.auth:
                from backend.core.config import Config
                resp = order_mgr.auth.session.get(
                    f"{Config.ZERODHA_API_BASE}/oms/orders",
                    timeout=(4, 10),
                )
                if resp.status_code == 200:
                    raw_orders = resp.json().get("data", []) or []
                    today_str = datetime.now(IST).strftime("%Y-%m-%d")
                    for o in raw_orders:
                        # ✅ FIX: prefer exchange_timestamp (actual fill date) over
                        # order_timestamp (placement time — may be yesterday for AMO
                        # orders placed the prior evening).  Accept the order only if
                        # at least one of the two timestamps matches today's IST date.
                        # This prevents yesterday's AMO / evening orders from bleeding
                        # into "Today's Orders" before the market opens next morning.
                        exch_ts  = str(o.get("exchange_timestamp") or "")
                        place_ts = str(o.get("order_timestamp")    or "")
                        # Use exchange_timestamp when available (non-empty & non-"None")
                        primary_ts = exch_ts if exch_ts and exch_ts.lower() not in ("", "none", "null") else place_ts
                        ts_str = primary_ts
                        # Only include if today's date appears in the chosen timestamp
                        if today_str not in ts_str:
                            continue
                        sym = o.get("tradingsymbol", "")
                        if sym not in active_etfs and sym not in bnh_symbols and sym != LIQUIDCASE_SYMBOL:
                            continue
                        orders.append({
                            "order_id":         o.get("order_id"),
                            "tradingsymbol":     sym,
                            "transaction_type":  o.get("transaction_type"),
                            "quantity":          o.get("quantity"),
                            "filled_quantity":   o.get("filled_quantity"),
                            "average_price":     o.get("average_price"),
                            "price":             o.get("price"),
                            "status":            o.get("status"),
                            "order_timestamp":   ts_str,
                        })
        except Exception:
            pass

        # ── Available Cash + Margin ───────────────────────────────────────────
        # ✅ FIX: three bugs fixed here:
        # 1. Used order_mgr.auth.session (shared with trading loop) — not thread-safe.
        #    Fix: fresh one-shot requests.Session per snapshot call.
        # 2. timeout=10 too tight on GH Actions runners under load.
        #    Fix: 15 s with one automatic retry.
        # 3. bare except: pass — silently swallowed all errors.
        #    Fix: log to stderr + store reason in snapshot for dashboard display.
        available_cash        = None
        available_margin      = None
        available_margin_note = None
        try:
            import requests as _req
            order_mgr = dashboard_state.get("order_manager")
            if order_mgr and hasattr(order_mgr, "auth") and order_mgr.auth:
                auth_mgr = order_mgr.auth
                enctoken = getattr(auth_mgr, "enctoken", None)
                if not enctoken:
                    available_margin_note = "enctoken not available"
                else:
                    from backend.core.config import Config
                    _snap_session = _req.Session()
                    _snap_session.headers.update({
                        "Authorization": f"enctoken {enctoken}",
                        "X-Kite-Version": "3",
                    })
                    margins_url = f"{Config.ZERODHA_API_BASE}/oms/user/margins"
                    mresp = None
                    for _attempt in range(2):
                        try:
                            # timeout=(connect, read) — single-int only covers read;
                            # a silent TCP drop on GH Actions would hang forever otherwise.
                            mresp = _snap_session.get(margins_url, timeout=(4, 15))
                            break
                        except _req.exceptions.Timeout:
                            if _attempt == 0:
                                import time as _time; _time.sleep(2)
                            else:
                                available_margin_note = "timeout after retry"
                        except Exception as _e:
                            available_margin_note = f"request error: {_e}"
                            break
                    # ✅ FIX: on 403 TokenException, refresh token.
                    # Try CDP first (local), fall back to fresh TOTP login (cloud/GitHub Actions).
                    # PAUSE GUARD: never run credentials login while the dashboard Pause
                    # button is active — that would immediately log the user out of Kite.
                    if mresp is not None and mresp.status_code == 403:
                        try:
                            import sys as _sys
                            refreshed = False

                            # ── Pause guard ───────────────────────────────────────────────
                            # Check both the in-process flag (set by cloud_launcher) and the
                            # gh-pages pause_flag.json (set by the dashboard Pause button).
                            # ON GH ACTIONS: skip the guard entirely — there is no browser
                            # session to protect, and a stale pause_flag blocks recovery.
                            import os as _osga
                            _on_gha = _osga.environ.get("GITHUB_ACTIONS", "").lower() == "true"
                            _is_paused = False if _on_gha else getattr(auth_mgr, "_bot_paused", False)
                            if not _is_paused and not _on_gha:
                                try:
                                    import os as _os, json as _js, urllib.request as _ur, base64 as _b64
                                    _owner = _os.environ.get("GITHUB_REPOSITORY", "/").split("/")[0]
                                    _repo  = _os.environ.get("GITHUB_REPOSITORY", "/").split("/")[-1]
                                    _tok   = _os.environ.get("GITHUB_TOKEN", "")
                                    if _owner and _repo and _tok:
                                        _preq = _ur.Request(
                                            f"https://api.github.com/repos/{_owner}/{_repo}/contents/pause_flag.json?ref=gh-pages",
                                            headers={"Authorization": f"token {_tok}", "Accept": "application/vnd.github.v3+json"},
                                        )
                                        import socket as _sock2
                                        _old2 = _sock2.getdefaulttimeout()
                                        _sock2.setdefaulttimeout(5)
                                        try:
                                            with _ur.urlopen(_preq, timeout=5) as _pr:
                                                _pdata = _js.loads(_pr.read())
                                                _pflag = _js.loads(_b64.b64decode(_pdata["content"]))
                                                _is_paused = bool(_pflag.get("paused", False))
                                        finally:
                                            _sock2.setdefaulttimeout(_old2)
                                except Exception:
                                    pass  # network error → assume not paused, safe to proceed

                            if _is_paused:
                                print("[snapshot] 403 received but bot is PAUSED — "
                                      "credentials login suppressed to protect browser session.", file=_sys.stderr)
                            else:
                                # Try CDP first (works if browser is open locally)
                                try:
                                    refreshed = auth_mgr.handle_session_expiry()
                                except Exception:
                                    pass
                                # If CDP didn't work, do a fresh TOTP re-login
                                if not refreshed or getattr(auth_mgr, "enctoken", None) == enctoken:
                                    if hasattr(auth_mgr, "_login_with_credentials"):
                                        print("[snapshot] CDP failed — attempting fresh TOTP re-login...", file=_sys.stderr)
                                        refreshed = auth_mgr._login_with_credentials()
                            if refreshed:
                                new_enc = getattr(auth_mgr, "enctoken", None)
                                if new_enc:
                                    _snap_session.headers.update({"Authorization": f"enctoken {new_enc}"})
                                    mresp = _snap_session.get(margins_url, timeout=(4, 15))
                                    print("[snapshot] margins token refreshed — retrying ✅", file=_sys.stderr)
                        except Exception as _re:
                            import sys as _sys
                            print(f"[snapshot] margins token refresh failed: {_re}", file=_sys.stderr)

                    if mresp is not None:
                        if mresp.status_code == 200:
                            mdata      = mresp.json()
                            equity     = mdata.get("data", {}).get("equity", {})
                            avail      = equity.get("available", {})
                            utilised   = equity.get("utilised",  {})
                            net_bal      = float(avail.get("cash", 0)             or 0)
                            open_bal     = float(avail.get("opening_balance", 0) or 0)
                            live_bal     = float(avail.get("live_balance", 0)    or 0)
                            collateral   = float(avail.get("collateral", 0)       or 0)
                            debits       = float(utilised.get("debits", 0)        or 0)
                            # live_balance matches what Kite displays as available cash
                            available_cash   = round(live_bal if live_bal > 0 else (net_bal if net_bal > 0 else open_bal), 2)
                            available_margin = round(collateral - debits + available_cash, 2)
                        else:
                            available_margin_note = f"HTTP {mresp.status_code}"
                            import sys as _sys
                            print(f"[snapshot] margins API error: HTTP {mresp.status_code} — {mresp.text[:200]}", file=_sys.stderr)
        except Exception as _me:
            available_margin_note = str(_me)[:80]
            import sys as _sys
            print(f"[snapshot] margins fetch exception: {_me}", file=_sys.stderr)

        # ── Today's Net Positions ─────────────────────────────────────────────
        positions_data = []
        try:
            if portfolio and hasattr(portfolio, "_fetch_positions"):
                # Always fetch fresh positions so closed trades are immediately
                # reflected (portfolio.positions is only updated on portfolio.sync()
                # which runs on a 60s cycle and may lag behind an actual close).
                _fresh_pos = portfolio._fetch_positions()
                _net_list  = (_fresh_pos or {}).get("net", []) if _fresh_pos else None
                # Fall back to cached if the live fetch fails
                if _net_list is None and hasattr(portfolio, "positions"):
                    _net_list = portfolio.positions.get("net", []) or []
            elif portfolio and hasattr(portfolio, "positions"):
                _net_list = portfolio.positions.get("net", []) or []
            else:
                _net_list = []

            for pos in (_net_list or []):
                sym = pos.get("tradingsymbol", "")
                qty = int(pos.get("quantity", 0) or 0)
                if qty == 0:
                    continue
                positions_data.append({
                    "symbol":       sym,
                    "quantity":     qty,
                    "avg":          round(float(pos.get("average_price", 0) or 0), 2),
                    "ltp":          round(float(pos.get("last_price", 0) or 0), 2),
                    "pnl":          round(float(pos.get("pnl", 0) or 0), 2),
                    "unrealised":   round(float(pos.get("unrealised", 0) or 0), 2),
                    "realised":     round(float(pos.get("realised", 0) or 0), 2),
                    "buy_qty":      int(pos.get("buy_quantity", 0) or 0),
                    "sell_qty":     int(pos.get("sell_quantity", 0) or 0),
                    "product":      pos.get("product", ""),
                })
        except Exception:
            pass

        # ── Market indices (NIFTY 50, INDIA VIX) ────────────────────────────
        indices = {}

        try:
            for idx_name in ("NIFTY 50", "INDIA VIX"):
                ltp_val = realtime.get_ltp(idx_name) if realtime else None
                ohlc_i  = (realtime.get_ohlc(idx_name) if realtime else None) or {}
                prev_c  = ohlc_i.get("close")
                chg     = None
                chg_pct = None
                if ltp_val and prev_c and float(prev_c) > 0:
                    chg     = round(ltp_val - float(prev_c), 2)
                    chg_pct = round(chg / float(prev_c) * 100, 2)
                indices[idx_name] = {
                    "ltp":        round(ltp_val, 2) if ltp_val else None,
                    "prev_close": round(float(prev_c), 2) if prev_c else None,
                    "change":     chg,
                    "change_pct": chg_pct,
                    "open":       round(float(ohlc_i["open"]), 2) if ohlc_i.get("open") else None,
                    "high":       round(float(ohlc_i["high"]), 2) if ohlc_i.get("high") else None,
                    "low":        round(float(ohlc_i["low"]),  2) if ohlc_i.get("low")  else None,
                }
        except Exception as _ie:
            import sys as _sys
            print(f"[snapshot] indices fetch error: {_ie}", file=_sys.stderr)

        # ── Build snapshot ────────────────────────────────────────────────────

        # ── Mutual Fund Holdings ──────────────────────────────────────────────
        # ATTEMPTED coin.zerodha.com/api/mf/holdings (2026-06-30) to fix a
        # long-pending defect: OMS's last_price lags the real NAV by ~0.25%
        # (multiple days of growth) and never populates last_price_date.
        # A live DevTools capture confirmed Coin's values exactly match
        # Kite's own Holdings CSV export — but a SECOND capture of the
        # actual cookies that request sends revealed Coin does NOT use the
        # enctoken/Authorization scheme at all. It authenticates via its own
        # `coin_session` cookie, and sits behind Cloudflare bot-protection
        # (`cf_clearance`, `__cf_bm`, `_cfuvid` — only issued after passing
        # a real browser JS challenge). Neither of those can be obtained or
        # refreshed by this bot's CDP/TOTP auth chain, which only knows how
        # to mint/refresh the kite.zerodha.com enctoken. Result: every call
        # got "Invalid session" (HTTP 403) regardless of headers sent.
        # REVERTED to OMS-only. The OMS-shape branch below (pnl/current not
        # populated → computed from qty*ltp) is the only path now; it's
        # correct, just stale by Zerodha's own design on this endpoint.
        # If a future browser-automation step is added that can capture
        # coin_session (the same way enctoken.json is captured today), the
        # Coin response shape is already documented and field-mapped in git
        # history — search for "Coin shape:" in a prior version of this file.
        mf_holdings = []
        mf_summary  = {}
        try:
            order_mgr = dashboard_state.get("order_manager")
            if order_mgr and hasattr(order_mgr, "auth") and order_mgr.auth:
                auth_mgr = order_mgr.auth
                enctoken = getattr(auth_mgr, "enctoken", None)
                if enctoken:
                    import requests as _req2
                    from backend.core.config import Config
                    _mf_sess = _req2.Session()
                    _mf_sess.headers.update({
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
                        'Referer':    'https://kite.zerodha.com/',
                        'Origin':     'https://kite.zerodha.com',
                        'Accept':     'application/json, text/plain, */*',
                        "Authorization": f"enctoken {enctoken}",
                        # Do NOT add X-Kite-Version: 3 — breaks enctoken-based
                        # web sessions on OMS (see AuthManager.__init__ comment).
                    })
                    mf_resp = _mf_sess.get(
                        f"{Config.ZERODHA_API_BASE}/oms/mf/holdings",
                        timeout=(4, 15),
                        allow_redirects=True,
                    )
                    if mf_resp.status_code != 200:
                        import sys as _mfsys2
                        print(f"[snapshot] MF holdings HTTP {mf_resp.status_code}: {mf_resp.text[:200]}", file=_mfsys2.stderr)
                    if mf_resp.status_code == 200 and mf_resp.text.strip().startswith("{"):
                        mf_data  = mf_resp.json()
                        raw_list = mf_data.get("data", []) or []
                        for h in raw_list:
                            # quantity field from Zerodha OMS = TOTAL units (free + pledged).
                            # pledged_quantity is a SUBSET — do NOT add it to quantity.
                            qty         = float(h.get("quantity", 0) or 0)
                            pledged_qty = float(h.get("pledged_quantity", 0) or 0)
                            free_qty    = max(0.0, qty - pledged_qty)
                            avg_nav  = float(h.get("average_price", 0) or 0)
                            # last_price = most recent NAV Zerodha has published
                            # on OMS specifically (can lag by several days —
                            # confirmed against Kite's own Holdings CSV export;
                            # Coin has the fresher value but isn't reachable from
                            # an unattended bot, see note above). last_price_date
                            # is always empty on this endpoint.
                            ltp      = float(h.get("last_price", 0) or 0)
                            nav_date = h.get("last_price_date", "") or ""
                            invested = round(qty * avg_nav, 2)
                            cur_val  = round(qty * ltp, 2)
                            # Zerodha OMS returns pnl=0.0 — not populated. Compute manually.
                            pnl      = round(cur_val - invested, 2)
                            pnl_pct  = round(pnl / invested * 100, 2) if invested else 0
                            # day_change = per-unit NAV Δ from previous close × total qty.
                            day_chg_abs = float(h.get("day_change", 0) or 0)
                            day_chg_pct = float(h.get("day_change_percentage", 0) or 0)
                            day_pnl_h   = round(day_chg_abs * qty, 2)
                            mf_holdings.append({
                                "name":          h.get("fund", h.get("tradingsymbol", "")),
                                "folio":         h.get("folio", ""),
                                "units":         round(qty, 3),
                                "free_units":    round(free_qty, 3),
                                "pledged_units": round(pledged_qty, 3),
                                "avg_nav":       round(avg_nav, 3),
                                "ltp":           round(ltp, 3),
                                "nav_date":      nav_date,
                                "invested":      invested,
                                "cur_val":       cur_val,
                                "pnl":           pnl,
                                "pnl_pct":       pnl_pct,
                                "day_chg_abs":   day_chg_abs,
                                "day_chg_pct":   round(day_chg_pct, 2),
                                "day_pnl":       day_pnl_h,
                            })
                        total_invested = sum(h["invested"] for h in mf_holdings)
                        total_cur_val  = sum(h["cur_val"]  for h in mf_holdings)
                        total_pnl      = round(total_cur_val - total_invested, 2)
                        total_pnl_pct  = round(total_pnl / total_invested * 100, 2) if total_invested else 0
                        total_day_pnl     = sum(h["day_pnl"] for h in mf_holdings)
                        total_day_pnl_pct = round(total_day_pnl / total_invested * 100, 2) if total_invested else 0
                        mf_summary = {
                            "total_invested":   round(total_invested, 2),
                            "total_cur_val":    round(total_cur_val, 2),
                            "total_pnl":        round(total_pnl, 2),
                            "total_pnl_pct":    round(total_pnl_pct, 2),
                            "day_pnl":          round(total_day_pnl, 2),
                            "day_pnl_pct":      round(total_day_pnl_pct, 2),
                            # Temporary: expose raw API fields so field names can be verified
                            "debug_raw_first":  {k: v for k, v in (raw_list[0].items() if raw_list else {}.items())},
                        }
        except Exception as _mfe:
            import sys as _mfsys
            print(f"[snapshot] MF holdings fetch failed: {_mfe}", file=_mfsys.stderr)
        snapshot = {
            "account":     ACCOUNT,
            "timestamp":   datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
            "bot_running": True,
            "total_value": round(total_value, 2),
            "today_pnl":   round(today_pnl, 2),
            "today_pnl_pct": round(today_pnl / (total_value - today_pnl) * 100, 2)
                              if total_value > today_pnl and total_value > 0 else 0,
            "liquidcase": {
                "quantity": liq_qty,
                "free_quantity": liq_free,
                "price":    round(liq_price, 2),
                "value":    round(liq_val, 2),
                "pct":      round(liq_val / total_value * 100, 2) if total_value else 0,
            },
            "slots": {
                "total":       slots_count,
                "used":        slots_used,
                "available":   max(0, slots_count - slots_used),
                "active_etfs": active_etfs,
                "bnh_symbols": bnh_symbols,
                "bnh_held":    bnh_held,
            },
            "holdings":         holdings,
            "positions":        positions_data,
            "williams_r":       wr_data,
            "signals":          signals,
            "orders":           orders,
            "indices":          indices,
            "mf_holdings":      mf_holdings,
            "mf_summary":       mf_summary,
            "available_cash":   available_cash,
            "available_margin": available_margin,
            "available_margin_note": available_margin_note,
        }

        SNAPSHOT_PATH.write_text(json.dumps(snapshot, indent=2))

    except Exception as e:
        # Write a minimal error snapshot so the dashboard shows something
        try:
            SNAPSHOT_PATH.write_text(json.dumps({
                "account":     ACCOUNT,
                "timestamp":   datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                "bot_running": True,
                "error":       str(e),
            }, indent=2))
        except Exception:
            pass


def start_snapshot_thread(dashboard_state: dict):
    """
    Start background thread that writes /tmp/status_ps5673.json every 2 minutes.
    Returns immediately. Thread is daemon so it dies with the process.
    """
    def _loop():
        # Write immediately on start so dashboard has data right away
        while True:
            try:
                write_snapshot(dashboard_state)
            except Exception as _loop_err:
                import sys as _sys
                print(f"[snapshot] write_snapshot crashed — will retry next cycle: {_loop_err}", file=_sys.stderr)
            time.sleep(INTERVAL_SEC)

    t = threading.Thread(target=_loop, daemon=True, name="SnapshotWriter-PS5673")
    t.start()
    print(f"  → Snapshot writer started (every {INTERVAL_SEC}s → {SNAPSHOT_PATH}) ✅")
