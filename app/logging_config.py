from __future__ import annotations

import logging


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx includes the complete Telegram Bot API URL at INFO level. The bot
    # token is part of that URL, so request logging must stay disabled.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
