from __future__ import annotations

import base64
import binascii
import json
import mimetypes
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings

AttachmentKind = Literal["photo", "document"]

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[-\w.+/]+)(?:;[^,]*)?;base64,(?P<data>.+)$",
    re.DOTALL,
)
_SAFE_NAME_RE = re.compile(r"[^\w .()\[\]-]+", re.UNICODE)


class AttachmentError(RuntimeError):
    """Raised when an attachment cannot be handled safely."""


@dataclass(frozen=True)
class TelegramAttachment:
    file_id: str
    file_unique_id: str
    filename: str
    mime_type: str
    size_bytes: int
    kind: AttachmentKind


@dataclass(frozen=True)
class AgentAttachment:
    filename: str
    mime_type: str
    kind: AttachmentKind
    data: bytes | None = None
    source_url: str | None = None
    source_server: str | None = None
    source_container_id: str | None = None
    source_file_id: str | None = None


def safe_filename(value: str | None, *, default: str = "attachment.bin") -> str:
    name = (value or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _SAFE_NAME_RE.sub("_", name).strip(" .")
    if not name:
        name = default
    return name[:160]


def telegram_attachment_from_message(message: dict[str, Any]) -> TelegramAttachment | None:
    document = message.get("document")
    if isinstance(document, dict) and isinstance(document.get("file_id"), str):
        filename = safe_filename(document.get("file_name"))
        mime_type = str(document.get("mime_type") or "").strip().lower()
        if not mime_type or mime_type == "application/octet-stream":
            mime_type = mimetypes.guess_type(filename)[0] or mime_type or "application/octet-stream"
        return TelegramAttachment(
            file_id=document["file_id"],
            file_unique_id=str(document.get("file_unique_id") or document["file_id"]),
            filename=filename,
            mime_type=mime_type,
            size_bytes=max(int(document.get("file_size") or 0), 0),
            kind="photo" if mime_type.startswith("image/") else "document",
        )

    photos = message.get("photo")
    if not isinstance(photos, list):
        return None
    candidates = [item for item in photos if isinstance(item, dict) and item.get("file_id")]
    if not candidates:
        return None
    photo = max(
        candidates,
        key=lambda item: (
            int(item.get("file_size") or 0),
            int(item.get("width") or 0) * int(item.get("height") or 0),
        ),
    )
    unique_id = str(photo.get("file_unique_id") or photo["file_id"])
    return TelegramAttachment(
        file_id=str(photo["file_id"]),
        file_unique_id=unique_id,
        filename=safe_filename(f"photo-{unique_id}.jpg"),
        mime_type="image/jpeg",
        size_bytes=max(int(photo.get("file_size") or 0), 0),
        kind="photo",
    )


def encode_telegram_attachment(attachment: TelegramAttachment, text: str) -> str:
    return json.dumps(
        {"v": 1, "text": text, "attachment": asdict(attachment)},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def decode_message_payload(payload: str | None) -> tuple[str, TelegramAttachment | None]:
    raw = payload or ""
    if not raw.startswith("{"):
        return raw, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw, None
    if not isinstance(parsed, dict) or parsed.get("v") != 1:
        return raw, None
    item = parsed.get("attachment")
    if not isinstance(item, dict):
        return str(parsed.get("text") or ""), None
    try:
        attachment = TelegramAttachment(
            file_id=str(item["file_id"]),
            file_unique_id=str(item["file_unique_id"]),
            filename=safe_filename(str(item["filename"])),
            mime_type=str(item["mime_type"]).lower(),
            size_bytes=max(int(item["size_bytes"]), 0),
            kind="photo" if item.get("kind") == "photo" else "document",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AttachmentError("Invalid attachment job payload") from exc
    return str(parsed.get("text") or ""), attachment


def openai_user_message(
    text: str,
    attachment: TelegramAttachment,
    data: bytes,
) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode("ascii")
    content: list[dict[str, Any]] = []
    if attachment.kind == "photo" and attachment.mime_type.startswith("image/"):
        content.append(
            {
                "type": "input_image",
                "image_url": f"data:{attachment.mime_type};base64,{encoded}",
                "detail": "auto",
            }
        )
    else:
        content.append(
            {
                "type": "input_file",
                "filename": attachment.filename,
                "file_data": f"data:{attachment.mime_type};base64,{encoded}",
            }
        )
    content.append(
        {
            "type": "input_text",
            "text": text or f"Проанализируй вложение «{attachment.filename}».",
        }
    )
    return {"role": "user", "content": content}


def _object_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dumped if isinstance(dumped, dict) else None
    attributes = getattr(value, "__dict__", None)
    return dict(attributes) if isinstance(attributes, dict) else None


def _decode_base64(value: str, max_bytes: int) -> bytes:
    match = _DATA_URL_RE.match(value.strip())
    raw = match.group("data") if match else value
    raw = "".join(raw.split())
    if len(raw) > ((max_bytes + 2) // 3) * 4 + 8:
        raise AttachmentError("Attachment exceeds the configured output limit")
    try:
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttachmentError("Attachment contains invalid base64 data") from exc
    if len(data) > max_bytes:
        raise AttachmentError("Attachment exceeds the configured output limit")
    return data


def _mime_and_data(value: str, fallback_mime: str, max_bytes: int) -> tuple[str, bytes]:
    match = _DATA_URL_RE.match(value.strip())
    mime_type = match.group("mime").lower() if match else fallback_mime
    return mime_type, _decode_base64(value, max_bytes)


def _extension_for(mime_type: str) -> str:
    return mimetypes.guess_extension(mime_type, strict=False) or ".bin"


def _attachment_kind(mime_type: str) -> AttachmentKind:
    return "photo" if mime_type in {"image/jpeg", "image/png", "image/webp"} else "document"


def extract_response_attachments(
    response: Any,
    *,
    max_count: int,
    max_bytes: int,
) -> list[AgentAttachment]:
    found: list[AgentAttachment] = []
    container_files: set[tuple[str, str]] = set()

    def add_inline(
        data_value: str,
        *,
        mime_type: str,
        filename: str | None,
        source_server: str | None,
    ) -> None:
        if len(found) >= max_count:
            return
        resolved_mime, data = _mime_and_data(data_value, mime_type, max_bytes)
        default = f"attachment-{len(found) + 1}{_extension_for(resolved_mime)}"
        found.append(
            AgentAttachment(
                filename=safe_filename(filename, default=default),
                mime_type=resolved_mime,
                kind=_attachment_kind(resolved_mime),
                data=data,
                source_server=source_server,
            )
        )

    def add_url(
        url: str,
        *,
        mime_type: str,
        filename: str | None,
        source_server: str | None,
    ) -> None:
        if len(found) >= max_count or not url.lower().startswith("https://"):
            return
        resolved_mime = (
            mime_type
            or mimetypes.guess_type(urlparse(url).path)[0]
            or "application/octet-stream"
        )
        default = Path(urlparse(url).path).name or f"attachment-{len(found) + 1}.bin"
        found.append(
            AgentAttachment(
                filename=safe_filename(filename, default=default),
                mime_type=resolved_mime.lower(),
                kind=_attachment_kind(resolved_mime.lower()),
                source_url=url,
                source_server=source_server,
            )
        )

    def add_container_file(annotation: dict[str, Any]) -> None:
        if len(found) >= max_count:
            return
        container_id = str(annotation.get("container_id") or "").strip()
        file_id = str(annotation.get("file_id") or "").strip()
        if not container_id or not file_id or (container_id, file_id) in container_files:
            return
        container_files.add((container_id, file_id))
        filename = safe_filename(
            str(annotation.get("filename") or ""),
            default=f"generated-{len(found) + 1}.bin",
        )
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        found.append(
            AgentAttachment(
                filename=filename,
                mime_type=mime_type.lower(),
                kind=_attachment_kind(mime_type.lower()),
                source_server="openai",
                source_container_id=container_id,
                source_file_id=file_id,
            )
        )

    def walk(value: Any, source_server: str | None = None) -> None:
        if len(found) >= max_count:
            return
        if isinstance(value, list):
            for item in value:
                walk(item, source_server)
            return
        item = _object_dict(value)
        if item is None:
            return
        item_type = str(item.get("type") or "").lower()
        mime_type = str(item.get("mimeType") or item.get("mime_type") or "").lower()
        filename = item.get("filename") or item.get("name")

        if item_type == "resource" and isinstance(item.get("resource"), dict):
            walk(item["resource"], source_server)
            return
        data_value = item.get("data") or item.get("blob") or item.get("file_data")
        if isinstance(data_value, str) and (mime_type or data_value.startswith("data:")):
            add_inline(
                data_value,
                mime_type=mime_type or "application/octet-stream",
                filename=str(filename) if filename else None,
                source_server=source_server,
            )
            return
        text_value = item.get("text")
        if isinstance(text_value, str) and mime_type.startswith("text/"):
            if len(text_value.encode("utf-8")) <= max_bytes:
                add_inline(
                    base64.b64encode(text_value.encode("utf-8")).decode("ascii"),
                    mime_type=mime_type,
                    filename=str(filename) if filename else None,
                    source_server=source_server,
                )
            return
        url_value = next(
            (
                item.get(key)
                for key in ("download_url", "file_url", "url", "uri")
                if isinstance(item.get(key), str)
            ),
            None,
        )
        if isinstance(url_value, str) and (mime_type or filename or item_type == "resource_link"):
            add_url(
                url_value,
                mime_type=mime_type,
                filename=str(filename) if filename else None,
                source_server=source_server,
            )
            return
        for child in item.values():
            if isinstance(child, (dict, list)):
                walk(child, source_server)

    for output_item in getattr(response, "output", []) or []:
        item = _object_dict(output_item)
        if not item:
            continue
        if item.get("type") == "image_generation_call" and isinstance(item.get("result"), str):
            add_inline(
                item["result"],
                mime_type="image/png",
                filename=f"generated-{item.get('id') or len(found) + 1}.png",
                source_server=None,
            )
            continue
        if item.get("type") == "message":
            for content_item in item.get("content") or []:
                content = _object_dict(content_item)
                if not content:
                    continue
                for annotation_item in content.get("annotations") or []:
                    annotation = _object_dict(annotation_item)
                    if annotation and annotation.get("type") == "container_file_citation":
                        add_container_file(annotation)
            continue
        if item.get("type") != "mcp_call" or not isinstance(item.get("output"), str):
            continue
        try:
            payload = json.loads(item["output"])
        except json.JSONDecodeError:
            continue
        walk(payload, str(item.get("server_label") or "") or None)
    return found


def materialize_attachment(
    attachment: AgentAttachment,
    settings: Settings,
    *,
    access_token: str | None,
) -> AgentAttachment:
    if attachment.data is not None:
        if len(attachment.data) > settings.attachment_max_output_bytes:
            raise AttachmentError("Attachment exceeds the configured output limit")
        if attachment.mime_type not in settings.allowed_attachment_mime_types():
            raise AttachmentError("Attachment MIME type is not allowed")
        return attachment
    if not attachment.source_url:
        raise AttachmentError("Attachment has neither data nor a source URL")
    parsed = urlparse(attachment.source_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in settings.attachment_download_hosts():
        raise AttachmentError("Attachment source host is not allowed")

    server_hosts = {
        (urlparse(server.server_url).hostname or "").lower(): server.server_label
        for server in settings.mcp_servers()
    }
    headers: dict[str, str] = {}
    if attachment.source_server and server_hosts.get(host) == attachment.source_server:
        if not access_token:
            raise AttachmentError("Authorized attachment download requires a user token")
        headers["Authorization"] = f"Bearer {access_token}"

    chunks: list[bytes] = []
    total = 0
    with httpx.stream(
        "GET",
        attachment.source_url,
        headers=headers,
        timeout=settings.attachment_http_timeout_seconds,
        follow_redirects=False,
    ) as response:
        response.raise_for_status()
        try:
            declared = int(response.headers.get("content-length") or 0)
        except ValueError:
            declared = 0
        if declared > settings.attachment_max_output_bytes:
            raise AttachmentError("Attachment exceeds the configured output limit")
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > settings.attachment_max_output_bytes:
                raise AttachmentError("Attachment exceeds the configured output limit")
            chunks.append(chunk)
        response_mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
    mime_type = response_mime or attachment.mime_type
    if mime_type not in settings.allowed_attachment_mime_types():
        raise AttachmentError("Attachment MIME type is not allowed")
    return replace(
        attachment,
        mime_type=mime_type,
        kind=_attachment_kind(mime_type),
        data=b"".join(chunks),
        source_url=None,
    )


def encrypt_attachments(attachments: list[AgentAttachment], key: str) -> str:
    if not key:
        raise AttachmentError("TOKEN_ENCRYPTION_KEY is required for outbound attachments")
    try:
        cipher = Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise AttachmentError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc
    payload = [
        {
            "filename": item.filename,
            "mime_type": item.mime_type,
            "kind": item.kind,
            "data": base64.b64encode(item.data or b"").decode("ascii"),
        }
        for item in attachments
    ]
    return cipher.encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode("ascii")


def decrypt_attachments(
    value: str | None,
    key: str,
    *,
    max_count: int = 4,
    max_bytes: int = 10 * 1024 * 1024,
) -> list[AgentAttachment]:
    if not value:
        return []
    try:
        cipher = Fernet(key.encode("ascii"))
        raw = cipher.decrypt(value.encode("ascii"))
        payload = json.loads(raw)
    except (ValueError, UnicodeEncodeError, InvalidToken, json.JSONDecodeError) as exc:
        raise AttachmentError("Saved outbound attachments cannot be decrypted") from exc
    if not isinstance(payload, list):
        raise AttachmentError("Saved outbound attachment payload is invalid")
    if len(payload) > max_count:
        raise AttachmentError("Saved outbound attachment count exceeds the limit")
    result: list[AgentAttachment] = []
    for item in payload:
        if not isinstance(item, dict):
            raise AttachmentError("Saved outbound attachment entry is invalid")
        result.append(
            AgentAttachment(
                filename=safe_filename(str(item.get("filename") or "attachment.bin")),
                mime_type=str(item.get("mime_type") or "application/octet-stream"),
                kind="photo" if item.get("kind") == "photo" else "document",
                data=_decode_base64(str(item.get("data") or ""), max_bytes),
            )
        )
    return result
