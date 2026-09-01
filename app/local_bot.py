from __future__ import annotations

import logging
import signal
import time

from sqlalchemy.orm import Session, sessionmaker

from app.agent import build_agent_client
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.ingest import ingest_update
from app.logging_config import configure_logging
from app.metrics import build_metrics_exporter
from app.telegram import HttpTelegramClient, PollingTelegramClient
from app.worker import JobProcessor

logger = logging.getLogger(__name__)


class LocalBotRuntime:
    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        telegram: PollingTelegramClient,
        processor: JobProcessor,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.telegram = telegram
        self.processor = processor
        self.offset: int | None = None

    def start(self) -> dict:
        self.telegram.delete_webhook(
            drop_pending_updates=self.settings.telegram_drop_pending_updates
        )
        identity = self.telegram.get_me()
        logger.info(
            "Telegram polling enabled for @%s (id=%s)",
            identity.get("username", "unknown"),
            identity.get("id", "unknown"),
        )
        return identity

    def poll_once(self) -> int:
        updates = self.telegram.get_updates(
            offset=self.offset,
            timeout=self.settings.telegram_poll_timeout_seconds,
        )
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.offset = update_id + 1
            result = ingest_update(
                self.settings,
                self.factory,
                update,
            )
            if not result.get("accepted"):
                logger.info(
                    "Telegram update skipped update_id=%s reason=%s",
                    update_id,
                    result.get("reason"),
                )
        processed = 0
        while self.processor.run_once():
            processed += 1
        while self.processor.poll_oauth_once():
            processed += 1
        while self.processor.run_once():
            processed += 1
        return processed


def build_local_runtime(settings: Settings) -> LocalBotRuntime:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for local polling")
    if settings.agent_backend == "openai" and not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required when AGENT_BACKEND=openai")

    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    telegram = HttpTelegramClient(settings.telegram_bot_token)
    processor = JobProcessor(
        settings,
        factory,
        build_agent_client(settings),
        telegram,
        build_metrics_exporter(),
    )
    return LocalBotRuntime(settings, factory, telegram, processor)


def main() -> None:
    configure_logging()
    settings = get_settings()
    runtime = build_local_runtime(settings)
    identity = runtime.start()
    print(
        f"Local bot @{identity.get('username', 'unknown')} is running. "
        "Send /start in a private chat. Press Ctrl+C to stop.",
        flush=True,
    )

    running = True

    def stop(*_args) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    while running:
        try:
            runtime.poll_once()
        except Exception:
            logger.exception("Local polling cycle failed")
            time.sleep(2)
    logger.info("Local bot stopped")


if __name__ == "__main__":
    main()
