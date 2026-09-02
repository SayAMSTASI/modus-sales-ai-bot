from __future__ import annotations

import difflib
import json
import logging
import signal
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select
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
    ConversationFeedback,
    ConversationMessage,
    JobStatus,
    OAuthAuthorizationSession,
    OAuthDeviceSession,
    PendingConversationFeedback,
    SkillEditSession,
    UpdateJob,
    UsageEvent,
    UserAccess,
    UserQuestionAudit,
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
            status = "не подключён: требуется вход через Keycloak"
        lines.append(f"- {server.server_label}: {status}.")
    lines.append(
        "Не заявляй, что можешь использовать отключённый или неавторизованный MCP. "
        "Если нужного MCP нет, предложи открыть раздел «🔐 Авторизация»."
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
                    {"text": "👤 Пользователи"},
                ],
                [
                    {"text": "👥 Администраторы"},
                    {"text": "🛠 Навыки"},
                ],
                [
                    {"text": "🔎 MCP статус"},
                    {"text": "📊 Активность"},
                ],
                [
                    {"text": "⭐ Удовлетворённость"},
                ],
            ]
        )
    return {
        "keyboard": keyboard,
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Напишите задачу агенту…",
    }


def auth_action_markup(*, authorized: bool) -> dict[str, Any]:
    if authorized:
        buttons = [
            [
                {
                    "text": "🔎 Проверить подключения",
                    "callback_data": "oauth:status",
                }
            ],
            [{"text": "🚪 Выйти", "callback_data": "oauth:logout"}],
        ]
    else:
        buttons = [
            [{"text": "🔐 Войти через Keycloak", "callback_data": "oauth:login"}],
            [
                {
                    "text": "🔎 Проверить подключения",
                    "callback_data": "oauth:status",
                }
            ],
        ]
    return {"inline_keyboard": buttons}


def admin_dashboard_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "👤 Пользователи", "callback_data": "user:list:all"},
                {"text": "👥 Администраторы", "callback_data": "admin:list"},
            ],
            [
                {"text": "📊 Активность", "callback_data": "report:activity"},
                {
                    "text": "⭐ Удовлетворённость",
                    "callback_data": "report:satisfaction",
                },
            ],
            [
                {"text": "🛠 Навыки", "callback_data": "skill:list"},
                {"text": "🔎 MCP", "callback_data": "oauth:status"},
            ],
        ]
    }


def help_text(*, admin: bool) -> str:
    text = (
        "# Что умеет Sales AI\n\n"
        "• Общается в обычном диалоге и помнит контекст.\n"
        "• Применяет общий набор sales skills.\n"
        "• Читает задачи Jira, данные CRM Bitrix и записи KTalk через MCP.\n"
        "• Работает только с вашими корпоративными правами после входа.\n\n"
        "Администраторы видят автора, время и текст вопросов в журнале активности. "
        "Ответы агента и OAuth-токены в этот журнал не копируются.\n\n"
        "## Как пользоваться\n"
        "1. Нажмите «🔐 Авторизация», затем «Войти через Keycloak».\n"
        "2. Просто опишите задачу обычным текстом.\n"
        "3. Для нового контекста нажмите «🆕 Новый диалог».\n\n"
        "## Примеры\n"
        "• Покажи задачу SH-501 и кратко перескажи статус.\n"
        "• Найди последние сделки компании в Bitrix.\n"
        "• Сделай протокол последней встречи KTalk."
    )
    if admin:
        text += (
            "\n\n## Администратору\n"
            "• «⚙️ Управление» — панель управления.\n"
            "• «👤 Пользователи» — карточки, вопросы и управление доступом.\n"
            "• «👥 Администраторы» — список администраторов.\n"
            "• «🛠 Навыки» — просмотр и изменение skills.\n"
            "• «📊 Активность» — пользователи, запросы и расход.\n"
            "• «⭐ Удовлетворённость» — оценки завершённых диалогов.\n\n"
            "Все основные действия выполняются кнопками."
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
        messages_cleanup = session.execute(
            delete(ConversationMessage).where(
                ConversationMessage.expires_at <= datetime.now(UTC)
            )
        )
        audit_cutoff = datetime.now(UTC) - timedelta(
            days=max(1, self.settings.question_audit_retention_days)
        )
        audit_cleanup = session.execute(
            delete(UserQuestionAudit).where(UserQuestionAudit.asked_at < audit_cutoff)
        )
        if messages_cleanup.rowcount or audit_cleanup.rowcount:
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

    @staticmethod
    def _feedback_markup() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": f"{rating} {'⭐' if rating == 5 else ''}".strip(),
                        "callback_data": f"feedback:{rating}",
                    }
                    for rating in range(1, 6)
                ]
            ]
        }

    def _send_feedback_request(self, session: Session, job: UpdateJob) -> None:
        pending = session.scalar(
            select(PendingConversationFeedback).where(
                PendingConversationFeedback.telegram_user_id == job.telegram_user_id
            )
        )
        if pending is None:
            messages = session.scalars(
                select(ConversationMessage)
                .where(
                    ConversationMessage.telegram_user_id == job.telegram_user_id,
                    ConversationMessage.chat_id == job.chat_id,
                )
                .order_by(ConversationMessage.created_at)
            ).all()
            question_count = sum(item.role == "user" for item in messages)
            answer_count = sum(item.role == "assistant" for item in messages)
            if answer_count == 0:
                session.execute(
                    delete(ConversationMessage).where(
                        ConversationMessage.telegram_user_id == job.telegram_user_id,
                        ConversationMessage.chat_id == job.chat_id,
                    )
                )
                self._send_and_finish(
                    session,
                    job,
                    "Начат новый диалог — напишите новую задачу.",
                    main_menu_markup(
                        admin=is_admin(session, self.settings, job.telegram_user_id)
                    ),
                )
                return
            pending = PendingConversationFeedback(
                telegram_user_id=job.telegram_user_id,
                chat_id=job.chat_id,
                conversation_started_at=messages[0].created_at,
                question_count=question_count,
                answer_count=answer_count,
            )
            session.add(pending)
        self._send_and_finish(
            session,
            job,
            "# Оцените прошлый диалог\n\n"
            "1 — совсем не помогли, 5 — полностью решили задачу.",
            self._feedback_markup(),
        )

    def _process_feedback_callback(
        self,
        session: Session,
        job: UpdateJob,
        callback_id: str,
        data: str,
    ) -> bool:
        if not data.startswith("feedback:"):
            return False
        try:
            rating = int(data.split(":", 1)[1])
            if rating not in range(1, 6):
                raise ValueError
        except ValueError:
            self.telegram.answer_callback_query(callback_id, "Некорректная оценка")
            self._finish(session, job)
            return True
        pending = session.scalar(
            select(PendingConversationFeedback).where(
                PendingConversationFeedback.telegram_user_id == job.telegram_user_id
            )
        )
        if pending is None:
            self.telegram.answer_callback_query(callback_id, "Оценка уже сохранена")
            self._send_and_finish(
                session,
                job,
                "Этот диалог уже завершён. Можно отправлять новую задачу.",
                main_menu_markup(
                    admin=is_admin(session, self.settings, job.telegram_user_id)
                ),
            )
            return True
        session.add(
            ConversationFeedback(
                telegram_user_id=job.telegram_user_id,
                user_hash=stable_user_hash(
                    job.telegram_user_id,
                    self.settings.safety_identifier_secret,
                ),
                rating=rating,
                conversation_started_at=pending.conversation_started_at,
                question_count=pending.question_count,
                answer_count=pending.answer_count,
            )
        )
        session.execute(
            delete(ConversationMessage).where(
                ConversationMessage.telegram_user_id == job.telegram_user_id,
                ConversationMessage.chat_id == pending.chat_id,
            )
        )
        session.delete(pending)
        self.telegram.answer_callback_query(callback_id, "Спасибо за оценку")
        self._send_and_finish(
            session,
            job,
            f"Спасибо! Оценка {rating} из 5 сохранена. Начат новый диалог.",
            main_menu_markup(
                admin=is_admin(session, self.settings, job.telegram_user_id)
            ),
        )
        return True

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

    @staticmethod
    def _skill_actions_markup() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "👁 Посмотреть", "callback_data": "skill:show"},
                    {"text": "✏️ Изменить", "callback_data": "skill:edit"},
                ],
                [{"text": "↩️ Откатить версию", "callback_data": "skill:rollback"}],
                [{"text": "⬅️ К списку навыков", "callback_data": "skill:list"}],
            ]
        }

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
        if action != "approve" and target_id in self.settings.admin_ids():
            return (
                "Это базовый администратор из ADMIN_TELEGRAM_IDS. "
                "Его нельзя заблокировать через Telegram."
            )
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
        session.execute(
            delete(PendingConversationFeedback).where(
                PendingConversationFeedback.telegram_user_id == target_id
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
            return f"# Навык выбран\n\n{skill_name}\n\nВыберите действие."
        edit_session, skill_name = self._selected_skill(session, job.telegram_user_id)
        if edit_session is None or skill_name is None:
            return "Сначала выберите навык из списка."
        if action == "apply" and edit_session.state == "awaiting_confirm":
            draft = (edit_session.draft_content or "").strip()
            if not draft:
                return "Черновик пуст. Снова нажмите «Изменить»."
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
        if action == "show":
            content, source = self._current_skill_content(session, skill_name)
            return f"# {skill_name}\n\nИсточник: {source}\n\n{content}"
        if action == "edit":
            edit_session.state = "awaiting_content"
            edit_session.draft_content = None
            return (
                f"# Изменение навыка\n\n{skill_name}\n\n"
                "Отправьте новый полный текст одним сообщением."
            )
        if action == "rollback":
            current = active_skill_version(session, skill_name)
            if current is None:
                return "Используется базовая версия из Git — откатывать нечего."
            previous = rollback_skill(session, skill_name)
            source = f"версия {previous.version}" if previous else "базовая версия из Git"
            self._audit(
                session,
                admin_id=job.telegram_user_id,
                action="skill_rollback",
                target_id=job.telegram_user_id,
                metadata={"skill": skill_name, "to": source},
            )
            return f"Навык {skill_name} возвращён к: {source}."
        return "Действие устарело. Вернитесь к списку навыков."

    def _process_callback(self, session: Session, job: UpdateJob) -> None:
        try:
            payload = json.loads(job.payload_text or "{}")
            callback_id = str(payload["callback_query_id"])
            data = str(payload["data"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self._finish(session, job)
            return
        if self._process_feedback_callback(session, job, callback_id, data):
            return
        if data.startswith("oauth:"):
            user = session.scalar(
                select(UserAccess).where(
                    UserAccess.telegram_user_id == job.telegram_user_id
                )
            )
            action = data.split(":", 1)[1]
            self.telegram.answer_callback_query(callback_id, "Готово")
            if action == "login":
                self._start_login(session, job, user)
            elif action == "logout":
                self._logout(session, job)
            else:
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
                    auth_action_markup(authorized=authorized),
                )
            return
        if not is_admin(session, self.settings, job.telegram_user_id):
            self.telegram.answer_callback_query(callback_id, "Недостаточно прав")
            self._send_and_finish(session, job, "Команда доступна только администратору.")
            return
        parts = data.split(":")
        markup: dict[str, Any] | None = None
        if data == "admin:dashboard":
            text = "# Управление ботом\n\nВыберите раздел."
            markup = admin_dashboard_markup()
        elif data == "admin:list":
            text, markup = self._admins_screen(session)
        elif len(parts) == 3 and parts[0] == "admin" and parts[1] in {
            "pick_add",
            "pick_remove",
        }:
            mode = "add" if parts[1] == "pick_add" else "remove"
            try:
                text, markup = self._admin_candidates_screen(
                    session,
                    mode=mode,
                    page=int(parts[2]),
                )
            except ValueError:
                text, markup = self._admins_screen(session)
        elif len(parts) == 3 and parts[0] == "admin" and parts[1] in {
            "confirm_add",
            "confirm_remove",
        }:
            try:
                action = "add" if parts[1] == "confirm_add" else "remove"
                text, markup = self._admin_confirmation(
                    session,
                    action=action,
                    target_id=int(parts[2]),
                )
            except ValueError:
                text = "Некорректный пользователь."
        elif len(parts) == 3 and parts[0] == "admin" and parts[1] in {
            "add",
            "remove",
        }:
            self.telegram.answer_callback_query(callback_id, "Готово")
            job.kind = "admin_add" if parts[1] == "add" else "admin_remove"
            job.payload_text = parts[2]
            self._process_admin_command(session, job)
            return
        elif len(parts) == 2 and parts[0] == "report":
            if parts[1] == "activity":
                job.kind = "activity"
            else:
                job.kind = "satisfaction"
            self.telegram.answer_callback_query(callback_id, "Готово")
            self._process_admin_command(session, job)
            return
        elif data == "skill:list":
            self.telegram.answer_callback_query(callback_id, "Готово")
            job.kind = "skills"
            self._process_skill_command(session, job)
            return
        elif len(parts) == 3 and parts[0] == "access" and parts[1] in {
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
            if parts[1] == "edit":
                markup = {
                    "inline_keyboard": [
                        [{"text": "Отмена", "callback_data": "skill:cancel"}]
                    ]
                }
            else:
                markup = self._skill_actions_markup()
        elif len(parts) >= 3 and parts[0] == "user":
            action = parts[1]
            try:
                if action == "list":
                    page = int(parts[3]) if len(parts) == 4 else 0
                    text, markup = self._users_overview(session, parts[2], page)
                else:
                    target_id = int(parts[2])
                    if action == "view":
                        text, markup = self._user_card(session, target_id)
                    elif action == "questions":
                        text = self._user_questions_text(session, target_id)
                        markup = {
                            "inline_keyboard": [
                                [
                                    {
                                        "text": "⬅️ К пользователю",
                                        "callback_data": f"user:view:{target_id}",
                                    }
                                ]
                            ]
                        }
                    elif action in {"allow", "revoke"}:
                        access_action = "approve" if action == "allow" else "revoke"
                        text = self._process_access_callback(
                            session,
                            job,
                            access_action,
                            target_id,
                        )
                        _, markup = self._user_card(session, target_id)
                    else:
                        text = "Неизвестное действие."
            except ValueError:
                text = "Некорректный Telegram ID."
        else:
            text = "Неизвестное действие."
        self.telegram.answer_callback_query(callback_id, "Готово")
        self._send_and_finish(session, job, text, markup)

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
                + [
                    [
                        {
                            "text": "⬅️ Панель управления",
                            "callback_data": "admin:dashboard",
                        }
                    ]
                ]
            }
            self._send_and_finish(
                session,
                job,
                "# Навыки агента\n\nВыберите навык для просмотра или изменения.",
                markup,
            )
            return
        edit_session, skill_name = self._selected_skill(session, job.telegram_user_id)
        if edit_session is None or skill_name is None:
            self._send_and_finish(session, job, "Сначала выберите навык из списка.")
            return
        if job.kind == "skill_show":
            content, source = self._current_skill_content(session, skill_name)
            self._send_and_finish(
                session,
                job,
                f"Skill: {skill_name}\nИсточник: {source}\n\n{content}",
                self._skill_actions_markup(),
            )
        elif job.kind == "skill_edit":
            edit_session.state = "awaiting_content"
            edit_session.draft_content = None
            self._send_and_finish(
                session,
                job,
                f"Отправьте новый полный текст skill {skill_name} одним сообщением. "
                "Для отмены нажмите кнопку ниже.",
                {
                    "inline_keyboard": [
                        [{"text": "Отмена", "callback_data": "skill:cancel"}]
                    ]
                },
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

    @staticmethod
    def _status_label(status: str) -> str:
        return {
            AccessStatus.active: "🟢 активен",
            AccessStatus.pending: "🟡 ожидает",
            AccessStatus.revoked: "🔴 заблокирован",
        }.get(status, status)

    def _user_questions_text(
        self,
        session: Session,
        telegram_user_id: int,
    ) -> str:
        user = session.scalar(
            select(UserAccess).where(UserAccess.telegram_user_id == telegram_user_id)
        )
        if user is None:
            return "Пользователь не найден."
        questions = session.scalars(
            select(UserQuestionAudit)
            .where(UserQuestionAudit.telegram_user_id == telegram_user_id)
            .order_by(UserQuestionAudit.asked_at.desc())
            .limit(10)
        ).all()
        if not questions:
            return f"У пользователя {telegram_user_id} пока нет сохранённых вопросов."
        username = f"@{user.telegram_username}" if user.telegram_username else "без username"
        rows = [f"# Последние вопросы\n\n{username} · {telegram_user_id}"]
        for item in questions:
            text = item.question_text.replace("\n", " ").strip()
            if len(text) > 500:
                text = text[:497] + "..."
            asked_at = item.asked_at.strftime("%d.%m.%Y %H:%M")
            rows.append(f"\n{asked_at} UTC · {item.result}\n{text}")
        return "\n".join(rows)

    def _user_card(
        self,
        session: Session,
        telegram_user_id: int,
    ) -> tuple[str, dict[str, Any] | None]:
        user = session.scalar(
            select(UserAccess).where(UserAccess.telegram_user_id == telegram_user_id)
        )
        if user is None:
            return "Пользователь не найден.", None
        user_hash = stable_user_hash(
            telegram_user_id,
            self.settings.safety_identifier_secret,
        )
        question_count, last_question = session.execute(
            select(
                func.count(UserQuestionAudit.id),
                func.max(UserQuestionAudit.asked_at),
            ).where(UserQuestionAudit.telegram_user_id == telegram_user_id)
        ).one()
        requests, input_tokens, output_tokens, cost = session.execute(
            select(
                func.count(UsageEvent.id),
                func.coalesce(func.sum(UsageEvent.input_tokens), 0),
                func.coalesce(func.sum(UsageEvent.output_tokens), 0),
                func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0),
            ).where(UsageEvent.user_hash == user_hash)
        ).one()
        feedback_count, average_rating = session.execute(
            select(
                func.count(ConversationFeedback.id),
                func.avg(ConversationFeedback.rating),
            ).where(ConversationFeedback.telegram_user_id == telegram_user_id)
        ).one()
        username = f"@{user.telegram_username}" if user.telegram_username else "не указан"
        last_activity = (
            last_question.strftime("%d.%m.%Y %H:%M UTC")
            if last_question is not None
            else "нет"
        )
        rating = f"{float(average_rating):.2f}/5" if average_rating is not None else "нет"
        admin_status = is_admin(session, self.settings, telegram_user_id)
        role_label = "администратор" if admin_status else "пользователь"
        text = (
            "# Карточка пользователя\n\n"
            f"Telegram ID: {telegram_user_id}\n"
            f"Username: {username}\n"
            f"Статус: {self._status_label(user.status)}\n"
            f"Роль: {role_label}\n"
            f"Последняя активность: {last_activity}\n"
            f"Вопросов в журнале: {question_count}\n"
            f"Успешных запросов: {requests}\n"
            f"Токены input/output: {input_tokens}/{output_tokens}\n"
            f"Расчётная стоимость: ${float(cost):.4f}\n"
            f"Оценка диалогов: {rating} ({feedback_count})"
        )
        action = "allow" if user.status != AccessStatus.active else "revoke"
        action_text = "✅ Разблокировать" if action == "allow" else "⛔ Заблокировать"
        buttons = [
            [
                {
                    "text": "📝 Последние вопросы",
                    "callback_data": f"user:questions:{telegram_user_id}",
                }
            ],
            [
                {
                    "text": action_text,
                    "callback_data": f"user:{action}:{telegram_user_id}",
                }
            ],
        ]
        if user.status == AccessStatus.active and not admin_status:
            buttons.append(
                [
                    {
                        "text": "🛡 Назначить администратором",
                        "callback_data": f"admin:confirm_add:{telegram_user_id}",
                    }
                ]
            )
        elif user.role == "admin" and telegram_user_id not in self.settings.admin_ids():
            buttons.append(
                [
                    {
                        "text": "➖ Снять права администратора",
                        "callback_data": f"admin:confirm_remove:{telegram_user_id}",
                    }
                ]
            )
        buttons.append([{"text": "⬅️ К списку", "callback_data": "user:list:all"}])
        markup = {"inline_keyboard": buttons}
        return text, markup

    def _users_overview(
        self,
        session: Session,
        status_filter: str,
        page: int = 0,
    ) -> tuple[str, dict[str, Any]]:
        allowed_filters = {"all", "active", "pending", "revoked"}
        if status_filter not in allowed_filters:
            status_filter = "all"
        page_size = max(1, min(self.settings.admin_users_page_size, 50))
        statement = select(UserAccess).order_by(UserAccess.updated_at.desc())
        count_statement = select(func.count(UserAccess.id))
        if status_filter != "all":
            statement = statement.where(UserAccess.status == status_filter)
            count_statement = count_statement.where(UserAccess.status == status_filter)
        filtered_total = session.scalar(count_statement) or 0
        max_page = max(0, (filtered_total - 1) // page_size)
        page = max(0, min(page, max_page))
        users = session.scalars(
            statement.offset(page * page_size).limit(page_size)
        ).all()
        totals = {
            status: session.scalar(
                select(func.count(UserAccess.id)).where(UserAccess.status == status)
            )
            or 0
            for status in (AccessStatus.active, AccessStatus.pending, AccessStatus.revoked)
        }
        text = (
            "# Пользователи\n\n"
            f"🟢 Активные: {totals[AccessStatus.active]}\n"
            f"🟡 Ожидают: {totals[AccessStatus.pending]}\n"
            f"🔴 Заблокированы: {totals[AccessStatus.revoked]}\n\n"
            f"Фильтр: {status_filter}. "
            f"Показано {page * page_size + 1 if filtered_total else 0}–"
            f"{min((page + 1) * page_size, filtered_total)} из {filtered_total}.\n"
            "Нажмите пользователя для карточки."
        )
        buttons = []
        for user in users:
            username = f"@{user.telegram_username}" if user.telegram_username else str(
                user.telegram_user_id
            )
            symbol = {
                AccessStatus.active: "🟢",
                AccessStatus.pending: "🟡",
                AccessStatus.revoked: "🔴",
            }.get(user.status, "•")
            buttons.append(
                [
                    {
                        "text": f"{symbol} {username}",
                        "callback_data": f"user:view:{user.telegram_user_id}",
                    }
                ]
            )
        buttons.append(
            [
                {"text": "Все", "callback_data": "user:list:all"},
                {"text": "Активные", "callback_data": "user:list:active"},
            ]
        )
        buttons.append(
            [
                {"text": "Ожидают", "callback_data": "user:list:pending"},
                {"text": "Блок", "callback_data": "user:list:revoked"},
            ]
        )
        paging = []
        if page > 0:
            paging.append(
                {
                    "text": "⬅️ Назад",
                    "callback_data": f"user:list:{status_filter}:{page - 1}",
                }
            )
        if page < max_page:
            paging.append(
                {
                    "text": "Далее ➡️",
                    "callback_data": f"user:list:{status_filter}:{page + 1}",
                }
            )
        if paging:
            buttons.append(paging)
        buttons.append(
            [
                {
                    "text": "⬅️ Панель управления",
                    "callback_data": "admin:dashboard",
                }
            ]
        )
        return text, {"inline_keyboard": buttons}

    def _activity_text(self, session: Session) -> str:
        now = datetime.now(UTC)
        day = now - timedelta(days=1)
        week = now - timedelta(days=7)
        questions_day = session.scalar(
            select(func.count(UserQuestionAudit.id)).where(
                UserQuestionAudit.asked_at >= day
            )
        ) or 0
        questions_week = session.scalar(
            select(func.count(UserQuestionAudit.id)).where(
                UserQuestionAudit.asked_at >= week
            )
        ) or 0
        requests, tokens, cost, average_ms = session.execute(
            select(
                func.count(UsageEvent.id),
                func.coalesce(
                    func.sum(UsageEvent.input_tokens + UsageEvent.output_tokens), 0
                ),
                func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0),
                func.avg(UsageEvent.duration_ms),
            ).where(UsageEvent.occurred_at >= week)
        ).one()
        active_users = session.scalar(
            select(func.count(UserAccess.id)).where(
                UserAccess.status == AccessStatus.active
            )
        ) or 0
        return (
            "# Активность пилота\n\n"
            f"Активных пользователей: {active_users}\n"
            f"Вопросов за 24 часа: {questions_day}\n"
            f"Вопросов за 7 дней: {questions_week}\n"
            f"Успешных OpenAI-запросов за 7 дней: {requests}\n"
            f"Токенов за 7 дней: {tokens}\n"
            f"Стоимость за 7 дней: ${float(cost):.4f}\n"
            f"Среднее время ответа: {float(average_ms or 0):.0f} мс"
        )

    @staticmethod
    def _satisfaction_text(session: Session) -> str:
        ratings = list(session.scalars(select(ConversationFeedback.rating)))
        week_start = datetime.now(UTC) - timedelta(days=7)
        weekly = list(
            session.scalars(
                select(ConversationFeedback.rating).where(
                    ConversationFeedback.created_at >= week_start
                )
            )
        )
        if not ratings:
            return "# Удовлетворённость\n\nОценок пока нет."
        average = sum(ratings) / len(ratings)
        csat = sum(rating >= 4 for rating in ratings) / len(ratings) * 100
        distribution = " · ".join(
            f"{rating}⭐: {ratings.count(rating)}" for rating in range(1, 6)
        )
        weekly_text = (
            f"{sum(weekly) / len(weekly):.2f}/5 ({len(weekly)})"
            if weekly
            else "нет оценок"
        )
        return (
            "# Удовлетворённость\n\n"
            f"Всего оценок: {len(ratings)}\n"
            f"Средняя оценка: {average:.2f}/5\n"
            f"CSAT (оценки 4–5): {csat:.1f}%\n"
            f"За последние 7 дней: {weekly_text}\n\n"
            f"Распределение: {distribution}"
        )

    def _admins_screen(self, session: Session) -> tuple[str, dict[str, Any]]:
        rows: list[str] = []
        removable_count = 0
        for admin_id in sorted(active_admin_ids(session, self.settings)):
            user = session.scalar(
                select(UserAccess).where(UserAccess.telegram_user_id == admin_id)
            )
            username = (
                f"@{user.telegram_username}"
                if user is not None and user.telegram_username
                else "без username"
            )
            if admin_id in self.settings.admin_ids():
                source = "базовый"
            else:
                source = "назначен в боте"
                removable_count += 1
            rows.append(f"• {username} · {admin_id} · {source}")
        buttons = [
            [{"text": "➕ Добавить администратора", "callback_data": "admin:pick_add:0"}]
        ]
        if removable_count:
            buttons.append(
                [
                    {
                        "text": "➖ Снять права администратора",
                        "callback_data": "admin:pick_remove:0",
                    }
                ]
            )
        buttons.append(
            [{"text": "⬅️ Панель управления", "callback_data": "admin:dashboard"}]
        )
        return "# Администраторы\n\n" + "\n".join(rows), {
            "inline_keyboard": buttons
        }

    def _admin_candidates_screen(
        self,
        session: Session,
        *,
        mode: str,
        page: int,
    ) -> tuple[str, dict[str, Any]]:
        active_admins = active_admin_ids(session, self.settings)
        users = session.scalars(
            select(UserAccess)
            .where(UserAccess.status == AccessStatus.active)
            .order_by(UserAccess.updated_at.desc())
        ).all()
        if mode == "add":
            candidates = [
                user for user in users if user.telegram_user_id not in active_admins
            ]
            title = "# Выберите нового администратора"
            action = "confirm_add"
            empty = "Все активные пользователи уже являются администраторами."
        else:
            candidates = [
                user
                for user in users
                if user.role == "admin"
                and user.telegram_user_id not in self.settings.admin_ids()
            ]
            title = "# Выберите администратора"
            action = "confirm_remove"
            empty = "Нет администраторов, права которых можно снять в боте."
        page_size = 8
        max_page = max(0, (len(candidates) - 1) // page_size)
        page = max(0, min(page, max_page))
        visible = candidates[page * page_size : (page + 1) * page_size]
        buttons: list[list[dict[str, str]]] = []
        for user in visible:
            label = (
                f"@{user.telegram_username}"
                if user.telegram_username
                else f"Пользователь {user.telegram_user_id}"
            )
            buttons.append(
                [
                    {
                        "text": label,
                        "callback_data": f"admin:{action}:{user.telegram_user_id}",
                    }
                ]
            )
        paging: list[dict[str, str]] = []
        picker_action = "pick_add" if mode == "add" else "pick_remove"
        if page > 0:
            paging.append(
                {
                    "text": "⬅️ Назад",
                    "callback_data": f"admin:{picker_action}:{page - 1}",
                }
            )
        if page < max_page:
            paging.append(
                {
                    "text": "Далее ➡️",
                    "callback_data": f"admin:{picker_action}:{page + 1}",
                }
            )
        if paging:
            buttons.append(paging)
        buttons.append([{"text": "Отмена", "callback_data": "admin:list"}])
        body = (
            f"{title}\n\nВыберите пользователя из списка."
            if candidates
            else f"{title}\n\n{empty}"
        )
        return body, {"inline_keyboard": buttons}

    def _admin_confirmation(
        self,
        session: Session,
        *,
        action: str,
        target_id: int,
    ) -> tuple[str, dict[str, Any]]:
        target = session.scalar(
            select(UserAccess).where(UserAccess.telegram_user_id == target_id)
        )
        if target is None:
            return "Пользователь не найден.", {
                "inline_keyboard": [
                    [{"text": "⬅️ К администраторам", "callback_data": "admin:list"}]
                ]
            }
        username = f"@{target.telegram_username}" if target.telegram_username else "без username"
        adding = action == "add"
        title = "Назначить администратора?" if adding else "Снять права администратора?"
        button = "✅ Назначить" if adding else "➖ Снять права"
        callback = f"admin:{action}:{target_id}"
        return (
            f"# {title}\n\n{username}\nTelegram ID: {target_id}",
            {
                "inline_keyboard": [
                    [{"text": button, "callback_data": callback}],
                    [{"text": "Отмена", "callback_data": "admin:list"}],
                ]
            },
        )

    def _process_admin_command(self, session: Session, job: UpdateJob) -> None:
        if not is_admin(session, self.settings, job.telegram_user_id):
            self._send_and_finish(session, job, "Команда доступна только администратору.")
            return

        if job.kind == "users":
            status_filter = (job.payload_text or "all").strip().lower() or "all"
            text, markup = self._users_overview(session, status_filter)
            self._send_and_finish(session, job, text, markup)
            return

        if job.kind == "activity":
            self._send_and_finish(
                session,
                job,
                self._activity_text(session),
                {
                    "inline_keyboard": [
                        [
                            {
                                "text": "👤 Пользователи",
                                "callback_data": "user:list:all",
                            },
                            {
                                "text": "⭐ Удовлетворённость",
                                "callback_data": "report:satisfaction",
                            },
                        ],
                        [
                            {
                                "text": "⬅️ Панель управления",
                                "callback_data": "admin:dashboard",
                            }
                        ],
                    ]
                },
            )
            return

        if job.kind == "satisfaction":
            self._send_and_finish(
                session,
                job,
                self._satisfaction_text(session),
                {
                    "inline_keyboard": [
                        [
                            {
                                "text": "📊 Активность",
                                "callback_data": "report:activity",
                            }
                        ],
                        [
                            {
                                "text": "⬅️ Панель управления",
                                "callback_data": "admin:dashboard",
                            }
                        ],
                    ]
                },
            )
            return

        if job.kind == "admin":
            self._send_and_finish(
                session,
                job,
                "# Управление ботом\n\n"
                "Выберите раздел. Все основные действия выполняются кнопками.",
                admin_dashboard_markup(),
            )
            return

        if job.kind == "admins":
            text, markup = self._admins_screen(session)
            self._send_and_finish(session, job, text, markup)
            return

        if job.kind in {"user", "questions", "allow"}:
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
            if job.kind == "user":
                text, markup = self._user_card(session, target_id)
            elif job.kind == "questions":
                text = self._user_questions_text(session, target_id)
                markup = {
                    "inline_keyboard": [
                        [
                            {
                                "text": "⬅️ К пользователю",
                                "callback_data": f"user:view:{target_id}",
                            }
                        ]
                    ]
                }
            else:
                text = self._process_access_callback(
                    session,
                    job,
                    "approve",
                    target_id,
                )
                _, markup = self._user_card(session, target_id)
            self._send_and_finish(session, job, text, markup)
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
            if target is None:
                self._send_and_finish(
                    session,
                    job,
                    "Пользователь не найден. Сначала он должен открыть бота и "
                    "зарегистрироваться.",
                    self._admins_screen(session)[1],
                )
                return
            if target.status != AccessStatus.active:
                self._send_and_finish(
                    session,
                    job,
                    "Сначала разрешите пользователю обычный доступ, затем назначьте "
                    "его администратором.",
                    self._admins_screen(session)[1],
                )
                return
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
            self._notice(
                session,
                target_id,
                target.chat_id,
                "Вам выданы права администратора Sales AI Bot.",
            )
            _, markup = self._admins_screen(session)
            self._send_and_finish(
                session,
                job,
                f"✅ Пользователь {target_id} назначен администратором.",
                markup,
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
        _, markup = self._admins_screen(session)
        self._send_and_finish(
            session,
            job,
            f"Права администратора у пользователя {target_id} сняты. "
            "Обычный доступ сохранён.",
            markup,
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
            self._send_and_finish(
                session,
                job,
                "Сначала нажмите «Запустить» и получите доступ к боту.",
            )
            return
        try:
            existing = self.oauth_store.access_token(session, job.telegram_user_id)
        except OAuthLoginRequired:
            existing = None
        if existing:
            self._send_and_finish(
                session,
                job,
                "Keycloak уже подключён. Для нового входа сначала нажмите «Выйти».",
                auth_action_markup(authorized=True),
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
                "учётной записью.\n\n"
                f"Ссылка действует {authorization.expires_in // 60 or 1} мин. "
                "Пароль и OTP в Telegram не отправляйте.",
                {
                    "inline_keyboard": [
                        [
                            {
                                "text": "🔐 Открыть Keycloak",
                                "url": authorization.authorization_url,
                            }
                        ],
                        [{"text": "Отмена", "callback_data": "oauth:status"}],
                    ]
                },
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
            else f"\n\nКод: `{authorization.user_code}`"
        )
        self._send_and_finish(
            session,
            job,
            "Откройте официальную страницу Keycloak и войдите корпоративной "
            f"учётной записью.{code_line}\n\n"
            f"Ссылка действует {authorization.expires_in // 60 or 1} мин. "
            "Пароль и OTP в Telegram не отправляйте.",
            {
                "inline_keyboard": [
                    [{"text": "🔐 Открыть Keycloak", "url": link}],
                    [{"text": "Отмена", "callback_data": "oauth:status"}],
                ]
            },
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
        self._send_and_finish(
            session,
            job,
            text,
            auth_action_markup(authorized=False),
        )

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
            pending_feedback = session.scalar(
                select(PendingConversationFeedback).where(
                    PendingConversationFeedback.telegram_user_id
                    == job.telegram_user_id
                )
            )
            if pending_feedback is not None:
                self._send_and_finish(
                    session,
                    job,
                    "Сначала оцените ответы прошлого диалога, затем можно будет "
                    "начать новый.",
                    self._feedback_markup(),
                )
                return
            message_text = (
                message_text if message_text is not None else (job.payload_text or "")
            )
            question_audit = session.scalar(
                select(UserQuestionAudit).where(
                    UserQuestionAudit.update_job_id == job.id
                )
            )
            if question_audit is None:
                question_audit = UserQuestionAudit(
                    update_job_id=job.id,
                    telegram_user_id=job.telegram_user_id,
                    chat_id=job.chat_id,
                    question_text=message_text[
                        : max(1, self.settings.question_audit_max_chars)
                    ],
                    scenario=classify_scenario(message_text),
                    mcp_server=required_mcp_server,
                    result="processing",
                )
                session.add(question_audit)
                session.commit()
            limit_reason = check_limits(session, user, self.settings)
            if limit_reason:
                question_audit.result = "limit_denied"
                session.commit()
                self._send_and_finish(
                    session,
                    job,
                    "Общий дневной лимит пилота исчерпан. Обратитесь к администратору.",
                )
                return
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
                question_audit.result = "authorization_required"
                session.commit()
                self._send_and_finish(
                    session,
                    job,
                    "Для MCP требуется корпоративная авторизация. Откройте раздел "
                    "«🔐 Авторизация» и войдите через Keycloak.",
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
                question_audit.result = "mcp_unavailable"
                session.commit()
                self._send_and_finish(
                    session,
                    job,
                    "MCP пока не подключён: отсутствует авторизация или "
                    "не утверждён read-only список tools. Проверьте раздел "
                    "«🔐 Авторизация».",
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
            question_audit.result = "ok"
            question_audit.request_id = result.request_id
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
                self._send_and_finish(session, job, "Не удалось открыть главное меню.")
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
                    "# Sales AI готов к работе\n\n"
                    "Напишите задачу обычным текстом или выберите действие ниже. "
                    "Для Jira, Bitrix и KTalk сначала нужна корпоративная авторизация.\n\n"
                    "Администраторы пилота видят автора, время и текст вопросов; "
                    "ответы агента в журнал не копируются.",
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
            self._send_feedback_request(session, job)
        elif job.kind == "auth":
            try:
                authorized = bool(
                    self.oauth_store.access_token(session, job.telegram_user_id)
                )
            except (OAuthConfigurationError, OAuthLoginRequired):
                authorized = False
            text = (
                "# Авторизация\n\nКорпоративная авторизация активна."
                if authorized
                else "# Авторизация\n\nНажмите «Войти через Keycloak». Бот пришлёт "
                "официальную ссылку. Пароль и OTP в Telegram отправлять не нужно."
            )
            self._send_and_finish(
                session,
                job,
                text,
                auth_action_markup(authorized=authorized),
            )
        elif job.kind == "tools":
            self._send_and_finish(
                session,
                job,
                "# Подключённые инструменты\n\n"
                "• Jira — задачи, поиск, поля, доски и спринты.\n"
                "• Bitrix — сделки, лиды, контакты, компании, задачи и пользователи.\n"
                "• KTalk — записи, участники, транскрипции и протоколы.\n\n"
                "Просто напишите задачу обычным сообщением — агент сам выберет "
                "нужную систему.",
                {
                    "inline_keyboard": [
                        [
                            {
                                "text": "🔎 Проверить подключения",
                                "callback_data": "oauth:status",
                            }
                        ]
                    ]
                },
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
                auth_action_markup(authorized=authorized),
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
                            "Откройте раздел «🔐 Авторизация».",
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
        elif job.kind in {
            "admin",
            "admins",
            "admin_add",
            "admin_remove",
            "users",
            "user",
            "questions",
            "allow",
            "activity",
            "satisfaction",
        }:
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
                    self._send_and_finish(session, job, "Сначала нажмите «Запустить».")
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
                "Запрос не обработан. Нажмите «Запустить» и дождитесь "
                "подтверждения доступа.",
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
                question_audit = session.scalar(
                    select(UserQuestionAudit).where(
                        UserQuestionAudit.update_job_id == job.id
                    )
                )
                if question_audit is not None:
                    question_audit.result = f"error:{type(exc).__name__}"[:32]
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
                    "Срок ссылки Keycloak истёк. Снова нажмите «🔐 Авторизация».",
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
                    "Keycloak подключён. Откройте «🔐 Авторизация» и нажмите "
                    "«Проверить подключения».",
                )
                session.delete(device)
            elif result.state == "pending":
                device.interval_seconds = result.next_interval or device.interval_seconds
                device.next_poll_at = now + timedelta(seconds=device.interval_seconds)
            else:
                message = (
                    "Авторизация отклонена. Снова нажмите «🔐 Авторизация»."
                    if result.state == "access_denied"
                    else "Срок авторизации истёк. Снова нажмите «🔐 Авторизация»."
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
