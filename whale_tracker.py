"""
Hyperliquid Whale Tracker
Pulls the public leaderboard, filters down to a shortlist of "quality" wallets
by size/ROI/volume, stores a daily historical snapshot of that shortlist, and
publishes an HTML report (Coin Dominance + Quality Whales + Active Whales)
for GitHub Pages.
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

    df = df[df["account_value"] >= MIN_ACCOUNT_VALUE]
    df = df[df["month_roi"] >= MIN_30D_ROI]
    df = df[df["month_volume"] >= MIN_30D_VOLUME]

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
            leverage_info = pos.get("leverage", {})
            leverage_value = leverage_info.get("value") if isinstance(leverage_info, dict) else leverage_info
            positions.append({
                "coin": pos.get("coin", "?"),
                "side": "LONG" if szi > 0 else "SHORT",
                "size_usd": abs(szi) * float(pos.get("entryPx", 0)),
                "entry_price": float(pos.get("entryPx", 0)),
                "unrealized_pnl": float(pos.get("unrealizedPnl", 0)),
                "leverage": float(leverage_value) if leverage_value is not None else None,
            })
        return positions
    except Exception:
        return []


def positions_summary_html(positions, account_value):
    if not positions:
        return "<span style='color:#999'>no open positions / unavailable</span>"
    positions = sorted(positions, key=lambda p: p["size_usd"], reverse=True)[:3]
    parts = []
    for p in positions:
        color = "#0a7d2c" if p["side"] == "LONG" else "#c0392b"
        pct_of_capital = (p["size_usd"] / account_value * 100) if account_value > 0 else 0
        lev = f", {p['leverage']:.1f}x" if p.get("leverage") else ""
        parts.append(
            f"<span style='color:{color}'>{p['side']} {p['coin']} "
            f"<strong>{pct_of_capital:.0f}% of capital</strong> "
            f"(${p['size_usd']:,.0f} @ ${p['entry_price']:,.4f}{lev})</span>"
        )
    return " · ".join(parts)


COIN_HISTORY_PATH = "coin_exposure_history.csv"
COIN_HISTORY_COLUMNS = ["timestamp", "coin", "long_usd", "short_usd"]


def load_coin_history():
    try:
        df = pd.read_csv(COIN_HISTORY_PATH)
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except FileNotFoundError:
        return pd.DataFrame(columns=COIN_HISTORY_COLUMNS)


def find_reference_snapshot(history_df, coin, target_time, tolerance_hours=3):
    """Find the historical row for this coin closest to ~24h before now, within a tolerance window."""
    if history_df.empty:
        return None
    coin_rows = history_df[history_df["coin"] == coin]
    if coin_rows.empty:
        return None
    coin_rows = coin_rows.copy()
    coin_rows["diff_hours"] = (coin_rows["timestamp"] - target_time).abs().dt.total_seconds() / 3600
    best = coin_rows.loc[coin_rows["diff_hours"].idxmin()]
    if best["diff_hours"] > tolerance_hours:
        return None
    return best


def pct_change_label(old_val, new_val, has_reference):
    if not has_reference:
        return "no data yet"
    if old_val == 0:
        if new_val == 0:
            return "—"
        return "🆕 new exposure"
    change = (new_val - old_val) / old_val * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.0f}%"


def save_coin_history(history_df, dominance_df, now):
    new_rows = []
    for _, r in dominance_df.iterrows():
        new_rows.append({
            "timestamp": now.isoformat(),
            "coin": r["coin"],
            "long_usd": r["long_usd"],
            "short_usd": r["short_usd"],
        })
    new_df = pd.DataFrame(new_rows)
    combined = pd.concat([history_df.drop(columns=["diff_hours"], errors="ignore"), new_df], ignore_index=True)
    # keep only the last 14 days to avoid unbounded growth
    cutoff = now - pd.Timedelta(days=14)
    combined["timestamp"] = pd.to_datetime(combined["timestamp"])
    combined = combined[combined["timestamp"] >= cutoff]
    combined.to_csv(COIN_HISTORY_PATH, index=False)


def build_coin_dominance(positions_by_wallet: dict) -> pd.DataFrame:
    """Aggregate all tracked whales' positions by coin: how many long/short, net exposure, avg entry, avg leverage."""
    stats = {}
    for wallet, positions in positions_by_wallet.items():
        for p in positions:
            coin = p["coin"]
            if coin not in stats:
                stats[coin] = {
                    "coin": coin, "long_count": 0, "short_count": 0,
                    "long_usd": 0.0, "short_usd": 0.0,
                    "long_entry_weighted_sum": 0.0, "short_entry_weighted_sum": 0.0,
                    "leverage_weighted_sum": 0.0, "leverage_weight_total": 0.0,
                }
            size = p["size_usd"]
            entry = p["entry_price"]
            if p["side"] == "LONG":
                stats[coin]["long_count"] += 1
                stats[coin]["long_usd"] += size
                stats[coin]["long_entry_weighted_sum"] += entry * size
            else:
                stats[coin]["short_count"] += 1
                stats[coin]["short_usd"] += size
                stats[coin]["short_entry_weighted_sum"] += entry * size
            if p.get("leverage"):
                stats[coin]["leverage_weighted_sum"] += p["leverage"] * size
                stats[coin]["leverage_weight_total"] += size

    rows = list(stats.values())
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["total_usd"] = df["long_usd"] + df["short_usd"]
    df["net_usd"] = df["long_usd"] - df["short_usd"]
    df["total_traders"] = df["long_count"] + df["short_count"]
    df["avg_long_entry"] = df.apply(
        lambda r: r["long_entry_weighted_sum"] / r["long_usd"] if r["long_usd"] > 0 else None, axis=1
    )
    df["avg_short_entry"] = df.apply(
        lambda r: r["short_entry_weighted_sum"] / r["short_usd"] if r["short_usd"] > 0 else None, axis=1
    )
    df["avg_leverage"] = df.apply(
        lambda r: r["leverage_weighted_sum"] / r["leverage_weight_total"] if r["leverage_weight_total"] > 0 else None,
        axis=1,
    )
    df = df.sort_values("total_usd", ascending=False)
    return df


def coin_dominance_html(df: pd.DataFrame, history_df, now, top_n=15) -> str:
    if df.empty:
        return "<p>No position data available.</p>"
    target_time = now - pd.Timedelta(hours=24)
    rows = ""
    for _, r in df.head(top_n).iterrows():
        ref = find_reference_snapshot(history_df, r["coin"], target_time)
        has_ref = ref is not None
        long_change = pct_change_label(ref["long_usd"] if has_ref else None, r["long_usd"], has_ref)
        short_change = pct_change_label(ref["short_usd"] if has_ref else None, r["short_usd"], has_ref)

        net_color = "#0a7d2c" if r["net_usd"] > 0 else "#c0392b"
        net_label = "net LONG" if r["net_usd"] > 0 else "net SHORT"
        long_avg = f"${r['avg_long_entry']:,.4f}" if pd.notna(r["avg_long_entry"]) else "—"
        short_avg = f"${r['avg_short_entry']:,.4f}" if pd.notna(r["avg_short_entry"]) else "—"
        avg_lev = f"{r['avg_leverage']:.1f}x" if pd.notna(r["avg_leverage"]) else "—"
        rows += f"""
        <tr>
            <td><strong>{r['coin']}</strong></td>
            <td>{r['total_traders']} tracked whale(s)</td>
            <td>${r['long_usd']:,.0f} ({r['long_count']}) <span style="color:#666">{long_change} vs 24h ago</span></td>
            <td>${r['short_usd']:,.0f} ({r['short_count']}) <span style="color:#666">{short_change} vs 24h ago</span></td>
            <td style="color:{net_color}"><strong>${abs(r['net_usd']):,.0f} {net_label}</strong></td>
            <td>{long_avg}</td>
            <td>{short_avg}</td>
            <td>{avg_lev}</td>
        </tr>
        """
    return f"""
    <table>
        <thead><tr><th>Coin</th><th>Whales holding it</th><th>Long exposure</th><th>Short exposure</th><th>Net positioning</th><th>Avg Long Entry</th><th>Avg Short Entry</th><th>Avg Leverage</th></tr></thead>
        <tbody>{rows}</tbody>
    </table>
    """


def build_html_report(shortlist: pd.DataFrame) -> str:
    today = dt.date.today().isoformat()

    quality = shortlist.head(QUALITY_TIER_SIZE)
    active = shortlist.sort_values("day_pnl", ascending=False).head(QUALITY_TIER_SIZE)

    wallets_to_check = pd.concat([quality["wallet"], active["wallet"]]).unique()
    print(f"Fetching live positions for {len(wallets_to_check)} wallets...")
    positions_by_wallet = {}
    for w in wallets_to_check:
        positions_by_wallet[w] = fetch_positions(w)
        time.sleep(0.15)

    dominance_df = build_coin_dominance(positions_by_wallet)
    now = pd.Timestamp.utcnow()
    coin_history_df = load_coin_history()
    dominance_html = coin_dominance_html(dominance_df, coin_history_df, now)
    save_coin_history(coin_history_df, dominance_df, now)

    def table_rows(df):
        rows = ""
        for _, w in df.iterrows():
            wallet = w["wallet"]
            short_addr = wallet[:6] + "..." + wallet[-4:]
            explorer_url = f"https://hyperdash.info/trader/{wallet}"
            positions = positions_by_wallet.get(wallet, [])
            positions_html = positions_summary_html(positions, w["account_value"])

            total_position_usd = sum(p["size_usd"] for p in positions)
            account_leverage = total_position_usd / w["account_value"] if w["account_value"] > 0 else 0
            leverage_label = f"{account_leverage:.1f}x" if positions else "—"

            rows += f"""
            <tr>
                <td><a href="{explorer_url}" target="_blank">{short_addr}</a></td>
                <td>${w['account_value']:,.0f}</td>
                <td>{leverage_label}</td>
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
        body {{ font-family: -apple-system, Arial, sans-serif; max-width: 1300px; margin: 20px auto; padding: 0 15px; }}
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

    <h2>📊 Coin Dominance (aggregated across all tracked whales)</h2>
    {dominance_html}

    <h2>🏆 Quality Whales (top {QUALITY_TIER_SIZE} by blended score)</h2>
    <table>
        <thead><tr><th>Wallet</th><th>Account Value</th><th>Account Leverage</th><th>24h ROI</th><th>7D ROI</th><th>30D ROI</th><th>30D Volume</th><th>Current Positions (top 3)</th></tr></thead>
        <tbody>{quality_html}</tbody>
    </table>

    <h2>🔥 Active Whales (top {QUALITY_TIER_SIZE} by today's PnL, within shortlist)</h2>
    <table>
        <thead><tr><th>Wallet</th><th>Account Value</th><th>Account Leverage</th><th>24h ROI</th><th>7D ROI</th><th>30D ROI</th><th>30D Volume</th><th>Current Positions (top 3)</th></tr></thead>
        <tbody>{active_html}</tbody>
    </table>

    <p style="color:#555; font-size:0.85em; margin-top:20px;">
        Shortlist criteria: account value ≥ ${MIN_ACCOUNT_VALUE:,.0f}, 30D ROI ≥ {MIN_30D_ROI*100:.0f}%,
        30D volume ≥ ${MIN_30D_VOLUME:,.0f}. Score = 0.6×30D ROI + 0.3×7D ROI + 0.1×24h ROI (early formula, will refine with backtesting).
        Click a wallet to view its full history on Hyperdash. Leverage/entry prices are size-weighted averages across tracked whales.
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
