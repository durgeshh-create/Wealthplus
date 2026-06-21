#!/usr/bin/env python3
"""
seed_csvs.py — Standalone CSV seeder for GitHub Actions
Reads enctoken from config/enctoken.json, downloads daily OHLC CSVs
from Zerodha for all symbols, and saves them to data/daily/.
Only downloads symbols that are missing or stale (last candle > 1 day old).
Run from the account directory (RD1858/ or PS5673/).
"""

import json
import sys
import time
import os
import requests
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import StringIO

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT_DIR  = Path(__file__).parent
CONFIG_DIR   = ACCOUNT_DIR / 'config'
# Always use RD1858/data/daily as the shared CSV store
REPO_ROOT    = Path(__file__).parent.parent  # up from RD1858/
DAILY_DIR    = REPO_ROOT / 'RD1858' / 'data' / 'daily'
ENCTOKEN_FILE = CONFIG_DIR / 'enctoken.json'

API_BASE     = 'https://kite.zerodha.com/oms'
INSTRUMENTS_URL = 'https://api.kite.trade/instruments'

FETCH_DAYS   = 400   # days of history to download
MAX_WORKERS  = 3     # Zerodha rate limit ~3 req/s
STALE_DAYS   = 1     # re-download if last candle is older than this
MAX_RETRIES  = 4      # retries on HTTP 429 before giving up on a symbol
RETRY_BACKOFF_BASE = 2.0   # seconds; doubles each retry (2s, 4s, 8s, 16s)
REQUEST_PACING_SEC = 0.6   # pause between completed requests (was 0.35 — too aggressive, caused cascading 429s)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Load enctoken ─────────────────────────────────────────────────────────────
def load_enctoken():
    try:
        data = json.loads(ENCTOKEN_FILE.read_text())
        return data.get('enctoken')
    except Exception as e:
        print(f'[seed] ERROR: Could not read enctoken: {e}')
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

# ── Fetch instrument tokens ────────────────────────────────────────────────────
def fetch_instrument_tokens(session):
    print('[seed] Fetching instrument list from Zerodha…')
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
                print(f'[seed] Got {len(df)} instruments from {url}')
                return df
        except Exception as e:
            print(f'[seed] Instruments fetch {url}: {e}')
    print('[seed] ERROR: Could not fetch instrument list')
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
                          columns=['timestamp','open','high','low','close','volume','oi'])
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
    import pandas as pd

    print(f'\n{"="*60}')
    print(f'  WealthAlgo CSV Seeder — {datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")}')
    print(f'  Account: {ACCOUNT_DIR.name}')
    print(f'{"="*60}\n')

    DAILY_DIR.mkdir(parents=True, exist_ok=True)

    enctoken = load_enctoken()
    session  = make_session(enctoken)

    # Validate token
    try:
        r = session.get(f'{API_BASE}/user/profile', timeout=8)
        if r.status_code != 200:
            print(f'[seed] ERROR: Token invalid (HTTP {r.status_code}) — skipping seed')
            sys.exit(0)  # exit 0 so workflow continues
        user = r.json().get('data', {}).get('user_name', 'unknown')
        print(f'[seed] Authenticated as: {user}')
    except Exception as e:
        print(f'[seed] ERROR: Auth check failed: {e} — skipping seed')
        sys.exit(0)

    # Get all symbols from existing CSVs + instrument_tokens.json
    symbols = set()
    for f in DAILY_DIR.glob('*.csv'):
        symbols.add(f.stem)

    # Also add symbols from instrument_tokens.json
    tok_file = CONFIG_DIR / 'instrument_tokens.json'
    if tok_file.exists():
        try:
            data = json.loads(tok_file.read_text())
            for info in data.get('instrument_tokens', {}).values():
                sym = info.get('symbol', '')
                if sym and ' ' not in sym:  # skip indices like "NIFTY 50"
                    symbols.add(sym)
        except Exception:
            pass

    # Filter to only stale symbols
    stale = [s for s in sorted(symbols) if is_stale(DAILY_DIR / f'{s}.csv')]
    fresh = len(symbols) - len(stale)

    print(f'[seed] Total symbols: {len(symbols)} | Fresh: {fresh} | Stale/missing: {len(stale)}')

    if not stale:
        print('[seed] All CSVs are up to date — nothing to download ✅')
        return

    # Fetch instrument tokens
    df_inst = fetch_instrument_tokens(session)

    # Resolve tokens for stale symbols
    token_map = {}
    no_token  = []
    for sym in stale:
        tok = resolve_token(df_inst, sym)
        if tok:
            token_map[sym] = tok
        else:
            no_token.append(sym)

    if no_token:
        print(f'[seed] {len(no_token)} symbols not found in Zerodha instruments: {", ".join(no_token[:10])}{"…" if len(no_token)>10 else ""}')

    to_download = list(token_map.items())
    print(f'[seed] Downloading {len(to_download)} CSVs (3 workers, rate-limited)…\n')

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_and_save, sym, tok, session): sym
                   for sym, tok in to_download}
        for i, fut in enumerate(as_completed(futures), 1):
            sym, result = fut.result()
            if result.startswith('ok:'):
                ok += 1
                candles = result.split(':')[1]
                print(f'  ✓ [{i:>4}/{len(to_download)}] {sym:<20} {candles} candles')
            else:
                failed += 1
                print(f'  ✗ [{i:>4}/{len(to_download)}] {sym:<20} {result}')
            # Pace requests to respect Zerodha's rate limit. With MAX_WORKERS
            # concurrent threads, this sleep happens once per completed
            # request, so the *effective* rate is roughly
            # MAX_WORKERS / REQUEST_PACING_SEC requests/sec.
            time.sleep(REQUEST_PACING_SEC)

    print(f'\n[seed] Done — ✓ {ok} downloaded, ✗ {failed} failed, ⏭ {len(no_token)} not found')
    print(f'[seed] CSVs saved to: {DAILY_DIR}\n')

if __name__ == '__main__':
    main()
