from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class TelegramClient(Protocol):
    def send_message(self, chat_id: int, text: str) -> None: ...


class PollingTelegramClient(TelegramClient, Protocol):
    def get_me(self) -> dict[str, Any]: ...

    def delete_webhook(self, *, drop_pending_updates: bool) -> None: ...

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]: ...


class HttpTelegramClient:
    def __init__(self, token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"

    def _post(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float = 20,
    ) -> Any:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(f"{self._base_url}/{method}", json=payload)
            response.raise_for_status()
            body = response.json()
        if not body.get("ok"):
            description = body.get("description", "unknown")
            raise RuntimeError(f"Telegram API {method} failed: {description}")
        return body.get("result")

    def send_message(self, chat_id: int, text: str) -> None:
        self._post("sendMessage", {"chat_id": chat_id, "text": text[:4096]})

    def get_me(self) -> dict[str, Any]:
        result = self._post("getMe", {})
        if not isinstance(result, dict):
            raise RuntimeError("Telegram API getMe returned an invalid result")
        return result

    def delete_webhook(self, *, drop_pending_updates: bool) -> None:
        self._post("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": timeout,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._post("getUpdates", payload, timeout=timeout + 10)
        if not isinstance(result, list):
            raise RuntimeError("Telegram API getUpdates returned an invalid result")
        return [item for item in result if isinstance(item, dict)]


class LoggingTelegramClient:
    def send_message(self, chat_id: int, text: str) -> None:
        logger.info("Mock Telegram delivery chat_id=%s chars=%s", chat_id, len(text))


@dataclass
class InMemoryTelegramClient:
    messages: list[tuple[int, str]] = field(default_factory=list)

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append((chat_id, text))


def build_telegram_client(settings: Settings) -> TelegramClient:
    if settings.telegram_bot_token:
        return HttpTelegramClient(settings.telegram_bot_token)
    return LoggingTelegramClient()
