"""
get_copilot_token.py
--------------------
One-time script to authenticate with GitHub Copilot via the Device Flow
(the same flow VS Code uses internally).

Usage:
    python get_copilot_token.py

After running, paste the printed token into .env as:
    COPILOT_OAUTH_TOKEN=ghu_xxxxxxxxxxxx
"""

import sys
import time
import requests

# GitHub OAuth app client ID used by the official Copilot CLI / Neovim plugin
CLIENT_ID = "Iv1.b507a08c87ecfe98"
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
SCOPE = "read:user"


def main() -> None:
    # ── Step 1: Request device code ──────────────────────────────────────────
    resp = requests.post(
        DEVICE_CODE_URL,
        json={"client_id": CLIENT_ID, "scope": SCOPE},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()

    device_code = data["device_code"]
    user_code = data["user_code"]
    verification_uri = data["verification_uri"]
    interval = data.get("interval", 5)
    expires_in = data.get("expires_in", 900)

    print("\n" + "=" * 60)
    print("  GitHub Copilot — Device Authentication")
    print("=" * 60)
    print(f"\n  1. Open:  {verification_uri}")
    print(f"  2. Enter: {user_code}")
    print(f"\n  Waiting for you to authenticate (expires in {expires_in}s)...")
    print("=" * 60)

    # ── Step 2: Poll for token ────────────────────────────────────────────────
    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        token_resp = requests.post(
            TOKEN_URL,
            json={
                "client_id": CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers={"Accept": "application/json"},
            timeout=15,
        )
        token_data = token_resp.json()
        error = token_data.get("error")

        if error == "authorization_pending":
            print("  Waiting...", end="\r")
            continue
        elif error == "slow_down":
            interval += 5
            continue
        elif error == "expired_token":
            print("\n  ERROR: Code expired. Please run the script again.")
            sys.exit(1)
        elif error:
            print(f"\n  ERROR: {error} — {token_data.get('error_description', '')}")
            sys.exit(1)

        oauth_token = token_data.get("access_token", "")
        if oauth_token:
            print(f"\n\n  ✅  Authentication successful!")
            print(f"\n  Your Copilot OAuth token:\n")
            print(f"  {oauth_token}")
            print(f"\n  Add this to your .env file:")
            print(f"  COPILOT_OAUTH_TOKEN={oauth_token}")
            print()
            return

    print("\n  ERROR: Timed out waiting for authentication.")
    sys.exit(1)


if __name__ == "__main__":
    main()
