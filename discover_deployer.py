"""
Discovery: do known tokenized stocks (NVDA, TSLA, SPY, GME) share the same
contract creator address? If so, we can exclude ALL stock tokens by
creator address instead of maintaining an endless ticker whitelist.
"""

import os
import json
import requests

API_KEY = os.environ.get("BLOCKSCOUT_API_KEY")
CHAIN_ID = "4663"
BASE_URL = f"https://api.blockscout.com/{CHAIN_ID}/api/v2"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}


def get_all_tokens():
    all_items = []
    url = f"{BASE_URL}/tokens/"
    params = {}
    for _ in range(20):
        resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        all_items.extend(data.get("items", []))
        next_page = data.get("next_page_params")
        if not next_page:
            break
        params = {k: (str(v).lower() if isinstance(v, bool) else v) for k, v in next_page.items()}
    return all_items


def get_contract_info(token_address):
    try:
        resp = requests.get(
            f"{BASE_URL}/smart-contracts/{token_address}",
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"    status {resp.status_code} for {token_address}")
            return None
        return resp.json()
    except Exception as e:
        print(f"    error: {e}")
        return None


def get_address_info(token_address):
    """Alternate endpoint - address details sometimes include creator info directly."""
    try:
        resp = requests.get(
            f"{BASE_URL}/addresses/{token_address}",
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"    address endpoint status {resp.status_code}")
            return None
        return resp.json()
    except Exception as e:
        print(f"    error: {e}")
        return None


def main():
    print("Finding known stock tokens...")
    all_tokens = get_all_tokens()
    targets = {"NVDA", "TSLA", "SPY", "GME"}
    found = {t["symbol"]: t["address_hash"] for t in all_tokens if t.get("symbol") in targets}
    print(f"Found: {found}")

    for symbol, address in found.items():
        print(f"\n=== {symbol} ({address}) ===")

        print("--- /smart-contracts/ endpoint ---")
        data = get_contract_info(address)
        if data:
            creator_fields = {k: v for k, v in data.items() if "creat" in k.lower() or "deploy" in k.lower()}
            print("Creator/deployer fields:", json.dumps(creator_fields, indent=2) if creator_fields else "none found")

        print("--- /addresses/ endpoint ---")
        addr_data = get_address_info(address)
        if addr_data:
            creator_fields2 = {k: v for k, v in addr_data.items() if "creat" in k.lower() or "deploy" in k.lower()}
            print("Creator/deployer fields:", json.dumps(creator_fields2, indent=2) if creator_fields2 else "none found")


if __name__ == "__main__":
    main()
