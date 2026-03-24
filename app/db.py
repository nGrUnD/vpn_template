from __future__ import annotations

from typing import Optional

import asyncpg

from .config import DatabaseConfig


_pool: Optional[asyncpg.Pool] = None


DEFAULT_TARIFFS = [
    {"name": "3 дня", "months": 0, "price_stars": 1, "traffic_gb": 3, "badge": "Тест", "sort_order": 0},
    {"name": "1 месяц", "months": 1, "price_stars": 55, "traffic_gb": 30, "badge": None, "sort_order": 1},
    {"name": "2 месяца", "months": 2, "price_stars": 100, "traffic_gb": 60, "badge": "−9%", "sort_order": 2},
    {"name": "3 месяца", "months": 3, "price_stars": 140, "traffic_gb": 100, "badge": "−15%", "sort_order": 3},
    {"name": "6 месяцев", "months": 6, "price_stars": 250, "traffic_gb": 250, "badge": "−24%", "sort_order": 4},
    {"name": "12 месяцев", "months": 12, "price_stars": 500, "traffic_gb": 500, "badge": "−24%", "sort_order": 5},
]


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
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS threexui_sub_id TEXT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS subscription_url TEXT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS subscription_json_url TEXT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS backend_key TEXT;
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS backend_inbound_id INT;
            """
        )
        await conn.execute(
            """
            UPDATE subscriptions
            SET backend_key = 'default'
            WHERE backend_key IS NULL OR backend_key = '';
            """
        )
        await conn.execute(
            """
            UPDATE subscriptions
            SET backend_inbound_id = 1
            WHERE backend_inbound_id IS NULL OR backend_inbound_id <= 0;
            """
        )
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_subscriptions_backend_key ON subscriptions(backend_key);
            """
        )
        await conn.execute(
            """
            ALTER TABLE subscriptions
            ADD COLUMN IF NOT EXISTS auto_renew BOOLEAN NOT NULL DEFAULT FALSE;
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

        # Синхронизация дефолтных тарифов. Кастомные тарифы не трогаем.
        for item in DEFAULT_TARIFFS:
            existing_id = await conn.fetchval("SELECT id FROM tariffs WHERE name = $1 ORDER BY id ASC LIMIT 1", item["name"])
            if existing_id:
                await conn.execute(
                    """
                    UPDATE tariffs
                    SET months = $2,
                        price_rub = $3,
                        price_stars = $3,
                        traffic_gb = $4,
                        badge = $5,
                        sort_order = $6,
                        is_active = TRUE
                    WHERE id = $1
                    """,
                    existing_id,
                    item["months"],
                    item["price_stars"],
                    item["traffic_gb"],
                    item["badge"],
                    item["sort_order"],
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO tariffs (name, months, price_rub, price_stars, traffic_gb, badge, sort_order)
                    VALUES ($1, $2, $3, $3, $4, $5, $6)
                    """,
                    item["name"],
                    item["months"],
                    item["price_stars"],
                    item["traffic_gb"],
                    item["badge"],
                    item["sort_order"],
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

