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
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS vpn_balance INT NOT NULL DEFAULT 0;
            """
        )
        await conn.execute(
            """
            ALTER TABLE users
            ADD COLUMN IF NOT EXISTS vpn_balance_stars INT NOT NULL DEFAULT 0;
            """
        )
        await conn.execute(
            """
            UPDATE users
            SET vpn_balance_stars = vpn_balance
            WHERE vpn_balance_stars = 0 AND vpn_balance <> 0;
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
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS device_os TEXT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS tariff_id INTEGER;
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS tariff_price_rub INT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS tariff_price_stars INT;
            """
        )
        await conn.execute(
            """
            UPDATE subscriptions
            SET tariff_price_stars = tariff_price_rub
            WHERE tariff_price_stars IS NULL AND tariff_price_rub IS NOT NULL;
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS tariff_months INT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS tariff_traffic_gb INT;
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
        await conn.execute(
            """
            ALTER TABLE tariffs
            ADD COLUMN IF NOT EXISTS price_stars INT;
            """
        )
        await conn.execute(
            """
            UPDATE tariffs
            SET price_stars = price_rub
            WHERE price_stars IS NULL;
            """
        )
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_transactions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                amount INT NOT NULL,
                currency TEXT NOT NULL DEFAULT 'XTR',
                description TEXT,
                payload TEXT UNIQUE,
                telegram_payment_charge_id TEXT,
                provider_payment_charge_id TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                paid_at TIMESTAMPTZ
            );
            """
        )
        await conn.execute(
            """
            ALTER TABLE wallet_transactions
            ADD COLUMN IF NOT EXISTS provider_amount INT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE wallet_transactions
            ADD COLUMN IF NOT EXISTS provider_currency TEXT;
            """
        )

        # Дефолтные тарифы при первом запуске
        n = await conn.fetchval("SELECT COUNT(*) FROM tariffs")
        if n == 0:
            await conn.executemany(
                """
                INSERT INTO tariffs (name, months, price_rub, price_stars, traffic_gb, badge, sort_order)
                VALUES ($1, $2, $3, $4, $5, $6, $7);
                """,
                [
                    ("1 месяц", 1, 300, 300, 30, None, 0),
                    ("3 месяца", 3, 750, 750, 100, "−17%", 1),
                    ("12 месяцев", 12, 2400, 2400, 500, "−33%", 2),
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

