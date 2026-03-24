from __future__ import annotations

import datetime as dt
from typing import Optional

import asyncpg

from app.db import get_pool
from app.services.backends import get_registry_client
from app.threexui_client import ThreeXUIClient, ThreeXUIClientInfo


def _resolve_duration_days(months: int | None, tariff_name: str | None) -> int:
    """
    Длительность периода в днях. Для тарифов с months > 0 — месяцы по 30 дней.
    Для months == 0 (короткие тарифы вроде «3 дня») — по названию тарифа из БД,
    а не по server_label устройства (оно может быть «Android» и т.п.).
    """
    m = int(months or 0)
    name = (tariff_name or "").strip().lower()
    if m > 0:
        return m * 30
    if "3 дня" in name or "3 days" in name or "3 day" in name:
        return 3
    return 30


def _build_device_label(device_os: str | None, sequence: int) -> str | None:
    if not device_os:
        return None
    return device_os if sequence <= 1 else f"{device_os} {sequence}"


def _build_device_remark(telegram_id: int, device_os: str | None, sequence: int) -> str:
    if not device_os:
        return f"raccster_vpn_{telegram_id}"
    normalized = "".join(ch.lower() if ch.isalnum() else "_" for ch in device_os).strip("_") or "device"
    return f"raccster_vpn_{telegram_id}_{normalized}_{sequence}"


async def _next_device_sequence(db_user_id: int, device_os: str | None) -> int:
    if not device_os:
        return 1
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval(
            """
            SELECT COUNT(*)
            FROM subscriptions
            WHERE user_id = $1 AND device_os = $2
            """,
            db_user_id,
            device_os,
        )
    return int(count or 0) + 1


async def get_active_subscriptions_by_telegram_id(
    telegram_id: int,
    threexui_registry: dict[str, ThreeXUIClient] | None = None,
    default_backend_key: str = "default",
) -> list[asyncpg.Record]:
    """
    Список активных подписок пользователя.
    Если передан registry 3x-ui, перед возвратом синхронизирует с панелью:
    подписки, у которых клиент удалён в 3x-ui, помечаются is_active=FALSE.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                s.id,
                s.server_label,
                s.threexui_client_id,
                s.threexui_sub_id,
                s.subscription_url,
                s.subscription_json_url,
                s.backend_key,
                s.backend_inbound_id,
                s.config,
                s.device_os,
                COALESCE(s.tariff_id, t.id) AS tariff_id,
                COALESCE(s.tariff_price_stars, s.tariff_price_rub, t.price_stars, t.price_rub) AS tariff_price_stars,
                COALESCE(s.tariff_months, t.months) AS tariff_months,
                COALESCE(s.tariff_traffic_gb, t.traffic_gb) AS tariff_traffic_gb,
                s.is_active,
                s.expires_at,
                s.created_at,
                COALESCE(s.auto_renew, FALSE) AS auto_renew
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN tariffs t ON t.id = s.tariff_id OR (s.tariff_id IS NULL AND t.name = s.server_label)
            WHERE u.telegram_id = $1 AND s.is_active = TRUE AND (s.expires_at IS NULL OR s.expires_at > NOW())
            ORDER BY s.created_at DESC;
            """,
            telegram_id,
        )
        rows = list(rows)
        if threexui_registry and rows:
            still_active = []
            for r in rows:
                client_id = r.get("threexui_client_id")
                backend_key = str(r.get("backend_key") or default_backend_key)
                inbound_id = int(r.get("backend_inbound_id") or 1)
                threexui = get_registry_client(threexui_registry, backend_key, default_backend_key)
                if client_id and not await threexui.client_exists(inbound_id, client_id):
                    await conn.execute("UPDATE subscriptions SET is_active = FALSE WHERE id = $1", r["id"])
                else:
                    still_active.append(r)
            rows = still_active
    return rows


async def create_test_subscription(
    db_user_id: int,
    telegram_id: int,
    threexui: ThreeXUIClient,
    backend_key: str = "default",
    backend_inbound_id: int = 1,
) -> asyncpg.Record:
    """
    Create a short-lived test VPN subscription for the user.

    Uses ThreeXUIClient to provision a VLESS client and stores
    resulting config in the database.
    """
    # total_gb=0 в 3x-ui означает безлимитный трафик (как при создании через панель)
    client_info: ThreeXUIClientInfo = await threexui.create_vless_client(
        telegram_id=telegram_id,
        expire_days=1,
        total_gb=0,
        remark=f"test_{telegram_id}",
        inbound_id=backend_inbound_id,
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
                threexui_sub_id,
                subscription_url,
                subscription_json_url,
                backend_key,
                backend_inbound_id,
                config,
                is_active,
                expires_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE, $10)
            RETURNING *;
            """,
            db_user_id,
            client_info.server_label,
            client_info.client_id,
            client_info.sub_id,
            client_info.subscription_url,
            client_info.subscription_json_url,
            backend_key,
            backend_inbound_id,
            client_info.config_text,
            expires_at,
        )

    return row


async def create_subscription_from_tariff(
    *,
    db_user_id: int,
    telegram_id: int,
    threexui: ThreeXUIClient,
    months: int,
    traffic_gb: int,
    tariff_name: str,
    tariff_id: int | None = None,
    tariff_price_stars: int | None = None,
    device_os: str | None = None,
    backend_key: str = "default",
    backend_inbound_id: int = 1,
) -> asyncpg.Record:
    """Создать обычную VPN-подписку по тарифу."""
    expire_days = _resolve_duration_days(months, tariff_name)
    total_gb = int(traffic_gb or 0)
    device_sequence = await _next_device_sequence(db_user_id, device_os)
    device_label = _build_device_label(device_os, device_sequence)
    client_info: ThreeXUIClientInfo = await threexui.create_vless_client(
        telegram_id=telegram_id,
        expire_days=expire_days,
        total_gb=total_gb,
        remark=_build_device_remark(telegram_id, device_os, device_sequence),
        inbound_id=backend_inbound_id,
    )

    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=expire_days)

    pool = await get_pool()
    async with pool.acquire() as conn:
        row: Optional[asyncpg.Record] = await conn.fetchrow(
            """
            INSERT INTO subscriptions (
                user_id,
                server_label,
                threexui_client_id,
                threexui_sub_id,
                subscription_url,
                subscription_json_url,
                backend_key,
                backend_inbound_id,
                config,
                is_active,
                expires_at,
                device_os,
                tariff_id,
                tariff_price_rub,
                tariff_price_stars,
                tariff_months,
                tariff_traffic_gb
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, TRUE, $10, $11, $12, $13, $13, $14, $15)
            RETURNING *;
            """,
            db_user_id,
            device_label or tariff_name,
            client_info.client_id,
            client_info.sub_id,
            client_info.subscription_url,
            client_info.subscription_json_url,
            backend_key,
            backend_inbound_id,
            client_info.config_text,
            expires_at,
            device_os,
            tariff_id,
            tariff_price_stars,
            months,
            traffic_gb,
        )

    return row


async def list_subscriptions_due_for_auto_renewal() -> list[asyncpg.Record]:
    """
    Подписки с включённым автопродлением, срок которых истекает в течение 24 ч или уже истёк.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(
            """
            SELECT s.id, u.telegram_id
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            WHERE COALESCE(s.auto_renew, FALSE) = TRUE
              AND s.is_active = TRUE
              AND s.expires_at IS NOT NULL
              AND s.expires_at <= NOW() + INTERVAL '24 hours'
              AND s.threexui_client_id IS NOT NULL
            ORDER BY s.expires_at ASC
            """
        )


async def get_subscription_for_user(subscription_id: int, telegram_id: int) -> Optional[asyncpg.Record]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            """
            SELECT
                s.*,
                u.telegram_id,
                t.name AS tariff_name,
                COALESCE(s.tariff_price_stars, s.tariff_price_rub, t.price_stars, t.price_rub) AS effective_tariff_price_stars,
                COALESCE(s.tariff_months, t.months) AS effective_tariff_months,
                COALESCE(s.tariff_traffic_gb, t.traffic_gb) AS effective_tariff_traffic_gb
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN tariffs t ON t.id = s.tariff_id OR (s.tariff_id IS NULL AND t.name = s.server_label)
            WHERE s.id = $1 AND u.telegram_id = $2
            """,
            subscription_id,
            telegram_id,
        )


async def extend_subscription_for_user(
    *,
    subscription_id: int,
    telegram_id: int,
    threexui: ThreeXUIClient,
    months: int | None = None,
    traffic_gb: int | None = None,
    tariff_id: int | None = None,
    tariff_price_stars: int | None = None,
    backend_inbound_id: int | None = None,
) -> Optional[asyncpg.Record]:
    """Продлить существующую подписку по сохраненным параметрам тарифа."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await get_subscription_for_user(subscription_id, telegram_id)
        if not row:
            return None

        months = int(months if months is not None else (row["effective_tariff_months"] or 0))
        traffic_gb = int(traffic_gb if traffic_gb is not None else (row["effective_tariff_traffic_gb"] or 0))
        client_id = row["threexui_client_id"]
        if not client_id:
            return None
        tariff_name_for_duration = row.get("tariff_name") or row.get("server_label")
        expire_days = _resolve_duration_days(months, tariff_name_for_duration)
        inbound_id = int(row.get("backend_inbound_id") or backend_inbound_id or 1)
        updated = await threexui.extend_client(
            inbound_id,
            client_id,
            add_days=expire_days,
            add_total_gb=traffic_gb,
        )
        if not updated:
            return None

        current_expiry = row["expires_at"]
        now = dt.datetime.now(dt.timezone.utc)
        base = current_expiry if current_expiry and current_expiry > now else now
        new_expiry = base + dt.timedelta(days=expire_days)
        return await conn.fetchrow(
            """
            UPDATE subscriptions
            SET expires_at = $2,
                is_active = TRUE,
                tariff_id = COALESCE($3, tariff_id),
                tariff_price_stars = COALESCE($4, tariff_price_stars),
                tariff_months = COALESCE($5, tariff_months),
                tariff_traffic_gb = COALESCE($6, tariff_traffic_gb)
            WHERE id = $1
            RETURNING *
            """,
            subscription_id,
            new_expiry,
            tariff_id,
            tariff_price_stars,
            months,
            traffic_gb,
        )

