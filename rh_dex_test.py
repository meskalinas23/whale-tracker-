"""
Test: what does DexScreener actually give us for Robinhood Chain?
Also re-checks Blockscout's token list, this time capturing contract
addresses (needed later for holder/transfer lookups).
"""

import os
import requests

API_KEY = os.environ.get("BLOCKSCOUT_API_KEY")
CHAIN_ID = "4663"


def test_dexscreener_search():
    """Search DexScreener for PONS - see if it's indexed and what chainId it reports."""
    resp = requests.get(
        "https://api.dexscreener.com/latest/dex/search",
        params={"q": "PONS"},
        headers={"User-Agent": "Mozilla/5.0 (compatible; screener-bot/1.0)"},
        timeout=15,
    )
    print(f"DexScreener search status: {resp.status_code}")
    try:
        data = resp.json()
        pairs = data.get("pairs", []) or []
        print(f"Found {len(pairs)} pairs matching 'PONS'.")
        for p in pairs[:10]:
            print(f"  chainId={p.get('chainId')} | {p.get('baseToken', {}).get('symbol')}/{p.get('quoteToken', {}).get('symbol')} "
                  f"| liquidity=${p.get('liquidity', {}).get('usd', 0):,.0f} | vol24h=${p.get('volume', {}).get('h24', 0):,.0f}")
    except Exception as e:
        print(f"Could not parse: {e}")
        print(resp.text[:500])


def test_dexscreener_token_profiles():
    """Check the latest token profiles feed for anything tagged to robinhood chain."""
    resp = requests.get(
        "https://api.dexscreener.com/token-profiles/latest/v1",
        headers={"User-Agent": "Mozilla/5.0 (compatible; screener-bot/1.0)"},
        timeout=15,
    )
    print(f"\nDexScreener token-profiles status: {resp.status_code}")
    try:
        data = resp.json()
        if isinstance(data, list):
            robinhood_items = [d for d in data if "robinhood" in str(d.get("chainId", "")).lower()]
            print(f"Total profiles: {len(data)}, robinhood-chain ones: {len(robinhood_items)}")
            for item in robinhood_items[:5]:
                print(f"  {item}")
        else:
            print("Unexpected shape:", str(data)[:300])
    except Exception as e:
        print(f"Could not parse: {e}")
        print(resp.text[:500])


def recheck_token_list_with_addresses():
    """Same token list as before, but print contract addresses this time."""
    resp = requests.get(
        f"https://api.blockscout.com/{CHAIN_ID}/api/v2/tokens/",
        headers={"Authorization": f"Bearer {API_KEY}"},
        timeout=15,
    )
    print(f"\nToken list status: {resp.status_code}")
    try:
        data = resp.json()
        items = data.get("items", [])
        print(f"Found {len(items)} tokens.")
        for t in items[:15]:
            print(f"  {t.get('symbol', '?')} | address={t.get('address', '?')} | type={t.get('type', '?')} | "
                  f"exchange_rate={t.get('exchange_rate', 'N/A')}")
    except Exception as e:
        print(f"Could not parse: {e}")
        print(resp.text[:500])


if __name__ == "__main__":
    test_dexscreener_search()
    test_dexscreener_token_profiles()
    recheck_token_list_with_addresses()
