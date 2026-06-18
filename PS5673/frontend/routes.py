"""
API routes for the dashboard.
Handles data requests from frontend.
"""

from flask import jsonify, request
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.config import Config
from backend.core.constants import LIQUIDCASE_SYMBOL, MARKET_INDICES
from backend.utils.logger import get_logger
from backend.indicators.calculator import calculate_daily_williams_r

logger = get_logger(__name__)


def _atomic_write_json(path, data):
    """
    Write JSON to `path` atomically: write to a temp file in the same
    directory, then os.replace() it over the target.
    ✅ FIX: plain `open(path, 'w')` truncates the file immediately, leaving a
    window (a few ms, but non-zero) where the file is empty or contains
    partial JSON. If snapshot.py's background thread reads settings.json
    during exactly that window, json.load() raises, _load_settings() swallows
    the error and returns {}, and the snapshot silently falls back to old
    hardcoded default symbol lists — which can make a just-moved symbol
    (e.g. MINDSPACE-RR moved from Active to Dip Accumulator) appear to revert
    on the dashboard until the next successful write overwrites it.
    os.replace() is atomic on POSIX and Windows — readers either see the
    fully-old file or the fully-new file, never a partial one.
    """
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def register_routes(app, dashboard_state):
    """Register all API routes."""
    
    @app.route('/api/portfolio')
    def get_portfolio():
        """Get current portfolio status."""
        try:
            portfolio = dashboard_state['portfolio_tracker']
            
            if not portfolio:
                return jsonify({'error': 'Portfolio not initialized'}), 503
            
            # ── Load settings once ────────────────────────────────────────────
            import json as _json
            settings_path = Path(__file__).parent.parent / 'config' / 'settings.json'
            _settings = {}
            if settings_path.exists():
                try:
                    with open(settings_path, 'r') as f:
                        _settings = _json.load(f)
                except Exception:
                    pass

            active_etfs = _settings.get('active_etfs',
                ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES',
                 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR'])
            bnh_symbols = _settings.get('bnh_symbols', ['MID150BEES'])

            # Holdings filter = LIQUIDCASE + Active Strategy + Dip Accumulator
            PORTFOLIO_ETFS = set(active_etfs) | set(bnh_symbols) | {LIQUIDCASE_SYMBOL}
            
            # Get LIQUIDCASE info
            liquidcase_qty = portfolio.liquidcase_quantity
            liquidcase_price = dashboard_state['realtime_manager'].get_ltp(LIQUIDCASE_SYMBOL) if dashboard_state['realtime_manager'] else None
            
            if liquidcase_price is None:
                liquidcase_price = portfolio.liquidcase_value / liquidcase_qty if liquidcase_qty > 0 else 0
            
            liquidcase_value = liquidcase_qty * liquidcase_price
            
            total_value = liquidcase_value
            holdings_list = []
            today_pnl = 0.0
            
            # ── Collect held symbols filtered to PORTFOLIO_ETFS ───────────────
            held_symbols = set()
            
            for holding in portfolio.holdings:
                symbol = holding.get('tradingsymbol', '')
                if symbol == LIQUIDCASE_SYMBOL or symbol not in PORTFOLIO_ETFS:
                    continue
                free_qty    = int(holding.get('quantity', 0))
                pledged_qty = int(holding.get('collateral_quantity', 0))
                t1_qty      = int(holding.get('t1_quantity', 0))
                if free_qty + pledged_qty + t1_qty > 0:
                    held_symbols.add(symbol)
            
            for position in portfolio.positions.get('net', []):
                symbol = position.get('tradingsymbol', '')
                if symbol != LIQUIDCASE_SYMBOL and symbol in PORTFOLIO_ETFS and int(position.get('quantity', 0)) != 0:
                    held_symbols.add(symbol)
            
            # ── Build holdings list ───────────────────────────────────────────
            for symbol in held_symbols:
                qty = portfolio.get_quantity_held(symbol)
                if qty == 0:
                    continue
                
                avg_price = portfolio.get_average_price(symbol) or 0
                ltp = dashboard_state['realtime_manager'].get_ltp(symbol) if dashboard_state['realtime_manager'] else None
                
                if ltp is None:
                    for holding in portfolio.holdings:
                        if holding.get('tradingsymbol') == symbol:
                            ltp = float(holding.get('last_price', avg_price))
                            break
                    if ltp is None:
                        for position in portfolio.positions.get('net', []):
                            if position.get('tradingsymbol') == symbol:
                                ltp = float(position.get('last_price', avg_price))
                                break
                    if ltp is None:
                        ltp = avg_price
                
                value   = qty * ltp if ltp else 0
                total_value += value
                pnl     = (ltp - avg_price) * qty if ltp and avg_price else 0
                pnl_pct = ((ltp - avg_price) / avg_price * 100) if ltp and avg_price else 0

                # Tag which strategy owns this symbol
                strategy_tag = 'bnh' if symbol in set(bnh_symbols) else 'active'
                
                holdings_list.append({
                    'symbol':        symbol,
                    'quantity':      qty,
                    'average_price': round(avg_price, 2),
                    'ltp':           round(ltp, 2),
                    'value':         round(value, 2),
                    'pnl':           round(pnl, 2),
                    'pnl_pct':       round(pnl_pct, 2),
                    'strategy':      strategy_tag,
                })
            
            # ── Today's P&L — from both strategies ───────────────────────────
            all_monitored = set(active_etfs) | set(bnh_symbols)
            for position in portfolio.positions.get('day', []):
                symbol = position.get('tradingsymbol', '')
                if symbol == LIQUIDCASE_SYMBOL:
                    continue
                if symbol in all_monitored:
                    today_pnl += float(position.get('pnl', 0))
            
            # ── Slot stats ────────────────────────────────────────────────────
            monitored_count  = sum(1 for s in active_etfs if portfolio.is_symbol_held(s))
            bnh_held_count   = sum(1 for s in bnh_symbols  if portfolio.is_symbol_held(s))
            slots_total      = len(active_etfs)
            slots_available  = max(0, slots_total - monitored_count)
            
            opening_value   = total_value - today_pnl if total_value > 0 else 0
            today_pnl_pct   = (today_pnl / opening_value * 100) if opening_value > 0 else 0
            
            return jsonify({
                'total_value':   round(total_value, 2),
                'today_pnl':     round(today_pnl, 2),
                'today_pnl_pct': round(today_pnl_pct, 2),
                'liquidcase': {
                    'quantity':   liquidcase_qty,
                    'price':      round(liquidcase_price, 2),
                    'value':      round(liquidcase_value, 2),
                    'percentage': round((liquidcase_value / total_value * 100) if total_value else 0, 2)
                },
                'slots': {
                    'total':       slots_total,
                    'used':        monitored_count,
                    'available':   slots_available,
                    'active_etfs': active_etfs,
                    'bnh_symbols': bnh_symbols,
                },
                'holdings': holdings_list
            })
            
        except Exception as e:
            logger.error(f"Error getting portfolio: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500


    @app.route('/api/positions')
    def get_positions():
        """
        Return ALL current Zerodha net positions (equivalent to the Positions tab
        in Kite), enriched with live LTP where available.
        Excludes zero-quantity positions that are fully squared off.
        Re-fetches from Zerodha on every call so refresh button gets live data.
        """
        try:
            portfolio = dashboard_state['portfolio_tracker']
            realtime  = dashboard_state.get('realtime_manager')
            if not portfolio:
                return jsonify({'error': 'Portfolio not initialized'}), 503

            # Always fetch fresh positions from Zerodha so refresh button works
            fresh = portfolio._fetch_positions()
            if fresh is not None:
                net_positions = fresh.get('net', [])
                day_positions = fresh.get('day', [])
            else:
                # Fall back to cached if fetch fails
                net_positions = portfolio.positions.get('net', [])
                day_positions = portfolio.positions.get('day', [])

            # Build day P&L lookup {symbol: pnl}
            day_pnl_map = {}
            for p in day_positions:
                sym = p.get('tradingsymbol', '')
                if sym:
                    day_pnl_map[sym] = float(p.get('pnl', 0))

            rows = []
            for pos in net_positions:
                qty = int(pos.get('quantity', 0))
                if qty == 0:
                    continue  # fully squared off

                sym       = pos.get('tradingsymbol', '')
                exchange  = pos.get('exchange', 'NSE')
                product   = pos.get('product', '')
                avg_price = float(pos.get('average_price', 0) or 0)
                buy_qty   = int(pos.get('buy_quantity', 0))
                sell_qty  = int(pos.get('sell_quantity', 0))
                buy_val   = float(pos.get('buy_value', 0) or 0)
                sell_val  = float(pos.get('sell_value', 0) or 0)

                # Live LTP — try realtime feed, fall back to Zerodha's last_price
                ltp = None
                if realtime and sym:
                    try:
                        raw = realtime.get_ltp(sym)
                        if raw: ltp = float(raw)
                    except Exception:
                        pass
                if ltp is None:
                    ltp = float(pos.get('last_price', avg_price) or avg_price)

                cur_value = qty * ltp
                pnl       = (ltp - avg_price) * qty if avg_price else 0
                pnl_pct   = (pnl / (avg_price * abs(qty)) * 100) if avg_price and qty else 0
                day_pnl   = day_pnl_map.get(sym, float(pos.get('pnl', 0)))

                rows.append({
                    'symbol':        sym,
                    'exchange':      exchange,
                    'product':       product,
                    'quantity':      qty,
                    'buy_quantity':  buy_qty,
                    'sell_quantity': sell_qty,
                    'avg_price':     round(avg_price, 2),
                    'ltp':           round(ltp, 2),
                    'value':         round(cur_value, 2),
                    'pnl':           round(pnl, 2),
                    'pnl_pct':       round(pnl_pct, 2),
                    'day_pnl':       round(day_pnl, 2),
                })

            # Sort: open longs first by value descending, then shorts
            rows.sort(key=lambda r: (-abs(r['value']), r['symbol']))

            total_pnl     = sum(r['pnl']     for r in rows)
            total_day_pnl = sum(r['day_pnl'] for r in rows)
            total_value   = sum(r['value']   for r in rows if r['quantity'] > 0)

            return jsonify({
                'positions':       rows,
                'count':           len(rows),
                'total_pnl':       round(total_pnl, 2),
                'total_day_pnl':   round(total_day_pnl, 2),
                'total_value':     round(total_value, 2),
            })

        except Exception as e:
            logger.error(f"get_positions error: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500


    @app.route('/api/market')
    def get_market_data():
        """Get live market data for all monitored ETFs."""
        try:
            settings_path = Path(__file__).parent.parent / 'config' / 'settings.json'
            
            # Read settings once — used for active_etfs, profit_target, and target_price below
            _settings = {}
            if settings_path.exists():
                with open(settings_path, 'r') as f:
                    _settings = json.load(f)
            
            active_etfs = _settings.get('active_etfs', ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR'])
            profit_target = float(_settings.get('profit_target_pct', Config.PROFIT_TARGET_PCT))
            
            realtime = dashboard_state['realtime_manager']
            portfolio = dashboard_state['portfolio_tracker']
            historical = dashboard_state['historical_manager']
            
            if not realtime:
                return jsonify({'error': 'Realtime data not available'}), 503
            
            market_data = []
            
            # Add active ETFs
            for symbol in active_etfs:
                ltp = realtime.get_ltp(symbol)
                if ltp is None:
                    ltp = 0
                    
                ohlc = realtime.get_ohlc(symbol)
                
                # Calculate change %
                prev_close = ohlc.get('close', 0) if ohlc else 0
                change_pct = ((ltp - prev_close) / prev_close * 100) if ltp and prev_close else 0
                
                # Calculate Williams %R
                williams_r = None
                try:
                    hist_data = historical.get_daily_data(symbol)
                    if hist_data is not None and len(hist_data) > 0:
                        # Get today's OHLC for proper intraday calculation
                        live_high = ohlc.get('high', ltp) if ohlc and ltp > 0 else None
                        live_low = ohlc.get('low', ltp) if ohlc and ltp > 0 else None
                        
                        # Use daily W%R calculator with live data
                        williams_r = calculate_daily_williams_r(
                            hist_data,
                            live_price=ltp if ltp > 0 else None,
                            live_high=live_high,
                            live_low=live_low
                        )
                except Exception as e:
                    logger.debug(f"Could not calculate W%R for {symbol}: {e}")
                
                # Determine status and signal
                status = 'UNKNOWN'
                signal = 'HOLD'
                
                # CRITICAL FIX: Get qty_held FIRST (directly from Zerodha API)
                # Don't rely on is_symbol_held() which uses locked_symbols cache
                qty_held = portfolio.get_quantity_held(symbol) if portfolio else 0
                
                # Determine is_held based on ACTUAL quantity (source of truth)
                if qty_held > 0:
                    is_held = True
                elif qty_held < 0:
                    # Negative quantity = oversold today, treat as not held
                    logger.warning(f"⚠️ Negative qty for {symbol}: {qty_held} (oversold today)")
                    is_held = False
                    qty_held = 0
                else:
                    # Zero quantity
                    is_held = False
                
                # Get average price (will try holdings, day positions, net positions)
                avg_price = portfolio.get_average_price(symbol) if portfolio and qty_held > 0 else None
                
                # Calculate profit % (requires both avg_price and current price)
                profit_pct = None
                if avg_price and ltp > 0:
                    profit_pct = ((ltp - avg_price) / avg_price) * 100
                
                # Determine action based on strategy
                action = 'HOLD'
                if is_held and qty_held > 0:
                    if profit_pct is not None and profit_pct >= profit_target:
                        action = 'SELL'
                    else:
                        action = 'HOLD'
                else:
                    # Check for BUY signal (W%R ≤ -80 and not held)
                    # Note: Williams R threshold is kept static at -80 (doesn't need to be dynamic)
                    if williams_r is not None and williams_r <= Config.WILLIAMS_R_THRESHOLD:
                        # Check if slot available
                        if portfolio and portfolio.available_slots > 0:
                            action = 'BUY'
                        else:
                            action = 'WAIT'  # Want to buy but no slots
                    else:
                        action = 'WATCH'  # Monitoring, no signal yet
                
                # Get day high and low from OHLC
                day_high = ohlc.get('high') if ohlc else None
                day_low = ohlc.get('low') if ohlc else None
                
                target_price = None
                profit_amount = None
                if avg_price and is_held:
                    target_price = avg_price * (1 + profit_target / 100)
                    profit_amount = (ltp - avg_price) * qty_held if ltp > 0 else 0
                
                # Market depth (top bid/ask)
                depth       = realtime.get_depth(symbol) if hasattr(realtime, 'get_depth') else {}
                top_bid     = depth.get('top_bid')  if depth else None
                top_ask     = depth.get('top_ask')  if depth else None

                market_data.append({
                    'symbol':        symbol,
                    'ltp':           round(ltp, 2) if ltp else 0,
                    'day_high':      round(day_high, 2) if day_high else None,
                    'day_low':       round(day_low, 2) if day_low else None,
                    'change_pct':    round(change_pct, 2),
                    'williams_r':    round(williams_r, 2) if williams_r is not None else None,
                    'qty_held':      qty_held,
                    'avg_price':     round(avg_price, 2) if avg_price else None,
                    'target_price':  round(target_price, 2) if target_price else None,
                    'profit_amount': round(profit_amount, 2) if profit_amount is not None else None,
                    'profit_pct':    round(profit_pct, 2) if profit_pct is not None else None,
                    'action':        action,
                    'is_held':       is_held,
                    'top_bid':       round(top_bid, 2) if top_bid else None,
                    'top_ask':       round(top_ask, 2) if top_ask else None,
                })
            
            # Add LIQUIDCASE
            liquidcase_ltp = realtime.get_ltp(LIQUIDCASE_SYMBOL)
            if liquidcase_ltp is None:
                liquidcase_ltp = 0
            
            liquidcase_ohlc = realtime.get_ohlc(LIQUIDCASE_SYMBOL)
            liquidcase_prev = liquidcase_ohlc.get('close', 0) if liquidcase_ohlc else 0
            liquidcase_change = ((liquidcase_ltp - liquidcase_prev) / liquidcase_prev * 100) if liquidcase_ltp and liquidcase_prev else 0
            
            # Don't add LIQUIDCASE to market monitor - it's shown in portfolio stats
            # LIQUIDCASE is not a trading signal, just cash parking
            
            return jsonify(market_data)
            
        except Exception as e:
            logger.error(f"Error getting market data: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500
    
    
    @app.route('/api/signals')
    def get_signals():
        """Get active buy/sell signals based on strategy."""
        try:
            import json
            from backend.indicators.calculator import calculate_daily_williams_r
            
            # Get active ETFs
            settings_path = Path(__file__).parent.parent / 'config' / 'settings.json'
            if settings_path.exists():
                with open(settings_path, 'r') as f:
                    settings = json.load(f)
                    active_etfs = settings.get('active_etfs', ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR'])
            else:
                active_etfs = ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR']
            
            portfolio = dashboard_state.get('portfolio_tracker')
            realtime = dashboard_state.get('realtime_manager')
            historical = dashboard_state.get('historical_manager')
            
            if not all([portfolio, realtime, historical]):
                return jsonify([])
            
            signals = []
            
            for symbol in active_etfs:
                # Get current price
                ltp = realtime.get_ltp(symbol)
                if not ltp or ltp <= 0:
                    continue
                
                # Check if held
                is_held = portfolio.is_symbol_held(symbol)
                
                if is_held:
                    # Check for SELL signal (using dynamic profit target)
                    avg_price = portfolio.get_average_price(symbol)
                    if avg_price:
                        profit_pct = ((ltp - avg_price) / avg_price) * 100
                        
                        # Load profit target from settings
                        profit_target = Config.PROFIT_TARGET_PCT
                        try:
                            import json
                            settings_path_temp = Path(__file__).parent.parent / 'config' / 'settings.json'
                            if settings_path_temp.exists():
                                with open(settings_path_temp, 'r') as f:
                                    settings_temp = json.load(f)
                                    profit_target = float(settings_temp.get('profit_target_pct', Config.PROFIT_TARGET_PCT))
                        except Exception as e:
                            logger.debug(f"Using default profit target: {e}")
                        
                        if profit_pct >= profit_target:
                            signals.append({
                                'symbol': symbol,
                                'type': 'SELL',
                                'reason': f'Profit target hit: {profit_pct:.2f}%',
                                'current_price': round(ltp, 2),
                                'entry_price': round(avg_price, 2),
                                'profit_pct': round(profit_pct, 2)
                            })
                else:
                    # Check for BUY signal (Williams %R ≤ -80)
                    try:
                        hist_data = historical.get_daily_data(symbol)
                        if hist_data is not None and len(hist_data) > 0:
                            ohlc = realtime.get_ohlc(symbol)
                            live_high = ohlc.get('high', ltp) if ohlc else None
                            live_low = ohlc.get('low', ltp) if ohlc else None
                            
                            williams_r = calculate_daily_williams_r(
                                hist_data,
                                live_price=ltp,
                                live_high=live_high,
                                live_low=live_low
                            )
                            
                            if williams_r is not None and williams_r <= Config.WILLIAMS_R_THRESHOLD:
                                # Check if slot available
                                if portfolio.available_slots > 0:
                                    signals.append({
                                        'symbol': symbol,
                                        'type': 'BUY',
                                        'reason': f'Oversold: W%R = {williams_r:.1f}',
                                        'current_price': round(ltp, 2),
                                        'williams_r': round(williams_r, 1)
                                    })
                    except Exception as e:
                        logger.debug(f"Could not calculate signal for {symbol}: {e}")
            
            return jsonify(signals)
            
        except Exception as e:
            logger.error(f"Error getting signals: {e}", exc_info=True)
            return jsonify([]) 
    
    
    @app.route('/api/logs')
    def get_logs():
        """Get recent log entries."""
        try:
            log_file = Path(__file__).parent.parent / 'logs' / 'trading.log'
            
            if not log_file.exists():
                return jsonify([])
            
            # Read last 50 lines
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                recent_lines = lines[-50:] if len(lines) > 50 else lines
            
            logs = []
            for line in recent_lines:
                if line.strip():
                    logs.append({'text': line.strip()})
            
            return jsonify(logs)
            
        except Exception as e:
            logger.error(f"Error getting logs: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500
    
    


    # ═══════════════════════════════════════════════════════
    # INTRADAY ENGINE ROUTES
    # ═══════════════════════════════════════════════════════

    @app.route('/api/intraday/status')
    def intraday_status():
        """Return intraday engine status and live indicators."""
        engine = dashboard_state.get('intraday_engine')
        if not engine:
            return jsonify({'running': False, 'available': False})
        status = engine.get_status()
        status['available'] = True
        return jsonify(status)

    @app.route('/api/intraday/start', methods=['POST'])
    def intraday_start():
        """Start the intraday engine."""
        engine = dashboard_state.get('intraday_engine')
        if not engine:
            return jsonify({'success': False, 'error': 'Intraday engine not initialised'}), 503
        ok = engine.start()
        return jsonify({'success': ok, 'running': engine._running})

    @app.route('/api/intraday/stop', methods=['POST'])
    def intraday_stop():
        """Stop the intraday engine."""
        engine = dashboard_state.get('intraday_engine')
        if not engine:
            return jsonify({'success': False, 'error': 'Intraday engine not initialised'}), 503
        ok = engine.stop()
        return jsonify({'success': ok, 'running': engine._running})

    @app.route('/api/intraday/close', methods=['POST'])
    def intraday_close_position():
        """Manually close the current intraday position."""
        engine = dashboard_state.get('intraday_engine')
        if not engine:
            return jsonify({'success': False, 'error': 'Not initialised'}), 503
        if not engine.position:
            return jsonify({'success': False, 'error': 'No open position'}), 400
        ok = engine.close('Manual close from dashboard')
        return jsonify({'success': bool(ok)})

    @app.route('/api/intraday/rearm', methods=['POST'])
    def intraday_rearm():
        """Cancel the per-attempt cooldown so the next qualifying tick
        can fire a DCA add immediately, without waiting for the next
        15-min candle close."""
        engine = dashboard_state.get('intraday_engine')
        if not engine:
            return jsonify({'success': False, 'error': 'Not initialised'}), 503
        result = engine.rearm()
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code


    @app.route('/api/intraday/reset_halts', methods=['POST'])
    def intraday_reset_halts():
        """
        Manually clear the LONG / SHORT / BOTH per-side halt for the rest
        of the session.  Body: {"side": "LONG" | "SHORT" | "BOTH"}.
        After this, the engine ignores the corresponding daily caps until
        the next midnight session reset.
        """
        from flask import request
        engine = dashboard_state.get('intraday_engine')
        if not engine:
            return jsonify({'success': False, 'error': 'Not initialised'}), 503
        body = request.get_json(silent=True) or {}
        side = (body.get('side') or 'BOTH').upper()
        if side not in ('LONG', 'SHORT', 'BOTH'):
            return jsonify({'success': False, 'error': f"Invalid side: {side}"}), 400
        result = engine.reset_halts(side)
        status_code = 200 if result.get('success') else 400
        return jsonify(result), status_code


    @app.route('/api/bnh/save_settings', methods=['POST'])
    def bnh_save_settings():
        """Save Dip Accumulator parameters (max_cash_per_etf, max_cash_per_transaction, partial_profit_pct)."""
        import json as _json
        from flask import request as _req
        body = _req.get_json(silent=True) or {}
        try:
            settings_path = Config.SETTINGS_FILE
            if settings_path.exists():
                with open(settings_path) as f:
                    settings = _json.load(f)
            else:
                settings = {}
            if 'bnh_max_cash_per_etf' in body:
                settings['bnh_max_cash_per_etf'] = float(body['bnh_max_cash_per_etf'])
            if 'bnh_max_cash_per_transaction' in body:
                settings['bnh_max_cash_per_transaction'] = float(body['bnh_max_cash_per_transaction'])
            if 'bnh_partial_profit_pct' in body:
                val = float(body['bnh_partial_profit_pct'])
                if val < 1.0 or val > 50.0:
                    return jsonify({'success': False, 'error': 'Harvest target must be between 1% and 50%'}), 400
                settings['bnh_partial_profit_pct'] = val
            _atomic_write_json(settings_path, settings)
            return jsonify({'success': True,
                            'bnh_max_cash_per_etf': settings.get('bnh_max_cash_per_etf'),
                            'bnh_max_cash_per_transaction': settings.get('bnh_max_cash_per_transaction'),
                            'bnh_partial_profit_pct': settings.get('bnh_partial_profit_pct')})
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    def _check_market_open():
        """
        Returns None if market appears open (prices available + Zerodha margins OK).
        Returns a human-readable error string if market is closed or Zerodha rejects.
        """
        realtime = dashboard_state.get('realtime_manager')
        auth     = dashboard_state.get('auth_manager')

        # 1. Check WebSocket prices — if all ETFs return None/0, market is closed
        if realtime:
            liq_price = realtime.get_ltp(LIQUIDCASE_SYMBOL)
            if not liq_price or liq_price <= 0:
                # Confirm via Zerodha margins API to get their actual error message
                if auth:
                    try:
                        resp = auth.session.get(
                            f"{Config.ZERODHA_API_BASE}/oms/user/margins", timeout=6
                        )
                        if resp.status_code == 403:
                            try:
                                msg = resp.json().get('message') or 'Session expired or market closed'
                            except Exception:
                                msg = 'Session expired or market closed'
                            return f"Zerodha: {msg}"
                        if resp.status_code != 200:
                            return f"Zerodha API error (HTTP {resp.status_code}) — market may be closed"
                    except Exception as e:
                        return f"Could not reach Zerodha: {e}"
                return "No live prices available — market is likely closed"
        return None  # all good

    @app.route('/api/force_buy_now', methods=['POST'])
    def force_buy_now_route():
        """Force-execute all buy signals right now, bypassing scheduled time."""
        results = {'active_strategy': [], 'bnh': None, 'errors': []}
        sig_gen  = dashboard_state.get('signal_generator')
        executor = dashboard_state.get('executor')
        engine   = dashboard_state.get('intraday_engine')
        if not sig_gen or not executor:
            return jsonify({'success': False, 'error': 'Bot not initialised'}), 503
        if Config.is_dry_run():
            return jsonify({'success': False, 'error': 'Switch to LIVE mode first'}), 400

        # ✅ FIX: detect market-closed / Zerodha error before attempting execution
        market_err = _check_market_open()
        if market_err:
            return jsonify({'success': False, 'error': market_err}), 400

        # Active Strategy — direct buy check, W%R still required but errors are surfaced
        try:
            forced = sig_gen.get_buy_signals_direct()
            if forced:
                res = executor.execute_signals({'buy': forced, 'sell': []})
                for action, ok in res.items():
                    if action.endswith('__reason'):
                        continue  # reason keys handled below
                    sym = action.replace('BUY_', '').replace('SELL_', '')
                    reason = res.get(f"{action}__reason")
                    results['active_strategy'].append({'symbol': sym, 'success': ok, 'reason': reason})
                    if not ok and reason:
                        results['errors'].append(f"{sym}: {reason}")
            logger.info(f"Force Buy Now — Active: {len(results['active_strategy'])} executed")
        except Exception as e:
            logger.error(f"Force Buy Now — Active error: {e}", exc_info=True)
            results['errors'].append(f"Active Strategy: {e}")
        # Buy & Hold
        try:
            if engine and hasattr(engine, 'force_buy_now'):
                bnh = engine.force_buy_now()
                results['bnh'] = bnh
                logger.info(f"Force Buy Now — BnH: {bnh}")
        except Exception as e:
            logger.error(f"Force Buy Now — BnH error: {e}", exc_info=True)
            results['errors'].append(f"BnH: {e}")

        any_traded = bool(
            any(s.get('success') for s in results['active_strategy'])
            or (results['bnh'] and results['bnh'].get('success'))
        )
        if not any_traded and not results['errors']:
            # Executed without error but nothing traded — tell user why
            results['errors'].append("No symbols met buy conditions right now (check W%R signals)")
        ok = any_traded
        return jsonify({'success': ok, 'results': results,
                        'error': '; '.join(results['errors']) if not ok else None})

    @app.route('/api/force_sell_now', methods=['POST'])
    def force_sell_now_route():
        """Force-execute all sell signals right now for Active Strategy."""
        results = {'active_strategy': [], 'errors': []}
        sig_gen  = dashboard_state.get('signal_generator')
        executor = dashboard_state.get('executor')
        if not sig_gen or not executor:
            return jsonify({'success': False, 'error': 'Bot not initialised'}), 503
        if Config.is_dry_run():
            return jsonify({'success': False, 'error': 'Switch to LIVE mode first'}), 400

        # ✅ FIX: detect market-closed / Zerodha error before attempting execution
        market_err = _check_market_open()
        if market_err:
            return jsonify({'success': False, 'error': market_err}), 400

        try:
            # Use direct sell check — doesn't require W%R to be available,
            # so symbols meeting profit target are never silently skipped.
            sell_signals = sig_gen.get_sell_signals_direct()
            if sell_signals:
                res = executor.execute_signals({'buy': [], 'sell': sell_signals})
                for action, ok in res.items():
                    results['active_strategy'].append({'symbol': action.replace('SELL_',''), 'success': ok})
            logger.info(f"Force Sell Now — {len(results['active_strategy'])} executed")
        except Exception as e:
            logger.error(f"Force Sell Now error: {e}", exc_info=True)
            results['errors'].append(str(e))

        ok = bool(results['active_strategy'])
        if not ok and not results['errors']:
            results['errors'].append("No symbols met sell conditions right now (check profit targets)")
        return jsonify({'success': ok, 'results': results,
                        'error': '; '.join(results['errors']) if not ok else None})

    @app.route('/api/trade_all_now', methods=['POST'])
    def trade_all_now():
        """Force-execute all trades meeting buy conditions, bypassing scheduled time."""
        results = {'active_strategy': [], 'bnh': None, 'errors': []}
        sig_gen  = dashboard_state.get('signal_generator')
        executor = dashboard_state.get('executor')
        engine   = dashboard_state.get('intraday_engine')
        if not sig_gen or not executor:
            return jsonify({'success': False, 'error': 'Bot not initialised'}), 503
        if Config.is_dry_run():
            return jsonify({'success': False, 'error': 'DRY RUN mode — switch to LIVE first'}), 400

        # ✅ FIX: detect market-closed / Zerodha error before attempting execution
        market_err = _check_market_open()
        if market_err:
            return jsonify({'success': False, 'error': market_err}), 400

        # Active Strategy — buys
        try:
            forced = sig_gen.get_buy_signals_direct()
            if forced:
                res = executor.execute_signals({'buy': forced, 'sell': []})
                for action, ok in res.items():
                    results['active_strategy'].append({'symbol': action.replace('BUY_',''), 'success': ok})
            logger.info(f"Trade All Now — Active buys: {len(results['active_strategy'])} executed")
        except Exception as e:
            logger.error(f"Trade All Now — Active buy error: {e}", exc_info=True)
            results['errors'].append(f"Active Strategy buy: {e}")
        # Active Strategy — sells
        try:
            sell_sigs = sig_gen.get_sell_signals_direct()
            if sell_sigs:
                res = executor.execute_signals({'buy': [], 'sell': sell_sigs})
                for action, ok in res.items():
                    results['active_strategy'].append({'symbol': action.replace('SELL_',''), 'success': ok})
            logger.info(f"Trade All Now — Active sells: {len(sell_sigs)} signals found")
        except Exception as e:
            logger.error(f"Trade All Now — Active sell error: {e}", exc_info=True)
            results['errors'].append(f"Active Strategy sell: {e}")
        # Buy & Hold
        try:
            if engine and hasattr(engine, 'force_buy_now'):
                bnh = engine.force_buy_now()
                results['bnh'] = bnh
                logger.info(f"Trade All Now — BnH: {bnh}")
        except Exception as e:
            logger.error(f"Trade All Now — BnH error: {e}", exc_info=True)
            results['errors'].append(f"BnH: {e}")

        any_traded = bool(results['active_strategy'] or (results['bnh'] and results['bnh'].get('success')))
        if not any_traded and not results['errors']:
            results['errors'].append("No symbols met buy conditions right now (check W%R signals)")
        ok = any_traded
        return jsonify({'success': ok, 'results': results,
                        'error': '; '.join(results['errors']) if not ok else None})

    @app.route('/api/depth/<symbol>')
    def get_market_depth(symbol):
        """
        Return top-5 bid/ask depth for a symbol.
        Primary: live WebSocket cache (MODE_FULL ticks).
        Fallback: Zerodha /oms/quote?i= (full quote with depth).
        """
        try:
            sym      = symbol.upper()
            auth     = dashboard_state.get('auth_manager')
            realtime = dashboard_state.get('realtime_manager')

            if not auth or not realtime:
                return jsonify({'error': 'Not ready'}), 503

            # ── Try WebSocket cache first ──────────────────────────
            ws_depth  = realtime.get_depth(sym) if hasattr(realtime, 'get_depth') else None
            ltp       = realtime.get_ltp(sym)

            if ws_depth and (ws_depth.get('top_bid') or ws_depth.get('top_ask')):
                return jsonify({
                    'symbol':     sym,
                    'ltp':        round(ltp, 2) if ltp else None,
                    'top_bid':    round(ws_depth['top_bid'],  2) if ws_depth.get('top_bid')  else None,
                    'top_ask':    round(ws_depth['top_ask'],  2) if ws_depth.get('top_ask')  else None,
                    'buy_depth':  [{'price': round(d.get('price',0),2), 'quantity': d.get('quantity',0)}
                                   for d in ws_depth.get('buy_depth',  [])],
                    'sell_depth': [{'price': round(d.get('price',0),2), 'quantity': d.get('quantity',0)}
                                   for d in ws_depth.get('sell_depth', [])],
                    'source': 'websocket',
                })

            # ── Fallback: REST quote via Kite OMS ─────────────────────
            # /oms/quote on kite.zerodha.com requires the INSTRUMENT TOKEN
            # integer as the key (e.g. NSE:2779137), NOT the trading symbol
            # (NSE:LIQUIDCASE returns 400 InputException every time).
            resp = None
            instrument_key = None

            if realtime and hasattr(realtime, 'instrument_tokens'):
                for raw_token, tok_info in realtime.instrument_tokens.items():
                    if tok_info.get('symbol') == sym:
                        exchange      = tok_info.get('exchange', 'NSE').upper()
                        instrument_key = f'{exchange}:{raw_token}'
                        resp = auth.session.get(
                            f"{Config.ZERODHA_API_BASE}/oms/quote",
                            params={'i': instrument_key},
                            timeout=6
                        )
                        break

            if resp is not None and resp.status_code == 200:
                qdata = resp.json().get('data', {}).get(instrument_key, {})
                depth  = qdata.get('depth', {})
                buys   = depth.get('buy',  [])
                sells  = depth.get('sell', [])
                q_ltp  = qdata.get('last_price') or ltp
                top_bid = float(buys[0]['price'])  if buys  and buys[0].get('price')  else None
                top_ask = float(sells[0]['price']) if sells and sells[0].get('price') else None
                return jsonify({
                    'symbol':     sym,
                    'ltp':        round(q_ltp, 2) if q_ltp else None,
                    'top_bid':    round(top_bid, 2) if top_bid else None,
                    'top_ask':    round(top_ask, 2) if top_ask else None,
                    'buy_depth':  [{'price': round(float(d.get('price',0)),2), 'quantity': d.get('quantity',0)} for d in buys[:5]],
                    'sell_depth': [{'price': round(float(d.get('price',0)),2), 'quantity': d.get('quantity',0)} for d in sells[:5]],
                    'source': 'rest',
                })
            else:
                logger.debug(
                    f"Depth REST fallback failed for {sym}: key={instrument_key} "                    f"status={resp.status_code if resp else 'no-token'}",
                )

            # ── Nothing available — return LTP only ───────────────
            return jsonify({
                'symbol':     sym,
                'ltp':        round(ltp, 2) if ltp else None,
                'top_bid':    None,
                'top_ask':    None,
                'buy_depth':  [],
                'sell_depth': [],
                'source':     'none',
            })

        except Exception as e:
            logger.error(f"Error fetching depth for {symbol}: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500


    @app.route('/api/indices')
    def get_indices():
        """
        Get market indices live data.
        Primary: WebSocket cache (MODE_FULL ticks).
        Fallback: REST /oms/quote when WS hasn't delivered ticks yet.
        """
        try:
            realtime = dashboard_state.get('realtime_manager')
            auth     = dashboard_state.get('auth_manager')
            if not realtime:
                return jsonify({'error': 'Realtime manager not initialized'}), 503

            # Map index name → Kite quote instrument key
            INDEX_QUOTE_KEYS = {
                'NIFTY 50':         'NSE:NIFTY 50',
                'NIFTY MIDCAP 150': 'NSE:NIFTY MIDCAP 150',
                'INDIA VIX':        'NSE:INDIA VIX',
            }

            indices_data = []

            for index_name, index_info in MARKET_INDICES.items():
                ltp      = realtime.get_ltp(index_name)
                ohlc     = realtime.get_ohlc(index_name) or {}
                prev_close = float(ohlc.get('close') or 0)

                # If WebSocket hasn't provided a price yet, hit REST quote
                if (not ltp or ltp <= 0) and auth:
                    try:
                        # Use instrument token key (EXCHANGE:TOKEN) — not name
                        idx_token = None
                        idx_exchange = 'NSE'
                        if realtime and hasattr(realtime, 'instrument_tokens'):
                            for raw_tok, tinfo in realtime.instrument_tokens.items():
                                if tinfo.get('symbol') == index_name:
                                    idx_token   = raw_tok
                                    idx_exchange = tinfo.get('exchange', 'NSE').upper()
                                    break
                        if idx_token:
                            quote_key = f'{idx_exchange}:{idx_token}'
                            qr = auth.session.get(
                                f"{Config.ZERODHA_API_BASE}/oms/quote",
                                params={'i': quote_key},
                                timeout=6
                            )
                            if qr.status_code == 200:
                                qd = qr.json().get('data', {}).get(quote_key, {})
                                ltp        = qd.get('last_price') or ltp
                                prev_close = prev_close or float(qd.get('ohlc', {}).get('close') or 0)
                    except Exception as qe:
                        logger.debug(f"REST quote fallback for {index_name}: {qe}")

                display = float(ltp) if ltp and ltp > 0 else prev_close
                closed  = (not ltp or ltp <= 0)

                if display > 0:
                    change     = (display - prev_close) if (prev_close > 0 and not closed) else 0.0
                    change_pct = (change / prev_close * 100) if prev_close > 0 else 0.0
                else:
                    change, change_pct = 0.0, 0.0

                indices_data.append({
                    'name':          index_name,
                    'ltp':           round(display, 2),
                    'change':        round(change, 2),
                    'change_pct':    round(change_pct, 2),
                    'prev_close':    round(prev_close, 2),
                    'market_closed': closed,
                })

            return jsonify(indices_data)

        except Exception as e:
            logger.error(f"Error fetching indices data: {e}", exc_info=True)
            return jsonify([
                {'name': n, 'ltp': 0, 'change': 0, 'change_pct': 0, 'prev_close': 0}
                for n in ['NIFTY 50', 'NIFTY MIDCAP 150', 'INDIA VIX']
            ])


    @app.route('/api/bot/start', methods=['POST'])
    def start_bot():
        """Start the trading bot."""
        try:
            dashboard_state['bot_running'] = True
            logger.info("Bot started via dashboard")
            return jsonify({'status': 'started'})
        except Exception as e:
            logger.error(f"Error starting bot: {e}")
            return jsonify({'error': str(e)}), 500
    
    
    @app.route('/api/bot/stop', methods=['POST'])
    def stop_bot():
        """Stop the trading bot."""
        try:
            dashboard_state['bot_running'] = False
            logger.info("Bot stopped via dashboard")
            return jsonify({'status': 'stopped'})
        except Exception as e:
            logger.error(f"Error stopping bot: {e}")
            return jsonify({'error': str(e)}), 500
    
    

    @app.route('/api/bot/pause', methods=['POST'])
    def pause_bot():
        """
        Pause all Zerodha OMS API calls without stopping Flask or the WS feed.
        The frontend also stops its polling intervals when this is called.
        This lets the user log into Kite in the browser without the bot's
        constant OMS requests competing with / invalidating the browser session.

        Sets _bot_paused on auth_manager so keep_alive hibernate watchdog and
        handle_session_expiry both skip re-auth while paused — preventing any
        credentials login from firing and logging the browser out.
        """
        try:
            dashboard_state['bot_paused'] = True
            dashboard_state['bot_running'] = False
            # Tell auth_manager we are paused so ALL re-auth paths are blocked:
            # handle_session_expiry, keep_alive hibernate watchdog, etc.
            auth = dashboard_state.get('auth_manager')
            if auth:
                auth._bot_paused = True
            logger.info("Bot PAUSED — all OMS API calls and re-auth suspended")
            return jsonify({'status': 'paused'})
        except Exception as e:
            logger.error(f"Error pausing bot: {e}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/bot/resume', methods=['POST'])
    def resume_bot():
        """
        Resume OMS API calls after a pause. Re-enables trading engine and
        frontend polling. Also triggers a fresh CDP token pull so the bot
        picks up whatever fresh enctoken the user just logged in with.
        """
        try:
            dashboard_state['bot_paused'] = False
            dashboard_state['bot_running'] = True

            # Clear pause flag on auth_manager before pulling token
            auth = dashboard_state.get('auth_manager')
            if auth:
                auth._bot_paused = False
                auth._last_reauth_attempt = 0.0   # reset debounce so pull fires immediately
                expected_uid = auth._expected_user_id() if hasattr(auth, '_expected_user_id') else ''
                if auth._load_enctoken_from_browser(expected_uid=expected_uid):
                    logger.info("Resume: fresh enctoken pulled from browser via CDP")
                else:
                    logger.info("Resume: CDP pull skipped (no Kite tab found) — using existing token")

            logger.info("Bot RESUMED — OMS API calls restored")
            return jsonify({'status': 'resumed'})
        except Exception as e:
            logger.error(f"Error resuming bot: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/portfolio/sync', methods=['POST'])
    def sync_portfolio():
        """Force portfolio sync. Returns changed=True if holdings/positions changed."""
        try:
            portfolio = dashboard_state['portfolio_tracker']

            if not portfolio:
                return jsonify({'error': 'Portfolio not initialized'}), 503

            # Snapshot before sync to detect changes
            before_symbols = set(portfolio.locked_symbols) if hasattr(portfolio, 'locked_symbols') else set()
            before_slots   = getattr(portfolio, 'available_slots', None)

            portfolio.sync()

            after_symbols = set(portfolio.locked_symbols) if hasattr(portfolio, 'locked_symbols') else set()
            after_slots   = getattr(portfolio, 'available_slots', None)

            changed = (before_symbols != after_symbols) or (before_slots != after_slots)
            logger.info(f"Portfolio synced — changed={changed}")

            return jsonify({'status': 'synced', 'changed': changed})
        except Exception as e:
            logger.error(f"Error syncing portfolio: {e}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/wake', methods=['POST'])
    def handle_wake():
        """
        Called by the browser JS immediately after detecting a hibernate/wake
        event (via visibilitychange, online, pageshow, Page Lifecycle resume,
        or monotonic-clock drift).

        This gives the Python backend a fast signal to:
          1. Re-sync the portfolio (holdings / positions may be stale)
          2. Check WebSocket health and force-reconnect if ticks are dead
        The keep_alive watchdog will independently handle full re-auth if the
        Zerodha session has expired, but this endpoint ensures the portfolio
        is refreshed immediately rather than waiting for the next watchdog cycle.
        """
        try:
            results = {}

            # 1. Portfolio resync
            portfolio = dashboard_state.get('portfolio_tracker')
            if portfolio:
                try:
                    portfolio.sync()
                    results['portfolio'] = 'synced'
                    logger.info("Portfolio resynced via /api/wake")
                except Exception as pe:
                    results['portfolio'] = f'error: {pe}'
                    logger.warning(f"/api/wake portfolio sync error: {pe}")
            else:
                results['portfolio'] = 'not_initialized'

            # 2. WebSocket health check — reconnect if no recent ticks
            realtime = dashboard_state.get('realtime_manager')
            if realtime:
                try:
                    from backend.utils.keep_alive import _last_tick_ts, _inject_heartbeat
                    import time
                    stale_for = time.monotonic() - _last_tick_ts.get('t', 0)
                    if stale_for > 30:   # >30 s without a tick after wake = reconnect
                        logger.warning(f"/api/wake: WS stale {stale_for:.0f}s — forcing reconnect")
                        if realtime.kws:
                            try: realtime.kws.close()
                            except Exception: pass
                        realtime.is_connected = False
                        realtime._reconnect_attempts = 0
                        if realtime.initialize():
                            _inject_heartbeat(realtime)
                            import threading
                            threading.Thread(
                                target=realtime.start, daemon=True,
                                name='WakeWSReconnect'
                            ).start()
                            results['websocket'] = 'reconnected'
                        else:
                            results['websocket'] = 'initialize_failed'
                    else:
                        results['websocket'] = f'healthy ({stale_for:.0f}s since last tick)'
                except Exception as we:
                    results['websocket'] = f'error: {we}'
                    logger.warning(f"/api/wake WS check error: {we}")
            else:
                results['websocket'] = 'not_initialized'

            return jsonify({'status': 'ok', 'results': results})

        except Exception as e:
            logger.error(f"/api/wake error: {e}")
            return jsonify({'error': str(e)}), 500


    @app.route('/api/scanner', methods=['POST'])
    def run_scanner():
        """
        Scan a list of symbols and return those where:
          weekly  Williams %R(14) <= weekly_threshold  (default -70)
          daily   Williams %R(14) <= daily_threshold   (default -60)
        Falls back gracefully when CSV data is unavailable.
        Uses the in-process historical manager cache first; reads CSV directly
        for symbols not yet in cache. For symbols with no local data, fetches
        historical candles from Zerodha OMS using instrument tokens.
        """
        import pandas as pd
        from datetime import datetime, timedelta
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from backend.indicators.calculator import calculate_williams_r

        # ── Build instrument-token lookup for the scanner ─────────────────────
        # We need token→symbol and symbol→token maps to fetch candles from OMS.
        # Priority: realtime_manager.instrument_tokens (already resolved tokens)
        # then historical_manager._instrument_tokens.
        _sym_to_token: dict = {}
        realtime = dashboard_state.get('realtime_manager')
        if realtime and hasattr(realtime, 'instrument_tokens'):
            for token, info in realtime.instrument_tokens.items():
                sym = info.get('symbol', '')
                if sym and sym not in _sym_to_token:
                    _sym_to_token[sym] = (token, info.get('exchange', 'NSE'))

        historical_mgr = dashboard_state.get('historical_manager')
        if historical_mgr and hasattr(historical_mgr, '_instrument_tokens'):
            for sym, token in historical_mgr._instrument_tokens.items():
                if sym not in _sym_to_token:
                    _sym_to_token[sym] = (str(token), 'NSE')

        _auth = dashboard_state.get('auth_manager')

        def _fetch_candles_oms(symbol: str, timeframe: str = 'day') -> 'Optional[pd.DataFrame]':
            """
            Fetch up to 400 days of OHLC candles from Zerodha OMS for any symbol.
            Resolves the instrument token on-the-fly from the public instruments CSV
            if the symbol is not already in _sym_to_token.
            Returns a DataFrame with columns [high, low, close] or None on failure.
            """
            if not _auth:
                return None
            try:
                token_info = _sym_to_token.get(symbol)
                if not token_info:
                    # Try to resolve via public instruments CSV (no auth needed)
                    try:
                        import io
                        resp = _auth.session.get(
                            'https://api.kite.trade/instruments/NSE',
                            timeout=15
                        )
                        if resp.status_code == 200:
                            df_inst = pd.read_csv(io.StringIO(resp.text))
                            df_inst.columns = [c.lower() for c in df_inst.columns]
                            match = df_inst[df_inst['tradingsymbol'] == symbol]
                            if not match.empty:
                                row = match.iloc[0]
                                tok = str(int(row['instrument_token']))
                                exch = str(row.get('exchange', 'NSE'))
                                _sym_to_token[symbol] = (tok, exch)
                                token_info = (tok, exch)
                    except Exception:
                        pass
                if not token_info:
                    return None

                token, exchange = token_info
                today = datetime.now().date()
                fetch_from = today - timedelta(days=400)
                fetch_to   = today

                url = f"{Config.ZERODHA_API_BASE}/oms/instruments/historical/{token}/{timeframe}"
                params = {
                    'from':       fetch_from.strftime('%Y-%m-%d'),
                    'to':         fetch_to.strftime('%Y-%m-%d'),
                    'continuous': 0,
                    'oi':         1,
                }
                resp = _auth.session.get(url, params=params, timeout=20)
                if resp.status_code != 200:
                    return None
                candles = resp.json().get('data', {}).get('candles', [])
                if not candles:
                    return None
                df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
                for col in ('high', 'low', 'close'):
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.dropna(subset=['high', 'low', 'close'])
                return df if len(df) >= 14 else None
            except Exception as e:
                logger.debug(f"Scanner OMS fetch for {symbol}/{timeframe}: {e}")
                return None

        try:
            body             = request.get_json(force=True) or {}
            symbols          = [s.strip().upper() for s in body.get('symbols', []) if s.strip()]
            daily_thresh     = float(body.get('daily_threshold',  -60))
            weekly_thresh    = float(body.get('weekly_threshold', -70))

            if not symbols:
                return jsonify({'error': 'No symbols supplied'}), 400

            historical = historical_mgr  # already resolved above
            period     = 14
            import os as _os

            def _is_stale(path, max_age_days=10) -> bool:
                """Return True if the file is older than max_age_days or doesn't exist."""
                try:
                    age = (_os.path.getmtime(str(path)))
                    from datetime import datetime as _dt
                    return (_dt.now().timestamp() - age) / 86400 > max_age_days
                except Exception:
                    return True

            def _load_csv_direct(path, skip_if_stale=False) -> 'Optional[pd.DataFrame]':
                """Load a CSV OHLC file. Skips stale files when skip_if_stale=True."""
                try:
                    if not path.exists():
                        return None
                    if skip_if_stale and _is_stale(path):
                        return None
                    df = pd.read_csv(path)
                    df.columns = [c.lower() for c in df.columns]
                    df = df.rename(columns={'timestamp': 'date'})
                    df = df.sort_values('date').reset_index(drop=True)
                    for col in ('high', 'low', 'close'):
                        if col in df.columns:
                            df[col] = pd.to_numeric(df[col], errors='coerce')
                    df = df.dropna(subset=['high', 'low', 'close'])
                    return df if len(df) >= period else None
                except Exception:
                    return None

            def _derive_weekly(daily_df: 'pd.DataFrame') -> 'Optional[pd.DataFrame]':
                """
                Resample a daily OHLC DataFrame to weekly candles (week-ending Friday).
                This is the most accurate way to compute weekly W%R — it uses the same
                daily data that the app already has, with no extra API call.
                """
                try:
                    if daily_df is None or len(daily_df) < period:
                        return None
                    df = daily_df.copy()
                    # Ensure a datetime index
                    date_col = 'date' if 'date' in df.columns else df.columns[0]
                    df[date_col] = pd.to_datetime(df[date_col], errors='coerce')
                    df = df.dropna(subset=[date_col])
                    df = df.set_index(date_col)
                    agg = {'high': 'max', 'low': 'min', 'close': 'last'}
                    if 'open' in df.columns:
                        agg['open'] = 'first'
                    if 'volume' in df.columns:
                        agg['volume'] = 'sum'
                    weekly = df.resample('W-FRI').agg(agg).dropna(subset=['close']).reset_index()
                    for col in ('high', 'low', 'close'):
                        weekly[col] = pd.to_numeric(weekly[col], errors='coerce')
                    weekly = weekly.dropna(subset=['high', 'low', 'close'])
                    return weekly if len(weekly) >= period else None
                except Exception:
                    return None

            def _compute(sym):
                # ══════════════════════════════════════════════════════════════
                # Mirrors Active Strategy calculation exactly.
                #
                # KEY FIXES vs old scanner:
                #   1. Daily W%R: was using ltp as high=low=close, collapsing
                #      today's range → wrong W%R. Now uses calculate_daily_williams_r()
                #      with real intraday high/low from the realtime OHLC feed —
                #      the same function the Active Strategy tab calls.
                #
                #   2. Weekly W%R: was derived from daily_df AFTER the live row
                #      was appended, so today's partial candle corrupted the
                #      current week's close. Now derived from hist_df (historical
                #      only, no live row) so weekly reflects completed days only.
                # ══════════════════════════════════════════════════════════════

                # ── Step 1: Historical daily data (no live row yet) ───────────
                # Stored as hist_df so weekly derivation is clean.
                hist_df = None

                # A. In-process historical cache (active ETFs — already current)
                if historical and sym in getattr(historical, 'cache', {}):
                    hist_df = historical.get_daily_data(sym)

                # B. Local daily CSV
                if hist_df is None:
                    hist_df = _load_csv_direct(
                        Path(__file__).parent.parent / 'data' / 'daily' / f'{sym}.csv',
                        skip_if_stale=False)

                # C. Zerodha OMS (fetch_to=yesterday, no partial today candle)
                if hist_df is None:
                    hist_df = _fetch_candles_oms(sym, 'day')

                # ── Step 2: Live price + real intraday OHLC ───────────────────
                # Matches Active Strategy route exactly (see get_market_data).
                ltp       = None
                live_high = None
                live_low  = None
                try:
                    if realtime:
                        raw = realtime.get_ltp(sym)
                        if raw:
                            ltp = float(raw)
                        ohlc = realtime.get_ohlc(sym)
                        if ohlc:
                            h = ohlc.get('high')
                            l = ohlc.get('low')
                            if h: live_high = float(h)
                            if l: live_low  = float(l)
                except Exception:
                    pass

                # ── Step 3: Daily W%R via calculate_daily_williams_r() ────────
                # Identical call to what Active Strategy uses. This function
                # appends the live row internally with correct high/low/close —
                # no manual concat needed, no risk of duplicate today candle.
                from backend.indicators.calculator import calculate_daily_williams_r
                daily_wr = None
                if hist_df is not None and len(hist_df) >= period:
                    # Guard: reject zero/None ltp (no tick yet → use historical only)
                    _ltp_valid  = ltp if (ltp is not None and ltp > 0) else None
                    # Guard: only use live_high/low when they bracket ltp sensibly;
                    # if they fail the sanity check, fall back to ltp (point candle)
                    # rather than None, so calculate_daily_williams_r still appends
                    # today's row and doesn't silently revert to pure historical W%R.
                    _high_valid = (live_high if (live_high and _ltp_valid and live_high >= _ltp_valid)
                                   else _ltp_valid)
                    _low_valid  = (live_low  if (live_low  and _ltp_valid and live_low  <= _ltp_valid)
                                   else _ltp_valid)
                    daily_wr = calculate_daily_williams_r(
                        hist_df,
                        live_price=_ltp_valid,
                        live_high=_high_valid,
                        live_low=_low_valid,
                        period=period,
                    )

                # ── Step 4: Weekly W%R ────────────────────────────────────────
                # Derived from hist_df (historical only, NO live row) so the
                # current partial day does not bleed into the current week candle.
                weekly_df = None

                # 1. Derive weekly from historical daily (most accurate & fresh)
                if hist_df is not None and len(hist_df) >= period:
                    weekly_df = _derive_weekly(hist_df)

                # 2. Historical manager weekly loader (for active symbols)
                if weekly_df is None and historical and hasattr(historical, '_load_weekly_data'):
                    try:
                        weekly_df = historical._load_weekly_data(sym)
                    except Exception:
                        pass

                # 3. Local weekly CSV — skip if stale (>10 days old)
                if weekly_df is None:
                    weekly_df = _load_csv_direct(
                        Path(__file__).parent.parent / 'data' / 'weekly' / f'{sym}.csv',
                        skip_if_stale=True)

                # 4. Zerodha OMS weekly candles as final fallback
                if weekly_df is None:
                    weekly_df = _fetch_candles_oms(sym, 'week')

                weekly_wr = None
                if weekly_df is not None and len(weekly_df) >= period:
                    weekly_wr = calculate_williams_r(weekly_df, period=period)

                # ── Step 5: 20-Day SMA (DMA) ─────────────────────────────────
                # Uses the same hist_df (historical daily) used for W%R.
                # LTP is used as the reference price; falls back to last close.
                dma20     = None
                below_dma = False
                ref_price = ltp  # live price if available
                if hist_df is not None and len(hist_df) >= 20:
                    try:
                        closes = pd.to_numeric(hist_df['close'], errors='coerce').dropna()
                        if len(closes) >= 20:
                            dma20 = float(closes.iloc[-20:].mean())
                            if ref_price is None:
                                ref_price = float(closes.iloc[-1])
                            below_dma = bool(ref_price <= dma20)
                    except Exception:
                        pass

                # ── Step 6: 1-Month Avg Trade Value ──────────────────────────
                # avg_trade_val = mean(volume, last 22 trading days) × LTP
                avg_trade_val = None
                if hist_df is not None and 'volume' in hist_df.columns:
                    try:
                        vols = pd.to_numeric(hist_df['volume'], errors='coerce').dropna()
                        if len(vols) >= 5:
                            lookback = min(22, len(vols))
                            avg_vol  = float(vols.iloc[-lookback:].mean())
                            price    = ref_price if ref_price and ref_price > 0 else (
                                float(pd.to_numeric(hist_df['close'], errors='coerce').dropna().iloc[-1])
                                if len(hist_df) else None
                            )
                            if price and price > 0:
                                avg_trade_val = avg_vol * price
                    except Exception:
                        pass

                return sym, daily_wr, weekly_wr, dma20, below_dma, ref_price, avg_trade_val

            # Run in parallel — 20 workers for fast scanning across large universes
            results = []
            no_data = []
            with ThreadPoolExecutor(max_workers=20) as ex:
                futures = {ex.submit(_compute, sym): sym for sym in symbols}
                for fut in as_completed(futures):
                    try:
                        sym, dwr, wwr, dma20, below_dma, ltp_price, avg_tv = fut.result()
                    except Exception:
                        sym = futures[fut]; dwr = wwr = None; dma20 = None; below_dma = False; ltp_price = None; avg_tv = None

                    if dwr is None and wwr is None:
                        no_data.append(sym)
                        continue

                    # Cast to plain Python types — numpy.float64 / numpy.bool_
                    # are not JSON-serialisable
                    if dwr  is not None: dwr  = float(dwr)
                    if wwr  is not None: wwr  = float(wwr)
                    if dma20 is not None: dma20 = float(dma20)
                    if ltp_price is not None: ltp_price = float(ltp_price)

                    daily_ok  = bool(dwr  is not None and dwr  <= daily_thresh)
                    weekly_ok = bool(wwr  is not None and wwr  <= weekly_thresh)
                    both_ok   = bool(daily_ok and weekly_ok)

                    if avg_tv is not None: avg_tv = float(avg_tv)
                    results.append({
                        'symbol':        sym,
                        'daily_wr':      round(dwr,  2) if dwr  is not None else None,
                        'weekly_wr':     round(wwr,  2) if wwr  is not None else None,
                        'daily_ok':      daily_ok,
                        'weekly_ok':     weekly_ok,
                        'both_ok':       both_ok,
                        'dma20':         round(dma20, 2) if dma20 is not None else None,
                        'below_20dma':   bool(below_dma),
                        'ltp':           round(ltp_price, 2) if ltp_price is not None else None,
                        'avg_trade_val': round(avg_tv, 0) if avg_tv is not None else None,
                    })

            # Sort by Daily W%R ascending — most negative (oversold) on top.
            # Symbols with no daily W%R data are pushed to the end.
            def _sort_key(r):
                return (r['daily_wr'] if r['daily_wr'] is not None else 1)

            results.sort(key=_sort_key)

            return jsonify({
                'results':        results,
                'no_data':        no_data,
                'daily_thresh':   daily_thresh,
                'weekly_thresh':  weekly_thresh,
                'total_scanned':  len(symbols),
                'both_passing':   sum(1 for r in results if r['both_ok']),
                'daily_passing':  sum(1 for r in results if r['daily_ok']),
                'weekly_passing': sum(1 for r in results if r['weekly_ok']),
                'dma20_passing':  sum(1 for r in results if r['below_20dma']),
            })

        except Exception as e:
            logger.error(f"/api/scanner error: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500


    # ── Scanner seed state (module-level so status route can read it) ─────────
    _seed_state = {
        'running':   False,
        'total':     0,
        'done':      0,
        'ok':        0,
        'failed':    0,
        'skipped':   0,
        'current':   '',
        'log':       [],   # last N messages
    }

    @app.route('/api/scanner/seed', methods=['POST'])
    def scanner_seed():
        """
        Bulk-download daily OHLC CSVs for every symbol in the scanner universe
        (ETF list, Nifty 200, Nifty Midcap 150) that does not yet have a fresh
        local CSV.  Runs in a background thread; poll /api/scanner/seed/status
        for progress.

        Body (JSON, all optional):
          symbols  – explicit list; omit to use the full scanner universe
          force    – true → re-download even if a fresh CSV already exists
        """
        import threading, requests as _req

        if _seed_state['running']:
            return jsonify({'ok': False, 'error': 'Seed already running'}), 409

        body    = request.get_json(force=True) or {}
        symbols = [s.strip().upper() for s in body.get('symbols', []) if s.strip()]
        force   = bool(body.get('force', False))

        # If no explicit list was sent, the JS will have sent the full universe
        if not symbols:
            return jsonify({'ok': False, 'error': 'No symbols supplied'}), 400

        hist = dashboard_state.get('historical_manager')
        auth = dashboard_state.get('auth_manager')
        if not auth:
            return jsonify({'ok': False, 'error': 'Not authenticated'}), 503

        def _seed_worker(syms, historical, auth_mgr, force_dl):
            import requests as _rq
            import pandas as pd
            from io import StringIO as _SIO
            from datetime import datetime, timedelta

            _seed_state.update(running=True, total=len(syms), done=0,
                               ok=0, failed=0, skipped=0, current='', log=[])

            # ── Step 1: Fetch full instruments CSV once ───────────────────────
            df_inst = None
            for url, use_auth in [
                ('https://api.kite.trade/instruments', False),
                (f'{Config.ZERODHA_API_BASE}/oms/instruments/NSE', True),
            ]:
                try:
                    sess = auth_mgr.session if use_auth else _rq.Session()
                    r = sess.get(url, timeout=15)
                    if r.status_code == 200 and r.text.strip():
                        df_inst = pd.read_csv(_SIO(r.text), low_memory=False)
                        df_inst.columns = [c.lower() for c in df_inst.columns]
                        _seed_state['log'].append(f'Instruments CSV: {len(df_inst)} rows from {url}')
                        break
                except Exception as _e:
                    _seed_state['log'].append(f'Instruments fetch {url}: {_e}')

            if df_inst is None:
                _seed_state.update(running=False, current='Failed — could not fetch instruments list')
                return

            SEGS = ['NSE-EQ', 'NSE', 'BSE-EQ', 'BSE']

            def _resolve_token(sym):
                for seg in SEGS:
                    m = df_inst[(df_inst['tradingsymbol'] == sym) & (df_inst['segment'] == seg)]
                    if not m.empty:
                        return str(int(m.iloc[0]['instrument_token'])), str(m.iloc[0].get('exchange', 'NSE'))
                m = df_inst[df_inst['tradingsymbol'] == sym]
                if not m.empty:
                    return str(int(m.iloc[0]['instrument_token'])), str(m.iloc[0].get('exchange', 'NSE'))
                return None, None

            today      = datetime.now().date()
            fetch_from = today - timedelta(days=400)
            fetch_to   = today - timedelta(days=1)

            def _fetch_and_save(sym):
                """Download 400 days of daily OHLC for sym and save as CSV."""
                csv_path = Config.DAILY_DATA_DIR / f'{sym}.csv'

                # Skip if fresh CSV already exists (unless force=True)
                if not force_dl and csv_path.exists():
                    try:
                        existing = pd.read_csv(csv_path)
                        if 'date' in existing.columns:
                            last = pd.to_datetime(existing['date']).max().date()
                            if (today - last).days < 2:
                                return 'skipped'
                    except Exception:
                        pass  # corrupt CSV → re-download

                token, _ = _resolve_token(sym)
                if not token:
                    # Also check historical manager's already-known tokens
                    if historical:
                        token = historical._instrument_tokens.get(sym)
                if not token:
                    return 'no_token'

                try:
                    url = (f'{Config.ZERODHA_API_BASE}/oms/instruments/'
                           f'historical/{token}/day')
                    params = {
                        'from':       fetch_from.strftime('%Y-%m-%d'),
                        'to':         fetch_to.strftime('%Y-%m-%d'),
                        'continuous': 0,
                        'oi':         1,
                    }
                    resp = auth_mgr.session.get(url, params=params, timeout=30)
                    if resp.status_code != 200:
                        return f'http_{resp.status_code}'

                    candles = resp.json().get('data', {}).get('candles', [])
                    if not candles:
                        return 'no_candles'

                    df = pd.DataFrame(candles,
                                      columns=['timestamp','open','high','low',
                                               'close','volume','oi'])
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                    if df['timestamp'].dt.tz is not None:
                        df['timestamp'] = df['timestamp'].dt.tz_localize(None)
                    df = df.sort_values('timestamp').reset_index(drop=True)

                    # If an existing CSV has older bars, merge them in
                    if csv_path.exists():
                        try:
                            old = pd.read_csv(csv_path)
                            if 'date' in old.columns:
                                old.rename(columns={'date': 'timestamp'}, inplace=True)
                            old['timestamp'] = pd.to_datetime(old['timestamp'])
                            if old['timestamp'].dt.tz is not None:
                                old['timestamp'] = old['timestamp'].dt.tz_localize(None)
                            df = pd.concat([old, df], ignore_index=True)
                            df['_d'] = df['timestamp'].dt.date
                            df = df.drop_duplicates(subset=['_d']).drop(columns=['_d'])
                            df = df.sort_values('timestamp').reset_index(drop=True)
                        except Exception:
                            pass  # corrupt old CSV — use fresh download only

                    save = df.copy()
                    save.insert(0, 'date', save['timestamp'].dt.strftime('%Y-%m-%d'))
                    save = save.drop(columns=['timestamp'])
                    Config.DAILY_DATA_DIR.mkdir(parents=True, exist_ok=True)
                    save.to_csv(csv_path, index=False)

                    # Also refresh the historical manager's in-memory cache
                    if historical:
                        df_cache = df[df['timestamp'].dt.date < today].reset_index(drop=True)
                        historical.cache.setdefault(sym, {})['daily'] = df_cache
                        historical._instrument_tokens[sym] = token

                    return f'ok:{len(df)}'

                except Exception as _e:
                    return f'err:{_e}'

            # ── Step 2: Process sequentially (Zerodha rate-limits ~3 req/s) ──
            LOG_MAX = 200
            for sym in syms:
                _seed_state['current'] = sym
                result = _fetch_and_save(sym)
                _seed_state['done'] += 1
                if result == 'skipped':
                    _seed_state['skipped'] += 1
                    msg = f'⏭  {sym}: already fresh'
                elif result == 'no_token':
                    _seed_state['failed'] += 1
                    msg = f'✗  {sym}: symbol not found in Zerodha instruments'
                elif result.startswith('ok:'):
                    _seed_state['ok'] += 1
                    msg = f'✓  {sym}: {result[3:]} candles saved'
                else:
                    _seed_state['failed'] += 1
                    msg = f'✗  {sym}: {result}'
                _seed_state['log'].append(msg)
                if len(_seed_state['log']) > LOG_MAX:
                    _seed_state['log'] = _seed_state['log'][-LOG_MAX:]

            _seed_state.update(running=False, current='Complete')

        threading.Thread(
            target=_seed_worker,
            args=(symbols, hist, auth, force),
            daemon=True, name='ScannerSeed'
        ).start()

        return jsonify({'ok': True, 'total': len(symbols)})


    @app.route('/api/scanner/seed/status', methods=['GET'])
    def scanner_seed_status():
        """Return current seed progress."""
        return jsonify(dict(_seed_state))


    @app.route('/api/symbols/get', methods=['GET'])
    def get_symbols():
        """Return current active_etfs and bnh_symbols from settings.json"""
        try:
            settings_path = Path(__file__).parent.parent / 'config' / 'settings.json'
            settings = {}
            if settings_path.exists():
                settings = json.load(open(settings_path))
            return jsonify({
                'success': True,
                'active_etfs': settings.get('active_etfs', [
                    'MON100','GOLDBEES','SILVERBEES','JUNIORBEES',
                    'PSUBNKBEES','MINDSPACE-RR','EMBASSY-RR'
                ]),
                'bnh_symbols': settings.get('bnh_symbols', ['MID150BEES']),
            })
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/symbols/update', methods=['POST'])
    def update_symbols():
        """Update active_etfs or bnh_symbols in settings.json"""
        try:
            data = request.get_json()
            strategy = data.get('strategy')      # 'active' or 'bnh'
            symbols  = data.get('symbols', [])

            if strategy not in ('active', 'bnh'):
                return jsonify({'success': False, 'error': 'Invalid strategy'}), 400

            symbols = [s.strip().upper() for s in symbols if s.strip()]
            if not symbols:
                return jsonify({'success': False, 'error': 'Symbol list cannot be empty'}), 400

            settings_path = Path(__file__).parent.parent / 'config' / 'settings.json'
            settings = {}
            if settings_path.exists():
                settings = json.load(open(settings_path))

            # ── Cross-strategy duplicate check ────────────────────────────────
            other_key  = 'bnh_symbols' if strategy == 'active' else 'active_etfs'
            other_syms = set(settings.get(other_key, []))
            dupes = [s for s in symbols if s in other_syms]
            if dupes:
                return jsonify({
                    'success': False,
                    'error': f"{', '.join(dupes)} already exist in the other strategy. "
                             f"A symbol can only belong to one strategy at a time."
                }), 400

            key = 'active_etfs' if strategy == 'active' else 'bnh_symbols'
            old_symbols = set(settings.get(key, []))
            settings[key] = symbols

            _atomic_write_json(settings_path, settings)
            logger.info(f"Symbols updated — {key}: {symbols}")

            new_symbols = [s for s in symbols if s not in old_symbols]

            # ── For new symbols: fetch token + historical + realtime ──────────
            # Done in a single background thread per symbol, in sequence:
            #   1. Fetch instrument token (needed for both historical and realtime)
            #   2. Load/bootstrap historical CSV (needed for W%R)
            #   3. Subscribe to realtime WebSocket feed (needed for LTP)
            # This runs async so the HTTP response returns immediately,
            # but the JS does delayed refreshes at 8s and 20s to pick up the data.
            if new_symbols:
                hist = dashboard_state.get('historical_manager')
                rt   = dashboard_state.get('realtime_manager')

                def _bootstrap_new_symbols(syms, historical, realtime):
                    for sym in syms:
                        try:
                            logger.info(f"Bootstrapping new symbol: {sym}")
                            # Step 1+2: token fetch is inside ensure_symbol_loaded
                            if historical:
                                ok = historical.ensure_symbol_loaded(sym)
                                logger.info(f"  Historical for {sym}: {'✓' if ok else '✗ not available'}")
                            # Step 3: realtime subscription
                            if realtime and hasattr(realtime, 'add_symbols'):
                                res = realtime.add_symbols([sym])
                                logger.info(f"  Realtime for {sym}: {res}")
                        except Exception as _e:
                            logger.error(f"  Bootstrap error for {sym}: {_e}")

                import threading as _t
                _t.Thread(
                    target=_bootstrap_new_symbols,
                    args=(new_symbols, hist, rt),
                    daemon=True, name='SymBootstrap'
                ).start()
                logger.info(f"Symbol bootstrap started for: {new_symbols}")

            subscribe_result = {}

            # ── Sync BnH engine sym states (add new, exclude removed symbols) ─
            if strategy == 'bnh':
                intraday = dashboard_state.get('intraday_engine')
                if intraday and hasattr(intraday, '_sync_sym_states'):
                    intraday._sync_sym_states()
                    logger.info(f"BnH engine sym_states synced: {symbols}")

            return jsonify({
                'success': True,
                'key': key,
                'symbols': symbols,
                'new_subscribed': subscribe_result.get('added', []),
                'not_found': subscribe_result.get('not_found', []),
            })
        except Exception as e:
            logger.error(f"update_symbols error: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/settings/update', methods=['POST'])
    def update_settings():
        """Update settings in settings.json"""
        try:
            settings_path = Path(__file__).parent.parent / 'config' / 'settings.json'
            
            # Get current settings
            if settings_path.exists():
                with open(settings_path, 'r') as f:
                    settings = json.load(f)
            else:
                settings = {
                    'active_etfs': ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR'],
                    'slots_count': 2,
                    'profit_target_pct': 3.0,
                    'williams_r_threshold': -80,
                    'williams_r_period': 14,
                    'max_cash_per_stock': 50000,
                    'max_cash_per_transaction': 10000,
                }
            
            # Update with new values
            data = request.get_json()
            
            # Trading mode: store in settings.json, Config.is_dry_run() reads it live
            needs_restart = False
            if 'trading_mode' in data:
                mode_value = str(data['trading_mode']).strip().upper()
                data['trading_mode'] = 'LIVE' if mode_value == 'LIVE' else 'DRY_RUN'
                logger.warning(f"Trading mode changed to {data['trading_mode']}")
            
            settings.update(data)
            
            # Save back to file
            _atomic_write_json(settings_path, settings)
            
            logger.info(f"Settings updated via dashboard: {data}")
            
            response = {
                'success': True,
                'status':  'success',
                'settings': settings
            }

            if needs_restart:
                response['needs_restart'] = True

            return jsonify(response)
            
        except Exception as e:
            logger.error(f"Error updating settings: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/manual-exit', methods=['POST'])
    def manual_exit():
        """Manually exit a position - sells ETF and buys LIQUIDCASE."""
        try:
            from flask import request
            data = request.get_json()
            
            symbol      = data.get('symbol')
            quantity    = data.get('quantity')
            order_type  = (data.get('order_type') or 'MARKET').upper()
            limit_price = data.get('limit_price')   # float or None

            if not symbol or not quantity:
                return jsonify({
                    'success': False,
                    'error': 'Symbol and quantity required'
                }), 400

            # Validate order type
            if order_type not in ('MARKET', 'LIMIT'):
                order_type = 'MARKET'
            if order_type == 'LIMIT' and not limit_price:
                return jsonify({
                    'success': False,
                    'error': 'limit_price required for LIMIT orders'
                }), 400
            
            # Get required services
            order_manager = dashboard_state.get('order_manager')
            portfolio = dashboard_state.get('portfolio_tracker')
            realtime_manager = dashboard_state.get('realtime_manager')
            
            if not order_manager or not portfolio or not realtime_manager:
                return jsonify({
                    'success': False,
                    'error': 'Services not initialized'
                }), 503
            
            # Verify position exists
            if not portfolio.is_symbol_held(symbol):
                return jsonify({
                    'success': False,
                    'error': f'No position in {symbol}'
                }), 400
            
            # Get current holdings
            held_qty = portfolio.get_quantity_held(symbol)
            if quantity > held_qty:
                return jsonify({
                    'success': False,
                    'error': f'Cannot sell {quantity} units, only {held_qty} held'
                }), 400
            
            # Get current prices
            etf_price = realtime_manager.get_ltp(symbol)
            liquidcase_price = realtime_manager.get_ltp(LIQUIDCASE_SYMBOL)
            
            if not etf_price or not liquidcase_price:
                return jsonify({
                    'success': False,
                    'error': 'Cannot get current prices'
                }), 500
            
            # Calculate LIQUIDCASE quantity
            proceeds = quantity * etf_price
            liquidcase_qty = int(proceeds / liquidcase_price)
            
            if liquidcase_qty <= 0:
                return jsonify({
                    'success': False,
                    'error': 'Invalid LIQUIDCASE quantity calculated'
                }), 500
            
            # Execute atomic swap: SELL ETF -> BUY LIQUIDCASE
            logger.info(f"Manual exit requested: {symbol} x {quantity}")
            logger.info(f"SWAP: Sell {quantity} {symbol} @ ₹{etf_price:.2f} -> Buy {liquidcase_qty} {LIQUIDCASE_SYMBOL} @ ₹{liquidcase_price:.2f}")
            
            try:
                success = order_manager.execute_swap(
                    sell_symbol=symbol,
                    sell_quantity=quantity,
                    buy_symbol=LIQUIDCASE_SYMBOL,
                    buy_quantity=liquidcase_qty,
                    buy_price_estimate=liquidcase_price,
                    sell_price_estimate=etf_price,
                    sell_order_type=order_type,
                    sell_limit_price=float(limit_price) if order_type == 'LIMIT' and limit_price else None,
                )
            except RuntimeError as ze:
                logger.error(f"Zerodha error in manual exit: {ze}")
                return jsonify({'success': False, 'error': str(ze), 'zerodha_error': True}), 400

            if success:
                success_msg = f"Exit complete: Sold {quantity} {symbol}, Bought {liquidcase_qty} {LIQUIDCASE_SYMBOL}"
                logger.info(f"✓ {success_msg}")
                
                # Reset override settings after manual exit — same as auto-sell
                # Prevents cascade: raises profit threshold so remaining units don't re-trigger
                from backend.strategy.executor import StrategyExecutor
                StrategyExecutor.reset_to_default_settings()
                
                portfolio.sync()
                
                return jsonify({
                    'success': True,
                    'message': success_msg,
                    'settings_reset': True,
                    'order_details': {
                        'symbol': symbol,
                        'quantity': quantity,
                        'price': etf_price,
                        'proceeds': proceeds,
                        'liquidcase_qty': liquidcase_qty,
                        'liquidcase_price': liquidcase_price
                    }
                })
            else:
                logger.error(f"Manual exit swap failed")
                return jsonify({
                    'success': False,
                    'error': 'Swap execution failed'
                }), 500
                
        except Exception as e:
            logger.error(f"Error in manual exit: {e}", exc_info=True)
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
    
    @app.route('/api/manual-buy', methods=['POST'])
    def manual_buy():
        """Manually buy a position - sells LIQUIDCASE and buys ETF."""
        try:
            import math
            from flask import request
            data = request.get_json()
            
            symbol      = data.get('symbol')
            quantity    = data.get('quantity')
            order_type  = (data.get('order_type') or 'MARKET').upper()
            limit_price = data.get('limit_price')   # float or None for LIMIT orders
            if order_type not in ('MARKET', 'LIMIT'):
                order_type = 'MARKET'

            if not symbol or not quantity:
                return jsonify({
                    'success': False,
                    'error': 'Symbol and quantity required'
                }), 400

            # Get required services
            order_manager    = dashboard_state.get('order_manager')
            portfolio        = dashboard_state.get('portfolio_tracker')
            realtime_manager = dashboard_state.get('realtime_manager')

            if not order_manager or not portfolio or not realtime_manager:
                return jsonify({
                    'success': False,
                    'error': 'Services not initialized'
                }), 503

            # Get current prices
            etf_price        = realtime_manager.get_ltp(symbol)
            liquidcase_price = realtime_manager.get_ltp(LIQUIDCASE_SYMBOL)
            
            if not etf_price or not liquidcase_price:
                return jsonify({
                    'success': False,
                    'error': 'Cannot get current prices'
                }), 500
            
            # Calculate LIQUIDCASE quantity to sell.
            # Use ceil() so proceeds always cover the ETF cost.
            # int() (floor) can sell too few units and fail the pre-flight check.
            required_value = quantity * etf_price
            liquidcase_qty = math.ceil(required_value / liquidcase_price)
            
            if liquidcase_qty <= 0:
                return jsonify({
                    'success': False,
                    'error': 'Invalid LIQUIDCASE quantity calculated'
                }), 500
            
            # Check if enough LIQUIDCASE available
            liquidcase_held = portfolio.get_quantity_held(LIQUIDCASE_SYMBOL)
            if liquidcase_qty > liquidcase_held:
                return jsonify({
                    'success': False,
                    'error': f'Insufficient LIQUIDCASE: need {liquidcase_qty}, have {liquidcase_held}'
                }), 400
            
            # Execute atomic swap: SELL LIQUIDCASE -> BUY ETF
            logger.info(f"Manual buy requested: {symbol} x {quantity}")
            logger.info(f"SWAP: Sell {liquidcase_qty} {LIQUIDCASE_SYMBOL} @ ₹{liquidcase_price:.2f} -> Buy {quantity} {symbol} @ ₹{etf_price:.2f}")
            
            # Cash / margin check
            cash_ok, cash_msg = order_manager.check_margin_availability(symbol, quantity, etf_price)
            if not cash_ok:
                return jsonify({'success': False, 'error': f'Insufficient funds: {cash_msg}'}), 400

            order_label = f"{order_type}" + (f" @ ₹{float(limit_price):.2f}" if order_type == 'LIMIT' and limit_price else "")
            logger.info(f"Manual buy: {quantity} {symbol} [{order_label}]")

            # Smart buy: use available cash first, LIQUIDCASE only for shortfall
            realtime = dashboard_state.get('realtime_manager')
            try:
                success = order_manager.smart_buy(
                    buy_symbol=symbol,
                    buy_quantity=quantity,
                    buy_price_estimate=etf_price,
                    realtime_manager=realtime,
                    portfolio_tracker=portfolio,
                    buy_order_type=order_type,
                    buy_limit_price=float(limit_price) if limit_price else None,
                    buy_product='CNC',
                )
            except RuntimeError as ze:
                logger.error(f"Zerodha error in manual buy: {ze}")
                return jsonify({'success': False, 'error': str(ze), 'zerodha_error': True}), 400

            if success:
                order_desc = f"{order_label}" if order_type == 'LIMIT' else "MARKET"
                success_msg = f"Buy complete [{order_desc}]: Sold {liquidcase_qty} {LIQUIDCASE_SYMBOL}, Bought {quantity} {symbol}"
                logger.info(f"✓ {success_msg}")
                
                # Sync portfolio after swap
                portfolio.sync()
                
                return jsonify({
                    'success': True,
                    'message': success_msg,
                    'order_details': {
                        'symbol': symbol,
                        'quantity': quantity,
                        'price': etf_price,
                        'cost': required_value,
                        'liquidcase_qty': liquidcase_qty,
                        'liquidcase_price': liquidcase_price
                    }
                })
            else:
                logger.error(f"Manual buy swap failed")
                return jsonify({
                    'success': False,
                    'error': 'Swap execution failed'
                }), 500
                
        except Exception as e:
            logger.error(f"Error in manual buy: {e}", exc_info=True)
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500
    
    # Module-level cache for cash balance — survives across requests
    # so we can return the last known good value when Zerodha is temporarily 403.
    _cash_balance_cache = {'value': None, 'timestamp': 0, 'stale': False}

    @app.route('/api/cash-balance', methods=['GET'])
    def get_cash_balance():
        """
        Get available funds.

        Kite funds page "Available margin" formula (matches kite.zerodha.com/funds exactly):
            available_margin = opening_balance + collateral + intraday_payin + adhoc_margin - debits

        The API fields map as:
            equity.available.opening_balance  = Opening cash balance
            equity.available.collateral       = Pledged securities margin value
            equity.available.intraday_payin   = Funds added today (not yet settled)
            equity.available.adhoc_margin     = Temporary limit from broker
            equity.utilised.debits            = Margin used by positions
            equity.available.live_balance     = opening_balance + intraday_payin + adhoc_margin - debits
                                                (EXCLUDES collateral — hence differs from Kite funds page)
            equity.available.cash             = opening_balance (confusingly named; same as opening_balance)

        So the correct available_margin = live_balance + collateral
        And available_cash = opening_balance (i.e. equity.available.opening_balance or equity.available.cash)
        """
        import time
        auth_manager = dashboard_state.get('auth_manager')
        cache = _cash_balance_cache   # mutable dict — acts as closure-level state

        if not auth_manager:
            return jsonify({'error': 'Auth manager not initialized'}), 503

        try:
            response = auth_manager.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/user/margins",
                timeout=10
            )

            if response.status_code == 403:
                # Mid-session token expiry — attempt silent re-auth in background
                err_body = response.json() if response.text else {}
                if 'TokenException' in err_body.get('error_type', '') or \
                   'api_key' in err_body.get('message', '').lower():
                    logger.warning("Cash-balance: 403 TokenException — triggering re-auth")
                    if hasattr(auth_manager, 'handle_session_expiry'):
                        auth_manager.handle_session_expiry()
                if cache['value'] is not None:
                    age_min = (time.time() - cache['timestamp']) / 60
                    logger.info(f"Cash-balance: returning cached ₹{cache['value']:.2f} (age {age_min:.1f}m)")
                    return jsonify({
                        'available_margin': cache['value'],
                        'available_cash':   cache.get('cash', cache['value']),
                        'stale':            True,
                        'stale_reason':     'session_expired',
                    })
                return jsonify({'error': 'Session expired — please re-login'}), 403

            if response.status_code != 200:
                logger.error(f"Failed to fetch margins: {response.status_code} — {response.text[:300]}")
                if cache['value'] is not None:
                    return jsonify({'available_cash': cache['value'], 'stale': True})
                return jsonify({'error': 'Failed to fetch margin data'}), 500

            margins_data  = response.json()
            equity_margin = margins_data.get('data', {}).get('equity', {})
            available     = equity_margin.get('available', {})
            utilised      = equity_margin.get('utilised',  {})

            # ── Read every field Zerodha returns ────────────────────────────────
            opening_balance = float(available.get('opening_balance') or 0)
            collateral      = float(available.get('collateral')      or 0)
            intraday_payin  = float(available.get('intraday_payin')  or 0)
            adhoc           = float(available.get('adhoc_margin')    or 0)
            live_balance    = float(available.get('live_balance')    or 0)
            api_cash        = float(available.get('cash')            or 0)   # == opening_balance
            debits          = float(utilised.get('debits', 0)        or 0)

            # Available Cash = live_balance — matches what Kite displays
            available_cash = live_balance if live_balance > 0 else (opening_balance if opening_balance > 0 else api_cash)

            # Available Margin = Total Collateral − Used Margin + Available Cash
            used_margin      = debits
            available_margin = collateral - used_margin + available_cash

            logger.info(
                f"Funds — opening=₹{opening_balance:.2f}  collateral=₹{collateral:.2f}  "
                f"used_margin=₹{used_margin:.2f}  available_cash=₹{available_cash:.2f}  "
                f"→ available_margin=₹{available_margin:.2f} (collateral-used+cash)  "
                f"all_keys={list(available.keys())}"
            )

            # Cache for stale fallback — use correct key names
            cache['value']     = round(available_margin, 2)
            cache['cash']      = round(available_cash,   2)
            cache['timestamp'] = time.time()
            cache['stale']     = False

            return jsonify({
                'available_margin': round(available_margin, 2),
                'available_cash':   round(available_cash,   2),
                'opening_balance':  round(opening_balance,  2),
                'collateral':       round(collateral,       2),
                'live_balance':     round(live_balance,     2),
                'intraday_payin':   round(intraday_payin,   2),
                'adhoc_margin':     round(adhoc,            2),
                'utilised':         round(debits,           2),
                'stale':            False,
            })

        except Exception as e:
            logger.error(f"Error fetching cash balance: {e}", exc_info=True)
            if cache['value'] is not None:
                return jsonify({'available_cash': cache['value'], 'stale': True})
            return jsonify({'error': str(e)}), 500

    @app.route('/api/transactions')
    def get_transactions():
        """Get recent transactions/order history."""
        try:
            auth_manager = dashboard_state.get('auth_manager')
            
            if not auth_manager:
                return jsonify({'error': 'Auth manager not initialized'}), 503
            
            # Fetch orders from Zerodha
            response = auth_manager.session.get(
                f"{Config.ZERODHA_API_BASE}/oms/orders",
                timeout=10
            )
            
            if response.status_code != 200:
                if response.status_code == 403:
                    logger.debug(f"Orders endpoint temporarily unavailable (403) - Zerodha post-trade delay")
                else:
                    logger.warning(f"Failed to fetch orders: {response.status_code}")
                return jsonify([])
            
            orders_data = response.json().get('data', [])
            
            # Get active ETFs from settings
            import json
            settings_path = Path(__file__).parent.parent / 'config' / 'settings.json'
            if settings_path.exists():
                with open(settings_path, 'r') as f:
                    settings = json.load(f)
                    active_etfs = settings.get('active_etfs', ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR'])
            else:
                active_etfs = ['MON100', 'GOLDBEES', 'SILVERBEES', 'JUNIORBEES', 'PSUBNKBEES', 'MINDSPACE-RR', 'EMBASSY-RR']
            
            # Add LIQUIDCASE to monitored symbols
            monitored_symbols = active_etfs + [LIQUIDCASE_SYMBOL]
            
            # Filter and format transactions (only monitored ETFs + LIQUIDCASE, last 50)
            transactions = []
            for order in orders_data:
                symbol = order.get('tradingsymbol', '')
                
                # Only include monitored symbols
                if symbol not in monitored_symbols:
                    continue
                
                # Parse order timestamp
                order_time = order.get('order_timestamp', '')
                exchange_time = order.get('exchange_timestamp', '')
                
                # Use exchange time if available, else order time
                timestamp = exchange_time if exchange_time else order_time
                
                transactions.append({
                    'time': timestamp,
                    'type': order.get('transaction_type', ''),
                    'symbol': symbol,
                    'quantity': order.get('quantity', 0),
                    'price': round(float(order.get('average_price', 0)), 2) if order.get('average_price') else 0,
                    'status': order.get('status', ''),
                    'order_id': order.get('order_id', ''),
                    'filled_quantity': order.get('filled_quantity', 0),
                    'value': round(float(order.get('average_price', 0)) * int(order.get('filled_quantity', 0)), 2) if order.get('average_price') and order.get('filled_quantity') else 0
                })
            
            # Sort by time (most recent first) and limit to 50
            transactions.sort(key=lambda x: x['time'], reverse=True)
            transactions = transactions[:50]
            
            return jsonify(transactions)
            
        except Exception as e:
            logger.error(f"Error fetching transactions: {e}", exc_info=True)
            return jsonify([])
    
    @app.route('/api/buy-liquidcase', methods=['POST'])
    def buy_liquidcase():
        """Buy LIQUIDCASE with available cash."""
        try:
            from flask import request
            data = request.get_json()
            
            amount       = data.get('amount')           # Amount in rupees
            use_all_cash = data.get('use_all_cash', False)
            order_type   = (data.get('order_type') or 'MARKET').upper()
            limit_price  = data.get('limit_price')         # float or None
            if order_type not in ('MARKET', 'LIMIT'):
                order_type = 'MARKET'
            
            # Get required services
            order_manager = dashboard_state.get('order_manager')
            portfolio = dashboard_state.get('portfolio_tracker')
            auth_manager = dashboard_state.get('auth_manager')
            realtime_manager = dashboard_state.get('realtime_manager')
            
            if not all([order_manager, portfolio, auth_manager]):
                return jsonify({
                    'success': False,
                    'error': 'Services not initialized'
                }), 503
            
            # Get current LIQUIDCASE price
            liquidcase_ltp = realtime_manager.get_ltp(LIQUIDCASE_SYMBOL) if realtime_manager else None
            
            if not liquidcase_ltp or liquidcase_ltp <= 0:
                return jsonify({
                    'success': False,
                    'error': 'Unable to fetch LIQUIDCASE price'
                }), 400
            
            # If use_all_cash, fetch available margin (not just cash)
            if use_all_cash:
                response = auth_manager.session.get(
                    f"{Config.ZERODHA_API_BASE}/oms/user/margins",
                    timeout=10
                )
                
                if response.status_code != 200:
                    return jsonify({
                        'success': False,
                        'error': 'Failed to fetch margin data'
                    }), 500
                
                margins_data = response.json()
                equity_margin = margins_data.get('data', {}).get('equity', {})
                
                avail_r       = equity_margin.get('available', {})
                live_margin   = float(avail_r.get('live_balance', 0))
                net_margin    = float(avail_r.get('cash', 0))
                # live_balance is 0 outside market hours — use max of both
                available_margin = max(live_margin, net_margin)

                if available_margin <= 0:
                    return jsonify({
                        'success': False,
                        'error': 'No cash available'
                    }), 400

                # LIQUIDCASE is CNC delivery — no leverage, use 100% of available cash
                amount = available_margin

                # ── Cash reserve: never park the reserve into LIQUIDCASE ──
                cash_reserve = Config.get_cash_reserve()
                amount = max(0.0, amount - cash_reserve)
                if amount <= 0:
                    return jsonify({
                        'success': False,
                        'error': f'Cash after reserve (₹{cash_reserve:.0f}) is insufficient to buy LIQUIDCASE'
                    }), 400

                logger.info(f"Available margin: ₹{available_margin:.2f}, Safe amount for CNC after ₹{cash_reserve:.0f} reserve: ₹{amount:.2f}")
            
            # Validate amount
            if not amount or amount <= 0:
                return jsonify({
                    'success': False,
                    'error': 'Invalid amount'
                }), 400
            
            # Calculate quantity (floor to avoid fractional shares)
            quantity = int(amount / liquidcase_ltp)
            
            if quantity <= 0:
                logger.warning(f"LIQUIDCASE auto-buy skipped: Amount ₹{amount:.2f} too low for 1 unit @ ₹{liquidcase_ltp:.2f}")
                return jsonify({
                    'success': False,
                    'error': f'Amount too low. Need at least ₹{liquidcase_ltp:.2f} for 1 unit'
                }), 400
            
            # Final safety check: ensure we have enough for the calculated quantity
            total_cost = quantity * liquidcase_ltp
            if total_cost > amount:
                quantity -= 1  # Reduce by 1 unit to be safe
                if quantity <= 0:
                    logger.warning(f"LIQUIDCASE auto-buy skipped: Insufficient funds after safety check")
                    return jsonify({
                        'success': False,
                        'error': 'Insufficient funds after calculation'
                    }), 400
            
            # Place buy order for LIQUIDCASE
            logger.info(f"Buying LIQUIDCASE: {quantity} units @ ₹{liquidcase_ltp:.2f} (Total: ₹{quantity * liquidcase_ltp:.2f} from ₹{amount:.2f} available)")
            
            try:
                order_id, message = order_manager.place_order(
                    symbol=LIQUIDCASE_SYMBOL,
                    transaction_type='BUY',
                    quantity=quantity,
                    order_type=order_type,
                    price=float(limit_price) if order_type == 'LIMIT' and limit_price else None,
                )
            except Exception as ze:
                logger.error(f"Zerodha error buying LIQUIDCASE: {ze}")
                return jsonify({'success': False, 'error': str(ze), 'zerodha_error': True}), 400

            if order_id:
                # Verify order status (wait 2 seconds for Zerodha to process)
                import time
                time.sleep(2)
                
                order_status = order_manager.get_order_status(order_id)
                
                if order_status:
                    status = order_status.get('status')
                    status_msg = order_status.get('status_message', '')
                    
                    # Check if order was rejected
                    if status in ['REJECTED', 'CANCELLED']:
                        logger.error(f"LIQUIDCASE order REJECTED by Zerodha: {status_msg}")
                        return jsonify({
                            'success': False,
                            'zerodha_error': True,
                            'error': f'Order rejected: {status_msg}'
                        }), 400
                    elif status == 'COMPLETE':
                        logger.info(f"✅ LIQUIDCASE order COMPLETED: {quantity} units")
                    else:
                        logger.warning(f"LIQUIDCASE order status: {status}")
                
                success_msg = f"LIQUIDCASE buy order placed: {quantity} units (₹{amount:.2f})"
                logger.info(success_msg)
                
                # Sync portfolio after order
                portfolio.sync()
                
                return jsonify({
                    'success': True,
                    'message': success_msg,
                    'order_id': order_id,
                    'quantity': quantity,
                    'amount': round(quantity * liquidcase_ltp, 2)
                })
            else:
                logger.error(f"LIQUIDCASE buy failed: {message}")
                return jsonify({
                    'success': False,
                    'error': message
                }), 500
                
        except Exception as e:
            logger.error(f"Error buying LIQUIDCASE: {e}", exc_info=True)
            return jsonify({
                'success': False,
                'error': str(e)
            }), 500

    @app.route('/api/sell-liquidcase', methods=['POST'])
    def sell_liquidcase():
        """Manually sell LIQUIDCASE units."""
        try:
            from flask import request
            data = request.get_json()

            sell_all    = data.get('sell_all', False)
            quantity    = data.get('quantity')          # int or None
            order_type  = (data.get('order_type') or 'MARKET').upper()
            limit_price = data.get('limit_price')       # float or None
            if order_type not in ('MARKET', 'LIMIT'):
                order_type = 'MARKET'

            order_manager    = dashboard_state.get('order_manager')
            portfolio        = dashboard_state.get('portfolio_tracker')
            realtime_manager = dashboard_state.get('realtime_manager')

            if not all([order_manager, portfolio]):
                return jsonify({'success': False, 'error': 'Services not initialized'}), 503

            # Determine quantity to sell
            if sell_all:
                holdings = portfolio.get_holdings() or {}
                lc = holdings.get(LIQUIDCASE_SYMBOL) or holdings.get('LIQUIDCASE')
                qty_held = int(lc.get('quantity', 0)) if lc else 0
                if qty_held <= 0:
                    return jsonify({'success': False, 'error': 'No LIQUIDCASE units held'}), 400
                quantity = qty_held
            else:
                quantity = int(quantity) if quantity else 0
                if quantity <= 0:
                    return jsonify({'success': False, 'error': 'Invalid quantity'}), 400

            # Get LTP for message
            ltp = realtime_manager.get_ltp(LIQUIDCASE_SYMBOL) if realtime_manager else 0

            logger.info(f"Selling LIQUIDCASE: {quantity} units @ ₹{ltp:.2f} (order_type={order_type})")

            try:
                order_id, message = order_manager.place_order(
                    symbol=LIQUIDCASE_SYMBOL,
                    transaction_type='SELL',
                    quantity=quantity,
                    order_type=order_type,
                    price=float(limit_price) if order_type == 'LIMIT' and limit_price else None,
                )
            except Exception as ze:
                logger.error(f"Zerodha error selling LIQUIDCASE: {ze}")
                return jsonify({'success': False, 'error': str(ze), 'zerodha_error': True}), 400

            if order_id:
                import time
                time.sleep(2)
                order_status = order_manager.get_order_status(order_id)
                if order_status:
                    status = order_status.get('status')
                    status_msg = order_status.get('status_message', '')
                    if status in ['REJECTED', 'CANCELLED']:
                        return jsonify({'success': False, 'zerodha_error': True,
                                        'error': f'Order rejected: {status_msg}'}), 400

                success_msg = f"LIQUIDCASE sell order placed: {quantity} units"
                logger.info(success_msg)
                portfolio.sync()
                return jsonify({
                    'success': True,
                    'message': success_msg,
                    'order_id': order_id,
                    'quantity': quantity,
                    'amount': round(quantity * ltp, 2) if ltp else 0
                })
            else:
                logger.error(f"LIQUIDCASE sell failed: {message}")
                return jsonify({'success': False, 'error': message}), 500

        except Exception as e:
            logger.error(f"Error selling LIQUIDCASE: {e}", exc_info=True)
            return jsonify({'success': False, 'error': str(e)}), 500
