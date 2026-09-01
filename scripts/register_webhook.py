from __future__ import annotations

import argparse
import os

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description="Register the Telegram HTTPS webhook")
    parser.add_argument("--url", required=True, help="Public base URL, for example https://bot.example.ru")
    args = parser.parse_args()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    if not token or not secret:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET")
    response = httpx.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={
            "url": f"{args.url.rstrip('/')}/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": False,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise SystemExit(f"Telegram rejected webhook registration: {payload.get('description')}")
    print("Webhook registered")


if __name__ == "__main__":
    main()
