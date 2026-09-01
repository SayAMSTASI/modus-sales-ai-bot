from __future__ import annotations

from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import (
    AccessStatus,
    OAuthAuthorizationSession,
    UpdateJob,
    UserAccess,
)
from app.oauth import (
    OAuthConfigurationError,
    OAuthLoginRequired,
    OAuthProtocolError,
    OAuthTokenStore,
    oauth_state_hash,
)


def build_oauth_callback_router(
    settings: Settings,
    factory: sessionmaker[Session],
    oauth_store: OAuthTokenStore,
) -> APIRouter:
    router = APIRouter()

    @router.get("/oauth/callback", response_class=HTMLResponse)
    def oauth_callback(
        state: str = "",
        code: str = "",
        error: str = "",
    ) -> HTMLResponse:
        if not state or len(state) > 512:
            raise HTTPException(status_code=400, detail="Invalid OAuth state")
        with factory() as session:
            authorization = session.scalar(
                select(OAuthAuthorizationSession)
                .where(OAuthAuthorizationSession.state_hash == oauth_state_hash(state))
                .with_for_update()
            )
            if authorization is None:
                raise HTTPException(status_code=400, detail="OAuth session not found or used")
            expires_at = authorization.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= datetime.now(UTC):
                session.delete(authorization)
                session.commit()
                raise HTTPException(status_code=400, detail="OAuth session expired")
            user = session.scalar(
                select(UserAccess).where(
                    UserAccess.telegram_user_id == authorization.telegram_user_id
                )
            )
            if user is None or user.status != AccessStatus.active:
                session.delete(authorization)
                session.commit()
                raise HTTPException(status_code=403, detail="Pilot access is not active")
            if error:
                session.add(
                    UpdateJob(
                        telegram_user_id=authorization.telegram_user_id,
                        chat_id=authorization.chat_id,
                        kind="system_notice",
                        payload_text="Авторизация Keycloak отменена. Повторите /login.",
                    )
                )
                session.delete(authorization)
                session.commit()
                return HTMLResponse(
                    "Авторизация отменена. Можно закрыть страницу и вернуться в Telegram.",
                    status_code=400,
                )
            if not code or len(code) > 4096:
                raise HTTPException(status_code=400, detail="Authorization code is missing")
            try:
                verifier = oauth_store.cipher().decrypt(
                    authorization.code_verifier_encrypted
                )
                token = oauth_store.client.exchange_authorization_code(code, verifier)
            except (
                OAuthConfigurationError,
                OAuthLoginRequired,
                OAuthProtocolError,
                httpx.HTTPError,
            ) as exc:
                session.delete(authorization)
                session.commit()
                raise HTTPException(
                    status_code=400,
                    detail="Authorization code exchange failed",
                ) from exc
            oauth_store.save(session, authorization.telegram_user_id, token)
            session.add(
                UpdateJob(
                    telegram_user_id=authorization.telegram_user_id,
                    chat_id=authorization.chat_id,
                    kind="system_notice",
                    payload_text="Keycloak подключён. Можно пользоваться MCP.",
                )
            )
            session.delete(authorization)
            session.commit()
        return HTMLResponse(
            "Авторизация завершена. Можно закрыть страницу и вернуться в Telegram."
        )

    return router
