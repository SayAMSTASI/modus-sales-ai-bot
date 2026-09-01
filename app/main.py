from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text

from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.oauth import OAuthTokenStore
from app.oauth_callback import build_oauth_callback_router
from app.webhook import build_webhook_router


def create_app(
    settings: Settings | None = None,
    oauth_store: OAuthTokenStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    oauth_store = oauth_store or OAuthTokenStore(settings)

    application = FastAPI(title="Sales Telegram Pilot", version="0.1.0")
    application.state.settings = settings
    application.state.engine = engine
    application.state.session_factory = factory
    application.state.oauth_store = oauth_store
    application.include_router(build_webhook_router(settings, factory))
    application.include_router(build_oauth_callback_router(settings, factory, oauth_store))

    @application.get("/health/live")
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    def ready() -> dict[str, str]:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready"}

    return application


app = create_app()
