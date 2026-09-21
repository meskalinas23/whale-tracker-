"""
Robinhood Chain New-Coin Scanner + Whale Tracker
Combines Blockscout (token/holder data) and DexScreener (liquidity, pair age,
buy/sell counts) to find small, new, actively-traded tokens while filtering
out likely rugs/dead pairs. Logs every scan to build a historical dataset for
future holder-growth / rug-survival analysis.
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
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"
DEX_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; screener-bot/1.0)"}

# ---- Filters ----
MIN_MARKET_CAP = 100_000
MAX_MARKET_CAP = 50_000_000
MIN_AGE_HOURS = 6
MIN_VOLUME_MC_RATIO = 0.05      # 24h volume must be at least 5% of market cap
MIN_LIQUIDITY_MC_RATIO = 0.05   # liquidity must be at least 5% of market cap
MIN_HOLDERS = 100
TOP_HOLDERS_PER_TOKEN = 5
MAX_TOKENS_TO_ENRICH = 40        # cap how many candidates get the (slower) DexScreener + holders lookups
REQUEST_PAUSE_SEC = 0.2

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

# Manually pinned tokens: always tracked regardless of filters, with your own label.
PINNED_TOKENS = {
    # "0xTOKENADDRESS": "Watching this one",
}

HISTORY_PATH = "robinhood_token_history.csv"
HISTORY_COLUMNS = [
    "timestamp", "symbol", "address", "market_cap", "volume_24h", "liquidity_usd",
    "holders_count", "age_hours", "buys_24h", "sells_24h", "exchange_rate",
]


def classify_token(symbol: str) -> str:
    s = symbol.upper()
    if s in KNOWN_STOCK_TICKERS:
        return "stock"
    if s in KNOWN_MAJORS_AND_STABLES:
        return "major"
    return "memecoin"


def get_all_tokens():
    resp = requests.get(f"{BASE_URL}/tokens/", headers=HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", [])


def enrich_with_dexscreener(token_address):
    """Get liquidity, pair age, and buy/sell counts for a token from DexScreener."""
    try:
        resp = requests.get(
            f"{DEXSCREENER_TOKENS_URL}/{token_address}",
            headers=DEX_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None
        # use the pair with the highest liquidity if multiple exist
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        liquidity_usd = (best.get("liquidity") or {}).get("usd", 0) or 0
        created_at_ms = best.get("pairCreatedAt")
        age_hours = None
        if created_at_ms:
            created_at = dt.datetime.fromtimestamp(created_at_ms / 1000, tz=dt.timezone.utc)
            age_hours = (dt.datetime.now(dt.timezone.utc) - created_at).total_seconds() / 3600
        txns_24h = (best.get("txns") or {}).get("h24") or {}
        return {
            "liquidity_usd": liquidity_usd,
            "age_hours": age_hours,
            "buys_24h": txns_24h.get("buys", 0),
            "sells_24h": txns_24h.get("sells", 0),
        }
    except Exception as e:
        print(f"    DexScreener lookup failed for {token_address}: {e}")
        return None


def get_top_holders(token_address, decimals=18, exchange_rate=0.0, limit=TOP_HOLDERS_PER_TOKEN):
    try:
        resp = requests.get(f"{BASE_URL}/tokens/{token_address}/holders", headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        holders = []
        for h in items[:limit]:
            address = h.get("address", {}).get("hash", "?")
            raw_value = h.get("value", "0")
            try:
                amount = float(raw_value) / (10 ** decimals)
            except (ValueError, TypeError):
                amount = 0.0
            holders.append({"wallet": address, "amount": amount, "value_usd": amount * exchange_rate})
        return holders
    except Exception as e:
        print(f"    could not fetch holders for {token_address}: {e}")
        return []


def run_pipeline():
    """Returns (final_tokens, pipeline_counts) — counts show how many survive each filter stage."""
    counts = {}

    all_items = get_all_tokens()
    counts["total_tokens_seen"] = len(all_items)

    # Stage 1: basic Blockscout-only filters (cheap, no extra API calls)
    stage1 = []
    for t in all_items:
        try:
            market_cap = float(t.get("circulating_market_cap") or 0)
            holders = int(t.get("holders_count") or 0)
            volume_24h = float(t.get("volume_24h") or 0)
        except (TypeError, ValueError):
            continue
        address = t.get("address_hash", "")
        is_pinned = address in PINNED_TOKENS
        if is_pinned:
            pass  # pinned tokens skip the stage-1 filters entirely
        elif not (MIN_MARKET_CAP <= market_cap <= MAX_MARKET_CAP):
            continue
        elif holders < MIN_HOLDERS:
            continue
        try:
            decimals = int(t.get("decimals") or 18)
        except (TypeError, ValueError):
            decimals = 18
        stage1.append({
            "symbol": t.get("symbol", "?"), "name": t.get("name", "?"), "address": address,
            "market_cap": market_cap, "volume_24h": volume_24h, "holders_count": holders,
            "exchange_rate": float(t.get("exchange_rate") or 0), "decimals": decimals,
            "is_pinned": is_pinned, "pinned_label": PINNED_TOKENS.get(address),
        })
    counts["passed_market_cap_and_holders"] = len(stage1)

    # cap how many get the expensive DexScreener + holders lookups
    stage1_capped = stage1[:MAX_TOKENS_TO_ENRICH]
    counts["sent_for_enrichment"] = len(stage1_capped)

    # Stage 2: DexScreener enrichment (liquidity, age, buys/sells)
    stage2 = []
    for t in stage1_capped:
        enrichment = enrich_with_dexscreener(t["address"])
        time.sleep(REQUEST_PAUSE_SEC)
        if enrichment is None:
            if t["is_pinned"]:
                # pinned tokens survive even with no DexScreener data — just blank fields
                t.update({"liquidity_usd": 0, "age_hours": None, "buys_24h": 0, "sells_24h": 0})
                stage2.append(t)
            continue
        t.update(enrichment)
        stage2.append(t)
    counts["found_on_dexscreener"] = len(stage2)

    # Stage 3: age + volume/mc + liquidity/mc ratio filters
    final = []
    for t in stage2:
        if t["is_pinned"]:
            final.append(t)
            continue
        if t["age_hours"] is None or t["age_hours"] < MIN_AGE_HOURS:
            continue
        if t["market_cap"] <= 0:
            continue
        volume_ratio = t["volume_24h"] / t["market_cap"]
        liquidity_ratio = t["liquidity_usd"] / t["market_cap"]
        if volume_ratio < MIN_VOLUME_MC_RATIO:
            continue
        if liquidity_ratio < MIN_LIQUIDITY_MC_RATIO:
            continue
        t["volume_mc_ratio"] = volume_ratio
        t["liquidity_mc_ratio"] = liquidity_ratio
        final.append(t)
    counts["passed_age_and_ratio_filters"] = len(final)

    return final, counts


def save_history(tokens, now):
    rows = []
    for t in tokens:
        rows.append({
            "timestamp": now.isoformat(), "symbol": t["symbol"], "address": t["address"],
            "market_cap": t["market_cap"], "volume_24h": t["volume_24h"],
            "liquidity_usd": t.get("liquidity_usd", 0), "holders_count": t["holders_count"],
            "age_hours": t.get("age_hours"), "buys_24h": t.get("buys_24h", 0),
            "sells_24h": t.get("sells_24h", 0), "exchange_rate": t["exchange_rate"],
        })
    new_df = pd.DataFrame(rows)
    try:
        existing = pd.read_csv(HISTORY_PATH)
        combined = pd.concat([existing, new_df], ignore_index=True)
    except FileNotFoundError:
        combined = new_df
    # keep only the last 30 days to avoid unbounded growth
    if not combined.empty:
        combined["timestamp"] = pd.to_datetime(combined["timestamp"])
        cutoff = now - pd.Timedelta(days=30)
        combined = combined[combined["timestamp"] >= cutoff]
    combined.to_csv(HISTORY_PATH, index=False)


def build_html_report(final_tokens, holders_by_address, counts, now) -> str:
    def render_card(t):
        holders = holders_by_address.get(t["address"], [])
        holder_rows = ""
        for h in holders:
            wallet = h["wallet"]
            short_addr = wallet[:6] + "..." + wallet[-4:] if len(wallet) > 10 else wallet
            explorer_url = f"https://robinhoodchain.blockscout.com/address/{wallet}"
            pct_of_mcap = (h["value_usd"] / t["market_cap"] * 100) if t["market_cap"] > 0 else 0
            holder_rows += (
                f"<li><a href='{explorer_url}' target='_blank'>{short_addr}</a> — "
                f"{h['amount']:,.2f} {t['symbol']} (${h['value_usd']:,.0f}, {pct_of_mcap:.1f}% of mcap)</li>"
            )
        pin_badge = f"<span style='background:#fff3cd;color:#856404;padding:1px 6px;border-radius:3px;font-size:0.8em;margin-left:6px;'>📌 {t['pinned_label']}</span>" if t.get("is_pinned") and t.get("pinned_label") else ""
        age_label = f"{t['age_hours']:.0f}h" if t.get("age_hours") is not None else "—"
        vol_ratio_label = f"{t.get('volume_mc_ratio', 0)*100:.0f}%" if "volume_mc_ratio" in t else "—"
        liq_ratio_label = f"{t.get('liquidity_mc_ratio', 0)*100:.0f}%" if "liquidity_mc_ratio" in t else "—"
        return f"""
        <div class="token-card">
            <h3>{t['symbol']} — {t['name']}{pin_badge}</h3>
            <p>Price: ${t['exchange_rate']:,.6f} &nbsp;|&nbsp; Market Cap: ${t['market_cap']:,.0f} &nbsp;|&nbsp;
               Age: {age_label} &nbsp;|&nbsp; Holders: {t['holders_count']}</p>
            <p>24h Volume: ${t['volume_24h']:,.0f} ({vol_ratio_label} of mcap) &nbsp;|&nbsp;
               Liquidity: ${t.get('liquidity_usd', 0):,.0f} ({liq_ratio_label} of mcap) &nbsp;|&nbsp;
               Buys/Sells 24h: {t.get('buys_24h', 0)}/{t.get('sells_24h', 0)}</p>
            <strong>Top holders:</strong>
            <ul>{holder_rows if holder_rows else '<li>Could not fetch holder data</li>'}</ul>
        </div>
        """

    memecoins = [t for t in final_tokens if classify_token(t["symbol"]) == "memecoin"]
    stocks = [t for t in final_tokens if classify_token(t["symbol"]) == "stock"]
    majors = [t for t in final_tokens if classify_token(t["symbol"]) == "major"]

    memecoin_html = "".join(render_card(t) for t in memecoins) or "<p>No qualifying memecoins this run.</p>"
    stock_html = "".join(render_card(t) for t in stocks) or "<p>No qualifying tokenized stocks this run.</p>"
    major_html = "".join(render_card(t) for t in majors) or "<p>No qualifying majors/stablecoins this run.</p>"

    pipeline_html = "".join(f"<li>{k.replace('_', ' ')}: <strong>{v}</strong></li>" for k, v in counts.items())

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Robinhood Chain Scanner</title>
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
        .pipeline {{ background: #f5f5f5; border-radius: 6px; padding: 12px 18px; font-size: 0.85em; }}
        .pipeline ul {{ list-style: none; padding-left: 0; }}
    </style>
</head>
<body>
    <nav><a href="index.html">Hyperliquid Whales</a> | <strong>Robinhood Chain</strong></nav>
    <h1>Robinhood Chain — New Coin Scanner</h1>
    <p class="updated">Last updated: {now.strftime('%Y-%m-%d %H:%M UTC')}</p>

    <div class="pipeline">
        <strong>Filter pipeline (where coins get eliminated):</strong>
        <ul>{pipeline_html}</ul>
    </div>

    <h2>🚀 Memecoins & New Tokens</h2>
    <p style="color:#666; font-size:0.85em;">MC ${MIN_MARKET_CAP:,.0f}–${MAX_MARKET_CAP:,.0f}, age ≥ {MIN_AGE_HOURS}h,
       volume ≥ {MIN_VOLUME_MC_RATIO*100:.0f}% of mcap, liquidity ≥ {MIN_LIQUIDITY_MC_RATIO*100:.0f}% of mcap,
       {MIN_HOLDERS}+ holders. We don't verify launchpad or vet these — filters only.</p>
    {memecoin_html}

    <h2>📈 Tokenized Stocks (TradFi)</h2>
    {stock_html}

    <h2>🪙 Established Crypto & Stablecoins</h2>
    {major_html}

    <p class="notes">
        Data: Blockscout (holders, market cap) + DexScreener (liquidity, age, buys/sells).
        This is a discovery/filtering tool, not vetted advice — most new tokens fail regardless of passing these filters.
        Not financial advice.
    </p>
</body>
</html>
"""
    return html


def main():
    if not API_KEY:
        print("ERROR: BLOCKSCOUT_API_KEY not set.")
        return

    now = pd.Timestamp.utcnow()
    print("Running filter pipeline...")
    final_tokens, counts = run_pipeline()

    print("\nPipeline results:")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    print(f"\nFetching holders for {len(final_tokens)} qualifying tokens...")
    holders_by_address = {}
    for t in final_tokens:
        holders_by_address[t["address"]] = get_top_holders(t["address"], decimals=t["decimals"], exchange_rate=t["exchange_rate"])
        time.sleep(REQUEST_PAUSE_SEC)

    save_history(final_tokens, now)
    print(f"Updated {HISTORY_PATH}")

    html = build_html_report(final_tokens, holders_by_address, counts, now)
    os.makedirs("docs", exist_ok=True)
    with open("docs/robinhood.html", "w") as f:
        f.write(html)
    print("Wrote docs/robinhood.html")


if __name__ == "__main__":
    main()
