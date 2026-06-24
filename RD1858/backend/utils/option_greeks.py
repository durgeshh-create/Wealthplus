"""
option_greeks.py — Shared NFO option-symbol parsing and delta enrichment.

Used by both frontend/routes.py (the live /api/positions endpoint) and
backend/utils/snapshot.py (the periodic status_*.json the static
settings-editor/index.html dashboard actually reads). Centralized here so
both call sites share one correct symbol parser and one short-lived
option-chain cache, instead of duplicating (and risking re-diverging) the
same logic twice.

WHY THIS EXISTS
----------------
The dashboard previously tried to fetch option deltas directly from
api.kite.trade in the browser (see the old _fetchOptionDeltas in
index.html). Zerodha's API does not send CORS headers and rejects
browser-origin requests outright, regardless of login state — so that
call always silently failed. This module does the same fetch server-side,
where it actually works, using the bot's already-authenticated Kite
session.
"""

import re
import time

from backend.core.config import Config
from backend.utils.logger import get_logger

logger = get_logger(__name__)

# Weekly/monthly NFO option tradingsymbol grammar (Kite/NSE convention):
#   <UNDERLYING><YY><M><DD><strike><CE|PE>
# M is a SINGLE character month code: 1-9 for Jan-Sep, O/N/D for Oct/Nov/Dec.
# Anchoring DD and the single-char month explicitly (rather than a generic
# \d+ for the whole date block) is what makes this reliable — a naive
# \d{2,} for the date+strike block is ambiguous between where the date
# ends and the strike begins, since both are pure digits.
_OPT_SYM_RE = re.compile(r'^([A-Z]+)(\d{2})([1-9OND])(\d{2})(\d+)(CE|PE)$')
_MONTH_CODE_TO_NUM = {c: i + 1 for i, c in enumerate("123456789")}
_MONTH_CODE_TO_NUM.update({'O': 10, 'N': 11, 'D': 12})
_MONTHS_3LETTER = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

# Short-lived in-process cache so repeated polls (the dashboard refreshes
# every 30s, the snapshot writer roughly every 60s) don't re-hit Kite's
# option-chain endpoint for the same strike/expiry on every single call.
_option_chain_cache = {}   # (underlying, expiry_str) -> (fetched_at, chain_data)
_OPTION_CHAIN_TTL_SEC = 20


def parse_option_symbol(sym):
    """Parse an NFO tradingsymbol like NIFTY2662322200PE into its parts.
    Returns None if it doesn't match the expected grammar (e.g. it's an
    equity/ETF symbol, not an option)."""
    m = _OPT_SYM_RE.match(sym)
    if not m:
        return None
    underlying, yy, month_code, dd, strike, opt_type = m.groups()
    month_num = _MONTH_CODE_TO_NUM.get(month_code)
    if not month_num:
        return None
    return {
        'underlying': underlying,
        'expiry_str': f"{dd}{_MONTHS_3LETTER[month_num-1]}{yy}",  # e.g. "23JUN26"
        'strike': int(strike),
        'opt_type': opt_type,
    }


def fetch_option_chain(auth, underlying, expiry_str):
    """Fetch (and cache for _OPTION_CHAIN_TTL_SEC) the option chain for a
    given underlying + expiry via Kite's authenticated REST API."""
    cache_key = (underlying, expiry_str)
    cached = _option_chain_cache.get(cache_key)
    if cached and (time.time() - cached[0]) < _OPTION_CHAIN_TTL_SEC:
        return cached[1]
    try:
        r = auth.session.get(
            f"{Config.KITE_API_BASE}/oi/chain/{underlying}",
            params={'expiry': expiry_str},
            timeout=8,
        )
        if r.status_code == 200:
            chain = r.json().get('data', [])
            _option_chain_cache[cache_key] = (time.time(), chain)
            return chain
    except Exception as e:
        logger.debug(f"fetch_option_chain({underlying}, {expiry_str}): {e}")
    return None


def get_delta_for_symbol(auth, sym):
    """Returns the option delta (float) for a tradingsymbol, or None if it
    isn't an option, the chain fetch fails, or the strike isn't found.
    Never raises — always safe to call from a hot rendering/snapshot path."""
    if not auth:
        return None
    info = parse_option_symbol(sym)
    if not info:
        return None
    chain = fetch_option_chain(auth, info['underlying'], info['expiry_str'])
    if not chain:
        return None
    entry = next((e for e in chain if e.get('strike_price') == info['strike']), None)
    if not entry:
        return None
    leg_key = 'call_options' if info['opt_type'] == 'CE' else 'put_options'
    leg = entry.get(leg_key) or {}
    delta = (leg.get('greeks') or {}).get('delta')
    if delta is None:
        delta = (leg.get('option_chain') or {}).get('delta')
    return float(delta) if delta is not None else None


def enrich_rows_with_delta(rows, auth, symbol_key='symbol', qty_key='quantity'):
    """Mutates a list of dict rows in place, adding 'delta' and
    'delta_notional' to any row whose symbol_key value parses as an NFO
    option. Best-effort: leaves delta as None on any failure."""
    if not auth:
        for row in rows:
            row.setdefault('delta', None)
        return rows
    for row in rows:
        sym = row.get(symbol_key, '')
        delta = get_delta_for_symbol(auth, sym)
        row['delta'] = round(delta, 4) if delta is not None else None
        if delta is not None and row.get(qty_key) is not None:
            row['delta_notional'] = round(delta * row[qty_key], 2)
    return rows
