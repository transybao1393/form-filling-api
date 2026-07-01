"""Async SQLAlchemy engine + session factory + FastAPI `get_db` dependency.

SQLite via aiosqlite. The DB file lives under JOBS_DIR (mounted volume in
Docker) so it survives container restarts alongside job uploads.
"""

from __future__ import annotations

from typing import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from . import config
from .models import Base


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _on_sqlite_connect(dbapi_connection, _record):
    """Set pragmas on every new connection.

    - journal_mode=WAL  : readers don't block the single writer, and a
      crashed writer leaves a recoverable journal instead of a corrupt DB.
    - foreign_keys=ON   : enforce ForeignKey() constraints (SQLite ignores
      them by default unless this is explicitly enabled per-connection).
    - synchronous=NORMAL: WAL + NORMAL is the standard performance config.
    """
    cur = dbapi_connection.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
    finally:
        cur.close()


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(config.DATABASE_URL, echo=False, future=True)
        if config.DATABASE_URL.startswith("sqlite"):
            event.listen(_engine.sync_engine, "connect", _on_sqlite_connect)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            get_engine(), expire_on_commit=False, autoflush=False,
        )
    return _sessionmaker


async def init_models() -> None:
    """Create tables on first run. Idempotent — safe to call every startup."""
    config.JOBS_DIR.mkdir(parents=True, exist_ok=True)
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_templates_source_document_id(conn)


async def _migrate_templates_source_document_id(conn) -> None:
    """Add templates.source_document_id on existing SQLite DBs."""
    if not config.DATABASE_URL.startswith("sqlite"):
        return
    result = await conn.exec_driver_sql("PRAGMA table_info(templates)")
    columns = {row[1] for row in result.fetchall()}
    if "source_document_id" not in columns:
        await conn.exec_driver_sql(
            "ALTER TABLE templates ADD COLUMN source_document_id INTEGER "
            "REFERENCES documents(id) ON DELETE SET NULL"
        )


async def get_db() -> AsyncIterator[AsyncSession]:
    sm = get_sessionmaker()
    async with sm() as session:
        yield session
