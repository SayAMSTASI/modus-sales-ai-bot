from __future__ import annotations

import json
import logging
import signal
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from app.agent import (
    AgentClient,
    McpUnavailableError,
    build_agent_client,
    calculate_cost_usd,
    parse_allowed_tools,
)
from app.config import Settings, get_settings
from app.db import Base, make_engine, make_session_factory
from app.logging_config import configure_logging
from app.metrics import MetricsExporter, build_metrics_exporter
from app.models import (
    AccessStatus,
    ConversationMessage,
    JobStatus,
    UpdateJob,
    UsageEvent,
    UserAccess,
)
from app.policy import check_limits
from app.security import stable_user_hash
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


class JobProcessor:
    def __init__(
        self,
        settings: Settings,
        factory: sessionmaker[Session],
        agent: AgentClient,
        telegram: TelegramClient,
        metrics: MetricsExporter,
    ) -> None:
        self.settings = settings
        self.factory = factory
        self.agent = agent
        self.telegram = telegram
        self.metrics = metrics

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

    def _send_and_finish(self, session: Session, job: UpdateJob, text: str) -> None:
        self.telegram.send_message(job.chat_id, text)
        self._finish(session, job)

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

    def _process_message(
        self,
        session: Session,
        job: UpdateJob,
        user: UserAccess,
        *,
        message_text: str | None = None,
        required_mcp_server: str | None = None,
    ) -> None:
        if user.status != AccessStatus.active or user.role != "pilot_user":
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
                    "Дневной лимит пилота исчерпан. Обратитесь к администратору.",
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
                result = self.agent.respond(
                    messages=history,
                    safety_identifier=user_hash,
                    allowed_tools=parse_allowed_tools(user.allowed_tools_json),
                    required_mcp_server=required_mcp_server,
                )
            except McpUnavailableError:
                self._send_and_finish(
                    session,
                    job,
                    "MCP пока не подключён: отсутствует OAuth access token или "
                    "не утверждён read-only список tools. Используйте /mcp для статуса.",
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
        self.telegram.send_message(job.chat_id, response_text)
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
            self._send_and_finish(session, job, job.payload_text or "Статус доступа изменён.")
        elif job.kind == "start":
            if user is None:
                self._send_and_finish(session, job, "Не удалось создать заявку. Повторите /start.")
            elif user.status == AccessStatus.pending:
                self._send_and_finish(
                    session,
                    job,
                    f"Заявка {user.request_number} создана. "
                    "До подтверждения OpenAI и MCP не вызываются.",
                )
            elif user.status == AccessStatus.active:
                self._send_and_finish(session, job, "Доступ активен. Отправьте текстовый запрос.")
            else:
                self._send_and_finish(session, job, "Доступ отозван. Обратитесь к администратору.")
        elif job.kind == "help":
            status = user.status if user else "not_requested"
            self._send_and_finish(
                session,
                job,
                f"Команды: /start — заявка, /new — новый диалог, /help — помощь. Статус: {status}.",
            )
        elif job.kind == "new":
            session.execute(
                delete(ConversationMessage).where(
                    ConversationMessage.telegram_user_id == job.telegram_user_id,
                    ConversationMessage.chat_id == job.chat_id,
                )
            )
            self._send_and_finish(session, job, "Контекст очищен. Начат новый диалог.")
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
            self._send_and_finish(session, job, "Дневной лимит пилота исчерпан.")
        elif job.kind == "access_denied" or user is None:
            self._send_and_finish(
                session,
                job,
                "Запрос не обработан. Выполните /start и дождитесь подтверждения доступа.",
            )
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
                        seconds=min(2 ** job.attempts, 30)
                    )
                session.commit()
                logger.exception("Job failed job_id=%s attempt=%s", job.id, job.attempts)
            return True


def main() -> None:
    configure_logging()
    settings = get_settings()
    engine = make_engine(settings)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    processor = JobProcessor(
        settings,
        factory,
        build_agent_client(settings),
        build_telegram_client(settings),
        build_metrics_exporter(),
    )
    running = True

    def stop(*_args):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    logger.info("Worker started")
    while running:
        if not processor.run_once():
            time.sleep(settings.worker_poll_seconds)
    logger.info("Worker stopped")


if __name__ == "__main__":
    main()
