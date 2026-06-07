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
INTERVAL_SEC  = 120   # write every 2 minutes (was 300 — caused >10 min lag)


def _load_settings() -> dict:
    settings_path = Path(__file__).parent.parent.parent / "config" / "settings.json"
    if settings_path.exists():
        try:
            with open(settings_path) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def write_snapshot(dashboard_state: dict):
    """Build and write the full status snapshot. Never raises."""
    try:
        from backend.core.constants import LIQUIDCASE_SYMBOL
        from backend.indicators.calculator import calculate_daily_williams_r

        portfolio    = dashboard_state.get("portfolio_tracker")
        realtime     = dashboard_state.get("realtime_manager")
        historical   = dashboard_state.get("historical_manager")
        signal_gen   = dashboard_state.get("signal_generator")

        settings    = _load_settings()
        active_etfs = settings.get("active_etfs", [
            "MON100", "GOLDBEES", "SILVERBEES", "JUNIORBEES",
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
                print(f"[snapshot] {sym}: ltp={ltp} prev_close={prev_close}({prev_close_src}) qty={qty}", flush=True, file=_sys.stderr)
                today_move = round((ltp - prev_close) * qty, 2) if prev_close and prev_close > 0 else 0.0
                today_move_pct = round((ltp - prev_close) / prev_close * 100, 2) if prev_close and prev_close > 0 else 0.0

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
                    "strategy":   "bnh" if sym in bnh_symbols else "active",
                    "buys_today": signal_gen._get_buys_today(sym) if signal_gen and sym not in bnh_symbols else None,
                    "max_slots":  slots_count,
                })

            # LIQUIDCASE
            liq_qty   = getattr(portfolio, "liquidcase_quantity", 0)
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
                    timeout=10,
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
                            mresp = _snap_session.get(margins_url, timeout=15)
                            break
                        except _req.exceptions.Timeout:
                            if _attempt == 0:
                                import time as _time; _time.sleep(2)
                            else:
                                available_margin_note = "timeout after retry"
                        except Exception as _e:
                            available_margin_note = f"request error: {_e}"
                            break
                    if mresp is not None:
                        if mresp.status_code == 200:
                            mdata      = mresp.json()
                            equity     = mdata.get("data", {}).get("equity", {})
                            avail      = equity.get("available", {})
                            utilised   = equity.get("utilised",  {})
                            net_bal    = float(avail.get("cash", 0)             or 0)
                            open_bal   = float(avail.get("opening_balance", 0) or 0)
                            collateral = float(avail.get("collateral", 0)       or 0)
                            debits     = float(utilised.get("debits", 0)        or 0)
                            available_cash   = round(open_bal if open_bal > 0 else net_bal, 2)
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
            if portfolio and hasattr(portfolio, "positions"):
                for pos in (portfolio.positions.get("net", []) or []):
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

        # ── Market indices (NIFTY 50, INDIA VIX) ─────────────────────────────
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
        write_snapshot(dashboard_state)
        while True:
            time.sleep(INTERVAL_SEC)
            write_snapshot(dashboard_state)

    t = threading.Thread(target=_loop, daemon=True, name="SnapshotWriter-PS5673")
    t.start()
    print(f"  → Snapshot writer started (every {INTERVAL_SEC//60} min → {SNAPSHOT_PATH}) ✅")
