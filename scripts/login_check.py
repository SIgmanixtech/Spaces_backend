"""Sign in to Supabase Auth and prove the backend accepts the resulting token.

    python scripts/login_check.py you@example.com
    python scripts/login_check.py you@example.com --show-token

Prompts for the password (never stored or echoed), signs in exactly as the
frontend will, then calls this backend's /auth/me in-process with the token.
--show-token also prints the access token for pasting into Swagger's Authorize
dialog; it is valid for about an hour, so treat it like a password.
"""

import argparse
import getpass
import logging
import sys
from pathlib import Path

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("email")
    parser.add_argument("--show-token", action="store_true")
    args = parser.parse_args()

    env = dotenv_values(ROOT / ".env")
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_PUBLISHABLE_KEY")
    if not url or not key:
        print("Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in .env first.")
        return 1

    password = getpass.getpass(f"Password for {args.email}: ")
    response = httpx.post(
        f"{url.rstrip('/')}/auth/v1/token",
        params={"grant_type": "password"},
        headers={"apikey": key},
        json={"email": args.email, "password": password},
        timeout=20,
    )
    if response.status_code != 200:
        reason = response.json().get("msg") or response.json().get("error_description")
        print(f"Supabase sign-in failed ({response.status_code}): {reason}")
        return 1
    token = response.json()["access_token"]
    print("Supabase sign-in: OK")

    from fastapi.testclient import TestClient

    from app.main import app

    logging.disable(logging.CRITICAL)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {token}"}
        me = client.get("/api/v1/auth/me", headers=headers)
        print(f"GET /api/v1/auth/me: {me.status_code} {me.json()}")
        if me.status_code != 200:
            return 1
        properties = client.get("/api/v1/properties", headers=headers)
        print(
            f"GET /api/v1/properties: {properties.status_code} total={properties.json()['total']}"
        )

    if args.show_token:
        print("\nAccess token (paste into Swagger > Authorize):\n" + token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
