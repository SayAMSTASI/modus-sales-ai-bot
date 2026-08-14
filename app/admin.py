from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.models import AdminAudit, ConversationMessage, UpdateJob, UsageEvent, UserAccess
from app.security import csrf_token, require_admin, verify_csrf


def build_admin_router(settings: Settings, factory: sessionmaker[Session]) -> APIRouter:
    router = APIRouter()
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    admin_dependency = require_admin(settings)

    def safe_tools() -> set[str]:
        return {
            f"{server.server_label}:{tool}"
            for server in settings.mcp_servers()
            for tool in server.allowed_tools
        }

    def get_user(session: Session, telegram_user_id: int) -> UserAccess:
        user = session.scalar(
            select(UserAccess).where(UserAccess.telegram_user_id == telegram_user_id)
        )
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return user

    def audit(session: Session, admin: str, action: str, user: UserAccess, metadata: dict) -> None:
        session.add(
            AdminAudit(
                admin_name=admin,
                action=action,
                target_telegram_user_id=user.telegram_user_id,
                metadata_json=json.dumps(metadata, ensure_ascii=False),
            )
        )

    def notice(session: Session, user: UserAccess, text: str) -> None:
        session.add(
            UpdateJob(
                telegram_user_id=user.telegram_user_id,
                chat_id=user.chat_id,
                kind="system_notice",
                payload_text=text,
            )
        )

    @router.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, admin: str = Depends(admin_dependency)):
        with factory() as session:
            users = session.scalars(select(UserAccess).order_by(UserAccess.created_at.desc())).all()
            audits = session.scalars(
                select(AdminAudit).order_by(AdminAudit.occurred_at.desc()).limit(50)
            ).all()
            return templates.TemplateResponse(
                request=request,
                name="admin.html",
                context={
                    "users": users,
                    "audits": audits,
                    "csrf": csrf_token(settings),
                    "admin": admin,
                    "safe_tools": sorted(safe_tools()),
                },
            )

    @router.post("/admin/users/{telegram_user_id}/approve")
    def approve(
        telegram_user_id: int,
        admin: str = Depends(admin_dependency),
        csrf: str = Form(),
        corporate_name: str = Form(min_length=2, max_length=255),
        corporate_email: str = Form(min_length=3, max_length=255),
        role: str = Form(default="pilot_user", max_length=64),
        allowed_tools: str = Form(default=""),
        daily_request_limit: int = Form(default=50, ge=1, le=10000),
        daily_token_limit: int = Form(default=250000, ge=1000, le=100000000),
        daily_cost_limit_usd: float = Form(default=10.0, ge=0.01, le=100000),
    ):
        if not verify_csrf(csrf, settings):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        if role.strip() != "pilot_user":
            raise HTTPException(status_code=400, detail="Pilot only supports role pilot_user")
        requested = {item.strip() for item in allowed_tools.split(",") if item.strip()}
        invalid = requested - safe_tools()
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"Tools are not globally allowed: {sorted(invalid)}",
            )
        with factory() as session:
            user = get_user(session, telegram_user_id)
            user.status = "active"
            user.corporate_name = corporate_name.strip()
            user.corporate_email = corporate_email.strip()
            user.role = role.strip()
            user.allowed_tools_json = json.dumps(sorted(requested))
            user.daily_request_limit = daily_request_limit
            user.daily_token_limit = daily_token_limit
            user.daily_cost_limit_usd = daily_cost_limit_usd
            user.approved_by = admin
            user.approved_at = datetime.now(UTC)
            audit(session, admin, "approve", user, {"role": user.role, "tools": sorted(requested)})
            notice(session, user, "Доступ к пилоту подтверждён. Можно отправлять запросы боту.")
            session.commit()
        return RedirectResponse("/admin", status_code=303)

    def change_status(telegram_user_id: int, admin: str, csrf: str, status: str, action: str):
        if not verify_csrf(csrf, settings):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
        with factory() as session:
            user = get_user(session, telegram_user_id)
            user.status = status
            session.query(ConversationMessage).filter(
                ConversationMessage.telegram_user_id == telegram_user_id
            ).delete()
            audit(session, admin, action, user, {})
            message = (
                "Доступ к пилоту восстановлен."
                if status == "active"
                else "Доступ к пилоту закрыт. Обратитесь к администратору."
            )
            notice(session, user, message)
            session.commit()
        return RedirectResponse("/admin", status_code=303)

    @router.post("/admin/users/{telegram_user_id}/revoke")
    def revoke(
        telegram_user_id: int,
        admin: str = Depends(admin_dependency),
        csrf: str = Form(),
    ):
        return change_status(telegram_user_id, admin, csrf, "revoked", "revoke")

    @router.post("/admin/users/{telegram_user_id}/restore")
    def restore(
        telegram_user_id: int,
        admin: str = Depends(admin_dependency),
        csrf: str = Form(),
    ):
        return change_status(telegram_user_id, admin, csrf, "active", "restore")

    @router.get("/admin/usage.csv")
    def usage_csv(admin: str = Depends(admin_dependency)):
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "occurred_at",
                "user_hash",
                "request_id",
                "scenario",
                "result",
                "duration_ms",
                "model",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "estimated_cost_usd",
            ]
        )
        with factory() as session:
            for event in session.scalars(select(UsageEvent).order_by(UsageEvent.occurred_at)):
                writer.writerow(
                    [
                        event.occurred_at.isoformat(),
                        event.user_hash,
                        event.request_id,
                        event.scenario,
                        event.result,
                        event.duration_ms,
                        event.model,
                        event.input_tokens,
                        event.cached_input_tokens,
                        event.output_tokens,
                        event.estimated_cost_usd,
                    ]
                )
        response = StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv; charset=utf-8",
        )
        response.headers["Content-Disposition"] = "attachment; filename=usage.csv"
        return response

    return router
