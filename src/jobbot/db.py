"""Engine and session management.

SQLite is the default so the project runs with zero infrastructure. Two pragmas
matter and are easy to get wrong: foreign keys are off by default in SQLite (so
the schema's referential integrity would be decorative), and the default
journal mode serialises the scheduler against the review UI. Both are set on
every new connection.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, event, inspect, text
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from jobbot import models  # noqa: F401  -- import registers tables on SQLModel.metadata
from jobbot.config import Settings, get_settings

logger = logging.getLogger("jobbot.db")

_engine: Engine | None = None


def _configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def build_engine(settings: Settings | None = None, *, echo: bool = False) -> Engine:
    """Create a fresh engine. Tests use this directly to get isolated databases."""
    settings = settings or get_settings()
    url = settings.database_url
    kwargs: dict[str, Any] = {"echo": echo}

    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # "sqlite://" with no path is also an in-memory database.
        if ":memory:" in url or url.rstrip("/") == "sqlite:":
            # Without a shared static pool each connection gets its own empty
            # in-memory database, which silently breaks every test.
            kwargs["poolclass"] = StaticPool
        else:
            db_path = settings.db_path
            if db_path is not None:
                db_path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _configure_sqlite)
    return engine


def get_engine() -> Engine:
    """Process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        _engine = build_engine()
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Used by tests and by `jobbot db reset`."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def create_all(engine: Engine | None = None) -> None:
    SQLModel.metadata.create_all(engine or get_engine())


def missing_columns(engine: Engine | None = None) -> dict[str, list[str]]:
    """Columns the models declare that the database does not have yet."""
    engine = engine or get_engine()
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    gaps: dict[str, list[str]] = {}
    for name, table in SQLModel.metadata.tables.items():
        if name not in existing_tables:
            continue
        present = {column["name"] for column in inspector.get_columns(name)}
        missing = [column.name for column in table.columns if column.name not in present]
        if missing:
            gaps[name] = missing
    return gaps


def sync_schema(engine: Engine | None = None) -> dict[str, list[str]]:
    """Add columns the models gained since the database was created.

    A full migration framework is more machinery than a single-file, single-user
    database needs, but silently running against a stale schema is worse than
    either. This handles the one change that actually happens -- a new nullable
    column -- and refuses anything else loudly rather than guessing.

    Returns the columns it added, per table.
    """
    engine = engine or get_engine()
    create_all(engine)  # tables added since last time
    gaps = missing_columns(engine)
    if not gaps:
        return {}

    added: dict[str, list[str]] = {}
    with engine.begin() as connection:
        for table_name, columns in gaps.items():
            table = SQLModel.metadata.tables[table_name]
            for column_name in columns:
                column = table.columns[column_name]
                if not column.nullable and column.server_default is None:
                    raise RuntimeError(
                        f"cannot add {table_name}.{column_name} automatically: it is "
                        "NOT NULL with no default. Back up var/jobbot.db and run "
                        "'jobbot db reset'."
                    )
                type_sql = column.type.compile(engine.dialect)
                connection.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column_name}" {type_sql}')
                )
                added.setdefault(table_name, []).append(column_name)
                logger.info("added column %s.%s", table_name, column_name)
    return added


def drop_all(engine: Engine | None = None) -> None:
    SQLModel.metadata.drop_all(engine or get_engine())


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on any exception."""
    session = Session(engine or get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
