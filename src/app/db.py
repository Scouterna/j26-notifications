from __future__ import annotations

import logging

import asyncpg
from asyncpg import Pool, Record

from .config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class J26NotificationError(Exception):
    """Raised for expected application-level errors, e.g. database connection failure."""


pg_pool: Pool | None = None


async def connect_to_db() -> Pool:
    global pg_pool
    if pg_pool is None:
        logger.info("Connecting to DB")
        try:
            pg_pool = await asyncpg.create_pool(
                dsn=settings.POSTGRES_DSN,
                max_inactive_connection_lifetime=60,
            )
        except ConnectionRefusedError:
            raise J26NotificationError("Can't connect to database")
        logger.debug("Connected to DB")
    return pg_pool


async def close_db_connection() -> None:
    global pg_pool
    if pg_pool is not None:
        logger.info("Closing connection to DB")
        await pg_pool.close()
        pg_pool = None


async def db_execute(query: str, *args) -> None:
    async with pg_pool.acquire() as conn:
        logger.debug(f"PSQL: {query}")
        await conn.execute(query, *args)


async def db_fetch(query: str, *args) -> list[Record] | None:
    async with pg_pool.acquire() as conn:
        logger.debug(f"PSQL: {query}")
        return await conn.fetch(query, *args)


async def db_fetchrow(query: str, *args) -> Record | None:
    async with pg_pool.acquire() as conn:
        logger.debug(f"PSQL: {query}")
        return await conn.fetchrow(query, *args)


async def db_init_tables() -> None:
    logger.info("Initializing database tables")
    await db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id  TEXT PRIMARY KEY,
            channels TEXT[] NOT NULL DEFAULT '{}',
            tokens   TEXT[] NOT NULL DEFAULT '{}'
        )
    """)
    await db_execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id        BIGSERIAL PRIMARY KEY,
            channel   TEXT        NOT NULL,
            message   TEXT        NOT NULL,
            timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    await db_execute("CREATE INDEX IF NOT EXISTS messages_channel_idx   ON messages (channel)")
    await db_execute("CREATE INDEX IF NOT EXISTS messages_timestamp_idx ON messages (timestamp)")
    logger.info("Database tables ready")
