"""
Hyperliquid Whale Tracker
Pulls the public leaderboard, filters down to a shortlist of "quality" wallets
by size/ROI/volume, stores a daily historical snapshot of that shortlist, and
publishes an HTML report (Quality Whales + Active Whales) for GitHub Pages.
"""

import requests
import pandas as pd
import datetime as dt
import os
import time

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; whale-tracker/1.0)"}

# ---- Filtering thresholds ----
MIN_ACCOUNT_VALUE = 250_000       # smaller tier floor — catches skilled smaller traders too
MIN_ACCOUNT_VALUE_MAIN_TIER = 1_000_000
MIN_30D_ROI = 0.10                # 10%+ over 30 days to be considered
MIN_30D_VOLUME = 1_000_000        # skip near-inactive accounts
SHORTLIST_SIZE = 200              # how many wallets we keep and track historically
QUALITY_TIER_SIZE = 30            # top N of the shortlist = "Quality Whales"

HISTORY_PATH = "whale_history.csv"
HISTORY_COLUMNS = [
    "date", "wallet", "account_value",
    "day_pnl", "day_roi", "week_pnl", "week_roi",
    "month_pnl", "month_roi", "month_volume",
]


def fetch_leaderboard():
    resp = requests.get(LEADERBOARD_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("leaderboardRows", data if isinstance(data, list) else [])
    return rows


def parse_row(row):
    """Extract the fields we care about from one leaderboard row."""
    perf = dict(row.get("windowPerformances", []))
    try:
        account_value = float(row.get("accountValue", 0))
        day = perf.get("day", {})
        week = perf.get("week", {})
        month = perf.get("month", {})
        return {
            "wallet": row.get("ethAddress", ""),
            "account_value": account_value,
            "day_pnl": float(day.get("pnl", 0)),
            "day_roi": float(day.get("roi", 0)),
            "week_pnl": float(week.get("pnl", 0)),
            "week_roi": float(week.get("roi", 0)),
            "month_pnl": float(month.get("pnl", 0)),
            "month_roi": float(month.get("roi", 0)),
            "month_volume": float(month.get("vlm", 0)),
        }
    except (ValueError, TypeError):
        return None


def build_shortlist(rows):
    parsed = [p for p in (parse_row(r) for r in rows) if p is not None]
    df = pd.DataFrame(parsed)

    # apply filters
    df = df[df["account_value"] >= MIN_ACCOUNT_VALUE]
    df = df[df["month_roi"] >= MIN_30D_ROI]
    df = df[df["month_volume"] >= MIN_30D_VOLUME]

    # rank: bigger accounts get a small boost, but ROI is the main driver
    # (simple starting formula — refine once we have enough history to backtest)
    df["score"] = df["month_roi"] * 0.6 + (df["week_roi"] * 0.3) + (df["day_roi"] * 0.1)
    df = df.sort_values("score", ascending=False)

    shortlist = df.head(SHORTLIST_SIZE).copy()
    return shortlist


def update_history(shortlist: pd.DataFrame):
    today = dt.date.today().isoformat()

    try:
        history = pd.read_csv(HISTORY_PATH)
    except FileNotFoundError:
        history = pd.DataFrame(columns=HISTORY_COLUMNS)

    # drop any existing rows for today (in case of a re-run) then append fresh
    history = history[history["date"] != today]

    new_rows = shortlist.copy()
    new_rows.insert(0, "date", today)
    new_rows = new_rows[HISTORY_COLUMNS]

    history = pd.concat([history, new_rows], ignore_index=True)
    history.to_csv(HISTORY_PATH, index=False)
    return history


def fetch_positions(wallet: str):
    """Get a wallet's current open perp positions from Hyperliquid's official API."""
    try:
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "clearinghouseState", "user": wallet},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        positions = []
        for p in data.get("assetPositions", []):
            pos = p.get("position", {})
            szi = float(pos.get("szi", 0))
            if szi == 0:
                continue
            positions.append({
                "coin": pos.get("coin", "?"),
                "side": "LONG" if szi > 0 else "SHORT",
                "size_usd": abs(szi) * float(pos.get("entryPx", 0)),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
            })
        return positions
    except Exception:
        return []


def positions_summary_html(positions):
    if not positions:
        return "<span style='color:#999'>no open positions / unavailable</span>"
    positions = sorted(positions, key=lambda p: p["size_usd"], reverse=True)[:3]
    parts = []
    for p in positions:
        color = "#0a7d2c" if p["side"] == "LONG" else "#c0392b"
        parts.append(f"<span style='color:{color}'>{p['side']} {p['coin']} ${p['size_usd']:,.0f}</span>")
    return " · ".join(parts)


def build_html_report(shortlist: pd.DataFrame) -> str:
    today = dt.date.today().isoformat()

    quality = shortlist.head(QUALITY_TIER_SIZE)
    active = shortlist.sort_values("day_pnl", ascending=False).head(QUALITY_TIER_SIZE)

    # only fetch live positions for wallets we'll actually display (dedup across both tiers)
    wallets_to_check = pd.concat([quality["wallet"], active["wallet"]]).unique()
    print(f"Fetching live positions for {len(wallets_to_check)} wallets...")
    positions_by_wallet = {}
    for w in wallets_to_check:
        positions_by_wallet[w] = fetch_positions(w)
        time.sleep(0.15)

    def table_rows(df):
        rows = ""
        for _, w in df.iterrows():
            wallet = w["wallet"]
            short_addr = wallet[:6] + "..." + wallet[-4:]
            explorer_url = f"https://app.hyperliquid.xyz/explorer/address/{wallet}"
            positions_html = positions_summary_html(positions_by_wallet.get(wallet, []))
            rows += f"""
            <tr>
                <td><a href="{explorer_url}" target="_blank">{short_addr}</a></td>
                <td>${w['account_value']:,.0f}</td>
                <td>{w['day_roi']*100:.1f}%</td>
                <td>{w['week_roi']*100:.1f}%</td>
                <td>{w['month_roi']*100:.1f}%</td>
                <td>${w['month_volume']:,.0f}</td>
                <td>{positions_html}</td>
            </tr>
            """
        return rows

    quality_html = table_rows(quality)
    active_html = table_rows(active)

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Hyperliquid Whale Tracker</title>
    <style>
        body {{ font-family: -apple-system, Arial, sans-serif; max-width: 1200px; margin: 20px auto; padding: 0 15px; }}
        table {{ border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 0.85em; }}
        th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
        th {{ background: #f5f5f5; }}
        .updated {{ color: #666; font-size: 0.9em; }}
        h2 {{ margin-top: 30px; }}
        a {{ color: #1a73e8; text-decoration: none; }}
        a:hover {{ text-decoration: underline; }}
    </style>
</head>
<body>
    <h1>Hyperliquid Whale Tracker</h1>
    <p class="updated">Last updated: {today}</p>

    <h2>🏆 Quality Whales (top {QUALITY_TIER_SIZE} by blended score)</h2>
    <table>
        <thead><tr><th>Wallet</th><th>Account Value</th><th>24h ROI</th><th>7D ROI</th><th>30D ROI</th><th>30D Volume</th><th>Current Positions (top 3)</th></tr></thead>
        <tbody>{quality_html}</tbody>
    </table>

    <h2>🔥 Active Whales (top {QUALITY_TIER_SIZE} by today's PnL, within shortlist)</h2>
    <table>
        <thead><tr><th>Wallet</th><th>Account Value</th><th>24h ROI</th><th>7D ROI</th><th>30D ROI</th><th>30D Volume</th><th>Current Positions (top 3)</th></tr></thead>
        <tbody>{active_html}</tbody>
    </table>

    <p style="color:#555; font-size:0.85em; margin-top:20px;">
        Shortlist criteria: account value ≥ ${MIN_ACCOUNT_VALUE:,.0f}, 30D ROI ≥ {MIN_30D_ROI*100:.0f}%,
        30D volume ≥ ${MIN_30D_VOLUME:,.0f}. Score = 0.6×30D ROI + 0.3×7D ROI + 0.1×24h ROI (early formula, will refine with backtesting).
        Click a wallet to view its full history on Hyperliquid's own explorer.
        This tracks public on-chain positioning only — not financial advice, and past performance doesn't guarantee future results.
    </p>
</body>
</html>
"""
    return html


def main():
    print("Fetching Hyperliquid leaderboard...")
    rows = fetch_leaderboard()
    print(f"Got {len(rows)} total wallets.")

    shortlist = build_shortlist(rows)
    print(f"Shortlist after filtering: {len(shortlist)} wallets.")

    if len(shortlist) == 0:
        print("No wallets passed the filters — check thresholds.")
        return

    update_history(shortlist)
    print(f"Updated {HISTORY_PATH}")

    html = build_html_report(shortlist)
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w") as f:
        f.write(html)
    print("Wrote docs/index.html")


if __name__ == "__main__":
    main()
