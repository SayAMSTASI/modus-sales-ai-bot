from __future__ import annotations

import hashlib
import hmac
from datetime import date, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Settings

basic_scheme = HTTPBasic(auto_error=False)


def stable_user_hash(user_id: int, secret: str) -> str:
    digest = hmac.new(secret.encode(), str(user_id).encode(), hashlib.sha256).hexdigest()
    return f"tg_{digest[:40]}"


def verify_webhook_secret(actual: str | None, expected: str) -> bool:
    return bool(actual) and hmac.compare_digest(actual, expected)


def require_admin(settings: Settings):
    def dependency(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_scheme)],
    ) -> str:
        valid = bool(credentials) and hmac.compare_digest(
            credentials.username,
            settings.admin_username,
        )
        valid = valid and hmac.compare_digest(credentials.password, settings.admin_password)
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    return dependency


def csrf_token(settings: Settings, day: date | None = None) -> str:
    day = day or date.today()
    payload = f"admin:{day.isoformat()}".encode()
    return hmac.new(settings.safety_identifier_secret.encode(), payload, hashlib.sha256).hexdigest()


def verify_csrf(value: str, settings: Settings) -> bool:
    return any(
        hmac.compare_digest(value, csrf_token(settings, date.today() - timedelta(days=offset)))
        for offset in (0, 1)
    )
