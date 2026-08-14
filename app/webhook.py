from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.ingest import ingest_update
from app.security import verify_webhook_secret


def build_webhook_router(settings: Settings, factory: sessionmaker[Session]) -> APIRouter:
    router = APIRouter()

    @router.post("/telegram/webhook")
    def telegram_webhook(
        request: Request,
        update: dict[str, Any],
        secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> dict[str, Any]:
        if not verify_webhook_secret(secret, settings.telegram_webhook_secret):
            raise HTTPException(status_code=401, detail="Unauthorized")
        return ingest_update(settings, factory, update)

    return router
