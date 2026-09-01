from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models import SkillVersion


def available_skills(project_dir: Path) -> list[str]:
    skills_dir = project_dir / "skills"
    if not skills_dir.is_dir():
        return []
    return sorted(
        path.parent.name
        for path in skills_dir.glob("*/SKILL.md")
        if path.is_file()
    )


def base_skill_content(project_dir: Path, skill_name: str) -> str:
    if skill_name not in available_skills(project_dir):
        raise ValueError("Unknown skill")
    return (project_dir / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")


def active_skill_overrides(session: Session) -> dict[str, str]:
    rows = session.scalars(
        select(SkillVersion).where(SkillVersion.is_active.is_(True))
    ).all()
    return {row.skill_name: row.content for row in rows}


def active_skill_version(session: Session, skill_name: str) -> SkillVersion | None:
    return session.scalar(
        select(SkillVersion).where(
            SkillVersion.skill_name == skill_name,
            SkillVersion.is_active.is_(True),
        )
    )


def create_skill_version(
    session: Session,
    *,
    skill_name: str,
    content: str,
    admin_telegram_user_id: int,
) -> SkillVersion:
    next_version = int(
        session.scalar(
            select(func.coalesce(func.max(SkillVersion.version), 0)).where(
                SkillVersion.skill_name == skill_name
            )
        )
        or 0
    ) + 1
    session.execute(
        update(SkillVersion)
        .where(SkillVersion.skill_name == skill_name)
        .values(is_active=False)
    )
    version = SkillVersion(
        skill_name=skill_name,
        version=next_version,
        content=content,
        is_active=True,
        created_by_telegram_id=admin_telegram_user_id,
    )
    session.add(version)
    session.flush()
    return version


def rollback_skill(session: Session, skill_name: str) -> SkillVersion | None:
    current = active_skill_version(session, skill_name)
    if current is None:
        return None
    previous = session.scalar(
        select(SkillVersion)
        .where(
            SkillVersion.skill_name == skill_name,
            SkillVersion.version < current.version,
        )
        .order_by(SkillVersion.version.desc())
        .limit(1)
    )
    current.is_active = False
    if previous is not None:
        previous.is_active = True
    session.flush()
    return previous
