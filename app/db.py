from __future__ import annotations

from typing import Optional

import asyncpg

from .config import DatabaseConfig


_pool: Optional[asyncpg.Pool] = None


async def init_db(db_config: DatabaseConfig) -> None:
    """
    Initialize global connection pool and ensure base tables exist.
    """
    global _pool
    if _pool is not None:
        return

    _pool = await asyncpg.create_pool(dsn=db_config.url, min_size=1, max_size=5)

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                server_label TEXT,
                threexui_client_id TEXT,
                config TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )


async def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool is not initialized")
    return _pool


async def close_db() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None

