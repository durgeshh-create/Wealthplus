#!/usr/bin/env python3
"""
seed_marketwatch_csvs.py — Standalone CSV seeder for Marketwatch-only symbols.

Unlike seed_csvs.py (which only seeds symbols already present in
config/instrument_tokens.json — itself rebuilt from active_etfs/bnh_symbols
in config/settings.json, i.e. the bot's live trading universe), this script
seeds CSVs for whatever symbols are saved in the Scanner page's Marketwatch
list (settings-editor/marketwatch.json), regardless of whether the bot
trades them.

This is intentionally read-only with respect to trading config: it NEVER
touches active_etfs, bnh_symbols, or instrument_tokens.json, so adding a
symbol to Marketwatch can never change what the bot buys or sells.

Reads enctoken from config/enctoken.json (same session the bot already
authenticates with this run), resolves each Marketwatch symbol's Zerodha
instrument token fresh from the public instruments dump, and writes daily
OHLC CSVs to data/daily/ — the same folder the Scanner/Marketwatch panel
reads from.

Run from the account directory (RD1858/).
"""

import json
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO

import requests

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT_DIR  = Path(__file__).parent
CONFIG_DIR   = ACCOUNT_DIR / 'config'
# Same shared CSV store the Scanner/Marketwatch panel reads from.
REPO_ROOT    = Path(__file__).parent.parent   # up from RD1858/
DAILY_DIR    = REPO_ROOT / 'RD1858' / 'data' / 'daily'
ENCTOKEN_FILE = CONFIG_DIR / 'enctoken.json'
# Marketwatch list maintained by the Scanner page in settings-editor/index.html.
MARKETWATCH_FILE = REPO_ROOT / 'settings-editor' / 'marketwatch.json'

API_BASE        = 'https://kite.zerodha.com/oms'
INSTRUMENTS_URL = 'https://api.kite.trade/instruments'

FETCH_DAYS         = 400   # days of history to download
MAX_WORKERS         = 3     # Zerodha rate limit ~3 req/s
STALE_DAYS          = 1     # re-download if last candle is older than this
MAX_RETRIES         = 4     # retries on HTTP 429 before giving up on a symbol
RETRY_BACKOFF_BASE  = 2.0   # seconds; doubles each retry (2s, 4s, 8s, 16s)
REQUEST_PACING_SEC  = 0.6   # pause between completed requests

IST = timezone(timedelta(hours=5, minutes=30))


# ── Load enctoken ─────────────────────────────────────────────────────────────
def load_enctoken():
    try:
        data = json.loads(ENCTOKEN_FILE.read_text())
        return data.get('enctoken')
    except Exception as e:
        print(f'[mw-seed] ERROR: Could not read enctoken: {e}')
        sys.exit(1)


# ── Build authenticated session ───────────────────────────────────────────────
def make_session(enctoken):
    s = requests.Session()
    s.headers.update({
        'Authorization': f'enctoken {enctoken}',
        'User-Agent':    'Mozilla/5.0',
        'Referer':       'https://kite.zerodha.com/',
    })
    return s


# ── Load Marketwatch symbol list ──────────────────────────────────────────────
def load_marketwatch_symbols():
    if not MARKETWATCH_FILE.exists():
        print(f'[mw-seed] {MARKETWATCH_FILE} not found — nothing to seed')
        return []
    try:
        data = json.loads(MARKETWATCH_FILE.read_text())
        symbols = data if isinstance(data, list) else data.get('symbols', [])
        # De-dupe, preserve order, drop anything obviously malformed.
        seen, out = set(), []
        for sym in symbols:
            if isinstance(sym, str) and sym and sym not in seen:
                seen.add(sym)
                out.append(sym)
        return out
    except Exception as e:
        print(f'[mw-seed] ERROR: Could not read {MARKETWATCH_FILE}: {e}')
        return []


# ── Fetch instrument tokens ────────────────────────────────────────────────────
def fetch_instrument_tokens(session):
    print('[mw-seed] Fetching instrument list from Zerodha…')
    import pandas as pd
    for url, use_auth in [
        (INSTRUMENTS_URL, False),
        (f'{API_BASE}/instruments/NSE', True),
    ]:
        try:
            r = (session if use_auth else requests.Session()).get(url, timeout=15)
            if r.status_code == 200 and r.text.strip():
                df = pd.read_csv(StringIO(r.text), low_memory=False)
                df.columns = [c.lower() for c in df.columns]
                print(f'[mw-seed] Got {len(df)} instruments from {url}')
                return df
        except Exception as e:
            print(f'[mw-seed] Instruments fetch {url}: {e}')
    print('[mw-seed] ERROR: Could not fetch instrument list')
    sys.exit(1)


def resolve_token(df_inst, sym):
    for seg in ['NSE-EQ', 'NSE', 'BSE-EQ', 'BSE']:
        m = df_inst[(df_inst['tradingsymbol'] == sym) & (df_inst['segment'] == seg)]
        if not m.empty:
            return str(int(m.iloc[0]['instrument_token']))
    m = df_inst[df_inst['tradingsymbol'] == sym]
    if not m.empty:
        return str(int(m.iloc[0]['instrument_token']))
    return None


# ── Check if CSV is stale ─────────────────────────────────────────────────────
def is_stale(csv_path):
    if not csv_path.exists():
        return True
    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        if 'date' not in df.columns or df.empty:
            return True
        last = pd.to_datetime(df['date']).max().date()
        today = datetime.now(IST).date()
        return (today - last).days > STALE_DAYS
    except Exception:
        return True


# ── Download and save one symbol ──────────────────────────────────────────────
def fetch_and_save(sym, token, session):
    import pandas as pd
    csv_path = DAILY_DIR / f'{sym}.csv'

    today      = datetime.now(IST).date()
    fetch_from = (today - timedelta(days=FETCH_DAYS)).strftime('%Y-%m-%d')
    fetch_to   = (today - timedelta(days=1)).strftime('%Y-%m-%d')

    try:
        url = f'{API_BASE}/instruments/historical/{token}/day'
        r   = None
        for attempt in range(MAX_RETRIES + 1):
            r = session.get(url, params={
                'from': fetch_from, 'to': fetch_to, 'continuous': 0, 'oi': 1
            }, timeout=30)
            if r.status_code != 429:
                break
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_BASE * (2 ** attempt))

        if r.status_code != 200:
            return sym, f'http_{r.status_code}'

        candles = r.json().get('data', {}).get('candles', [])
        if not candles:
            return sym, 'no_candles'

        df = pd.DataFrame(candles,
                          columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        if df['timestamp'].dt.tz is not None:
            df['timestamp'] = df['timestamp'].dt.tz_localize(None)
        df = df.sort_values('timestamp').reset_index(drop=True)

        # Merge with existing CSV if present
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
                pass  # corrupt old CSV — use fresh only

        save = df.copy()
        save.insert(0, 'date', save['timestamp'].dt.strftime('%Y-%m-%d'))
        save = save.drop(columns=['timestamp'])
        DAILY_DIR.mkdir(parents=True, exist_ok=True)
        save.to_csv(csv_path, index=False)
        return sym, f'ok:{len(df)}'

    except Exception as e:
        return sym, f'err:{e}'


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f'\n{"="*60}')
    print(f'  Marketwatch CSV Seeder — {datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")}')
    print(f'  (read-only w.r.t. trading config — never touches active_etfs/bnh_symbols)')
    print(f'{"="*60}\n')

    symbols = load_marketwatch_symbols()
    if not symbols:
        print('[mw-seed] No Marketwatch symbols to seed — exiting')
        return
    print(f'[mw-seed] Marketwatch symbols: {", ".join(symbols)}')

    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    enctoken = load_enctoken()
    session  = make_session(enctoken)

    # Validate token
    try:
        r = session.get(f'{API_BASE}/user/profile', timeout=8)
        if r.status_code != 200:
            print(f'[mw-seed] ERROR: Token invalid (HTTP {r.status_code}) — skipping seed')
            sys.exit(0)  # exit 0 so workflow continues
        user = r.json().get('data', {}).get('user_name', 'unknown')
        print(f'[mw-seed] Authenticated as: {user}')
    except Exception as e:
        print(f'[mw-seed] ERROR: Auth check failed: {e} — skipping seed')
        sys.exit(0)

    # Filter to only stale/missing symbols
    stale = [s for s in symbols if is_stale(DAILY_DIR / f'{s}.csv')]
    fresh = len(symbols) - len(stale)
    print(f'[mw-seed] Total: {len(symbols)} | Fresh: {fresh} | Stale/missing: {len(stale)}')

    if not stale:
        print('[mw-seed] All Marketwatch CSVs are up to date — nothing to download ✅')
        return

    # Resolve tokens
    df_inst  = fetch_instrument_tokens(session)
    token_map, no_token = {}, []
    for sym in stale:
        tok = resolve_token(df_inst, sym)
        if tok:
            token_map[sym] = tok
        else:
            no_token.append(sym)

    if no_token:
        print(f'[mw-seed] {len(no_token)} symbol(s) not found in Zerodha instruments: {", ".join(no_token)}')
        print(f'[mw-seed] Double-check the exact NSE/Zerodha trading symbol for these.')

    to_download = list(token_map.items())
    if not to_download:
        print('[mw-seed] Nothing resolvable to download.')
        return

    print(f'[mw-seed] Downloading {len(to_download)} CSV(s) ({MAX_WORKERS} workers, rate-limited)…\n')

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_and_save, sym, tok, session): sym
                   for sym, tok in to_download}
        for i, fut in enumerate(as_completed(futures), 1):
            sym, result = fut.result()
            if result.startswith('ok:'):
                ok += 1
                candles = result.split(':')[1]
                print(f'  ✓ [{i:>3}/{len(to_download)}] {sym:<20} {candles} candles')
            else:
                failed += 1
                print(f'  ✗ [{i:>3}/{len(to_download)}] {sym:<20} {result}')
            time.sleep(REQUEST_PACING_SEC)

    print(f'\n[mw-seed] Done — ✓ {ok} downloaded, ✗ {failed} failed, ⏭ {len(no_token)} not found')
    print(f'[mw-seed] CSVs saved to: {DAILY_DIR}\n')


if __name__ == '__main__':
    main()
