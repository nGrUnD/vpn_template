from __future__ import annotations

import datetime as dt
from typing import Optional

import asyncpg

from app.db import get_pool
from app.threexui_client import ThreeXUIClient, ThreeXUIClientInfo


INBOUND_ID_FOR_SYNC = 1


async def get_active_subscriptions_by_telegram_id(
    telegram_id: int,
    threexui: ThreeXUIClient | None = None,
) -> list[asyncpg.Record]:
    """
    Список активных подписок пользователя.
    Если передан threexui, перед возвратом синхронизирует с панелью:
    подписки, у которых клиент удалён в 3x-ui, помечаются is_active=FALSE.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT s.id, s.server_label, s.threexui_client_id, s.config, s.is_active, s.expires_at, s.created_at
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE u.telegram_id = $1 AND s.is_active = TRUE AND (s.expires_at IS NULL OR s.expires_at > NOW())
            ORDER BY s.created_at DESC;
            """,
            telegram_id,
        )
        rows = list(rows)
        if threexui and rows:
            still_active = []
            for r in rows:
                client_id = r.get("threexui_client_id")
                if client_id and not await threexui.client_exists(INBOUND_ID_FOR_SYNC, client_id):
                    await conn.execute("UPDATE subscriptions SET is_active = FALSE WHERE id = $1", r["id"])
                else:
                    still_active.append(r)
            rows = still_active
    return rows


async def create_test_subscription(
    db_user_id: int,
    telegram_id: int,
    threexui: ThreeXUIClient,
) -> asyncpg.Record:
    """
    Create a short-lived test VPN subscription for the user.

    Uses ThreeXUIClient to provision a VLESS client and stores
    resulting config in the database.
    """
    client_info: ThreeXUIClientInfo = await threexui.create_vless_client(
        telegram_id=telegram_id,
        expire_days=1,
        total_gb=3,
        remark=f"test_{telegram_id}",
    )

    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row: Optional[asyncpg.Record] = await conn.fetchrow(
            """
            INSERT INTO subscriptions (
                user_id,
                server_label,
                threexui_client_id,
                config,
                is_active,
                expires_at
            ) VALUES ($1, $2, $3, $4, TRUE, $5)
            RETURNING *;
            """,
            db_user_id,
            client_info.server_label,
            client_info.client_id,
            client_info.config_text,
            expires_at,
        )

    return row

