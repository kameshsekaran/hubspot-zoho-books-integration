"""
Database engine, session factory, and Base model for SQLAlchemy.

Uses a single SQLAlchemy engine that supports both sync and async patterns.
For SQLite we enable WAL mode and foreign-key enforcement; for Postgres the
connection string is used as-is.

Usage in dependencies::

    from app.database import get_db

    @app.get("/example")
    async def example(db: Session = Depends(get_db)):
        ...
"""

import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_connect_args: dict = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url,
    connect_args=_connect_args,
    pool_pre_ping=True,
    echo=(settings.log_level == "DEBUG"),
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
    """Enable WAL mode and foreign-key enforcement for SQLite connections."""
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# ---------------------------------------------------------------------------
# Session factory
# ---------------------------------------------------------------------------

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)


# ---------------------------------------------------------------------------
# Declarative base
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    """Project-wide SQLAlchemy declarative base."""


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a SQLAlchemy session and ensures it is
    closed after the request completes.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session() -> Generator[Session, None, None]:
    """
    Context manager version of get_db for use outside FastAPI dependency
    injection (e.g., scripts, scheduled jobs).
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    """
    Create all tables that are registered on Base.metadata.

    Should be called once at application startup after all models have been
    imported so SQLAlchemy is aware of every table definition.
    """
    # Import models so their metadata is registered before create_all
    from app.models import job, link, log  # noqa: F401
    from app.zoho.auth import ZohoToken  # noqa: F401

    Base.metadata.create_all(bind=engine)
    logger.info("Database tables initialised.")


def verify_db_connection() -> bool:
    """
    Execute a trivial query to confirm the database is reachable.

    Returns True on success, False on failure.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:  # pragma: no cover
        logger.error("Database connection check failed: %s", exc)
        return False
