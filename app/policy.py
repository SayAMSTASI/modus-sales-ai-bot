from __future__ import annotations

from datetime import UTC, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import UsageEvent, UserAccess
from app.security import stable_user_hash


def check_limits(session: Session, user: UserAccess, settings: Settings) -> str | None:
    start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    user_hash = stable_user_hash(user.telegram_user_id, settings.safety_identifier_secret)
    user_stats = session.execute(
        select(
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.input_tokens + UsageEvent.output_tokens), 0),
            func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0),
        ).where(UsageEvent.occurred_at >= start, UsageEvent.user_hash == user_hash)
    ).one()
    global_stats = session.execute(
        select(
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0),
        ).where(UsageEvent.occurred_at >= start)
    ).one()
    if user_stats[0] >= user.daily_request_limit:
        return "user_request_limit"
    if user_stats[1] >= user.daily_token_limit:
        return "user_token_limit"
    if float(user_stats[2]) >= user.daily_cost_limit_usd:
        return "user_cost_limit"
    if global_stats[0] >= settings.global_daily_request_limit:
        return "global_request_limit"
    if float(global_stats[1]) >= settings.global_daily_cost_limit_usd:
        return "global_cost_limit"
    return None

