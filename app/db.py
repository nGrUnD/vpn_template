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

        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tariffs (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                months INT NOT NULL,
                price_rub INT NOT NULL,
                traffic_gb INT NOT NULL DEFAULT 0,
                badge TEXT,
                sort_order INT NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )

        # Дефолтные тарифы при первом запуске
        n = await conn.fetchval("SELECT COUNT(*) FROM tariffs")
        if n == 0:
            await conn.executemany(
                """
                INSERT INTO tariffs (name, months, price_rub, traffic_gb, badge, sort_order)
                VALUES ($1, $2, $3, $4, $5, $6);
                """,
                [
                    ("1 месяц", 1, 300, 30, None, 0),
                    ("3 месяца", 3, 750, 100, "−17%", 1),
                    ("12 месяцев", 12, 2400, 500, "−33%", 2),
                ],
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

