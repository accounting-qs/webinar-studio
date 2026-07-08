from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from config import settings
from db.base import Base  # noqa: F401 — re-exported for backwards compatibility


def _async_db_url(url: str) -> str:
    """Normalize DATABASE_URL to use asyncpg driver.
    Railway injects postgresql:// or postgres:// — both need +asyncpg suffix."""
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    if "postgresql+asyncpg://" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


engine = create_async_engine(
    _async_db_url(settings.DATABASE_URL),
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    # Managed poolers (Supabase/PgBouncer) close connections held/idle beyond a
    # few minutes. Recycle before that so a long-running sync never hands out a
    # server-closed connection mid-operation.
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
