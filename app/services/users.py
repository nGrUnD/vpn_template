from __future__ import annotations

from typing import Optional

import asyncpg
from aiogram.types import User as TgUser

from app.db import get_pool


async def get_or_create_user(tg_user: TgUser) -> asyncpg.Record:
    """
    Ensure user exists in DB, return its row.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row: Optional[asyncpg.Record] = await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1",
            tg_user.id,
        )
        if row:
            return row

        row = await conn.fetchrow(
            """
            INSERT INTO users (telegram_id, username, first_name, last_name)
            VALUES ($1, $2, $3, $4)
            RETURNING *;
            """,
            tg_user.id,
            tg_user.username,
            tg_user.first_name,
            tg_user.last_name,
        )
        return row

