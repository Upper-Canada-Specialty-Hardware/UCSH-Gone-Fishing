import os

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

if settings.DATABASE_URL:
    # Railway Postgres — convert to asyncpg scheme
    _url = settings.DATABASE_URL
    if _url.startswith("postgres://"):
        _url = _url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif _url.startswith("postgresql://"):
        _url = _url.replace("postgresql://", "postgresql+asyncpg://", 1)
    DATABASE_URL = _url
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        pool_size=5,
        max_overflow=10,
    )
else:
    # Local dev — SQLite
    DB_DIR = "app/data"
    DB_PATH = os.path.join(DB_DIR, "gone_fishing.db")
    DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
    os.makedirs(DB_DIR, exist_ok=True)
    engine = create_async_engine(DATABASE_URL, echo=False)

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
