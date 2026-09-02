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

MENU_ACTIONS = {
    "🆕 Новый диалог": "/new",
    "🔐 Авторизация": "/auth",
    "🧩 Инструменты": "/tools",
    "ℹ️ Возможности": "/help",
    "👥 Администраторы": "/admins",
    "🛠 Навыки": "/skills",
    "🔎 MCP статус": "/mcp_status",
    "⚙️ Управление": "/admin",
    "📊 Активность": "/activity",
    "⭐ Удовлетворённость": "/satisfaction",
    "👤 Пользователи": "/users",
}


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


def extract_callback(update: dict[str, Any]) -> tuple[int, int, int, str, str] | None:
    update_id = update.get("update_id")
    callback = update.get("callback_query")
    if not isinstance(update_id, int) or not isinstance(callback, dict):
        return None
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    user_id = sender.get("id")
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    callback_id = callback.get("id")
    data = callback.get("data")
    if not all(
        [
            isinstance(user_id, int),
            isinstance(chat_id, int),
            isinstance(callback_id, str),
            isinstance(data, str),
        ]
    ):
        return None
    if chat_type != "private" or user_id != chat_id:
        return None
    return update_id, user_id, chat_id, callback_id, data


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
    callback = extract_callback(update)
    if parsed is None and callback is not None:
        update_id, user_id, chat_id, callback_id, data = callback
        with factory() as session:
            if session.scalar(select(UpdateJob).where(UpdateJob.update_id == update_id)):
                return {"ok": True, "accepted": False, "reason": "duplicate"}
            session.add(
                UpdateJob(
                    update_id=update_id,
                    telegram_user_id=user_id,
                    chat_id=chat_id,
                    kind="callback",
                    payload_text=json.dumps(
                        {"callback_query_id": callback_id, "data": data},
                        ensure_ascii=False,
                    ),
                )
            )
            session.commit()
        return {"ok": True, "accepted": True}
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
        normalized_text = text.strip() if isinstance(text, str) else ""
        menu_command = MENU_ACTIONS.get(normalized_text, "")
        command = menu_command or (
            normalized_text.split(maxsplit=1)[0].lower()
            if normalized_text.startswith("/")
            else ""
        )

        created = False
        if command == "/start" and user is None:
            auto_approve = (
                user_id in settings.admin_ids()
                or user_id in settings.pilot_ids()
                or (
                    auto_approve_local_owner
                    and _can_auto_approve_local_owner(
                        session,
                        settings,
                        user_id,
                        user_exists=False,
                    )
                )
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
                approved_by="configured_allowlist" if auto_approve else None,
                approved_at=datetime.now(UTC) if auto_approve else None,
            )
            session.add(user)
            session.flush()
            created = True
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
                user.approved_by = "configured_allowlist"
                user.approved_at = datetime.now(UTC)
            elif (
                command == "/start"
                and user.status == AccessStatus.active
                and auto_approve_local_owner
                and user.approved_by in {"local_first_user", "configured_allowlist"}
            ):
                # Refresh the local owner's permissions after MCP configuration changes.
                user.allowed_tools_json = _local_allowed_tools(settings)

        kind = "message"
        payload = text if isinstance(text, str) else None
        if command in {
            "/start",
            "/menu",
            "/help",
            "/new",
            "/auth",
            "/tools",
            "/admin",
            "/admins",
            "/activity",
            "/satisfaction",
            "/login",
            "/logout",
            "/mcp_status",
            "/mcp_discover",
            "/skills",
            "/skill_show",
            "/skill_edit",
            "/skill_cancel",
            "/skill_rollback",
        }:
            kind = command[1:]
            if command == "/start" and created:
                payload = "new"
            elif command == "/mcp_discover" and isinstance(text, str):
                payload = text.removeprefix("/mcp_discover").strip().lower()
            else:
                payload = None
        elif command == "/revoke":
            kind = "revoke"
            payload = text.removeprefix("/revoke").strip() if isinstance(text, str) else ""
        elif command in {"/admin_add", "/admin_remove"}:
            kind = command[1:]
            command_parts = normalized_text.split(maxsplit=1)
            payload = command_parts[1] if len(command_parts) == 2 else ""
        elif command in {"/users", "/user", "/questions", "/allow"}:
            kind = command[1:]
            command_parts = normalized_text.split(maxsplit=1)
            payload = command_parts[1] if len(command_parts) == 2 else ""
        elif text is None:
            kind = "unsupported"
            payload = None
        elif len(text) > settings.max_message_chars:
            kind = "too_long"
            payload = None
        elif (
            user is None
            or user.status != AccessStatus.active
            or user.role not in {"pilot_user", "admin"}
        ):
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
