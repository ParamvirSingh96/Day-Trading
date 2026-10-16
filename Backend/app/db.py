from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import settings


class Base(DeclarativeBase):
    pass


_is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if _is_sqlite else {}
engine = create_engine(settings.database_url, future=True, connect_args=connect_args)

if _is_sqlite:
    # ── SQLite PRAGMAs applied on every new connection ───────────────────────
    # WAL (Write-Ahead Logging) lets readers and writers work concurrently
    # instead of taking an exclusive lock on every write. That's exactly
    # what this app hits: the scanner writes TradeRecords while the UI
    # reads /autotrade/status at the same moment. Without WAL, the reader
    # blocks the writer (or vice versa) for the duration of the txn.
    #
    # synchronous=NORMAL is the recommended pair for WAL — WAL already
    # makes every commit durable against crashes; synchronous=FULL just
    # adds extra fsyncs that give you durability against a power loss at
    # the cost of ~2× write latency. NORMAL is the standard choice.
    #
    # foreign_keys=ON is off by default in SQLite for historical reasons;
    # enabling it matches what every other DB enforces.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
