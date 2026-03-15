from __future__ import annotations

from typing import Any, Optional

import asyncpg

from app.db import get_pool


async def get_tariffs(active_only: bool = True) -> list[dict[str, Any]]:
    """Список тарифов для отображения (по умолчанию только активные)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        where = "WHERE is_active = TRUE" if active_only else ""
        rows = await conn.fetch(
            f"""
            SELECT id, name, months, COALESCE(price_stars, price_rub) AS price_stars, traffic_gb, badge, sort_order, is_active
            FROM tariffs
            {where}
            ORDER BY sort_order ASC, id ASC;
            """
        )
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "months": r["months"],
            "duration_label": r["name"] if int(r["months"] or 0) <= 0 else f"{r['months']} мес.",
            "price_stars": r["price_stars"],
            "traffic_gb": r["traffic_gb"],
            "badge": r["badge"],
            "sort_order": r["sort_order"],
            "is_active": r["is_active"],
        }
        for r in rows
    ]


async def get_tariff_by_id(tariff_id: int) -> Optional[asyncpg.Record]:
    """Один тариф по id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow("SELECT * FROM tariffs WHERE id = $1", tariff_id)


async def create_tariff(
    name: str,
    months: int,
    price_stars: int,
    traffic_gb: int = 0,
    badge: Optional[str] = None,
    sort_order: int = 0,
) -> asyncpg.Record:
    """Создать тариф."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            INSERT INTO tariffs (name, months, price_rub, price_stars, traffic_gb, badge, sort_order)
            VALUES ($1, $2, $3, $3, $4, $5, $6)
            RETURNING *;
            """,
            name,
            months,
            price_stars,
            traffic_gb,
            badge,
            sort_order,
        )


async def update_tariff(
    tariff_id: int,
    *,
    name: Optional[str] = None,
    months: Optional[int] = None,
    price_stars: Optional[int] = None,
    traffic_gb: Optional[int] = None,
    badge: Optional[str] = None,
    sort_order: Optional[int] = None,
    is_active: Optional[bool] = None,
) -> Optional[asyncpg.Record]:
    """Обновить тариф."""
    pool = await get_pool()
    updates = []
    values = []
    i = 1
    if name is not None:
        updates.append(f"name = ${i}")
        values.append(name)
        i += 1
    if months is not None:
        updates.append(f"months = ${i}")
        values.append(months)
        i += 1
    if price_stars is not None:
        updates.append(f"price_stars = ${i}")
        values.append(price_stars)
        i += 1
    if traffic_gb is not None:
        updates.append(f"traffic_gb = ${i}")
        values.append(traffic_gb)
        i += 1
    if badge is not None:
        updates.append(f"badge = ${i}")
        values.append(badge)
        i += 1
    if sort_order is not None:
        updates.append(f"sort_order = ${i}")
        values.append(sort_order)
        i += 1
    if is_active is not None:
        updates.append(f"is_active = ${i}")
        values.append(is_active)
        i += 1
    if not updates:
        return await get_tariff_by_id(tariff_id)
    values.append(tariff_id)
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            f"UPDATE tariffs SET {', '.join(updates)} WHERE id = ${i} RETURNING *;",
            *values,
        )


async def delete_tariff(tariff_id: int) -> bool:
    """Удалить тариф. Возвращает True если удалён."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM tariffs WHERE id = $1", tariff_id)
    return result == "DELETE 1"
