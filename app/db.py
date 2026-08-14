from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import Settings


class Base(DeclarativeBase):
    pass


def make_engine(settings: Settings):
    if settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.removeprefix("sqlite:///")
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    kwargs = {"pool_pre_ping": True}
    if settings.database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(settings.database_url, **kwargs)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def session_dependency(factory: sessionmaker[Session]):
    def dependency() -> Iterator[Session]:
        with factory() as session:
            yield session

    return dependency

