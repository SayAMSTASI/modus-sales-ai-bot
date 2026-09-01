from __future__ import annotations

import hashlib
import hmac


def stable_user_hash(user_id: int, secret: str) -> str:
    digest = hmac.new(secret.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
    return f"tg_{digest[:40]}"


def verify_webhook_secret(actual: str | None, expected: str) -> bool:
    return bool(actual) and hmac.compare_digest(actual, expected)
