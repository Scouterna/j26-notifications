from __future__ import annotations

import json
import logging

import asyncpg
from asyncpg import Connection, Pool, Record
from asyncpg.exceptions import UniqueViolationError

from .config import get_settings
from .exeptions import J26NotificationError

settings = get_settings()
logger = logging.getLogger(__name__)


class DuplicateDocumentError(Exception):
    """Raised when attempting to insert a document that already exists."""


pg_pool: Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog", format="text")
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog", format="text")


async def connect_to_db() -> Pool:
    global pg_pool
    if pg_pool is None:
        logging.info("Connecting to DB")
        try:
            pg_pool = await asyncpg.create_pool(dsn=settings.POSTGRES_DSN, init=_init_connection)
        except ConnectionRefusedError:
            raise J26NotificationError("Can't connect to database")
        logging.debug("Connected to DB")
    return pg_pool


async def close_db_connection() -> None:
    global pg_pool
    if pg_pool is not None:
        logging.info("Closing connection to DB")
        await pg_pool.close()
        pg_pool = None


async def db_execute(query: str, *args) -> None:
    async with pg_pool.acquire() as conn:
        await conn.execute(query, *args)


async def db_fetch(query: str, *args) -> list[Record] | None:
    async with pg_pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def db_fetchrow(query: str, *args) -> Record | None:
    async with pg_pool.acquire() as conn:
        return await conn.fetchrow(query, *args)
