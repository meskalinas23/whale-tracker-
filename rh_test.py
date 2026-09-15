"""
Quick connectivity test: can we reach Robinhood Chain's data via Blockscout's
API from GitHub Actions? Also checks what large token holders / stock token
data looks like, since this chain is brand new (mainnet since July 2026).
"""

import os
import requests

API_KEY = os.environ.get("BLOCKSCOUT_API_KEY")
BASE_URL = "https://api.blockscout.com/v2/api"
CHAIN_ID = "4663"  # Robinhood Chain


def test_connection():
    if not API_KEY:
        print("ERROR: BLOCKSCOUT_API_KEY environment variable not set.")
        return

    # Basic sanity check: fetch general chain stats
    resp = requests.get(
        BASE_URL,
        params={"chain_id": CHAIN_ID, "module": "stats", "action": "ethsupply", "apikey": API_KEY},
        timeout=15,
    )
    print(f"Stats endpoint status: {resp.status_code}")
    print("Response:", resp.text[:300])


def test_token_list():
    """See what tokens exist on the chain - useful to find the tokenized stock contracts."""
    resp = requests.get(
        "https://robinhoodchain.blockscout.com/api/v2/tokens",
        params={"apikey": API_KEY},
        timeout=15,
    )
    print(f"\nToken list status: {resp.status_code}")
    try:
        data = resp.json()
        items = data.get("items", [])
        print(f"Found {len(items)} tokens. First few:")
        for t in items[:10]:
            print(f"  {t.get('symbol', '?')} - {t.get('name', '?')} - type: {t.get('type', '?')}")
    except Exception as e:
        print(f"Could not parse response: {e}")
        print(resp.text[:500])


if __name__ == "__main__":
    test_connection()
    test_token_list()
