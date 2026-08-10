import os
import sys
import requests
import time
import pandas as pd
from datetime import datetime, timedelta

# ==== SETTINGS ====
# All credentials come from environment variables — when running via GitHub
# Actions, these are injected from repository Secrets (Settings -> Secrets
# and variables -> Actions). Nothing sensitive is hardcoded here because this
# file lives in a public repo.
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_DATA_FEED = "iex"     # "iex" = free real-time feed; "sip" requires a paid subscription
ALPACA_DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"
ALPACA_CLOCK_URL = "https://paper-api.alpaca.markets/v2/clock"  # switch to api.alpaca.markets if you ever go live

TIMEFRAME = "1Hour"          # Alpaca bar size: matches the original CANDLE_INTERVAL="1h"
LOOKBACK_CANDLES = 100
LOOKBACK_DAYS = 30            # calendar days back to request, comfortably covers 100 hourly bars
TOUCH_THRESHOLD = 3
CLUSTER_TOLERANCE = 0.005

SYMBOLS_PER_REQUEST = 100     # tickers batched into a single Alpaca call
SECONDS_BETWEEN_REQUESTS = 1  # spacing between batch calls (free tier allows 200/min)

# ==== TELEGRAM BOT CONFIG ====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

missing = [name for name, val in [
    ("ALPACA_API_KEY", ALPACA_API_KEY),
    ("ALPACA_SECRET_KEY", ALPACA_SECRET_KEY),
    ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
] if not val]
if missing:
    sys.exit(f"Missing required environment variables: {', '.join(missing)}")


# ==== TICKER UNIVERSE (built dynamically each cycle) ====

def get_sp500_tickers():
    """S&P 500 constituents, scraped from Wikipedia's maintained table."""
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    try:
        tables = pd.read_html(url)
        df = tables[0]
        return {str(t).strip().upper() for t in df["Symbol"].tolist()}
    except Exception as e:
        print(f"Failed to fetch S&P 500 list: {e}")
        return set()


def get_nasdaq_composite_tickers():
    """
    All common stocks listed on Nasdaq, via Nasdaq Trader's official daily
    symbol directory (no API key needed). Excludes test issues and ETFs
    (QQQ etc. are pulled in separately below).
    """
    url = "http://ftp.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
    try:
        resp = requests.get(url, timeout=15)
        lines = resp.text.strip().split("\n")
        tickers = set()
        for line in lines[1:-1]:  # first line = header, last line = "File Creation Time..."
            parts = line.split("|")
            if len(parts) < 7:
                continue
            symbol, test_issue, etf = parts[0], parts[3], parts[6]
            if test_issue == "Y" or etf == "Y":
                continue
            # Heuristic: skip symbols that look like preferred shares/units/warrants.
            # Loosen or remove this if you want a broader universe.
            if "$" in symbol:
                continue
            tickers.add(symbol.strip().upper())
        return tickers
    except Exception as e:
        print(f"Failed to fetch Nasdaq Composite list: {e}")
        return set()


def get_nasdaq100_tickers():
    """Nasdaq-100 constituents (what QQQ holds), scraped from Wikipedia."""
    url = "https://en.wikipedia.org/wiki/Nasdaq-100"
    try:
        tables = pd.read_html(url)
        for df in tables:
            col = "Ticker" if "Ticker" in df.columns else ("Symbol" if "Symbol" in df.columns else None)
            if col:
                return {str(t).strip().upper() for t in df[col].tolist()}
        return set()
    except Exception as e:
        print(f"Failed to fetch Nasdaq-100 list: {e}")
        return set()


# Manually specified extras, pulled from the user's TradingView watchlists
# ("Long Term Holds", "Potential Trades", "Long Term Buys Once On Sale", "Market Movers").
# These get unioned in below same as the other sources, so anything already
# covered by S&P 500 / Nasdaq is a no-op.
WATCHLIST_TICKERS = {
    "AAPL", "ACLS", "ACN", "AG", "AGI", "AMAT", "AMRC", "AMZN", "ASML", "AUGO",
    "BA", "BABA", "BIDU", "BULL", "BVN", "BYDDY", "COIN", "EQR", "GFI", "GOOG",
    "HBM", "HCC", "HL", "HOOD", "HWKN", "INVH", "META", "MMM", "MPC", "MSFT",
    "MSTR", "MTX", "NBIS", "NFLX", "NGVT", "NSIT", "NVDA", "NVO", "OLN", "OMF",
    "ONTO", "PLTR", "PSA", "QCOM", "SCCO", "SPY", "TGT", "TSLA", "TSM", "UBER",
    "UNH", "WPM",
}


def build_ticker_universe():
    """Union of all sources, deduplicated so overlapping tickers count once."""
    sp500 = get_sp500_tickers()
    nasdaq = get_nasdaq_composite_tickers()
    nasdaq100 = get_nasdaq100_tickers()
    universe = {t for t in (sp500 | nasdaq | nasdaq100 | WATCHLIST_TICKERS) if t}
    print(
        f"Universe built: {len(sp500)} S&P 500 + {len(nasdaq)} Nasdaq Composite + "
        f"{len(nasdaq100)} Nasdaq-100 + {len(WATCHLIST_TICKERS)} watchlist -> "
        f"{len(universe)} unique tickers"
    )
    return sorted(universe)


# ==== ALERT FUNCTION ====
def send_alert(message):
    print(f"[ALERT] {message}")
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        print(f"Telegram alert failed: {e}")


# ==== FETCH + ANALYZE ====
def alpaca_headers():
    return {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}


def fetch_bars_batch(symbols, start_iso):
    """Fetch recent hourly bars for a batch of symbols in as few calls as possible."""
    bars_by_symbol = {}
    page_token = None
    while True:
        params = {
            "symbols": ",".join(symbols),
            "timeframe": TIMEFRAME,
            "start": start_iso,
            "limit": 10000,
            "feed": ALPACA_DATA_FEED,
            "sort": "asc",
        }
        if page_token:
            params["page_token"] = page_token
        try:
            resp = requests.get(ALPACA_DATA_URL, headers=alpaca_headers(), params=params, timeout=15)
        except Exception as e:
            print(f"Batch request failed: {e}")
            return bars_by_symbol

        if resp.status_code == 429:
            print("Rate limited, backing off for 30s...")
            time.sleep(30)
            continue
        if resp.status_code != 200:
            print(f"Alpaca error {resp.status_code}: {resp.text[:200]}")
            return bars_by_symbol

        data = resp.json()
        for sym, bars in data.get("bars", {}).items():
            bars_by_symbol.setdefault(sym, []).extend(bars)

        page_token = data.get("next_page_token")
        if not page_token:
            break
    return bars_by_symbol


def analyze_bars(ticker, bars):
    if not bars or len(bars) < TOUCH_THRESHOLD:
        return
    highs = [b["h"] for b in bars]  # chronological order (oldest -> newest) since sort=asc
    clusters = {}
    for i, high in enumerate(highs):
        matched = False
        for key in clusters:
            if abs(key - high) / key < CLUSTER_TOLERANCE:
                clusters[key].append(i)
                matched = True
                break
        if not matched:
            clusters[high] = [i]
    major_resistances = [price for price, idxs in clusters.items() if len(idxs) >= TOUCH_THRESHOLD]
    if not major_resistances:
        return
    last = bars[-1]
    open_price = last["o"]
    close_price = last["c"]
    for resistance in major_resistances:
        if open_price < resistance and close_price > resistance:
            send_alert(f"{ticker}: Breakout above resistance at ${resistance:.2f} (close: ${close_price:.2f})")


def chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# ==== SCAN ====
# Runs one full pass and exits. Repetition is handled externally by the
# GitHub Actions schedule (see .github/workflows/scan.yml) rather than an
# internal sleep loop, since each pass now finishes in well under a minute.
def run_scan_once():
    tickers = build_ticker_universe()
    if not tickers:
        print("Universe came back empty this run.")
        return

    start_iso = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    batches = list(chunked(tickers, SYMBOLS_PER_REQUEST))
    print(f"Scanning {len(tickers)} tickers in {len(batches)} batches of up to {SYMBOLS_PER_REQUEST}")

    for i, batch in enumerate(batches, 1):
        bars_by_symbol = fetch_bars_batch(batch, start_iso)
        for ticker in batch:
            bars = bars_by_symbol.get(ticker, [])[-LOOKBACK_CANDLES:]
            analyze_bars(ticker, bars)
        if i % 5 == 0 or i == len(batches):
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Processed batch {i}/{len(batches)}")
        time.sleep(SECONDS_BETWEEN_REQUESTS)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Scan complete.")


def market_is_open():
    """
    Checks Alpaca's live market clock, which correctly accounts for weekends,
    holidays, and early-close days without needing a hardcoded calendar.
    """
    try:
        resp = requests.get(ALPACA_CLOCK_URL, headers=alpaca_headers(), timeout=10)
        if resp.status_code != 200:
            print(f"Could not check market clock ({resp.status_code}: {resp.text[:200]}), skipping this run.")
            return False
        return resp.json().get("is_open", False)
    except Exception as e:
        print(f"Market clock check failed: {e}, skipping this run.")
        return False


if __name__ == "__main__":
    if not market_is_open():
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Market is closed. Skipping this run.")
    else:
        run_scan_once()
