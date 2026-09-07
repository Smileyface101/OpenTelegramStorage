"""Async SQLite engine (WAL) and session factory."""
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app import config


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    url = url or f"sqlite+aiosqlite:///{config.DB_PATH}"
    engine = create_async_engine(url, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


engine = None
async_session: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str | None = None) -> None:
    global engine, async_session
    engine = make_engine(url)
    async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db():
    async with async_session() as session:
        yield session
