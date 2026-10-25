"""Database engine and session helpers (SQLModel on SQLite by default)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
        _engine = create_engine(settings.database_url, connect_args=connect_args)
    return _engine


def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _add_missing_columns(engine)
    _map_legacy_statuses(engine)


def _map_legacy_statuses(engine) -> None:
    """v0.1 project statuses (nieuw/gemeten/offerte/verzonden) -> the workflow statuses."""
    from app.workflow import LEGACY

    with engine.begin() as conn:
        for old, new in LEGACY.items():
            conn.execute(text("UPDATE project SET status = :new WHERE status = :old"), {"new": new, "old": old})


def _add_missing_columns(engine) -> None:
    """Tiny forward-only migration: new model columns are added to existing tables when they are nullable or
    have a plain default (SQLite can't add NOT NULL columns without one)."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = col.type.compile(dialect=engine.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if col.nullable:
                    conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl}'))
                elif isinstance(default, (int, float, str, bool)):
                    literal = repr(default).replace("True", "1").replace("False", "0") if not isinstance(default, str) else "'" + default.replace("'", "''") + "'"
                    conn.execute(
                        text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl} NOT NULL DEFAULT {literal}')
                    )


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    with Session(get_engine()) as session:
        yield session


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(get_engine()) as session:
        yield session
