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
    _split_legacy_addresses(engine)


def _split_legacy_addresses(engine) -> None:
    """v0.1 kept a customer's or supplier's address on one line (column 'address', no longer mapped): split it
    into street, house number, postcode, city and country. The old column is left as it was; a row whose street
    is still that whole line (nothing else filled in) is split again."""
    from app import address

    insp = inspect(engine)
    with engine.begin() as conn:
        for table in ("customer", "supplier"):
            if not insp.has_table(table) or "address" not in {c["name"] for c in insp.get_columns(table)}:
                continue
            rows = conn.execute(text(
                f"SELECT id, address FROM {table} WHERE address IS NOT NULL AND address != '' "
                "AND (street IS NULL OR street = address) AND house_number IS NULL AND postal_code IS NULL "
                "AND city IS NULL"
            )).all()
            for row_id, line in rows:
                parts, _, _ = address.complete(address.parse(line))
                conn.execute(
                    text(f"UPDATE {table} SET street = :street, house_number = :house_number, "
                         "postal_code = :postal_code, city = :city, country = :country WHERE id = :id"),
                    {**parts, "id": row_id},
                )


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
