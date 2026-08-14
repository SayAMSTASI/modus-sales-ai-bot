from __future__ import annotations

from fastapi import FastAPI
from sqlalchemy import text

from app.admin import build_admin_router
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.webhook import build_webhook_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)

    application = FastAPI(title="Sales Telegram Pilot", version="0.1.0")
    application.state.settings = settings
    application.state.engine = engine
    application.state.session_factory = factory
    application.include_router(build_webhook_router(settings, factory))
    application.include_router(build_admin_router(settings, factory))

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

