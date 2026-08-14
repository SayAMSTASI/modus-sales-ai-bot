from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import AccessStatus, UpdateJob, UserAccess
from app.policy import check_limits


def _request_number(user_id: int) -> str:
    today = datetime.now(UTC).strftime("%Y%m%d")
    return f"P-{today}-{str(user_id)[-6:]}-{secrets.token_hex(2).upper()}"


def extract_update(update: dict[str, Any]) -> tuple[int, int, int, str, str | None] | None:
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    if not isinstance(user_id, int) or not isinstance(chat_id, int):
        return None
    return update_id, user_id, chat_id, str(chat_type), message.get("text")


def _local_allowed_tools(settings: Settings) -> str:
    tools = [
        f"{server.server_label}:{tool}"
        for server in settings.mcp_servers()
        for tool in server.allowed_tools
    ]
    return json.dumps(tools)


def _can_auto_approve_local_owner(
    session: Session,
    settings: Settings,
    user_id: int,
    *,
    user_exists: bool,
) -> bool:
    if not settings.local_auto_approve_first_user:
        return False
    owner_id = settings.local_owner_telegram_user_id
    if owner_id is not None:
        active_count = session.scalar(
            select(func.count(UserAccess.id)).where(UserAccess.status == AccessStatus.active)
        )
        return user_id == owner_id and active_count == 0
    user_count = session.scalar(select(func.count(UserAccess.id)))
    return user_count == (1 if user_exists else 0)


def ingest_update(
    settings: Settings,
    factory: sessionmaker[Session],
    update: dict[str, Any],
    *,
    auto_approve_local_owner: bool = False,
) -> dict[str, Any]:
    parsed = extract_update(update)
    if parsed is None:
        return {"ok": True, "accepted": False, "reason": "unsupported_update"}
    update_id, user_id, chat_id, chat_type, text = parsed
    if chat_type != "private" or user_id != chat_id:
        return {"ok": True, "accepted": False, "reason": "private_chat_required"}

    with factory() as session:
        if session.scalar(select(UpdateJob).where(UpdateJob.update_id == update_id)):
            return {"ok": True, "accepted": False, "reason": "duplicate"}

        user = session.scalar(select(UserAccess).where(UserAccess.telegram_user_id == user_id))
        sender = update.get("message", {}).get("from") or {}
        username = str(sender.get("username") or "") or None
        command = (
            text.split(maxsplit=1)[0].lower()
            if isinstance(text, str) and text.startswith("/")
            else ""
        )

        if command == "/start" and user is None:
            auto_approve = auto_approve_local_owner and _can_auto_approve_local_owner(
                session,
                settings,
                user_id,
                user_exists=False,
            )
            user = UserAccess(
                telegram_user_id=user_id,
                chat_id=chat_id,
                telegram_username=username,
                corporate_name="Local owner" if auto_approve else None,
                status=AccessStatus.active if auto_approve else AccessStatus.pending,
                role="pilot_user",
                allowed_tools_json=_local_allowed_tools(settings) if auto_approve else "[]",
                request_number=_request_number(user_id),
                approved_by="local_first_user" if auto_approve else None,
                approved_at=datetime.now(UTC) if auto_approve else None,
            )
            session.add(user)
            session.flush()
        elif user is not None:
            user.chat_id = chat_id
            user.telegram_username = username
            if (
                command == "/start"
                and user.status == AccessStatus.pending
                and auto_approve_local_owner
                and _can_auto_approve_local_owner(
                    session,
                    settings,
                    user_id,
                    user_exists=True,
                )
            ):
                user.status = AccessStatus.active
                user.allowed_tools_json = _local_allowed_tools(settings)
                user.approved_by = "local_first_user"
                user.approved_at = datetime.now(UTC)
            elif (
                command == "/start"
                and user.status == AccessStatus.active
                and auto_approve_local_owner
                and user.approved_by == "local_first_user"
            ):
                # Refresh the local owner's permissions after MCP configuration changes.
                user.allowed_tools_json = _local_allowed_tools(settings)

        kind = "message"
        payload = text if isinstance(text, str) else None
        if command in {"/start", "/help", "/new"}:
            kind = command[1:]
            payload = None
        elif text is None:
            kind = "unsupported"
            payload = None
        elif len(text) > settings.max_message_chars:
            kind = "too_long"
            payload = None
        elif user is None or user.status != AccessStatus.active or user.role != "pilot_user":
            kind = "access_denied"
            payload = None
        else:
            limit_reason = check_limits(session, user, settings)
            if limit_reason:
                kind = "limit_denied"
                payload = limit_reason
            elif command == "/mcp":
                parts = text.split(maxsplit=2)
                if len(parts) < 3:
                    kind = "mcp_help"
                    payload = None
                else:
                    kind = "mcp"
                    payload = json.dumps(
                        {"server": parts[1].lower(), "query": parts[2]},
                        ensure_ascii=False,
                    )

        session.add(
            UpdateJob(
                update_id=update_id,
                telegram_user_id=user_id,
                chat_id=chat_id,
                kind=kind,
                payload_text=payload,
            )
        )
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            return {"ok": True, "accepted": False, "reason": "duplicate"}
    return {"ok": True, "accepted": True}
