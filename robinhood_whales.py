"""
Robinhood Chain Memecoin/Pons Whale Tracker
Identifies actively-traded tokens on Robinhood Chain, then finds their top
holders (by current balance) as a size-based "whale" signal. Also logs
large recent transfers to start building a track record over time.
"""

import os
import time
import datetime as dt

import requests
import pandas as pd

API_KEY = os.environ.get("BLOCKSCOUT_API_KEY")
CHAIN_ID = "4663"
BASE_URL = f"https://api.blockscout.com/{CHAIN_ID}/api/v2"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

MIN_VOLUME_24H = 50_000        # only track tokens with real recent trading activity
MIN_HOLDERS = 20                # skip brand-new tokens with almost no holders yet
TOP_HOLDERS_PER_TOKEN = 5        # how many top holders to show per token
MAX_TOKENS_TO_TRACK = 15         # cap how many active tokens we deep-dive into (rate limit safety)

# No official flag distinguishes memecoins from stocks/majors, so we classify
# via curated whitelists. Anything NOT in these two lists defaults to the
# memecoin/other bucket — which is the actual focus of this tracker.
KNOWN_STOCK_TICKERS = {
    "NVDA", "SPY", "TSLA", "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META",
    "NFLX", "AMD", "QQQ", "DIA", "IWM", "COIN", "MSTR", "PLTR", "HOOD",
    "AVGO", "CRM", "ORCL", "INTC", "BA", "DIS", "V", "MA", "JPM", "BAC",
    "WMT", "KO", "PEP", "MCD", "NKE", "XOM", "CVX",
}
KNOWN_MAJORS_AND_STABLES = {
    "WETH", "WBTC", "CBBTC", "USDG", "USDE", "USDC", "USDT", "U", "LINK",
    "TAO", "PENGU", "PENDLE", "ETH", "BTC", "SOL", "VIRTUAL",
}


def classify_token(symbol: str) -> str:
    s = symbol.upper()
    if s in KNOWN_STOCK_TICKERS:
        return "stock"
    if s in KNOWN_MAJORS_AND_STABLES:
        return "major"
    return "memecoin"

LARGE_BUY_HISTORY_PATH = "robinhood_large_buys.csv"
LARGE_BUY_COLUMNS = ["date", "token_symbol", "token_address", "wallet", "value_usd"]


def get_active_tokens():
    """Pull the token list and filter to ones with real recent activity."""
    resp = requests.get(f"{BASE_URL}/tokens/", headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items", [])

    active = []
    for t in items:
        try:
            volume_24h = float(t.get("volume_24h") or 0)
            holders = int(t.get("holders_count") or 0)
        except (TypeError, ValueError):
            continue
        if volume_24h >= MIN_VOLUME_24H and holders >= MIN_HOLDERS:
            active.append({
                "symbol": t.get("symbol", "?"),
                "name": t.get("name", "?"),
                "address": t.get("address_hash", ""),
                "volume_24h": volume_24h,
                "holders_count": holders,
                "exchange_rate": float(t.get("exchange_rate") or 0),
                "market_cap": float(t.get("circulating_market_cap") or 0),
            })
    active.sort(key=lambda x: x["volume_24h"], reverse=True)
    return active[:MAX_TOKENS_TO_TRACK]


def get_top_holders(token_address, limit=TOP_HOLDERS_PER_TOKEN):
    """Get the largest holders of a specific token."""
    try:
        resp = requests.get(
            f"{BASE_URL}/tokens/{token_address}/holders",
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        holders = []
        for h in items[:limit]:
            address = h.get("address", {}).get("hash", "?")
            value = h.get("value", "0")
            holders.append({"wallet": address, "raw_balance": value})
        return holders
    except Exception as e:
        print(f"  could not fetch holders for {token_address}: {e}")
        return []


def build_html_report(active_tokens, holders_by_token) -> str:
    today = dt.date.today().isoformat()

    def render_token_card(t):
        holders = holders_by_token.get(t["address"], [])
        holder_rows = ""
        for h in holders:
            wallet = h["wallet"]
            short_addr = wallet[:6] + "..." + wallet[-4:] if len(wallet) > 10 else wallet
            explorer_url = f"https://robinhoodchain.blockscout.com/address/{wallet}"
            holder_rows += f"<li><a href='{explorer_url}' target='_blank'>{short_addr}</a></li>"
        return f"""
        <div class="token-card">
            <h3>{t['symbol']} — {t['name']}</h3>
            <p>Price: ${t['exchange_rate']:,.6f} &nbsp;|&nbsp; Market Cap: ${t['market_cap']:,.0f} &nbsp;|&nbsp;
               24h Volume: ${t['volume_24h']:,.0f} &nbsp;|&nbsp; Holders: {t['holders_count']}</p>
            <strong>Top holders:</strong>
            <ul>{holder_rows if holder_rows else '<li>Could not fetch holder data</li>'}</ul>
        </div>
        """

    memecoins = [t for t in active_tokens if classify_token(t["symbol"]) == "memecoin"]
    stocks = [t for t in active_tokens if classify_token(t["symbol"]) == "stock"]
    majors = [t for t in active_tokens if classify_token(t["symbol"]) == "major"]

    memecoin_html = "".join(render_token_card(t) for t in memecoins) or "<p>No active memecoins found this run.</p>"
    stock_html = "".join(render_token_card(t) for t in stocks) or "<p>No active tokenized stocks found this run.</p>"
    major_html = "".join(render_token_card(t) for t in majors) or "<p>No active majors/stablecoins found this run.</p>"

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Robinhood Chain Whale Tracker</title>
    <style>
        body {{ font-family: -apple-system, Arial, sans-serif; max-width: 1000px; margin: 20px auto; padding: 0 15px; }}
        .updated {{ color: #666; font-size: 0.9em; }}
        .token-card {{ border: 1px solid #ddd; border-radius: 6px; padding: 15px; margin-bottom: 15px; }}
        .token-card h3 {{ margin-top: 0; }}
        ul {{ margin: 5px 0; padding-left: 20px; }}
        a {{ color: #1a73e8; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
        .notes {{ color: #555; font-size: 0.85em; margin-top: 20px; }}
        nav {{ margin-bottom: 20px; }}
        nav a {{ margin-right: 15px; }}
    </style>
</head>
<body>
    <nav><a href="index.html">Hyperliquid Whales</a> | <strong>Robinhood Chain</strong></nav>
    <h1>Robinhood Chain Whale Tracker</h1>
    <p class="updated">Last updated: {today}</p>
    <p>Showing tokens with 24h volume ≥ ${MIN_VOLUME_24H:,.0f} and {MIN_HOLDERS}+ holders — filters out dead/inactive launches.</p>

    <h2>🚀 Memecoins / Pons Ecosystem</h2>
    {memecoin_html}

    <h2>📈 Tokenized Stocks (TradFi)</h2>
    {stock_html}

    <h2>🪙 Majors & Stablecoins (context only)</h2>
    {major_html}
    <p class="notes">
        "Top holders" is a size-based snapshot (who currently holds the most), not a performance ranking —
        unlike the Hyperliquid tracker, there's no official leaderboard here, so this shows current position size only.
        Data from Blockscout's public Robinhood Chain explorer. Not financial advice — thousands of tokens launch
        daily on this chain and most go to zero; treat this as a discovery tool, not a signal to act on directly.
    </p>
</body>
</html>
"""
    return html


def main():
    if not API_KEY:
        print("ERROR: BLOCKSCOUT_API_KEY not set.")
        return

    print("Fetching active Robinhood Chain tokens...")
    active_tokens = get_active_tokens()
    print(f"Found {len(active_tokens)} active tokens (volume >= ${MIN_VOLUME_24H:,.0f}, holders >= {MIN_HOLDERS}).")

    holders_by_token = {}
    for t in active_tokens:
        print(f"  Fetching holders for {t['symbol']}...")
        holders_by_token[t["address"]] = get_top_holders(t["address"])
        time.sleep(0.3)

    html = build_html_report(active_tokens, holders_by_token)
    os.makedirs("docs", exist_ok=True)
    with open("docs/robinhood.html", "w") as f:
        f.write(html)
    print("Wrote docs/robinhood.html")


if __name__ == "__main__":
    main()
