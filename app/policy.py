from __future__ import annotations

from datetime import UTC, datetime, time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import UsageEvent, UserAccess


def check_limits(session: Session, user: UserAccess, settings: Settings) -> str | None:
    start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    global_stats = session.execute(
        select(
            func.count(UsageEvent.id),
            func.coalesce(func.sum(UsageEvent.estimated_cost_usd), 0.0),
        ).where(UsageEvent.occurred_at >= start)
    ).one()
    if global_stats[0] >= settings.global_daily_request_limit:
        return "global_request_limit"
    if float(global_stats[1]) >= settings.global_daily_cost_limit_usd:
        return "global_cost_limit"
    return None
