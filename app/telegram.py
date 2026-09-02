from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from app.attachments import AgentAttachment, AttachmentError
from app.config import Settings

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    {"command": "start", "description": "Открыть главное меню"},
    {"command": "menu", "description": "Показать кнопки управления"},
]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.+)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)]\((https?://[^)\s]+)\)")


def _render_inline(text: str) -> str:
    tokens: list[str] = []

    def stash(value: str) -> str:
        tokens.append(value)
        return f"\x00{len(tokens) - 1}\x00"

    text = _INLINE_CODE_RE.sub(
        lambda match: stash(f"<code>{html.escape(match.group(1))}</code>"),
        text,
    )
    text = _LINK_RE.sub(
        lambda match: stash(
            f'<a href="{html.escape(match.group(2), quote=True)}">'
            f"{html.escape(match.group(1), quote=False)}</a>"
        ),
        text,
    )
    escaped = html.escape(text, quote=False)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    for index, token in enumerate(tokens):
        escaped = escaped.replace(f"\x00{index}\x00", token)
    return escaped


def render_telegram_html(text: str) -> str:
    """Render a safe Markdown subset as Telegram HTML without raw heading markers."""
    rendered: list[str] = []
    code_lines: list[str] = []
    in_code = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                rendered.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
                code_lines = []
                in_code = False
            else:
                in_code = True
            continue
        if in_code:
            code_lines.append(line)
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            prefix = "◆ " if level == 1 else ("▸ " if level == 2 else "")
            heading_text = heading.group(2).replace("**", "")
            rendered.append(f"<b>{prefix}{_render_inline(heading_text)}</b>")
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            rendered.append(f"{bullet.group(1)}• {_render_inline(bullet.group(2))}")
            continue
        if line.startswith(">"):
            rendered.append(f"<blockquote>{_render_inline(line[1:].lstrip())}</blockquote>")
            continue
        rendered.append(_render_inline(line))
    if in_code:
        rendered.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
    return "\n".join(rendered)


def telegram_html_chunks(text: str, max_length: int = 4000) -> list[str]:
    """Split source recursively until every rendered HTML message fits Telegram."""
    rendered = render_telegram_html(text)
    if len(rendered) <= max_length:
        return [rendered]
    midpoint = len(text) // 2
    split_at = text.rfind("\n", 0, midpoint)
    if split_at <= 0:
        split_at = text.rfind(" ", 0, midpoint)
    if split_at <= 0:
        split_at = midpoint
    if split_at <= 0 or split_at >= len(text):
        return [html.escape(text[:max_length], quote=False)]
    return telegram_html_chunks(text[:split_at], max_length) + telegram_html_chunks(
        text[split_at:].lstrip("\n"), max_length
    )


class TelegramClient(Protocol):
    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None: ...

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None: ...

    def set_commands(self, commands: list[dict[str, str]]) -> None: ...

    def download_file(self, file_id: str, *, max_bytes: int) -> bytes: ...

    def send_attachment(
        self,
        chat_id: int,
        attachment: AgentAttachment,
        caption: str | None = None,
    ) -> None: ...


class PollingTelegramClient(TelegramClient, Protocol):
    def get_me(self) -> dict[str, Any]: ...

    def delete_webhook(self, *, drop_pending_updates: bool) -> None: ...

    def get_updates(self, *, offset: int | None, timeout: int) -> list[dict[str, Any]]: ...


class HttpTelegramClient:
    def __init__(self, token: str) -> None:
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._file_base_url = f"https://api.telegram.org/file/bot{token}"

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

    def _post_multipart(
        self,
        method: str,
        payload: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
        *,
        timeout: float = 60,
    ) -> Any:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                f"{self._base_url}/{method}",
                data=payload,
                files=files,
            )
            response.raise_for_status()
            body = response.json()
        if not body.get("ok"):
            description = body.get("description", "unknown")
            raise RuntimeError(f"Telegram API {method} failed: {description}")
        return body.get("result")

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = telegram_html_chunks(text)
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            }
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            self._post("sendMessage", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self._post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text[:200]},
        )

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self._post("setMyCommands", {"commands": commands})

    def download_file(self, file_id: str, *, max_bytes: int) -> bytes:
        result = self._post("getFile", {"file_id": file_id})
        if not isinstance(result, dict) or not isinstance(result.get("file_path"), str):
            raise AttachmentError("Telegram getFile returned no file path")
        declared = max(int(result.get("file_size") or 0), 0)
        if declared > max_bytes:
            raise AttachmentError("Telegram file exceeds the configured input limit")
        chunks: list[bytes] = []
        total = 0
        with httpx.stream(
            "GET",
            f"{self._file_base_url}/{result['file_path'].lstrip('/')}",
            timeout=60,
            follow_redirects=False,
        ) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise AttachmentError("Telegram file exceeds the configured input limit")
                chunks.append(chunk)
        return b"".join(chunks)

    def send_attachment(
        self,
        chat_id: int,
        attachment: AgentAttachment,
        caption: str | None = None,
    ) -> None:
        if attachment.data is None:
            raise AttachmentError("Outbound Telegram attachment has no data")
        is_photo = attachment.kind == "photo" and attachment.mime_type in {
            "image/jpeg",
            "image/png",
            "image/webp",
        }
        field = "photo" if is_photo else "document"
        method = "sendPhoto" if is_photo else "sendDocument"
        payload = {"chat_id": str(chat_id)}
        if caption:
            payload["caption"] = render_telegram_html(caption)[:1000]
            payload["parse_mode"] = "HTML"
        self._post_multipart(
            method,
            payload,
            {field: (attachment.filename, attachment.data, attachment.mime_type)},
        )

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
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._post("getUpdates", payload, timeout=timeout + 10)
        if not isinstance(result, list):
            raise RuntimeError("Telegram API getUpdates returned an invalid result")
        return [item for item in result if isinstance(item, dict)]


class LoggingTelegramClient:
    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        logger.info("Mock Telegram delivery chat_id=%s chars=%s", chat_id, len(text))

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        logger.info("Mock callback answer callback_query_id=%s", callback_query_id)

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        logger.info("Mock Telegram commands configured count=%s", len(commands))

    def download_file(self, file_id: str, *, max_bytes: int) -> bytes:
        raise AttachmentError("Telegram file download is unavailable without a bot token")

    def send_attachment(
        self,
        chat_id: int,
        attachment: AgentAttachment,
        caption: str | None = None,
    ) -> None:
        logger.info(
            "Mock Telegram attachment delivery chat_id=%s name=%s bytes=%s",
            chat_id,
            attachment.filename,
            len(attachment.data or b""),
        )


@dataclass
class InMemoryTelegramClient:
    messages: list[tuple[int, str]] = field(default_factory=list)
    markups: list[dict[str, Any] | None] = field(default_factory=list)
    callback_answers: list[tuple[str, str]] = field(default_factory=list)
    commands: list[dict[str, str]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    attachments: list[tuple[int, AgentAttachment, str | None]] = field(default_factory=list)

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self.messages.append((chat_id, text))
        self.markups.append(reply_markup)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        self.callback_answers.append((callback_query_id, text))

    def set_commands(self, commands: list[dict[str, str]]) -> None:
        self.commands = commands

    def download_file(self, file_id: str, *, max_bytes: int) -> bytes:
        if file_id not in self.files:
            raise AttachmentError("Test Telegram file is not registered")
        data = self.files[file_id]
        if len(data) > max_bytes:
            raise AttachmentError("Test Telegram file exceeds the configured input limit")
        return data

    def send_attachment(
        self,
        chat_id: int,
        attachment: AgentAttachment,
        caption: str | None = None,
    ) -> None:
        self.attachments.append((chat_id, attachment, caption))


def build_telegram_client(settings: Settings) -> TelegramClient:
    if settings.telegram_bot_token:
        return HttpTelegramClient(settings.telegram_bot_token)
    return LoggingTelegramClient()
