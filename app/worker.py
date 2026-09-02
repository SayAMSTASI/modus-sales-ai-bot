from __future__ import annotations

import difflib
import json
import logging
import signal
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent import AgentClient, McpUnavailableError, build_agent_client, calculate_cost_usd
from app.config import Settings, get_settings
from app.db import initialize_development_schema, make_engine, make_session_factory
from app.logging_config import configure_logging
from app.mcp_discovery import McpDiscoveryClient, McpDiscoveryError
from app.metrics import MetricsExporter, build_metrics_exporter
from app.models import (
    AccessStatus,
    AdminAudit,
    ConversationMessage,
    JobStatus,
    OAuthAuthorizationSession,
    OAuthDeviceSession,
    SkillEditSession,
    UpdateJob,
    UsageEvent,
    UserAccess,
)
from app.oauth import (
    KeycloakOAuthClient,
    OAuthConfigurationError,
    OAuthLoginRequired,
    OAuthProtocolError,
    OAuthTokenStore,
    oauth_state_hash,
)
from app.policy import check_limits
from app.security import stable_user_hash
from app.skills import (
    active_skill_overrides,
    active_skill_version,
    available_skills,
    base_skill_content,
    create_skill_version,
    rollback_skill,
)
from app.telegram import TelegramClient, build_telegram_client

logger = logging.getLogger(__name__)


def classify_scenario(text: str) -> str:
    lowered = text.lower()
    meeting = any(word in lowered for word in ("встреч", "ктолк", "ktalk", "запис", "расшифров"))
    crm = any(word in lowered for word in ("сделк", "клиент", "компан", "crm", "битрикс", "bitrix"))
    if meeting and crm:
        return "meeting_to_crm"
    if meeting:
        return "meeting"
    if crm:
        return "crm_lookup"
    if any(word in lowered for word in ("письм", "ответ", "предложен", "резюме", "итог")):
        return "draft_or_summary"
    return "other"


def shared_allowed_tools(settings: Settings) -> list[str]:
    return [
        f"{server.server_label}:{tool}"
        for server in settings.mcp_servers()
        for tool in server.allowed_tools
    ]


def mcp_capability_note(settings: Settings, *, authorized: bool) -> str:
    lines = [
        "Фактическая доступность MCP в текущем запросе:",
    ]
    for server in settings.mcp_servers():
        if not server.allowed_tools:
            status = "отключён: read-only allowlist пока пуст"
        elif authorized:
            status = f"подключён; разрешено tools: {', '.join(server.allowed_tools)}"
        else:
            status = "не подключён: пользователь не выполнил /login"
        lines.append(f"- {server.server_label}: {status}.")
    lines.append(
        "Не заявляй, что можешь использовать отключённый или неавторизованный MCP. "
        "Если нужного MCP нет, прямо назови причину и предложи /login или /mcp_status."
    )
    return "\n".join(lines)


def is_admin(session: Session, settings: Settings, telegram_user_id: int) -> bool:
    if telegram_user_id in settings.admin_ids():
        return True
    return bool(
        session.scalar(
            select(UserAccess.id).where(
                UserAccess.telegram_user_id == telegram_user_id,
                UserAccess.role == "admin",
                UserAccess.status == AccessStatus.active,
            )
        )
    )


def active_admin_ids(session: Session, settings: Settings) -> set[int]:
    result = set(settings.admin_ids())
    result.update(
        session.scalars(
            select(UserAccess.telegram_user_id).where(
                UserAccess.role == "admin",
                UserAccess.status == AccessStatus.active,
            )
        )
    )
    return result


def main_menu_markup(*, admin: bool) -> dict[str, Any]:
    keyboard = [
        [
            {"text": "🆕 Новый диалог"},
            {"text": "🔐 Авторизация"},
        ],
        [
            {"text": "🧩 Инструменты"},
            {"text": "ℹ️ Возможности"},
        ],
    ]
    if admin:
        keyboard.extend(
            [
                [
                    {"text": "⚙️ Управление"},
                    {"text": "🔎 MCP статус"},
                ],
                [
                    {"text": "👥 Администраторы"},
                    {"text": "🛠 Навыки"},
                ],
            ]
        )
    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Напишите задачу агенту…",
    }


def help_text(*, admin: bool) -> str:
    text = (
        "Что умеет Sales AI\n\n"
        "• Общается в обычном диалоге и помнит контекст.\n"
        "• Применяет общий набор sales skills.\n"
        "• Читает задачи Jira, данные CRM Bitrix и записи KTalk через MCP.\n"
        "• Работает только с вашими корпоративными правами после /login.\n\n"
        "Как пользоваться\n"
        "1. Нажмите «🔐 Авторизация» и выполните /login.\n"
        "2. Просто опишите задачу обычным текстом.\n"
        "3. Для нового контекста нажмите «🆕 Новый диалог».\n\n"
        "Примеры\n"
        "• Покажи задачу SH-501 и кратко перескажи статус.\n"
        "• Найди последние сделки компании в Bitrix.\n"
        "• Сделай протокол последней встречи KTalk."
    )
    if admin:
        text += (
            "\n\nАдминистратору\n"
            "• «⚙️ Управление» — команды управления.\n"
            "• «👥 Администраторы» — список администраторов.\n"
            "• «🛠 Навыки» — просмотр и изменение skills.\n"
            "• /admin_add <Telegram ID> — добавить администратора.\n"
            "• /admin_remove <Telegram ID> — убрать добавленного администратора.\n"
            "• /revoke <Telegram ID> — отозвать доступ пользователя."
        )
    return text


class JobProcessor:
    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        agent: AgentClient,
        telegram: TelegramClient,
        metrics: MetricsExporter,
        oauth_store: OAuthTokenStore | None = None,
        mcp_discovery: McpDiscoveryClient | None = None,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.agent = agent
        self.telegram = telegram
        self.metrics = metrics
        self.oauth_store = oauth_store or OAuthTokenStore(settings)
        self.mcp_discovery = mcp_discovery or McpDiscoveryClient(
            timeout_seconds=settings.oauth_http_timeout_seconds
        )

    def _claim(self, session: Session) -> UpdateJob | None:
        cleanup = session.execute(
            delete(ConversationMessage).where(
                ConversationMessage.expires_at <= datetime.now(UTC)
            )
        )
        if cleanup.rowcount:
            session.commit()
        job = session.scalar(
            select(UpdateJob)
            .where(
                UpdateJob.status == JobStatus.queued,
                UpdateJob.available_at <= datetime.now(UTC),
            )
            .order_by(UpdateJob.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None
        job.status = JobStatus.processing
        job.attempts += 1
        session.commit()
        return job

    def _finish(self, session: Session, job: UpdateJob) -> None:
        job.status = JobStatus.done
        job.payload_text = None
        job.response_text = None
        job.error_code = None
        job.finished_at = datetime.now(UTC)
        session.commit()

    def _send_and_finish(
        self,
        session: Session,
        job: UpdateJob,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        self._send(job.chat_id, text, reply_markup)
        self._finish(session, job)

    def _send(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = [text[index : index + 4000] for index in range(0, len(text), 4000)] or [""]
        for index, chunk in enumerate(chunks):
            markup = reply_markup if index == len(chunks) - 1 else None
            self.telegram.send_message(chat_id, chunk, markup)

    def _notice(self, session: Session, user_id: int, chat_id: int, text: str) -> None:
        session.add(
            UpdateJob(
                telegram_user_id=user_id,
                chat_id=chat_id,
                kind="system_notice",
                payload_text=text,
            )
        )

    def _audit(
        self,
        session: Session,
        *,
        admin_id: int,
        action: str,
        target_id: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            AdminAudit(
                admin_name=str(admin_id),
                action=action,
                target_telegram_user_id=target_id,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
            )
        )

    def _load_context(self, session: Session, job: UpdateJob) -> list[dict[str, str]]:
        now = datetime.now(UTC)
        session.execute(delete(ConversationMessage).where(ConversationMessage.expires_at <= now))
        rows = session.scalars(
            select(ConversationMessage)
            .where(
                ConversationMessage.telegram_user_id == job.telegram_user_id,
                ConversationMessage.chat_id == job.chat_id,
                ConversationMessage.expires_at > now,
            )
            .order_by(ConversationMessage.created_at.desc())
            .limit(self.settings.context_max_messages)
        ).all()
        selected: list[ConversationMessage] = []
        size = 0
        for row in rows:
            if size + len(row.content) > self.settings.context_max_chars:
                break
            selected.append(row)
            size += len(row.content)
        return [{"role": row.role, "content": row.content} for row in reversed(selected)]

    def _skill_session(self, session: Session, admin_id: int) -> SkillEditSession | None:
        return session.scalar(
            select(SkillEditSession).where(
                SkillEditSession.admin_telegram_user_id == admin_id
            )
        )

    def _selected_skill(
        self,
        session: Session,
        admin_id: int,
    ) -> tuple[SkillEditSession | None, str | None]:
        edit_session = self._skill_session(session, admin_id)
        if edit_session is None:
            return None, None
        if edit_session.skill_name not in available_skills(self.settings.project_dir):
            session.delete(edit_session)
            return None, None
        return edit_session, edit_session.skill_name

    def _current_skill_content(self, session: Session, skill_name: str) -> tuple[str, str]:
        version = active_skill_version(session, skill_name)
        if version is not None:
            return version.content, f"версия {version.version} из БД"
        return base_skill_content(self.settings.project_dir, skill_name), "базовая версия из Git"

    def _notify_access_request(self, session: Session, user: UserAccess) -> None:
        username = f"@{user.telegram_username}" if user.telegram_username else "без username"
        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "Разрешить",
                        "callback_data": f"access:approve:{user.telegram_user_id}",
                    },
                    {
                        "text": "Отклонить",
                        "callback_data": f"access:deny:{user.telegram_user_id}",
                    },
                ]
            ]
        }
        for admin_id in sorted(active_admin_ids(session, self.settings)):
            try:
                self.telegram.send_message(
                    admin_id,
                    "Новый запрос доступа к Sales Bot\n"
                    f"Telegram ID: {user.telegram_user_id}\n"
                    f"Пользователь: {username}",
                    markup,
                )
            except Exception:
                logger.exception("Failed to notify Telegram admin admin_id=%s", admin_id)

    def _process_access_callback(
        self,
        session: Session,
        job: UpdateJob,
        action: str,
        target_id: int,
    ) -> str:
        target = session.scalar(
            select(UserAccess).where(UserAccess.telegram_user_id == target_id)
        )
        if target is None:
            return "Пользователь не найден."
        if action == "approve":
            target.status = AccessStatus.active
            target.allowed_tools_json = json.dumps(shared_allowed_tools(self.settings))
            target.approved_by = str(job.telegram_user_id)
            target.approved_at = datetime.now(UTC)
            self._audit(
                session,
                admin_id=job.telegram_user_id,
                action="approve",
                target_id=target_id,
            )
            self._notice(
                session,
                target.telegram_user_id,
                target.chat_id,
                "Доступ подтверждён. Можно отправлять запросы боту.",
            )
            return f"Доступ пользователю {target_id} выдан."
        target.status = AccessStatus.revoked
        session.execute(
            delete(ConversationMessage).where(
                ConversationMessage.telegram_user_id == target_id
            )
        )
        self.oauth_store.delete(session, target_id)
        session.execute(
            delete(OAuthDeviceSession).where(
                OAuthDeviceSession.telegram_user_id == target_id
            )
        )
        session.execute(
            delete(OAuthAuthorizationSession).where(
                OAuthAuthorizationSession.telegram_user_id == target_id
            )
        )
        self._audit(
            session,
            admin_id=job.telegram_user_id,
            action="deny" if action == "deny" else "revoke",
            target_id=target_id,
        )
        self._notice(
            session,
            target.telegram_user_id,
            target.chat_id,
            "Доступ к пилоту закрыт. Обратитесь к администратору.",
        )
        return f"Доступ пользователя {target_id} закрыт."

    def _process_skill_callback(
        self,
        session: Session,
        job: UpdateJob,
        parts: list[str],
    ) -> str:
        action = parts[1] if len(parts) > 1 else ""
        if action == "select" and len(parts) == 3:
            skill_name = parts[2]
            if skill_name not in available_skills(self.settings.project_dir):
                return "Skill не найден."
            edit_session = self._skill_session(session, job.telegram_user_id)
            if edit_session is None:
                edit_session = SkillEditSession(
                    admin_telegram_user_id=job.telegram_user_id,
                    skill_name=skill_name,
                )
                session.add(edit_session)
            else:
                edit_session.skill_name = skill_name
                edit_session.state = "selected"
                edit_session.draft_content = None
            return f"Выбран skill: {skill_name}. Используйте /skill_show или /skill_edit."
        edit_session, skill_name = self._selected_skill(session, job.telegram_user_id)
        if edit_session is None or skill_name is None:
            return "Сначала выберите skill командой /skills."
        if action == "apply" and edit_session.state == "awaiting_confirm":
            draft = (edit_session.draft_content or "").strip()
            if not draft:
                return "Черновик пуст. Повторите /skill_edit."
            version = create_skill_version(
                session,
                skill_name=skill_name,
                content=draft,
                admin_telegram_user_id=job.telegram_user_id,
            )
            edit_session.state = "selected"
            edit_session.draft_content = None
            self._audit(
                session,
                admin_id=job.telegram_user_id,
                action="skill_apply",
                target_id=job.telegram_user_id,
                metadata={"skill": skill_name, "version": version.version},
            )
            return f"Skill {skill_name}: версия {version.version} применена."
        if action == "cancel":
            edit_session.state = "selected"
            edit_session.draft_content = None
            return "Изменение отменено."
        return "Действие устарело. Повторите команду /skills."

    def _process_callback(self, session: Session, job: UpdateJob) -> None:
        try:
            payload = json.loads(job.payload_text or "{}")
            callback_id = str(payload["callback_query_id"])
            data = str(payload["data"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._finish(session, job)
            return
        if not is_admin(session, self.settings, job.telegram_user_id):
            self.telegram.answer_callback_query(callback_id, "Недостаточно прав")
            self._send_and_finish(session, job, "Команда доступна только администратору.")
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[0] == "access" and parts[1] in {
            "approve",
            "deny",
            "revoke",
        }:
            try:
                text = self._process_access_callback(session, job, parts[1], int(parts[2]))
            except ValueError:
                text = "Некорректный Telegram ID."
        elif parts and parts[0] == "skill":
            text = self._process_skill_callback(session, job, parts)
        else:
            text = "Неизвестное действие."
        self.telegram.answer_callback_query(callback_id, text)
        self._send_and_finish(session, job, text)

    def _process_skill_command(self, session: Session, job: UpdateJob) -> None:
        if not is_admin(session, self.settings, job.telegram_user_id):
            self._send_and_finish(session, job, "Команда доступна только администратору.")
            return
        if job.kind == "skills":
            names = available_skills(self.settings.project_dir)
            markup = {
                "inline_keyboard": [
                    [{"text": name, "callback_data": f"skill:select:{name}"}]
                    for name in names
                ]
            }
            self._send_and_finish(
                session,
                job,
                "Выберите skill для просмотра или редактирования.",
                markup,
            )
            return
        edit_session, skill_name = self._selected_skill(session, job.telegram_user_id)
        if edit_session is None or skill_name is None:
            self._send_and_finish(session, job, "Сначала выберите skill командой /skills.")
            return
        if job.kind == "skill_show":
            content, source = self._current_skill_content(session, skill_name)
            self._send_and_finish(
                session,
                job,
                f"Skill: {skill_name}\nИсточник: {source}\n\n{content}",
            )
        elif job.kind == "skill_edit":
            edit_session.state = "awaiting_content"
            edit_session.draft_content = None
            self._send_and_finish(
                session,
                job,
                f"Отправьте новый полный текст skill {skill_name} одним сообщением. "
                "Для отмены используйте /skill_cancel.",
            )
        elif job.kind == "skill_cancel":
            edit_session.state = "selected"
            edit_session.draft_content = None
            self._send_and_finish(session, job, "Редактирование отменено.")
        elif job.kind == "skill_rollback":
            current = active_skill_version(session, skill_name)
            if current is None:
                self._send_and_finish(
                    session,
                    job,
                    "Для этого skill нет активной версии из БД; используется Git.",
                )
                return
            previous = rollback_skill(session, skill_name)
            source = f"версия {previous.version}" if previous else "базовая версия из Git"
            self._audit(
                session,
                admin_id=job.telegram_user_id,
                action="skill_rollback",
                target_id=job.telegram_user_id,
                metadata={"skill": skill_name, "to": source},
            )
            self._send_and_finish(
                session,
                job,
                f"Skill {skill_name} возвращён к: {source}.",
            )

    def _process_admin_command(self, session: Session, job: UpdateJob) -> None:
        if not is_admin(session, self.settings, job.telegram_user_id):
            self._send_and_finish(session, job, "Команда доступна только администратору.")
            return

        if job.kind == "admin":
            self._send_and_finish(
                session,
                job,
                "Управление ботом\n\n"
                "• 👥 Администраторы — посмотреть текущих администраторов.\n"
                "• 🛠 Навыки — выбрать и изменить skill.\n"
                "• 🔎 MCP статус — проверить корпоративные интеграции.\n\n"
                "Команды с параметром:\n"
                "/admin_add <Telegram ID> — добавить администратора;\n"
                "/admin_remove <Telegram ID> — убрать добавленного администратора;\n"
                "/revoke <Telegram ID> — отозвать доступ пользователя.",
                main_menu_markup(admin=True),
            )
            return

        if job.kind == "admins":
            rows: list[str] = []
            for admin_id in sorted(active_admin_ids(session, self.settings)):
                user = session.scalar(
                    select(UserAccess).where(UserAccess.telegram_user_id == admin_id)
                )
                username = (
                    f"@{user.telegram_username}"
                    if user is not None and user.telegram_username
                    else "без username"
                )
                source = (
                    "конфигурация сервера"
                    if admin_id in self.settings.admin_ids()
                    else "добавлен через Telegram"
                )
                rows.append(f"• {admin_id} — {username} — {source}")
            self._send_and_finish(
                session,
                job,
                "Администраторы\n\n"
                + "\n".join(rows)
                + "\n\nДобавить: /admin_add <Telegram ID>\n"
                "Убрать: /admin_remove <Telegram ID>",
                main_menu_markup(admin=True),
            )
            return

        raw_target = (job.payload_text or "").strip()
        try:
            target_id = int(raw_target)
            if target_id <= 0:
                raise ValueError
        except ValueError:
            self._send_and_finish(
                session,
                job,
                f"Формат: /{job.kind} <числовой Telegram ID>.",
                main_menu_markup(admin=True),
            )
            return

        target = session.scalar(
            select(UserAccess).where(UserAccess.telegram_user_id == target_id)
        )
        if job.kind == "admin_add":
            if target_id in active_admin_ids(session, self.settings):
                self._send_and_finish(
                    session,
                    job,
                    f"Пользователь {target_id} уже администратор.",
                    main_menu_markup(admin=True),
                )
                return
            target_existed = target is not None
            if target is None:
                target = UserAccess(
                    telegram_user_id=target_id,
                    chat_id=target_id,
                    status=AccessStatus.active,
                    role="admin",
                    allowed_tools_json=json.dumps(shared_allowed_tools(self.settings)),
                    request_number=(
                        f"A-{datetime.now(UTC):%Y%m%d}-{str(target_id)[-6:]}-{job.id}"
                    ),
                    approved_by=str(job.telegram_user_id),
                    approved_at=datetime.now(UTC),
                )
                session.add(target)
            else:
                target.status = AccessStatus.active
                target.role = "admin"
                target.allowed_tools_json = json.dumps(shared_allowed_tools(self.settings))
                target.approved_by = str(job.telegram_user_id)
                target.approved_at = datetime.now(UTC)
            self._audit(
                session,
                admin_id=job.telegram_user_id,
                action="admin_add",
                target_id=target_id,
            )
            if target_existed:
                self._notice(
                    session,
                    target_id,
                    target.chat_id,
                    "Вам выданы права администратора Sales AI Bot.",
                )
            self._send_and_finish(
                session,
                job,
                f"Администратор {target_id} добавлен. "
                "Если он ещё не писал боту, ему нужно отправить /start.",
                main_menu_markup(admin=True),
            )
            return

        if target_id in self.settings.admin_ids():
            self._send_and_finish(
                session,
                job,
                "Это базовый администратор из ADMIN_TELEGRAM_IDS. "
                "Его можно убрать только в защищённой конфигурации сервера.",
                main_menu_markup(admin=True),
            )
            return
        if target is None or target.role != "admin":
            self._send_and_finish(
                session,
                job,
                f"Пользователь {target_id} не является добавленным администратором.",
                main_menu_markup(admin=True),
            )
            return
        if target_id == job.telegram_user_id and len(
            active_admin_ids(session, self.settings)
        ) <= 1:
            self._send_and_finish(
                session,
                job,
                "Нельзя удалить последнего администратора.",
                main_menu_markup(admin=True),
            )
            return
        target.role = "pilot_user"
        self._audit(
            session,
            admin_id=job.telegram_user_id,
            action="admin_remove",
            target_id=target_id,
        )
        self._notice(
            session,
            target_id,
            target.chat_id,
            "Права администратора сняты. Пользовательский доступ сохранён.",
        )
        self._send_and_finish(
            session,
            job,
            f"Администратор {target_id} удалён. Пользовательский доступ сохранён.",
            main_menu_markup(admin=True),
        )

    def _process_admin_draft(self, session: Session, job: UpdateJob) -> bool:
        if not is_admin(session, self.settings, job.telegram_user_id):
            return False
        edit_session, skill_name = self._selected_skill(session, job.telegram_user_id)
        if (
            edit_session is None
            or skill_name is None
            or edit_session.state != "awaiting_content"
        ):
            return False
        draft = (job.payload_text or "").strip()
        if not draft:
            self._send_and_finish(session, job, "Текст skill не может быть пустым.")
            return True
        current, source = self._current_skill_content(session, skill_name)
        diff = "\n".join(
            difflib.unified_diff(
                current.splitlines(),
                draft.splitlines(),
                fromfile=source,
                tofile="новая версия",
                lineterm="",
            )
        )
        edit_session.state = "awaiting_confirm"
        edit_session.draft_content = draft
        markup = {
            "inline_keyboard": [
                [
                    {"text": "Применить", "callback_data": "skill:apply"},
                    {"text": "Отменить", "callback_data": "skill:cancel"},
                ]
            ]
        }
        preview = diff if diff else "Текст не отличается от текущей версии."
        self._send_and_finish(
            session,
            job,
            f"Изменения skill {skill_name}:\n\n{preview}",
            markup,
        )
        return True

    def _start_login(self, session: Session, job: UpdateJob, user: UserAccess | None) -> None:
        if user is None or user.status != AccessStatus.active:
            self._send_and_finish(session, job, "Сначала получите доступ через /start.")
            return
        try:
            existing = self.oauth_store.access_token(session, job.telegram_user_id)
        except OAuthLoginRequired:
            existing = None
        if existing:
            self._send_and_finish(
                session,
                job,
                "Keycloak уже подключён. Для нового входа сначала выполните /logout.",
            )
            return
        if self.settings.keycloak_flow == "authorization_code":
            try:
                authorization = self.oauth_store.client.start_authorization_code()
                cipher = self.oauth_store.cipher()
            except (OAuthConfigurationError, OAuthProtocolError, httpx.HTTPError) as exc:
                logger.warning("OAuth login start failed: %s", type(exc).__name__)
                self._send_and_finish(
                    session,
                    job,
                    "Авторизация пока не настроена. Администратору нужны "
                    "Keycloak client_id и HTTPS callback.",
                )
                return
            session.execute(
                delete(OAuthAuthorizationSession).where(
                    OAuthAuthorizationSession.telegram_user_id == job.telegram_user_id
                )
            )
            session.execute(
                delete(OAuthDeviceSession).where(
                    OAuthDeviceSession.telegram_user_id == job.telegram_user_id
                )
            )
            now = datetime.now(UTC)
            session.add(
                OAuthAuthorizationSession(
                    state_hash=oauth_state_hash(authorization.state),
                    telegram_user_id=job.telegram_user_id,
                    chat_id=job.chat_id,
                    code_verifier_encrypted=cipher.encrypt(authorization.code_verifier),
                    expires_at=now + timedelta(seconds=authorization.expires_in),
                )
            )
            self._send_and_finish(
                session,
                job,
                "Откройте официальную страницу Keycloak и войдите корпоративной "
                "учётной записью:\n"
                f"{authorization.authorization_url}\n"
                f"Ссылка действует {authorization.expires_in // 60 or 1} мин. "
                "Пароль и OTP в Telegram не отправляйте.",
            )
            return
        try:
            authorization = self.oauth_store.client.start_device_authorization()
            cipher = self.oauth_store.cipher()
        except (OAuthConfigurationError, OAuthProtocolError, httpx.HTTPError) as exc:
            logger.warning("OAuth login start failed: %s", type(exc).__name__)
            self._send_and_finish(
                session,
                job,
                "Авторизация пока не настроена. Администратору нужен Keycloak client_id.",
            )
            return
        session.execute(
            delete(OAuthDeviceSession).where(
                OAuthDeviceSession.telegram_user_id == job.telegram_user_id
            )
        )
        session.execute(
            delete(OAuthAuthorizationSession).where(
                OAuthAuthorizationSession.telegram_user_id == job.telegram_user_id
            )
        )
        now = datetime.now(UTC)
        session.add(
            OAuthDeviceSession(
                telegram_user_id=job.telegram_user_id,
                chat_id=job.chat_id,
                device_code_encrypted=cipher.encrypt(authorization.device_code),
                user_code=authorization.user_code,
                verification_uri=authorization.verification_uri,
                verification_uri_complete=authorization.verification_uri_complete,
                interval_seconds=authorization.interval,
                expires_at=now + timedelta(seconds=authorization.expires_in),
                next_poll_at=now + timedelta(seconds=authorization.interval),
            )
        )
        link = authorization.verification_uri_complete or authorization.verification_uri
        code_line = (
            ""
            if authorization.verification_uri_complete
            else f"\nКод: {authorization.user_code}"
        )
        self._send_and_finish(
            session,
            job,
            "Откройте официальную страницу Keycloak и войдите корпоративной учётной записью:\n"
            f"{link}{code_line}\n"
            f"Ссылка действует {authorization.expires_in // 60 or 1} мин. "
            "Пароль и OTP в Telegram не отправляйте.",
        )

    def _logout(self, session: Session, job: UpdateJob) -> None:
        removed = self.oauth_store.logout(session, job.telegram_user_id)
        session.execute(
            delete(OAuthDeviceSession).where(
                OAuthDeviceSession.telegram_user_id == job.telegram_user_id
            )
        )
        session.execute(
            delete(OAuthAuthorizationSession).where(
                OAuthAuthorizationSession.telegram_user_id == job.telegram_user_id
            )
        )
        text = (
            "Keycloak отключён. Сохранённые OAuth-токены удалены."
            if removed
            else "Активного подключения Keycloak нет."
        )
        self._send_and_finish(session, job, text)

    def _process_message(
        self,
        session: Session,
        job: UpdateJob,
        user: UserAccess,
        *,
        message_text: str | None = None,
        required_mcp_server: str | None = None,
    ) -> None:
        if user.status != AccessStatus.active:
            job.response_text = None
            self._send_and_finish(
                session,
                job,
                "Запрос не обработан. Доступ к пилоту не подтверждён или отозван.",
            )
            return
        if job.response_text is None:
            limit_reason = check_limits(session, user, self.settings)
            if limit_reason:
                self._send_and_finish(
                    session,
                    job,
                    "Общий дневной лимит пилота исчерпан. Обратитесь к администратору.",
                )
                return
            message_text = message_text if message_text is not None else (job.payload_text or "")
            history = self._load_context(session, job)
            history.append({"role": "user", "content": message_text})
            user_hash = stable_user_hash(
                job.telegram_user_id,
                self.settings.safety_identifier_secret,
            )
            try:
                mcp_token = self.oauth_store.access_token(session, job.telegram_user_id)
            except (OAuthConfigurationError, OAuthLoginRequired):
                mcp_token = None
            if required_mcp_server and not mcp_token:
                self._send_and_finish(
                    session,
                    job,
                    "Для MCP требуется корпоративная авторизация. Выполните /login.",
                )
                return
            history.insert(
                0,
                {
                    "role": "developer",
                    "content": mcp_capability_note(
                        self.settings,
                        authorized=bool(mcp_token),
                    ),
                },
            )
            try:
                result = self.agent.respond(
                    messages=history,
                    safety_identifier=user_hash,
                    allowed_tools=shared_allowed_tools(self.settings),
                    required_mcp_server=required_mcp_server,
                    mcp_access_token=mcp_token,
                    instruction_overrides=active_skill_overrides(session),
                )
            except McpUnavailableError:
                self._send_and_finish(
                    session,
                    job,
                    "MCP пока не подключён: отсутствует авторизация или "
                    "не утверждён read-only список tools. Используйте /login.",
                )
                return
            event = UsageEvent(
                user_hash=user_hash,
                request_id=result.request_id,
                scenario=classify_scenario(message_text),
                result="ok",
                duration_ms=result.duration_ms,
                model=result.model,
                input_tokens=result.usage.input_tokens,
                cached_input_tokens=result.usage.cached_input_tokens,
                output_tokens=result.usage.output_tokens,
                estimated_cost_usd=calculate_cost_usd(result.usage, self.settings),
            )
            session.add(event)
            job.response_text = result.text
            session.commit()
            try:
                self.metrics.export(event)
            except Exception:
                logger.exception("Metrics export failed event_id=%s", event.id)

        response_text = job.response_text or "Ответ не сформирован."
        original_text = message_text if message_text is not None else (job.payload_text or "")
        self._send(job.chat_id, response_text)
        expires = datetime.now(UTC) + timedelta(hours=self.settings.context_ttl_hours)
        session.add_all(
            [
                ConversationMessage(
                    telegram_user_id=job.telegram_user_id,
                    chat_id=job.chat_id,
                    role="user",
                    content=original_text,
                    expires_at=expires,
                ),
                ConversationMessage(
                    telegram_user_id=job.telegram_user_id,
                    chat_id=job.chat_id,
                    role="assistant",
                    content=response_text,
                    expires_at=expires,
                ),
            ]
        )
        self._finish(session, job)

    def _process(self, session: Session, job: UpdateJob) -> None:
        created_at = job.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - created_at > timedelta(hours=self.settings.job_payload_ttl_hours):
            self._send_and_finish(
                session,
                job,
                "Срок обработки сообщения истёк. Отправьте запрос повторно.",
            )
            return
        user = session.scalar(
            select(UserAccess).where(UserAccess.telegram_user_id == job.telegram_user_id)
        )
        if job.kind == "system_notice":
            self._send_and_finish(session, job, job.payload_text or "Статус изменён.")
        elif job.kind == "callback":
            self._process_callback(session, job)
        elif job.kind == "start":
            if user is None:
                self._send_and_finish(session, job, "Не удалось обработать /start.")
            elif user.status == AccessStatus.pending:
                if job.payload_text == "new":
                    self._notify_access_request(session, user)
                self._send_and_finish(
                    session,
                    job,
                    "Запрос доступа отправлен администратору. "
                    "До подтверждения OpenAI и MCP не вызываются.",
                    main_menu_markup(admin=False),
                )
            elif user.status == AccessStatus.active:
                self._send_and_finish(
                    session,
                    job,
                    "Sales AI готов к работе.\n\n"
                    "Напишите задачу обычным текстом или выберите действие ниже. "
                    "Для Jira, Bitrix и KTalk сначала нужна корпоративная авторизация.",
                    main_menu_markup(
                        admin=is_admin(session, self.settings, job.telegram_user_id)
                    ),
                )
            else:
                self._send_and_finish(session, job, "Доступ отозван. Обратитесь к администратору.")
        elif job.kind in {"menu", "help"}:
            admin = is_admin(session, self.settings, job.telegram_user_id)
            self._send_and_finish(
                session,
                job,
                (
                    "Выберите действие или напишите задачу агенту."
                    if job.kind == "menu"
                    else help_text(admin=admin)
                ),
                main_menu_markup(admin=admin),
            )
        elif job.kind == "new":
            session.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.telegram_user_id == job.telegram_user_id,
                    ConversationMessage.chat_id == job.chat_id,
                )
            )
            self._send_and_finish(
                session,
                job,
                "Контекст очищен. Начат новый диалог — напишите новую задачу.",
                main_menu_markup(
                    admin=is_admin(session, self.settings, job.telegram_user_id)
                ),
            )
        elif job.kind == "auth":
            try:
                authorized = bool(
                    self.oauth_store.access_token(session, job.telegram_user_id)
                )
            except (OAuthConfigurationError, OAuthLoginRequired):
                authorized = False
            text = (
                "Корпоративная авторизация активна.\n\n"
                "• /mcp_status — проверить интеграции;\n"
                "• /logout — завершить сессию."
                if authorized
                else "Для Jira, Bitrix и KTalk выполните /login.\n\n"
                "Бот пришлёт официальную ссылку Keycloak. Пароль и OTP в Telegram "
                "отправлять не нужно."
            )
            self._send_and_finish(
                session,
                job,
                text,
                main_menu_markup(
                    admin=is_admin(session, self.settings, job.telegram_user_id)
                ),
            )
        elif job.kind == "tools":
            self._send_and_finish(
                session,
                job,
                "Подключённые инструменты\n\n"
                "• Jira — задачи, поиск, поля, доски и спринты.\n"
                "• Bitrix — сделки, лиды, контакты, компании, задачи и пользователи.\n"
                "• KTalk — записи, участники, транскрипции и протоколы.\n\n"
                "Можно просто написать запрос. Для принудительного выбора системы:\n"
                "/mcp jira покажи SH-501\n"
                "/mcp bitrix покажи мой профиль\n"
                "/mcp ktalk покажи последние записи\n\n"
                "Проверка подключения: /mcp_status.",
                main_menu_markup(
                    admin=is_admin(session, self.settings, job.telegram_user_id)
                ),
            )
        elif job.kind == "login":
            self._start_login(session, job, user)
        elif job.kind == "logout":
            self._logout(session, job)
        elif job.kind == "mcp_status":
            try:
                authorized = bool(
                    self.oauth_store.access_token(session, job.telegram_user_id)
                )
            except (OAuthConfigurationError, OAuthLoginRequired):
                authorized = False
            self._send_and_finish(
                session,
                job,
                mcp_capability_note(self.settings, authorized=authorized),
            )
        elif job.kind == "mcp_discover":
            if not is_admin(session, self.settings, job.telegram_user_id):
                self._send_and_finish(session, job, "Команда доступна только администратору.")
            else:
                label = (job.payload_text or "").strip().lower()
                server = next(
                    (
                        item
                        for item in self.settings.mcp_servers()
                        if item.server_label == label
                    ),
                    None,
                )
                if server is None:
                    labels = ", ".join(
                        item.server_label for item in self.settings.mcp_servers()
                    )
                    self._send_and_finish(
                        session,
                        job,
                        f"Формат: /mcp_discover <сервер>. Доступно: {labels}.",
                    )
                else:
                    try:
                        access_token = self.oauth_store.access_token(
                            session, job.telegram_user_id
                        )
                        if not access_token:
                            raise OAuthLoginRequired("OAuth login is required")
                        tools = self.mcp_discovery.list_tools(server, access_token)
                    except (OAuthConfigurationError, OAuthLoginRequired):
                        self._send_and_finish(
                            session,
                            job,
                            "Для discovery требуется корпоративная авторизация. "
                            "Выполните /login.",
                        )
                    except (httpx.HTTPError, McpDiscoveryError) as exc:
                        logger.warning("MCP discovery failed for %s: %s", label, exc)
                        self._send_and_finish(
                            session,
                            job,
                            f"Не удалось получить каталог tools MCP {label}. "
                            "Проверьте доступ и состояние сервера.",
                        )
                    else:
                        if tools:
                            rows = [f"- {item.name}: {item.description}" for item in tools]
                            text = (
                                f"MCP {label}: найдено tools — {len(tools)}. "
                                "Discovery ничего не включает и не вызывает.\n"
                                + "\n".join(rows)
                            )
                        else:
                            text = f"MCP {label} вернул пустой каталог tools."
                        self._send_and_finish(session, job, text)
        elif job.kind in {
            "skills",
            "skill_show",
            "skill_edit",
            "skill_cancel",
            "skill_rollback",
        }:
            self._process_skill_command(session, job)
        elif job.kind in {"admin", "admins", "admin_add", "admin_remove"}:
            self._process_admin_command(session, job)
        elif job.kind == "revoke":
            if not is_admin(session, self.settings, job.telegram_user_id):
                self._send_and_finish(session, job, "Команда доступна только администратору.")
            else:
                try:
                    target_id = int((job.payload_text or "").strip())
                    text = self._process_access_callback(session, job, "revoke", target_id)
                except ValueError:
                    text = "Формат: /revoke <telegram_id>."
                self._send_and_finish(session, job, text)
        elif job.kind == "mcp_help":
            configured = [
                server.server_label
                for server in self.settings.mcp_servers()
                if server.allowed_tools
            ]
            labels = ", ".join(configured) if configured else "нет"
            self._send_and_finish(
                session,
                job,
                "Формат: /mcp <jira|bitrix|ktalk> <запрос>. "
                f"Серверы с утверждёнными tools: {labels}.",
            )
        elif job.kind == "mcp":
            try:
                request = json.loads(job.payload_text or "{}")
                server = str(request["server"])
                query = str(request["query"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                self._send_and_finish(
                    session,
                    job,
                    "Формат: /mcp <jira|bitrix|ktalk> <запрос>.",
                )
            else:
                if user is None:
                    self._send_and_finish(session, job, "Сначала выполните /start.")
                else:
                    self._process_message(
                        session,
                        job,
                        user,
                        message_text=query,
                        required_mcp_server=server,
                    )
        elif job.kind == "unsupported":
            self._send_and_finish(
                session,
                job,
                "В пилоте поддерживаются только текстовые сообщения.",
            )
        elif job.kind == "too_long":
            self._send_and_finish(session, job, "Сообщение слишком длинное для пилота.")
        elif job.kind == "limit_denied":
            self._send_and_finish(session, job, "Общий дневной лимит пилота исчерпан.")
        elif job.kind == "access_denied" or user is None:
            self._send_and_finish(
                session,
                job,
                "Запрос не обработан. Выполните /start и дождитесь подтверждения доступа.",
            )
        elif self._process_admin_draft(session, job):
            return
        else:
            self._process_message(session, job, user)

    def run_once(self) -> bool:
        with self.factory() as session:
            job = self._claim(session)
            if job is None:
                return False
            try:
                self._process(session, job)
            except Exception as exc:
                session.rollback()
                job = session.get(UpdateJob, job.id)
                if job is None:
                    raise
                job.error_code = type(exc).__name__
                if job.attempts >= self.settings.max_job_attempts:
                    job.status = JobStatus.failed
                    job.payload_text = None
                    job.response_text = None
                    job.finished_at = datetime.now(UTC)
                else:
                    job.status = JobStatus.queued
                    job.available_at = datetime.now(UTC) + timedelta(
                        seconds=min(2**job.attempts, 30)
                    )
                session.commit()
                logger.exception("Job failed job_id=%s attempt=%s", job.id, job.attempts)
            return True

    def poll_oauth_once(self) -> bool:
        with self.factory() as session:
            now = datetime.now(UTC)
            device = session.scalar(
                select(OAuthDeviceSession)
                .where(OAuthDeviceSession.next_poll_at <= now)
                .order_by(OAuthDeviceSession.next_poll_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if device is None:
                return False
            expires_at = device.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at <= now:
                self._notice(
                    session,
                    device.telegram_user_id,
                    device.chat_id,
                    "Срок ссылки Keycloak истёк. Повторите /login.",
                )
                session.delete(device)
                session.commit()
                return True
            try:
                code = self.oauth_store.cipher().decrypt(device.device_code_encrypted)
                result = self.oauth_store.client.poll_device_token(
                    code,
                    device.interval_seconds,
                )
            except (
                OAuthConfigurationError,
                OAuthLoginRequired,
                OAuthProtocolError,
                httpx.HTTPError,
            ):
                logger.exception("OAuth device polling failed user_id=%s", device.telegram_user_id)
                device.next_poll_at = now + timedelta(seconds=30)
                session.commit()
                return True
            if result.state == "authorized" and result.token is not None:
                self.oauth_store.save(session, device.telegram_user_id, result.token)
                self._notice(
                    session,
                    device.telegram_user_id,
                    device.chat_id,
                    "Keycloak подключён. Проверьте фактически разрешённые интеграции "
                    "командой /mcp_status.",
                )
                session.delete(device)
            elif result.state == "pending":
                device.interval_seconds = result.next_interval or device.interval_seconds
                device.next_poll_at = now + timedelta(seconds=device.interval_seconds)
            else:
                message = (
                    "Авторизация отклонена. Повторите /login."
                    if result.state == "access_denied"
                    else "Срок авторизации истёк. Повторите /login."
                )
                self._notice(session, device.telegram_user_id, device.chat_id, message)
                session.delete(device)
            session.commit()
            return True


def main() -> None:
    configure_logging()
    settings = get_settings()
    engine = make_engine(settings)
    initialize_development_schema(settings, engine)
    factory = make_session_factory(engine)
    processor = JobProcessor(
        settings,
        factory,
        build_agent_client(settings),
        build_telegram_client(settings),
        build_metrics_exporter(),
        OAuthTokenStore(settings, KeycloakOAuthClient(settings)),
    )
    running = True

    def stop(*_args):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logger.info("Worker started")
    while running:
        if not processor.run_once() and not processor.poll_oauth_once():
            time.sleep(settings.worker_poll_seconds)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
