from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import Config


class Database:
    def __init__(self, config: Config):
        self.engine = create_async_engine(
            config.database_url,
            connect_args={"server_settings": {"timezone": "UTC"}},
            isolation_level="READ COMMITTED",
            pool_size=config.database.pool_size,
            max_overflow=config.database.max_overflow,
            pool_timeout=config.database.pool_timeout_seconds,
            pool_pre_ping=True,
            pool_recycle=1_800,
        )
        self.session_factory = async_sessionmaker(
            self.engine, class_=AsyncSession, expire_on_commit=False
        )

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def close(self) -> None:
        await self.engine.dispose()
