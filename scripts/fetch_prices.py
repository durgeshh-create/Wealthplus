#!/usr/bin/env python3
"""
Fetches current NIFTY 50, India VIX, and NIFTY MID SELECT (MIDCPNIFTY proxy)
prices from Yahoo Finance's public chart endpoint and writes them to
./prices.json in the current directory (the workflow then copies this onto
the gh-pages branch as data/prices.json).

This runs server-side (inside a GitHub Actions runner), so there is no CORS
restriction to work around -- CORS only applies to requests made from a
browser. That's the whole reason this script exists: it replaces the old
approach of scraping these prices from the browser through third-party CORS
proxies, which was unreliable.
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone

YAHOO_SYMBOLS = {
    "nifty": "%5ENSEI",             # ^NSEI = NIFTY 50
    "vix": "%5EINDIAVIX",           # ^INDIAVIX = India VIX
    "midcp": "NIFTY_MID_SELECT.NS", # NIFTY MID SELECT (MIDCPNIFTY proxy)
}

HEADERS = {
    # Yahoo's endpoint blocks requests with no browser-like User-Agent.
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}


def fetch_price(symbol: str) -> float:
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?interval=1d&range=1d"
    )
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    result = data["chart"]["result"][0]
    meta = result["meta"]
    price = meta.get("regularMarketPrice", meta.get("previousClose"))
    if price is None:
        raise ValueError(f"No price found in Yahoo response for {symbol}")
    return float(price)


def main() -> int:
    prices = {}
    errors = {}

    for key, symbol in YAHOO_SYMBOLS.items():
        try:
            prices[key] = fetch_price(symbol)
        except Exception as e:  # noqa: BLE001
            errors[key] = str(e)

    if errors:
        # Fail the whole run rather than writing a partial/incomplete file.
        # The workflow simply won't push anything this cycle, and
        # data/prices.json on gh-pages stays at its last good value.
        print("Failed to fetch some prices:", errors, file=sys.stderr)
        return 1

    output = {
        "nifty": prices["nifty"],
        "vix": prices["vix"],
        "midcp": prices["midcp"],
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    with open("prices.json", "w") as f:
        json.dump(output, f, indent=2)
        f.write("\n")

    print("Wrote prices.json:", output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
