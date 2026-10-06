"""Datenbank-Anbindung (SQLAlchemy 2, synchron). PostgreSQL im Betrieb, SQLite für Tests."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _normalize_url(url: str) -> str:
    # Coolify liefert oft postgres://… – SQLAlchemy braucht den Treibernamen.
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def init_engine(url: str | None = None) -> Engine:
    global _engine, _SessionLocal
    url = _normalize_url(url or get_settings().database_url)
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    _engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _fk_on(dbapi_conn, _record):  # pragma: no cover - trivial
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    return _engine or init_engine()


def create_all() -> None:
    from . import models  # noqa: F401  (Modelle registrieren)

    Base.metadata.create_all(get_engine())
    migrate()


# Neue Spalten in bereits bestehenden Tabellen (create_all ergänzt nur fehlende Tabellen).
_NEW_COLUMNS = [
    # (Tabelle, Spalte, PostgreSQL-Typ, SQLite-Typ)
    ("mailboxes", "send_disabled", "BOOLEAN NOT NULL DEFAULT false", "BOOLEAN NOT NULL DEFAULT 0"),
    ("mailboxes", "auto_cleanup", "BOOLEAN NOT NULL DEFAULT false", "BOOLEAN NOT NULL DEFAULT 0"),
    ("users", "telegram_chat_id", "VARCHAR(32)", "VARCHAR(32)"),
    ("users", "last_report_date", "VARCHAR(10)", "VARCHAR(10)"),
    ("proposals", "notified_at", "TIMESTAMP WITH TIME ZONE", "DATETIME"),
]


def migrate() -> list[str]:
    """Ergänzt fehlende Spalten, damit ein Update keine Daten kostet. Gibt die ergänzten Spalten zurück."""
    from sqlalchemy import inspect, text

    engine = get_engine()
    inspector = inspect(engine)
    sqlite = engine.dialect.name == "sqlite"
    tables = set(inspector.get_table_names())
    added: list[str] = []
    with engine.begin() as conn:
        for table, column, pg_type, sqlite_type in _NEW_COLUMNS:
            if table not in tables:
                continue
            if column in {c["name"] for c in inspector.get_columns(table)}:
                continue
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {sqlite_type if sqlite else pg_type}"))
            added.append(f"{table}.{column}")
    return added


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        init_engine()
    assert _SessionLocal is not None
    db = _SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI-Abhängigkeit."""
    with session_scope() as db:
        yield db
