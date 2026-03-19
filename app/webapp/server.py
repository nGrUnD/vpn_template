from __future__ import annotations

import asyncio
import datetime as dt
from html import escape
from typing import Any, Dict, List

from aiohttp import ClientSession, web
from aiogram import Bot
from aiogram.types import LabeledPrice

from app.db import get_pool
from app.services.backends import get_registry_client, pick_backend_for_new_subscription
from app.services.subscriptions import (
    create_subscription_from_tariff,
    create_test_subscription,
    extend_subscription_for_user,
    get_subscription_for_user,
    get_active_subscriptions_by_telegram_id,
)
from app.services.tariffs import (
    create_tariff,
    delete_tariff,
    get_tariff_by_id,
    get_tariffs,
    update_tariff,
)
from app.services.users import get_or_create_user_by_telegram_id
from app.services.wallet import (
    cancel_pending_topup,
    create_pending_topup,
    get_wallet_summary,
    refund_purchase,
    spend_balance_for_purchase,
)
from app.threexui_client import ThreeXUIClient


def _admin_ids(app: web.Application) -> List[int]:
    return list(app.get("admin_ids", []))


def _threexui_registry(app: web.Application) -> Dict[str, ThreeXUIClient]:
    registry = app.get("threexui_registry") or {}
    if registry:
        return registry
    default_key = _default_backend_key(app)
    default_client = app.get("threexui")
    return {default_key: default_client} if default_client else {}


def _default_backend_key(app: web.Application) -> str:
    return str(app.get("default_threexui_key") or "default")


def _backend_configs(app: web.Application) -> Dict[str, Any]:
    return dict(app.get("threexui_backends") or {})


def _resolve_threexui_client(app: web.Application, backend_key: str | None = None) -> ThreeXUIClient:
    return get_registry_client(_threexui_registry(app), backend_key, _default_backend_key(app))


async def _create_stars_invoice_link(bot: Bot, *, title: str, description: str, payload: str, amount: int) -> str:
    if hasattr(bot, "create_invoice_link"):
        return await bot.create_invoice_link(
            title=title,
            description=description,
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=f"VPN баланс {amount}", amount=amount)],
        )

    url = f"https://api.telegram.org/bot{bot.token}/createInvoiceLink"
    payload_json = {
        "title": title,
        "description": description,
        "payload": payload,
        "provider_token": "",
        "currency": "XTR",
        "prices": [{"label": f"VPN баланс {amount}", "amount": amount}],
    }
    async with ClientSession() as session:
        async with session.post(url, json=payload_json) as response:
            response.raise_for_status()
            data = await response.json()
    if not data.get("ok") or not data.get("result"):
        raise RuntimeError("Failed to create invoice link")
    return str(data["result"])


async def _require_admin(request: web.Request) -> tuple[int | None, Dict[str, Any]]:
    """Читает JSON body, возвращает (telegram_id, data) если пользователь админ, иначе (None, data)."""
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return None, {}
    tid = data.get("telegram_id")
    if tid is None:
        return None, data
    try:
        telegram_id = int(tid)
    except (TypeError, ValueError):
        return None, data
    if telegram_id not in _admin_ids(request.app):
        return None, data
    return telegram_id, data


async def handle_health(_: web.Request) -> web.Response:
    return web.Response(text="ok", content_type="text/plain")


async def handle_tariffs(_: web.Request) -> web.Response:
    """Список тарифов для карточек в WebApp (только активные)."""
    tariffs = await get_tariffs(active_only=True)
    return web.json_response({"ok": True, "tariffs": tariffs})


async def handle_my_configs(request: web.Request) -> web.Response:
    """
    Список активных подписок пользователя.
    POST JSON: { "telegram_id": 123456789 }
    """
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)
    telegram_id = data.get("telegram_id")
    if not telegram_id:
        return web.json_response({"ok": False, "error": "telegram_id is required"}, status=400)
    telegram_id = int(telegram_id)
    registry = _threexui_registry(request.app)
    rows = await get_active_subscriptions_by_telegram_id(
        telegram_id,
        threexui_registry=registry,
        default_backend_key=_default_backend_key(request.app),
    )
    configs = []

    def format_gb(value: float) -> str:
        rounded = round(float(value), 2)
        if rounded.is_integer():
            return f"{int(rounded)} GB"
        return f"{rounded:.2f}".rstrip("0").rstrip(".") + " GB"

    default_backend_key = _default_backend_key(request.app)
    for r in rows:
        traffic = None
        if registry and r.get("threexui_client_id"):
            try:
                threexui = get_registry_client(registry, r.get("backend_key"), default_backend_key)
                inbound_id = int(r.get("backend_inbound_id") or 1)
                traffic = await threexui.get_client_traffic(inbound_id, r["threexui_client_id"])
            except Exception:
                traffic = None
        traffic_value = "Неизвестно"
        if traffic:
            if traffic.get("is_unlimited"):
                traffic_value = "Безлимит"
            elif traffic.get("remaining_bytes") is not None:
                remaining_gb = float(traffic["remaining_bytes"]) / (1024**3)
                traffic_value = format_gb(remaining_gb)
        elif r.get("tariff_traffic_gb") is not None:
            traffic_value = format_gb(float(r["tariff_traffic_gb"]))

        configs.append(
            {
                "id": r["id"],
                "server_label": r["server_label"],
                "config": r["config"],
                "sub_id": r.get("threexui_sub_id"),
                "subscription_url": r.get("subscription_url"),
                "subscription_json_url": r.get("subscription_json_url"),
                "backend_key": r.get("backend_key"),
                "device_os": r.get("device_os"),
                "can_extend": bool(r.get("tariff_price_stars")) and bool(r.get("tariff_months")),
                "renew_price_stars": r.get("tariff_price_stars"),
                "renew_months": r.get("tariff_months"),
                "traffic_value": traffic_value,
                "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            }
        )
    return web.json_response({"ok": True, "configs": configs})


async def handle_dashboard(request: web.Request) -> web.Response:
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)
    telegram_id = int(data.get("telegram_id", 0) or 0)
    if not telegram_id:
        return web.json_response({"ok": False, "error": "telegram_id is required"}, status=400)
    user = await get_or_create_user_by_telegram_id(
        telegram_id=telegram_id,
        username=data.get("username"),
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
    )
    wallet = await get_wallet_summary(user["id"])
    configs = await get_active_subscriptions_by_telegram_id(
        telegram_id,
        threexui_registry=_threexui_registry(request.app),
        default_backend_key=_default_backend_key(request.app),
    )
    return web.json_response(
        {
            "ok": True,
            "vpn_balance_stars": wallet["balance"],
            "device_count": len(configs),
        }
    )


async def handle_wallet(request: web.Request) -> web.Response:
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)
    telegram_id = int(data.get("telegram_id", 0) or 0)
    if not telegram_id:
        return web.json_response({"ok": False, "error": "telegram_id is required"}, status=400)
    user = await get_or_create_user_by_telegram_id(
        telegram_id=telegram_id,
        username=data.get("username"),
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
    )
    wallet = await get_wallet_summary(user["id"])
    return web.json_response(
        {
            "ok": True,
            "balance_stars": wallet["balance"],
            "transactions": wallet["transactions"],
        }
    )


async def handle_wallet_create_invoice(request: web.Request) -> web.Response:
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)
    telegram_id = int(data.get("telegram_id", 0) or 0)
    amount_stars = int(data.get("amount_stars", 0) or 0)
    if not telegram_id:
        return web.json_response({"ok": False, "error": "telegram_id is required"}, status=400)
    if amount_stars <= 0:
        return web.json_response({"ok": False, "error": "amount_stars must be positive"}, status=400)
    user = await get_or_create_user_by_telegram_id(
        telegram_id=telegram_id,
        username=data.get("username"),
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
    )
    tx = await create_pending_topup(user["id"], amount_stars)
    bot: Bot | None = request.app.get("bot")
    if bot is None:
        return web.json_response({"ok": False, "error": "Bot is not configured"}, status=500)
    invoice_link = await _create_stars_invoice_link(
        bot,
        title=f"Пополнение VPN-баланса на {amount_stars} Stars",
        description=f"Пополнение VPN-кошелька на {amount_stars} Stars",
        payload=tx["payload"],
        amount=amount_stars,
    )
    return web.json_response({"ok": True, "invoice_link": invoice_link, "amount_stars": amount_stars, "payload": tx["payload"]})


async def handle_wallet_cancel_invoice(request: web.Request) -> web.Response:
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)
    payload = (data.get("payload") or "").strip()
    if not payload:
        return web.json_response({"ok": False, "error": "payload is required"}, status=400)
    cancelled = await cancel_pending_topup(payload)
    return web.json_response({"ok": True, "cancelled": cancelled})


async def handle_purchase_tariff(request: web.Request) -> web.Response:
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    telegram_id = int(data.get("telegram_id", 0) or 0)
    tariff_id = int(data.get("tariff_id", 0) or 0)
    device_os = (data.get("device_os") or "").strip() or None
    if not telegram_id or not tariff_id:
        return web.json_response({"ok": False, "error": "telegram_id and tariff_id are required"}, status=400)

    user = await get_or_create_user_by_telegram_id(
        telegram_id=telegram_id,
        username=data.get("username"),
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
    )
    tariff = await get_tariff_by_id(tariff_id)
    if not tariff or not tariff["is_active"]:
        return web.json_response({"ok": False, "error": "Тариф не найден"}, status=404)

    ok, balance = await spend_balance_for_purchase(
        user["id"],
        int(tariff["price_stars"]),
        f"Покупка тарифа «{tariff['name']}»",
    )
    if not ok:
        return web.json_response({"ok": False, "error": "Недостаточно средств", "balance": balance}, status=400)

    try:
        backend_config = await pick_backend_for_new_subscription(
            registry=_threexui_registry(request.app),
            backend_configs=_backend_configs(request.app),
            default_key=_default_backend_key(request.app),
        )
        threexui = _resolve_threexui_client(request.app, backend_config.key)
        subscription = await create_subscription_from_tariff(
            db_user_id=user["id"],
            telegram_id=telegram_id,
            threexui=threexui,
            months=int(tariff["months"]),
            traffic_gb=int(tariff["traffic_gb"]),
            tariff_name=str(tariff["name"]),
            tariff_id=int(tariff["id"]),
            tariff_price_stars=int(tariff["price_stars"]),
            device_os=device_os,
            backend_key=backend_config.key,
            backend_inbound_id=backend_config.inbound_id,
        )
    except Exception:
        balance = await refund_purchase(user["id"], int(tariff["price_stars"]), f"Возврат за тариф «{tariff['name']}»")
        return web.json_response({"ok": False, "error": "Не удалось создать VPN-конфиг", "balance": balance}, status=500)

    return web.json_response(
        {
            "ok": True,
            "balance": balance,
            "subscription": {
                "id": subscription["id"],
                "config": subscription["config"],
                "sub_id": subscription.get("threexui_sub_id"),
                "subscription_url": subscription.get("subscription_url"),
                "subscription_json_url": subscription.get("subscription_json_url"),
                "backend_key": subscription.get("backend_key"),
                "server_label": subscription["server_label"],
                "expires_at": subscription["expires_at"].isoformat() if subscription["expires_at"] else None,
            },
        }
    )


async def handle_extend_subscription(request: web.Request) -> web.Response:
    try:
        data: Dict[str, Any] = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    telegram_id = int(data.get("telegram_id", 0) or 0)
    subscription_id = int(data.get("subscription_id", 0) or 0)
    tariff_id = int(data.get("tariff_id", 0) or 0)
    if not telegram_id or not subscription_id:
        return web.json_response({"ok": False, "error": "Invalid renew request"}, status=400)

    user = await get_or_create_user_by_telegram_id(
        telegram_id=telegram_id,
        username=data.get("username"),
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
    )
    subscription_row = await get_subscription_for_user(subscription_id, telegram_id)
    if not subscription_row:
        return web.json_response({"ok": False, "error": "Подписка не найдена"}, status=404)
    renew_price_stars = int((subscription_row["effective_tariff_price_stars"] or 0))
    renew_months = int(subscription_row["effective_tariff_months"] or 0)
    renew_traffic_gb = int(subscription_row["effective_tariff_traffic_gb"] or 0)
    effective_tariff_id = subscription_row.get("tariff_id")
    if tariff_id:
        tariff = await get_tariff_by_id(tariff_id)
        if not tariff or not tariff["is_active"]:
            return web.json_response({"ok": False, "error": "Тариф не найден"}, status=404)
        renew_price_stars = int(tariff["price_stars"])
        renew_months = int(tariff["months"])
        renew_traffic_gb = int(tariff["traffic_gb"])
        effective_tariff_id = int(tariff["id"])
    if renew_price_stars <= 0 or renew_months <= 0:
        return web.json_response({"ok": False, "error": "Выберите тариф для продления"}, status=400)
    ok, balance = await spend_balance_for_purchase(
        user["id"],
        renew_price_stars,
        f"Продление подписки #{subscription_id}",
    )
    if not ok:
        return web.json_response({"ok": False, "error": "Недостаточно средств", "balance": balance}, status=400)

    try:
        backend_key = str(subscription_row.get("backend_key") or _default_backend_key(request.app))
        backend_inbound_id = int(subscription_row.get("backend_inbound_id") or 1)
        subscription = await extend_subscription_for_user(
            subscription_id=subscription_id,
            telegram_id=telegram_id,
            threexui=_resolve_threexui_client(request.app, backend_key),
            months=renew_months,
            traffic_gb=renew_traffic_gb,
            tariff_id=effective_tariff_id,
            tariff_price_stars=renew_price_stars,
            backend_inbound_id=backend_inbound_id,
        )
    except Exception:
        subscription = None
    if not subscription:
        balance = await refund_purchase(user["id"], renew_price_stars, f"Возврат за продление подписки #{subscription_id}")
        return web.json_response({"ok": False, "error": "Не удалось продлить подписку", "balance": balance}, status=500)

    return web.json_response(
        {
            "ok": True,
            "balance": balance,
            "expires_at": subscription["expires_at"].isoformat() if subscription["expires_at"] else None,
        }
    )


async def handle_create_test_client(request: web.Request) -> web.Response:
    """
    WebApp: создать тестовый VPN-клиент в 3x-ui и сохранить подписку в БД.
    JSON: { "telegram_id": 123456789, "username": "...", "first_name": "...", "last_name": "..." }
    """
    data: Dict[str, Any] = await request.json()

    telegram_id = int(data.get("telegram_id", 0))
    if not telegram_id:
        return web.json_response({"ok": False, "error": "telegram_id is required"}, status=400)

    user = await get_or_create_user_by_telegram_id(
        telegram_id=telegram_id,
        username=data.get("username"),
        first_name=data.get("first_name"),
        last_name=data.get("last_name"),
    )
    backend_config = await pick_backend_for_new_subscription(
        registry=_threexui_registry(request.app),
        backend_configs=_backend_configs(request.app),
        default_key=_default_backend_key(request.app),
    )
    subscription = await create_test_subscription(
        db_user_id=user["id"],
        telegram_id=telegram_id,
        threexui=_resolve_threexui_client(request.app, backend_config.key),
        backend_key=backend_config.key,
        backend_inbound_id=backend_config.inbound_id,
    )

    return web.json_response(
        {
            "ok": True,
            "client_id": subscription["threexui_client_id"],
            "sub_id": subscription.get("threexui_sub_id"),
            "subscription_url": subscription.get("subscription_url"),
            "subscription_json_url": subscription.get("subscription_json_url"),
            "backend_key": subscription.get("backend_key"),
            "remark": subscription["server_label"],
            "message": subscription["config"],
        }
    )


# --- Admin API ---


async def handle_admin_me(request: web.Request) -> web.Response:
    """POST { telegram_id } -> { is_admin: bool }."""
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": True, "is_admin": False})
    tid = data.get("telegram_id")
    try:
        telegram_id = int(tid) if tid is not None else 0
    except (TypeError, ValueError):
        telegram_id = 0
    is_admin = telegram_id in _admin_ids(request.app)
    return web.json_response({"ok": True, "is_admin": is_admin})


async def handle_admin_tariffs_list(request: web.Request) -> web.Response:
    """POST (тело любое) -> список всех тарифов. Без проверки админа — страница админки грузится сразу; создание/удаление проверяются отдельно."""
    tariffs = await get_tariffs(active_only=False)
    return web.json_response({"ok": True, "tariffs": tariffs})


async def handle_admin_tariffs_create(request: web.Request) -> web.Response:
    """POST { telegram_id, name, months, price_stars, traffic_gb?, badge?, sort_order? }."""
    admin_id, data = await _require_admin(request)
    if admin_id is None:
        return web.json_response({"ok": False, "error": "Forbidden"}, status=403)
    name = data.get("name")
    months = data.get("months")
    price_stars = data.get("price_stars")
    if not name or months is None or price_stars is None:
        return web.json_response({"ok": False, "error": "name, months, price_stars required"}, status=400)
    try:
        months = int(months)
        price_stars = int(price_stars)
        traffic_gb = int(data.get("traffic_gb", 0))
        sort_order = int(data.get("sort_order", 0))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid numbers"}, status=400)
    badge = data.get("badge") or None
    row = await create_tariff(name=name, months=months, price_stars=price_stars, traffic_gb=traffic_gb, badge=badge, sort_order=sort_order)
    return web.json_response({"ok": True, "tariff": {"id": row["id"], "name": row["name"], "months": row["months"], "price_stars": row["price_stars"], "traffic_gb": row["traffic_gb"], "badge": row["badge"], "sort_order": row["sort_order"], "is_active": row["is_active"]}})


async def handle_admin_tariffs_update(request: web.Request) -> web.Response:
    """PATCH /api/admin/tariffs/{id} с телом { telegram_id, name?, months?, price_stars?, traffic_gb?, badge?, sort_order?, is_active? }."""
    admin_id, data = await _require_admin(request)
    if admin_id is None:
        return web.json_response({"ok": False, "error": "Forbidden"}, status=403)
    try:
        tariff_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid id"}, status=400)
    kwargs = {}
    if "name" in data:
        kwargs["name"] = data["name"]
    if "months" in data:
        kwargs["months"] = int(data["months"])
    if "price_stars" in data:
        kwargs["price_stars"] = int(data["price_stars"])
    if "traffic_gb" in data:
        kwargs["traffic_gb"] = int(data["traffic_gb"])
    if "badge" in data:
        kwargs["badge"] = data["badge"] or None
    if "sort_order" in data:
        kwargs["sort_order"] = int(data["sort_order"])
    if "is_active" in data:
        kwargs["is_active"] = bool(data["is_active"])
    row = await update_tariff(tariff_id, **kwargs)
    if not row:
        return web.json_response({"ok": False, "error": "Not found"}, status=404)
    return web.json_response({"ok": True, "tariff": {"id": row["id"], "name": row["name"], "months": row["months"], "price_stars": row["price_stars"], "traffic_gb": row["traffic_gb"], "badge": row["badge"], "sort_order": row["sort_order"], "is_active": row["is_active"]}})


async def handle_admin_tariffs_delete(request: web.Request) -> web.Response:
    """DELETE /api/admin/tariffs/{id} с телом { telegram_id }."""
    admin_id, _ = await _require_admin(request)
    if admin_id is None:
        return web.json_response({"ok": False, "error": "Forbidden"}, status=403)
    try:
        tariff_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid id"}, status=400)
    deleted = await delete_tariff(tariff_id)
    return web.json_response({"ok": True, "deleted": deleted})


def _admin_person_label(row: Dict[str, Any]) -> str:
    full_name = " ".join(part for part in [row.get("first_name"), row.get("last_name")] if part)
    username = row.get("username")
    telegram_id = row.get("telegram_id")
    if username:
        return f"@{escape(str(username))}"
    if full_name:
        return escape(full_name)
    return f"ID {telegram_id}"


async def _admin_fetch_overview_metrics() -> Dict[str, int]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS users_count,
                (SELECT COUNT(*) FROM subscriptions WHERE is_active = TRUE AND (expires_at IS NULL OR expires_at > NOW())) AS active_subscriptions,
                (SELECT COUNT(*) FROM subscriptions WHERE is_active = TRUE AND expires_at IS NOT NULL AND expires_at > NOW() AND expires_at <= NOW() + INTERVAL '7 days') AS expiring_soon,
                (SELECT COUNT(*) FROM wallet_transactions WHERE status = 'paid') AS paid_transactions,
                (SELECT COUNT(*) FROM wallet_transactions WHERE kind = 'topup' AND status = 'pending') AS pending_topups,
                (SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions WHERE kind = 'topup' AND status = 'paid' AND amount > 0) AS topup_volume,
                (SELECT COALESCE(SUM(vpn_balance_stars), 0) FROM users) AS total_balance
            """
        )
    return {
        "users_count": int(row["users_count"] or 0),
        "active_subscriptions": int(row["active_subscriptions"] or 0),
        "expiring_soon": int(row["expiring_soon"] or 0),
        "paid_transactions": int(row["paid_transactions"] or 0),
        "pending_topups": int(row["pending_topups"] or 0),
        "topup_volume": int(row["topup_volume"] or 0),
        "total_balance": int(row["total_balance"] or 0),
    }


async def _admin_fetch_payments_data(limit: int = 50, q: str = "", status: str = "", kind: str = "") -> Dict[str, Any]:
    pool = await get_pool()
    filters: List[str] = []
    params: List[Any] = []
    if q:
        params.append(f"%{q}%")
        idx = len(params)
        filters.append(
            f"(u.username ILIKE ${idx} OR u.first_name ILIKE ${idx} OR u.last_name ILIKE ${idx} "
            f"OR wt.description ILIKE ${idx} OR CAST(u.telegram_id AS TEXT) ILIKE ${idx})"
        )
    if status:
        params.append(status)
        filters.append(f"wt.status = ${len(params)}")
    if kind:
        params.append(kind)
        filters.append(f"wt.kind = ${len(params)}")
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) FILTER (WHERE kind = 'topup' AND status = 'paid') AS paid_topups,
                COUNT(*) FILTER (WHERE kind = 'topup' AND status = 'pending') AS pending_topups,
                COUNT(*) FILTER (WHERE kind = 'purchase' AND status = 'paid') AS purchases_count,
                COUNT(*) FILTER (WHERE kind = 'refund' AND status = 'paid') AS refunds_count,
                COALESCE(SUM(amount) FILTER (WHERE kind = 'topup' AND status = 'paid' AND amount > 0), 0) AS topup_volume,
                COALESCE(SUM(ABS(amount)) FILTER (WHERE kind = 'purchase' AND status = 'paid'), 0) AS purchase_volume
            FROM wallet_transactions
            """
        )
        rows = await conn.fetch(
            f"""
            SELECT
                wt.id,
                wt.kind,
                wt.status,
                wt.amount,
                wt.currency,
                wt.provider_amount,
                wt.provider_currency,
                wt.description,
                wt.payload,
                wt.created_at,
                wt.paid_at,
                u.telegram_id,
                u.username,
                u.first_name,
                u.last_name
            FROM wallet_transactions wt
            JOIN users u ON u.id = wt.user_id
            {where_sql}
            ORDER BY wt.created_at DESC
            LIMIT ${len(params) + 1}
            """,
            *params,
            limit,
        )
    return {
        "stats": {
            "paid_topups": int(stats["paid_topups"] or 0),
            "pending_topups": int(stats["pending_topups"] or 0),
            "purchases_count": int(stats["purchases_count"] or 0),
            "refunds_count": int(stats["refunds_count"] or 0),
            "topup_volume": int(stats["topup_volume"] or 0),
            "purchase_volume": int(stats["purchase_volume"] or 0),
        },
        "rows": list(rows),
    }


async def _admin_fetch_devices_data(
    limit: int = 50,
    q: str = "",
    status: str = "",
    os_name: str = "",
    backend_key: str = "",
    threexui_registry: Dict[str, ThreeXUIClient] | None = None,
    default_backend_key: str = "default",
) -> Dict[str, Any]:
    pool = await get_pool()
    filters: List[str] = []
    params: List[Any] = []
    if q:
        params.append(f"%{q}%")
        idx = len(params)
        filters.append(
            f"(u.username ILIKE ${idx} OR u.first_name ILIKE ${idx} OR u.last_name ILIKE ${idx} "
            f"OR s.server_label ILIKE ${idx} OR s.device_os ILIKE ${idx} OR CAST(u.telegram_id AS TEXT) ILIKE ${idx})"
        )
    if os_name:
        params.append(os_name)
        filters.append(f"s.device_os = ${len(params)}")
    if backend_key:
        params.append(backend_key)
        filters.append(f"COALESCE(s.backend_key, 'default') = ${len(params)}")
    if status == "active":
        filters.append("s.is_active = TRUE AND (s.expires_at IS NULL OR s.expires_at > NOW())")
    elif status == "inactive":
        filters.append("s.is_active = FALSE")
    elif status == "expired":
        filters.append("s.expires_at IS NOT NULL AND s.expires_at <= NOW()")
    elif status == "expiring":
        filters.append("s.is_active = TRUE AND s.expires_at IS NOT NULL AND s.expires_at > NOW() AND s.expires_at <= NOW() + INTERVAL '7 days'")
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_devices,
                COUNT(*) FILTER (WHERE is_active = TRUE AND (expires_at IS NULL OR expires_at > NOW())) AS active_devices,
                COUNT(*) FILTER (WHERE is_active = FALSE) AS disabled_devices,
                COUNT(*) FILTER (WHERE expires_at IS NOT NULL AND expires_at <= NOW()) AS expired_devices,
                COUNT(*) FILTER (WHERE is_active = TRUE AND expires_at IS NOT NULL AND expires_at > NOW() AND expires_at <= NOW() + INTERVAL '7 days') AS expiring_soon,
                COUNT(DISTINCT user_id) AS users_with_devices
            FROM subscriptions
            """
        )
        rows = await conn.fetch(
            f"""
            SELECT
                s.id,
                s.server_label,
                s.threexui_client_id,
                s.backend_key,
                s.backend_inbound_id,
                s.device_os,
                s.is_active,
                s.expires_at,
                s.created_at,
                COALESCE(s.tariff_price_stars, s.tariff_price_rub, t.price_stars, t.price_rub) AS price_stars,
                COALESCE(s.tariff_months, t.months) AS months,
                COALESCE(s.tariff_traffic_gb, t.traffic_gb) AS traffic_gb,
                u.telegram_id,
                u.username,
                u.first_name,
                u.last_name
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN tariffs t ON t.id = s.tariff_id OR (s.tariff_id IS NULL AND t.name = s.server_label)
            {where_sql}
            ORDER BY s.created_at DESC
            LIMIT ${len(params) + 1}
            """,
            *params,
            limit,
        )
        backend_rows = await conn.fetch(
            """
            SELECT
                COALESCE(backend_key, 'default') AS backend_key,
                COUNT(*) AS total
            FROM subscriptions
            GROUP BY COALESCE(backend_key, 'default')
            ORDER BY total DESC, backend_key ASC
            """
        )
    row_dicts = [dict(row) for row in rows]

    async def enrich_row(row: Dict[str, Any]) -> None:
        row["ip_limit"] = 3
        row["share_available"] = False
        row["share_ip_count"] = 0
        row["share_ips"] = []
        row["share_warning"] = False
        if not threexui_registry or not row.get("threexui_client_id"):
            return
        try:
            threexui = get_registry_client(threexui_registry, row.get("backend_key"), default_backend_key)
            inbound_id = int(row.get("backend_inbound_id") or 1)
            ip_info = await threexui.get_client_ips(inbound_id, str(row["threexui_client_id"]))
        except Exception:
            return
        row["share_available"] = bool(ip_info.get("available"))
        row["share_ip_count"] = int(ip_info.get("ip_count") or 0)
        row["share_ips"] = list(ip_info.get("ips") or [])[:5]
        row["share_warning"] = row["share_ip_count"] > int(row["ip_limit"])

    if row_dicts and threexui_registry:
        await asyncio.gather(*(enrich_row(row) for row in row_dicts))

    suspicious_devices = sum(1 for row in row_dicts if row.get("share_warning"))
    share_data_ready = any(row.get("share_available") for row in row_dicts)

    return {
        "stats": {
            "total_devices": int(stats["total_devices"] or 0),
            "active_devices": int(stats["active_devices"] or 0),
            "disabled_devices": int(stats["disabled_devices"] or 0),
            "expired_devices": int(stats["expired_devices"] or 0),
            "expiring_soon": int(stats["expiring_soon"] or 0),
            "users_with_devices": int(stats["users_with_devices"] or 0),
            "suspicious_devices": suspicious_devices,
            "share_data_ready": share_data_ready,
        },
        "rows": row_dicts,
        "backend_breakdown": [
            {"backend_key": str(row["backend_key"]), "total": int(row["total"] or 0)}
            for row in backend_rows
        ],
    }


async def _admin_get_subscription_row(subscription_id: int) -> Dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                s.id,
                s.user_id,
                s.server_label,
                s.threexui_client_id,
                s.backend_key,
                s.backend_inbound_id,
                s.is_active,
                s.expires_at,
                u.telegram_id,
                COALESCE(s.tariff_months, t.months, 1) AS tariff_months,
                COALESCE(s.tariff_traffic_gb, t.traffic_gb, 0) AS tariff_traffic_gb
            FROM subscriptions s
            JOIN users u ON u.id = s.user_id
            LEFT JOIN tariffs t ON t.id = s.tariff_id OR (s.tariff_id IS NULL AND t.name = s.server_label)
            WHERE s.id = $1
            """,
            subscription_id,
        )
    return dict(row) if row else None


async def _admin_extend_subscription_manual(
    *,
    subscription_id: int,
    days: int,
    threexui_registry: Dict[str, ThreeXUIClient],
    default_backend_key: str,
) -> bool:
    row = await _admin_get_subscription_row(subscription_id)
    if not row or not row.get("threexui_client_id") or days <= 0:
        return False
    tariff_months = max(int(row.get("tariff_months") or 1), 1)
    tariff_traffic = int(row.get("tariff_traffic_gb") or 0)
    period_days = max(tariff_months * 30, 1)
    add_traffic_gb = max(1, round(tariff_traffic * days / period_days)) if tariff_traffic > 0 else 1
    threexui = get_registry_client(threexui_registry, row.get("backend_key"), default_backend_key)
    updated = await threexui.extend_client(
        int(row.get("backend_inbound_id") or 1),
        str(row["threexui_client_id"]),
        add_days=days,
        add_total_gb=add_traffic_gb,
    )
    if not updated:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        current_expiry = row.get("expires_at")
        now = dt.datetime.now(dt.timezone.utc)
        base = current_expiry if current_expiry and current_expiry > now else now
        new_expiry = base + dt.timedelta(days=days)
        await conn.execute(
            """
            UPDATE subscriptions
            SET expires_at = $2,
                is_active = TRUE
            WHERE id = $1
            """,
            subscription_id,
            new_expiry,
        )
    return True


async def _admin_deactivate_subscription(subscription_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            UPDATE subscriptions
            SET is_active = FALSE,
                expires_at = NOW()
            WHERE id = $1
            """,
            subscription_id,
        )
    return result == "UPDATE 1"


async def _admin_fetch_users_data(limit: int = 50, q: str = "", segment: str = "") -> Dict[str, Any]:
    pool = await get_pool()
    filters: List[str] = []
    params: List[Any] = []
    if q:
        params.append(f"%{q}%")
        idx = len(params)
        filters.append(
            f"(u.username ILIKE ${idx} OR u.first_name ILIKE ${idx} OR u.last_name ILIKE ${idx} "
            f"OR CAST(u.telegram_id AS TEXT) ILIKE ${idx})"
        )
    if segment == "with_balance":
        filters.append("u.vpn_balance_stars > 0")
    elif segment == "with_devices":
        filters.append("COALESCE(dev.active_devices, 0) > 0")
    elif segment == "with_payments":
        filters.append("COALESCE(tx.total_transactions, 0) > 0")
    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
    async with pool.acquire() as conn:
        stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_users,
                COUNT(*) FILTER (WHERE vpn_balance_stars > 0) AS users_with_balance,
                COALESCE(SUM(vpn_balance_stars), 0) AS total_balance
            FROM users
            """
        )
        rows = await conn.fetch(
            f"""
            SELECT
                u.id,
                u.telegram_id,
                u.username,
                u.first_name,
                u.last_name,
                u.vpn_balance_stars,
                u.created_at,
                COALESCE(dev.active_devices, 0) AS active_devices,
                COALESCE(dev.total_devices, 0) AS total_devices,
                COALESCE(tx.total_transactions, 0) AS total_transactions,
                tx.last_transaction_at
            FROM users u
            LEFT JOIN (
                SELECT
                    user_id,
                    COUNT(*) AS total_devices,
                    COUNT(*) FILTER (WHERE is_active = TRUE AND (expires_at IS NULL OR expires_at > NOW())) AS active_devices
                FROM subscriptions
                GROUP BY user_id
            ) dev ON dev.user_id = u.id
            LEFT JOIN (
                SELECT
                    user_id,
                    COUNT(*) AS total_transactions,
                    MAX(created_at) AS last_transaction_at
                FROM wallet_transactions
                GROUP BY user_id
            ) tx ON tx.user_id = u.id
            {where_sql}
            ORDER BY u.created_at DESC
            LIMIT ${len(params) + 1}
            """,
            *params,
            limit,
        )
    return {
        "stats": {
            "total_users": int(stats["total_users"] or 0),
            "users_with_balance": int(stats["users_with_balance"] or 0),
            "total_balance": int(stats["total_balance"] or 0),
        },
        "rows": list(rows),
    }


async def _admin_fetch_user_profile(user_id: int) -> Dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            """
            SELECT
                u.id,
                u.telegram_id,
                u.username,
                u.first_name,
                u.last_name,
                u.vpn_balance_stars,
                u.created_at
            FROM users u
            WHERE u.id = $1
            """,
            user_id,
        )
        if not user:
            return None
        device_stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_devices,
                COUNT(*) FILTER (WHERE is_active = TRUE AND (expires_at IS NULL OR expires_at > NOW())) AS active_devices
            FROM subscriptions
            WHERE user_id = $1
            """,
            user_id,
        )
        tx_stats = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_transactions,
                COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) AS total_income,
                COALESCE(SUM(ABS(amount)) FILTER (WHERE amount < 0), 0) AS total_outcome
            FROM wallet_transactions
            WHERE user_id = $1
            """,
            user_id,
        )
        devices = await conn.fetch(
            """
            SELECT
                id,
                server_label,
                device_os,
                is_active,
                expires_at,
                COALESCE(tariff_price_stars, tariff_price_rub, 0) AS price_stars,
                COALESCE(tariff_traffic_gb, 0) AS traffic_gb,
                created_at
            FROM subscriptions
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 20
            """,
            user_id,
        )
        transactions = await conn.fetch(
            """
            SELECT
                id,
                kind,
                status,
                amount,
                description,
                provider_amount,
                provider_currency,
                created_at
            FROM wallet_transactions
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT 20
            """,
            user_id,
        )
    return {
        "user": dict(user),
        "device_stats": {
            "total_devices": int(device_stats["total_devices"] or 0),
            "active_devices": int(device_stats["active_devices"] or 0),
        },
        "tx_stats": {
            "total_transactions": int(tx_stats["total_transactions"] or 0),
            "total_income": int(tx_stats["total_income"] or 0),
            "total_outcome": int(tx_stats["total_outcome"] or 0),
        },
        "devices": list(devices),
        "transactions": list(transactions),
    }


async def _admin_adjust_user_balance(*, user_id: int, delta: int, description: str) -> bool:
    if delta == 0:
        return False
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if delta > 0:
                row = await conn.fetchrow(
                    """
                    UPDATE users
                    SET vpn_balance_stars = vpn_balance_stars + $2
                    WHERE id = $1
                    RETURNING vpn_balance_stars
                    """,
                    user_id,
                    delta,
                )
            else:
                row = await conn.fetchrow(
                    """
                    UPDATE users
                    SET vpn_balance_stars = vpn_balance_stars + $2
                    WHERE id = $1 AND vpn_balance_stars >= $3
                    RETURNING vpn_balance_stars
                    """,
                    user_id,
                    delta,
                    abs(delta),
                )
            if not row:
                return False
            await conn.execute(
                """
                INSERT INTO wallet_transactions (
                    user_id, kind, status, amount, currency, description
                ) VALUES ($1, 'admin_adjustment', 'paid', $2, 'XTR', $3)
                """,
                user_id,
                delta,
                description,
            )
    return True


async def _admin_fetch_analytics_data(days: int = 14) -> Dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        core = await conn.fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM users) AS total_users,
                (SELECT COUNT(*) FROM users WHERE created_at >= NOW() - make_interval(days => $1)) AS new_users_period,
                (SELECT COUNT(*) FROM subscriptions) AS total_devices,
                (SELECT COUNT(*) FROM subscriptions WHERE created_at >= NOW() - make_interval(days => $1)) AS new_devices_period,
                (SELECT COALESCE(SUM(amount), 0) FROM wallet_transactions WHERE kind = 'topup' AND status = 'paid' AND amount > 0) AS total_topups,
                (SELECT COALESCE(SUM(ABS(amount)), 0) FROM wallet_transactions WHERE kind = 'purchase' AND status = 'paid') AS total_purchases,
                (SELECT COUNT(DISTINCT user_id) FROM wallet_transactions WHERE kind = 'topup' AND status = 'paid') AS paying_users,
                (SELECT COUNT(DISTINCT user_id) FROM wallet_transactions WHERE kind = 'purchase' AND status = 'paid') AS buying_users,
                (SELECT COUNT(DISTINCT user_id) FROM subscriptions WHERE is_active = TRUE AND (expires_at IS NULL OR expires_at > NOW())) AS active_users
            """,
            days,
        )
        tariff_rows = await conn.fetch(
            """
            SELECT
                COALESCE(t.name, s.server_label, 'Без тарифа') AS label,
                COUNT(*) AS cnt
            FROM subscriptions s
            LEFT JOIN tariffs t ON t.id = s.tariff_id
            GROUP BY COALESCE(t.name, s.server_label, 'Без тарифа')
            ORDER BY cnt DESC, label ASC
            LIMIT 5
            """
        )
        platform_rows = await conn.fetch(
            """
            SELECT
                COALESCE(device_os, 'Не указано') AS label,
                COUNT(*) AS cnt
            FROM subscriptions
            GROUP BY COALESCE(device_os, 'Не указано')
            ORDER BY cnt DESC, label ASC
            LIMIT 5
            """
        )
        users_daily_rows = await conn.fetch(
            """
            SELECT DATE(created_at) AS day, COUNT(*) AS cnt
            FROM users
            WHERE created_at >= CURRENT_DATE - ($1::int - 1)
            GROUP BY DATE(created_at)
            ORDER BY day ASC
            """,
            days,
        )
        revenue_daily_rows = await conn.fetch(
            """
            SELECT DATE(created_at) AS day, COALESCE(SUM(amount), 0) AS amount
            FROM wallet_transactions
            WHERE kind = 'topup' AND status = 'paid' AND amount > 0
              AND created_at >= CURRENT_DATE - ($1::int - 1)
            GROUP BY DATE(created_at)
            ORDER BY day ASC
            """,
            days,
        )

    today = dt.date.today()
    users_by_day = {row["day"]: int(row["cnt"] or 0) for row in users_daily_rows}
    revenue_by_day = {row["day"]: int(row["amount"] or 0) for row in revenue_daily_rows}
    trend = []
    for offset in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=offset)
        trend.append(
            {
                "label": day.strftime("%d.%m"),
                "users": users_by_day.get(day, 0),
                "topups": revenue_by_day.get(day, 0),
            }
        )

    return {
        "core": {
            "total_users": int(core["total_users"] or 0),
            "new_users_period": int(core["new_users_period"] or 0),
            "total_devices": int(core["total_devices"] or 0),
            "new_devices_period": int(core["new_devices_period"] or 0),
            "total_topups": int(core["total_topups"] or 0),
            "total_purchases": int(core["total_purchases"] or 0),
            "paying_users": int(core["paying_users"] or 0),
            "buying_users": int(core["buying_users"] or 0),
            "active_users": int(core["active_users"] or 0),
        },
        "top_tariffs": [{"label": str(row["label"]), "value": int(row["cnt"] or 0)} for row in tariff_rows],
        "top_platforms": [{"label": str(row["label"]), "value": int(row["cnt"] or 0)} for row in platform_rows],
        "trend": trend,
        "days": days,
    }


def _admin_device_row_html(row: Dict[str, Any], *, redirect_query: str = "") -> str:
    now = dt.datetime.now(dt.timezone.utc)
    expires_at = row.get("expires_at")
    is_live = bool(row.get("is_active")) and (expires_at is None or expires_at > now)
    status_text = "Активно" if is_live else "Неактивно"
    status_class = "active" if is_live else "inactive"
    expires_text = expires_at.strftime("%d.%m.%Y") if expires_at else "—"
    redirect_input = f'<input type="hidden" name="redirect" value="{escape(redirect_query)}" />' if redirect_query else ""
    share_ips = ", ".join(str(ip) for ip in (row.get("share_ips") or [])[:3])
    if row.get("share_available"):
        share_count = int(row.get("share_ip_count") or 0)
        ip_limit = int(row.get("ip_limit") or 3)
        if row.get("share_warning"):
            share_html = f'<span class="badge promo">Шаринг? {share_count} IP</span>'
        elif share_count > 0:
            share_html = f'<span class="badge active">{share_count} IP / лимит {ip_limit}</span>'
        else:
            share_html = '<span class="badge inactive">IP не замечены</span>'
        if share_ips:
            share_html += f'<br><span style="color:#94a3b8;font-size:12px;">{escape(share_ips)}</span>'
    else:
        share_html = '<span class="badge inactive">IP API недоступно</span>'
    return (
        "<tr>"
        f"<td>{row['id']}</td>"
        f"<td>{_admin_person_label(row)}<br><span style=\"color:#94a3b8;font-size:12px;\">TG {row['telegram_id']}</span></td>"
        f"<td>{escape(str(row.get('backend_key') or 'default'))}</td>"
        f"<td>{escape(str(row.get('device_os') or '—'))}</td>"
        f"<td>{escape(str(row.get('server_label') or '—'))}</td>"
        f"<td>{int(row.get('price_stars') or 0)} ⭐</td>"
        f"<td>{int(row.get('traffic_gb') or 0)} GB</td>"
        f"<td>{expires_text}</td>"
        f"<td><span class=\"badge {status_class}\">{status_text}</span></td>"
        f"<td>{share_html}</td>"
        "<td><div class=\"cell-actions\">"
        f"<a class=\"btn\" href=\"/admin/devices?q={row['telegram_id']}\">Все устройства</a>"
        f"<form method=\"post\" action=\"/admin/devices/{row['id']}/extend\" class=\"mini-form\">{redirect_input}<input type=\"number\" name=\"days\" min=\"1\" value=\"30\" /><button type=\"submit\" class=\"primary\">Продлить</button></form>"
        f"<form method=\"post\" action=\"/admin/devices/{row['id']}/deactivate\" class=\"mini-form\" onsubmit=\"return confirm('Деактивировать устройство?');\">{redirect_input}<button type=\"submit\" class=\"danger\">Деактивировать</button></form>"
        "</div></td>"
        "</tr>"
    )


def _admin_notice_html(status: str | None = None, error: str | None = None) -> str:
    if status == "created":
        return '<div class="notice success">Тариф добавлен</div>'
    if status == "deleted":
        return '<div class="notice success">Тариф удален</div>'
    if status == "device_extended":
        return '<div class="notice success">Подписка продлена вручную</div>'
    if status == "device_deactivated":
        return '<div class="notice success">Устройство деактивировано</div>'
    if status == "user_balance_updated":
        return '<div class="notice success">Баланс пользователя обновлен</div>'
    if error == "required":
        return '<div class="notice error">Заполните название, месяцы и цену</div>'
    if error == "invalid":
        return '<div class="notice error">Поля месяцев, цены, трафика и порядка должны быть числами</div>'
    if error == "not_found":
        return '<div class="notice error">Тариф не найден</div>'
    if error == "device_not_found":
        return '<div class="notice error">Подписка или устройство не найдены</div>'
    if error == "device_invalid":
        return '<div class="notice error">Некорректные параметры действия</div>'
    if error == "user_not_found":
        return '<div class="notice error">Пользователь не найден</div>'
    if error == "balance_invalid":
        return '<div class="notice error">Некорректная сумма или операция с балансом</div>'
    if error == "server":
        return '<div class="notice error">Внутренняя ошибка сервера</div>'
    return ""


def _admin_layout(*, title: str, subtitle: str, active_tab: str, content_html: str, notice_html: str = "") -> str:
    nav_overview_class = "nav-link active" if active_tab == "overview" else "nav-link"
    nav_tariffs_class = "nav-link active" if active_tab == "tariffs" else "nav-link"
    nav_payments_class = "nav-link active" if active_tab == "payments" else "nav-link"
    nav_devices_class = "nav-link active" if active_tab == "devices" else "nav-link"
    nav_users_class = "nav-link active" if active_tab == "users" else "nav-link"
    nav_analytics_class = "nav-link active" if active_tab == "analytics" else "nav-link"
    html = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <title>Админ — Raccaster VPN</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { box-sizing: border-box; }
    :root {
      --bg: #0b1120;
      --surface: #111827;
      --surface-raised: #172033;
      --surface-soft: #1f2937;
      --border: #334155;
      --text: #e5e7eb;
      --muted: #94a3b8;
      --accent: #8b5cf6;
      --accent-soft: rgba(139, 92, 246, 0.16);
      --accent-2: #38bdf8;
      --success: #10b981;
      --danger: #ef4444;
      --warning: #f59e0b;
      --shadow: 0 16px 40px rgba(2, 6, 23, 0.28);
    }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top, rgba(56, 189, 248, 0.08), transparent 30%),
        radial-gradient(circle at right top, rgba(139, 92, 246, 0.12), transparent 26%),
        var(--bg);
      color: var(--text);
    }
    a { color: inherit; }
    .page {
      max-width: 1180px;
      margin: 0 auto;
      padding: 24px 16px 40px;
    }
    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      margin-bottom: 20px;
      flex-wrap: wrap;
    }
    .topbar-left { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
    .back-link {
      color: var(--muted);
      text-decoration: none;
      font-size: 14px;
    }
    .brand {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 14px;
      background: rgba(15, 23, 42, 0.48);
      border: 1px solid rgba(148, 163, 184, 0.14);
      box-shadow: var(--shadow);
      font-weight: 700;
    }
    .brand-mark {
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: linear-gradient(135deg, var(--accent), var(--accent-2));
      box-shadow: 0 0 20px rgba(139, 92, 246, 0.48);
    }
    .nav {
      display: inline-flex;
      gap: 8px;
      padding: 6px;
      border-radius: 16px;
      background: rgba(15, 23, 42, 0.58);
      border: 1px solid rgba(148, 163, 184, 0.12);
    }
    .nav-link {
      text-decoration: none;
      color: var(--muted);
      padding: 10px 14px;
      border-radius: 12px;
      font-size: 14px;
      font-weight: 600;
    }
    .nav-link.active {
      color: #fff;
      background: linear-gradient(135deg, rgba(139, 92, 246, 0.28), rgba(56, 189, 248, 0.2));
      border: 1px solid rgba(139, 92, 246, 0.3);
    }
    .hero {
      position: relative;
      overflow: hidden;
      padding: 24px;
      border-radius: 24px;
      background:
        radial-gradient(circle at top right, rgba(139, 92, 246, 0.25), transparent 28%),
        linear-gradient(135deg, #111827, #172033);
      border: 1px solid rgba(148, 163, 184, 0.14);
      box-shadow: var(--shadow);
      margin-bottom: 20px;
    }
    .hero::after {
      content: "";
      position: absolute;
      right: -60px;
      top: -60px;
      width: 220px;
      height: 220px;
      border-radius: 999px;
      background: radial-gradient(circle, rgba(56, 189, 248, 0.2), transparent 68%);
      pointer-events: none;
    }
    .hero-title {
      position: relative;
      z-index: 1;
      font-size: 30px;
      font-weight: 800;
      margin-bottom: 10px;
    }
    .hero-subtitle {
      position: relative;
      z-index: 1;
      font-size: 15px;
      line-height: 1.55;
      color: var(--muted);
      max-width: 760px;
    }
    .notice {
      border-radius: 16px;
      padding: 14px 16px;
      margin-bottom: 18px;
      border: 1px solid transparent;
    }
    .notice.success { background: rgba(16, 185, 129, 0.12); color: #a7f3d0; border-color: rgba(16, 185, 129, 0.26); }
    .notice.error { background: rgba(239, 68, 68, 0.12); color: #fecaca; border-color: rgba(239, 68, 68, 0.24); }
    .stats-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 20px;
    }
    .stat-card, .card {
      background: linear-gradient(180deg, var(--surface-raised), var(--surface));
      border: 1px solid rgba(148, 163, 184, 0.12);
      border-radius: 20px;
      padding: 18px;
      box-shadow: var(--shadow);
    }
    .stat-label {
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 10px;
    }
    .stat-value {
      font-size: 34px;
      font-weight: 800;
      margin-bottom: 6px;
    }
    .stat-meta {
      font-size: 14px;
      color: var(--muted);
      line-height: 1.45;
    }
    .section-grid {
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 16px;
      align-items: start;
    }
    .card-title {
      font-size: 18px;
      font-weight: 700;
      margin-bottom: 6px;
    }
    .card-subtitle {
      font-size: 14px;
      color: var(--muted);
      line-height: 1.5;
      margin-bottom: 16px;
    }
    .module-list {
      display: grid;
      gap: 12px;
    }
    .module-item {
      padding: 16px;
      border-radius: 18px;
      background: rgba(15, 23, 42, 0.46);
      border: 1px solid rgba(148, 163, 184, 0.12);
    }
    .module-title {
      font-size: 16px;
      font-weight: 700;
      margin-bottom: 6px;
    }
    .module-text {
      font-size: 14px;
      color: var(--muted);
      line-height: 1.5;
      margin-bottom: 12px;
    }
    .btn-row {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    .btn, button {
      appearance: none;
      border: 1px solid var(--border);
      background: var(--surface-soft);
      color: var(--text);
      border-radius: 12px;
      padding: 10px 14px;
      font-size: 14px;
      font-weight: 700;
      text-decoration: none;
      cursor: pointer;
    }
    .btn.primary, button.primary {
      background: linear-gradient(135deg, rgba(139, 92, 246, 0.32), rgba(56, 189, 248, 0.22));
      border-color: rgba(139, 92, 246, 0.32);
    }
    .btn.danger, button.danger {
      background: rgba(127, 29, 29, 0.38);
      border-color: rgba(239, 68, 68, 0.3);
    }
    .muted-list {
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.7;
    }
    .table-wrap {
      overflow-x: auto;
      border-radius: 16px;
      border: 1px solid rgba(148, 163, 184, 0.12);
      background: rgba(15, 23, 42, 0.36);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 760px;
    }
    th, td {
      padding: 12px 14px;
      text-align: left;
      border-bottom: 1px solid rgba(148, 163, 184, 0.12);
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 700;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    tbody tr:last-child td { border-bottom: none; }
    .badge {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 12px;
      font-weight: 700;
    }
    .badge.active { background: rgba(16, 185, 129, 0.14); color: #a7f3d0; }
    .badge.inactive { background: rgba(148, 163, 184, 0.14); color: #cbd5e1; }
    .badge.promo { background: rgba(245, 158, 11, 0.14); color: #fde68a; }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    .form-row { margin-bottom: 12px; }
    .form-row label {
      display: block;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--muted);
      margin-bottom: 6px;
    }
    input {
      width: 100%;
      padding: 11px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(15, 23, 42, 0.58);
      color: var(--text);
    }
    select {
      width: 100%;
      padding: 11px 12px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(15, 23, 42, 0.58);
      color: var(--text);
    }
    input::placeholder { color: #64748b; }
    .inline-form { display: inline; margin: 0; }
    .toolbar-form {
      display: grid;
      grid-template-columns: 1.4fr repeat(4, minmax(0, 1fr));
      gap: 12px;
      align-items: end;
    }
    .toolbar-actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin-top: 14px;
    }
    .cell-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .mini-form {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      margin: 0;
    }
    .mini-form input {
      width: 82px;
      padding: 8px 10px;
      font-size: 13px;
    }
    .mini-form button,
    .cell-actions .btn {
      padding: 8px 10px;
      font-size: 13px;
      border-radius: 10px;
    }
    .empty-state {
      padding: 18px;
      border-radius: 16px;
      background: rgba(15, 23, 42, 0.44);
      color: var(--muted);
      border: 1px dashed rgba(148, 163, 184, 0.18);
    }
    .bars-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-top: 16px;
    }
    .bar-list {
      display: grid;
      gap: 12px;
    }
    .bar-item {
      padding: 14px;
      border-radius: 16px;
      background: rgba(15, 23, 42, 0.44);
      border: 1px solid rgba(148, 163, 184, 0.12);
    }
    .bar-top {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 14px;
      margin-bottom: 8px;
    }
    .bar-label {
      font-weight: 700;
      color: var(--text);
    }
    .bar-value {
      color: var(--muted);
      white-space: nowrap;
    }
    .bar-track {
      width: 100%;
      height: 10px;
      border-radius: 999px;
      background: rgba(148, 163, 184, 0.14);
      overflow: hidden;
    }
    .bar-fill {
      height: 100%;
      border-radius: 999px;
      background: linear-gradient(135deg, rgba(139, 92, 246, 0.95), rgba(56, 189, 248, 0.95));
    }
    @media (max-width: 980px) {
      .stats-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .section-grid { grid-template-columns: 1fr; }
      .bars-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 640px) {
      .page { padding: 18px 12px 32px; }
      .hero-title { font-size: 24px; }
      .stats-grid { grid-template-columns: 1fr; }
      .form-grid { grid-template-columns: 1fr; }
      .toolbar-form { grid-template-columns: 1fr; }
      .nav { width: 100%; justify-content: stretch; }
      .nav-link { flex: 1; text-align: center; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="topbar">
      <div class="topbar-left">
        <a href="/" class="back-link">← В приложение</a>
        <div class="brand"><span class="brand-mark"></span><span>Raccaster VPN Admin</span></div>
      </div>
      <nav class="nav">
        <a href="/admin" class="__NAV_OVERVIEW__">Обзор</a>
        <a href="/admin/tariffs" class="__NAV_TARIFFS__">Тарифы</a>
        <a href="/admin/payments" class="__NAV_PAYMENTS__">Платежи</a>
        <a href="/admin/devices" class="__NAV_DEVICES__">Устройства</a>
        <a href="/admin/users" class="__NAV_USERS__">Пользователи</a>
        <a href="/admin/analytics" class="__NAV_ANALYTICS__">Аналитика</a>
      </nav>
    </div>
    <section class="hero">
      <div class="hero-title">__TITLE__</div>
      <div class="hero-subtitle">__SUBTITLE__</div>
    </section>
    __NOTICE_HTML__
    __CONTENT_HTML__
  </div>
</body>
</html>
"""
    return (
        html.replace("__TITLE__", escape(title))
        .replace("__SUBTITLE__", escape(subtitle))
        .replace("__NOTICE_HTML__", notice_html)
        .replace("__CONTENT_HTML__", content_html)
        .replace("__NAV_OVERVIEW__", nav_overview_class)
        .replace("__NAV_TARIFFS__", nav_tariffs_class)
        .replace("__NAV_PAYMENTS__", nav_payments_class)
        .replace("__NAV_DEVICES__", nav_devices_class)
        .replace("__NAV_USERS__", nav_users_class)
        .replace("__NAV_ANALYTICS__", nav_analytics_class)
    )


def _admin_home_html(tariffs: List[Dict[str, Any]], metrics: Dict[str, int]) -> str:
    total = len(tariffs)
    active = sum(1 for t in tariffs if t.get("is_active"))
    inactive = total - active
    with_badges = sum(1 for t in tariffs if (t.get("badge") or "").strip())
    cheapest = min((int(t.get("price_stars") or 0) for t in tariffs), default=0)
    highest = max((int(t.get("price_stars") or 0) for t in tariffs), default=0)
    recent = sorted(tariffs, key=lambda t: int(t.get("sort_order") or 0))[:5]
    rows_html = "".join(
        (
            "<tr>"
            f"<td>{escape(str(t.get('name') or ''))}</td>"
            f"<td>{int(t.get('months') or 0)}</td>"
            f"<td>{int(t.get('price_stars') or 0)} ⭐</td>"
            f"<td>{int(t.get('traffic_gb') or 0)} GB</td>"
            f"<td><span class=\"badge {'active' if t.get('is_active') else 'inactive'}\">{'Активен' if t.get('is_active') else 'Выключен'}</span></td>"
            "</tr>"
        )
        for t in recent
    )
    if not rows_html:
        rows_html = '<tr><td colspan="5">Тарифы пока не созданы</td></tr>'
    content_html = f"""
    <section class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Пользователи</div>
        <div class="stat-value">{metrics["users_count"]}</div>
        <div class="stat-meta">Всего пользователей в базе.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Активные устройства</div>
        <div class="stat-value">{metrics["active_subscriptions"]}</div>
        <div class="stat-meta">Действующие подписки и конфиги.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Истекают скоро</div>
        <div class="stat-value">{metrics["expiring_soon"]}</div>
        <div class="stat-meta">Подписок с окончанием в ближайшие 7 дней.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Баланс пользователей</div>
        <div class="stat-value">{metrics["total_balance"]} ⭐</div>
        <div class="stat-meta">Суммарный Stars-баланс по всем аккаунтам.</div>
      </div>
    </section>
    <section class="section-grid">
      <div class="card">
        <div class="card-title">Разделы админки</div>
        <div class="card-subtitle">Панель разделена на рабочие разделы: тарифы, платежи, устройства, пользователи и аналитика по продукту.</div>
        <div class="module-list">
          <div class="module-item">
            <div class="module-title">Тарифы</div>
            <div class="module-text">Управление каталогом, ценами, трафиком, бейджами и порядком показа тарифов в WebApp.</div>
            <div class="btn-row">
              <a class="btn primary" href="/admin/tariffs">Открыть тарифы</a>
              <a class="btn" href="/admin/tariffs#tariff-create">Добавить тариф</a>
            </div>
          </div>
          <div class="module-item">
            <div class="module-title">Платежи</div>
            <div class="module-text">Просмотр пополнений, покупок, возвратов и ожидающих операций кошелька пользователей.</div>
            <div class="btn-row">
              <a class="btn primary" href="/admin/payments">Открыть платежи</a>
            </div>
          </div>
          <div class="module-item">
            <div class="module-title">Устройства</div>
            <div class="module-text">Список подписок и устройств пользователей: статус, срок действия, тариф и привязка к аккаунту.</div>
            <div class="btn-row">
              <a class="btn primary" href="/admin/devices">Открыть устройства</a>
            </div>
          </div>
          <div class="module-item">
            <div class="module-title">Пользователи</div>
            <div class="module-text">База пользователей с поиском, карточкой профиля, балансом, устройствами и историей операций.</div>
            <div class="btn-row">
              <a class="btn primary" href="/admin/users">Открыть пользователей</a>
            </div>
          </div>
          <div class="module-item">
            <div class="module-title">Аналитика</div>
            <div class="module-text">Рост, выручка в Stars, воронка, топ тарифов и платформ для быстрых продуктовых решений.</div>
            <div class="btn-row">
              <a class="btn primary" href="/admin/analytics">Открыть аналитику</a>
            </div>
          </div>
          <div class="module-item">
            <div class="module-title">Проверка витрины</div>
            <div class="module-text">Быстрый переход в пользовательский WebApp, чтобы сразу проверить изменения после редактирования панели.</div>
            <div class="btn-row">
              <a class="btn" href="/">Открыть приложение</a>
            </div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Сводка системы</div>
        <div class="card-subtitle">Быстрые ориентиры по проекту и тарифной сетке.</div>
        <ul class="muted-list">
          <li>Всего тарифов: {total}</li>
          <li>Активные тарифы: {active}</li>
          <li>Неактивные тарифы: {inactive}</li>
          <li>Тарифов с бейджами: {with_badges}</li>
          <li>Минимальная цена: {cheapest} ⭐</li>
          <li>Максимальная цена: {highest} ⭐</li>
          <li>Оплаченных операций: {metrics["paid_transactions"]}</li>
          <li>Ожидающих пополнений: {metrics["pending_topups"]}</li>
          <li>Всего пополнено через кошелек: {metrics["topup_volume"]} ⭐</li>
        </ul>
      </div>
    </section>
    <section class="card" style="margin-top:16px;">
      <div class="card-title">Быстрый обзор тарифов</div>
      <div class="card-subtitle">Первые позиции каталога, чтобы быстро оценить сетку без перехода в редактор.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Название</th>
              <th>Месяцев</th>
              <th>Цена</th>
              <th>Трафик</th>
              <th>Статус</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </section>
    """
    return _admin_layout(
        title="Админ-панель",
        subtitle="Главный обзор проекта, навигация по разделам и быстрые действия для управления каталогом VPN.",
        active_tab="overview",
        content_html=content_html,
    )


def _admin_tariffs_html(tariffs: List[Dict[str, Any]], status: str | None = None, error: str | None = None) -> str:
    def render_tariff_row(t: Dict[str, Any]) -> str:
        badge_html = f"<span class='badge promo'>{escape(str(t.get('badge') or ''))}</span>" if t.get("badge") else "—"
        status_class = "active" if t.get("is_active") else "inactive"
        status_text = "Активен" if t.get("is_active") else "Выключен"
        return (
            "<tr>"
            f"<td>{t['id']}</td>"
            f"<td>{escape(str(t.get('name') or ''))}</td>"
            f"<td>{int(t.get('months') or 0)}</td>"
            f"<td>{int(t.get('price_stars') or 0)} ⭐</td>"
            f"<td>{int(t.get('traffic_gb') or 0)} GB</td>"
            f"<td>{badge_html}</td>"
            f"<td><span class=\"badge {status_class}\">{status_text}</span></td>"
            "<td>"
            f"<form method=\"post\" action=\"/admin/tariffs/{t['id']}/delete\" class=\"inline-form\" onsubmit=\"return confirm('Удалить тариф?');\">"
            "<button type=\"submit\" class=\"danger\">Удалить</button>"
            "</form>"
            "</td>"
            "</tr>"
        )

    rows_html = "".join(render_tariff_row(t) for t in tariffs)
    if not rows_html:
        rows_html = '<tr><td colspan="8">Нет тарифов</td></tr>'

    content_html = f"""
    <section class="section-grid">
      <form class="card" id="tariff-create" method="post" action="/admin/tariffs/create">
        <div class="card-title">Добавить тариф</div>
        <div class="card-subtitle">Создайте новый тариф для витрины WebApp. Сразу укажите цену в Stars, трафик, длительность и порядок показа.</div>
        <div class="form-grid">
          <div class="form-row"><label>Название</label><input type="text" name="name" placeholder="1 месяц" /></div>
          <div class="form-row"><label>Месяцев</label><input type="number" name="months" min="1" value="1" /></div>
          <div class="form-row"><label>Цена (Stars)</label><input type="number" name="price_stars" min="0" value="300" /></div>
          <div class="form-row"><label>Трафик (GB)</label><input type="number" name="traffic_gb" min="0" value="30" /></div>
          <div class="form-row"><label>Бейдж</label><input type="text" name="badge" placeholder="-17%" /></div>
          <div class="form-row"><label>Порядок</label><input type="number" name="sort_order" value="0" /></div>
        </div>
        <div class="btn-row" style="margin-top:8px;">
          <button type="submit" class="primary">Добавить тариф</button>
          <a class="btn" href="/admin">Назад в обзор</a>
        </div>
      </form>
      <div class="card">
        <div class="card-title">Как использовать раздел</div>
        <div class="card-subtitle">Здесь собран весь текущий рабочий функционал по управлению тарифной сеткой.</div>
        <ul class="muted-list">
          <li>Добавляйте новые тарифы через форму слева.</li>
          <li>Проверяйте итоговую сетку в таблице ниже.</li>
          <li>Удаляйте устаревшие или тестовые тарифы напрямую из списка.</li>
          <li>Порядок влияет на расположение тарифов в витрине WebApp.</li>
        </ul>
      </div>
    </section>
    <section class="card" style="margin-top:16px;">
      <div class="card-title">Управление тарифами</div>
      <div class="card-subtitle">Текущий список тарифов со статусами, бейджами и быстрым удалением.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Название</th>
              <th>Месяцев</th>
              <th>Цена</th>
              <th>Трафик</th>
              <th>Бейдж</th>
              <th>Статус</th>
              <th>Действие</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </section>
    """
    return _admin_layout(
        title="Управление тарифами",
        subtitle="Отдельный раздел для редактирования каталога тарифов. Основная админка теперь работает как dashboard с навигацией и обзором.",
        active_tab="tariffs",
        content_html=content_html,
        notice_html=_admin_notice_html(status=status, error=error),
    )


def _admin_payments_html(data: Dict[str, Any], *, q: str = "", status: str = "", kind: str = "", notice_html: str = "") -> str:
    stats = data["stats"]
    rows = data["rows"]
    q_value = escape(q)
    status_all = "selected" if not status else ""
    status_paid = "selected" if status == "paid" else ""
    status_pending = "selected" if status == "pending" else ""
    status_cancelled = "selected" if status == "cancelled" else ""
    kind_all = "selected" if not kind else ""
    kind_topup = "selected" if kind == "topup" else ""
    kind_purchase = "selected" if kind == "purchase" else ""
    kind_refund = "selected" if kind == "refund" else ""
    rows_html = "".join(
        (
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{_admin_person_label(row)}<br><span style=\"color:#94a3b8;font-size:12px;\">TG {row['telegram_id']}</span></td>"
            f"<td>{escape(str(row.get('description') or row.get('kind') or '—'))}</td>"
            f"<td><span class=\"badge {'active' if row.get('status') == 'paid' else 'inactive'}\">{escape(str(row.get('status') or '—'))}</span></td>"
            f"<td>{int(row.get('amount') or 0)} ⭐</td>"
            f"<td>{escape(str(row.get('provider_amount') or '—'))}{(' ' + escape(str(row.get('provider_currency')))) if row.get('provider_currency') else ''}</td>"
            f"<td>{row['created_at'].strftime('%d.%m.%Y %H:%M') if row.get('created_at') else '—'}</td>"
            "</tr>"
        )
        for row in rows
    )
    if not rows_html:
        rows_html = '<tr><td colspan="7">Операций пока нет</td></tr>'
    content_html = f"""
    <section class="card">
      <div class="card-title">Поиск и фильтры</div>
      <div class="card-subtitle">Ищите по Telegram ID, username, имени пользователя или описанию операции.</div>
      <form method="get" action="/admin/payments">
        <div class="toolbar-form">
          <div class="form-row"><label>Поиск</label><input type="text" name="q" value="{q_value}" placeholder="123456789, @user, Пополнение" /></div>
          <div class="form-row"><label>Статус</label><select name="status"><option value="" {status_all}>Все</option><option value="paid" {status_paid}>paid</option><option value="pending" {status_pending}>pending</option><option value="cancelled" {status_cancelled}>cancelled</option></select></div>
          <div class="form-row"><label>Тип операции</label><select name="kind"><option value="" {kind_all}>Все</option><option value="topup" {kind_topup}>topup</option><option value="purchase" {kind_purchase}>purchase</option><option value="refund" {kind_refund}>refund</option></select></div>
          <div class="form-row"><label>Действие</label><button type="submit" class="primary">Применить</button></div>
        </div>
        <div class="toolbar-actions"><a class="btn" href="/admin/payments">Сбросить фильтры</a></div>
      </form>
    </section>
    <section class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Пополнения</div>
        <div class="stat-value">{stats["paid_topups"]}</div>
        <div class="stat-meta">Завершенных пополнений баланса.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Объем пополнений</div>
        <div class="stat-value">{stats["topup_volume"]} ⭐</div>
        <div class="stat-meta">Сумма успешных top-up операций.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Покупки</div>
        <div class="stat-value">{stats["purchases_count"]}</div>
        <div class="stat-meta">Оплаченных списаний за тарифы.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Ожидают оплаты</div>
        <div class="stat-value">{stats["pending_topups"]}</div>
        <div class="stat-meta">Pending-инвойсов пополнения.</div>
      </div>
    </section>
    <section class="section-grid">
      <div class="card">
        <div class="card-title">Раздел платежей</div>
        <div class="card-subtitle">Здесь удобно отслеживать пополнения кошелька, покупки тарифов и возвраты. Это уже не редактор, а реально полезный операционный раздел.</div>
        <ul class="muted-list">
          <li>Всего списано на покупки: {stats["purchase_volume"]} ⭐</li>
          <li>Возвратов проведено: {stats["refunds_count"]}</li>
          <li>Pending top-up полезен для поиска зависших попыток оплаты</li>
          <li>Описание операции показывает, за что именно было списание или пополнение</li>
        </ul>
      </div>
      <div class="card">
        <div class="card-title">Быстрые действия</div>
        <div class="card-subtitle">Переходы в другие рабочие разделы.</div>
        <div class="btn-row">
          <a class="btn" href="/admin">Обзор</a>
          <a class="btn" href="/admin/tariffs">Тарифы</a>
          <a class="btn primary" href="/admin/devices">Устройства</a>
        </div>
      </div>
    </section>
    <section class="card" style="margin-top:16px;">
      <div class="card-title">Последние операции</div>
      <div class="card-subtitle">Последние транзакции кошелька по всем пользователям.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Пользователь</th>
              <th>Описание</th>
              <th>Статус</th>
              <th>Сумма</th>
              <th>Провайдер</th>
              <th>Дата</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </section>
    """
    return _admin_layout(
        title="Платежи и кошелек",
        subtitle="Операционный раздел для просмотра пополнений, покупок, возвратов и ожидающих платежных попыток.",
        active_tab="payments",
        content_html=content_html,
        notice_html=notice_html,
    )


def _admin_devices_html(
    data: Dict[str, Any],
    *,
    q: str = "",
    status: str = "",
    os_name: str = "",
    backend_key: str = "",
    redirect_query: str = "",
    notice_html: str = "",
) -> str:
    stats = data["stats"]
    rows = data["rows"]
    backend_breakdown = data.get("backend_breakdown") or []
    q_value = escape(q)
    os_all = "selected" if not os_name else ""
    os_windows = "selected" if os_name == "Windows" else ""
    os_ios = "selected" if os_name == "iOS" else ""
    os_android = "selected" if os_name == "Android" else ""
    backend_all = "selected" if not backend_key else ""
    backend_options_html = "".join(
        f'<option value="{escape(str(item["backend_key"]))}" {"selected" if backend_key == str(item["backend_key"]) else ""}>{escape(str(item["backend_key"]))}</option>'
        for item in backend_breakdown
    )
    status_all = "selected" if not status else ""
    status_active = "selected" if status == "active" else ""
    status_inactive = "selected" if status == "inactive" else ""
    status_expired = "selected" if status == "expired" else ""
    status_expiring = "selected" if status == "expiring" else ""
    rows_html = "".join(
        _admin_device_row_html(row, redirect_query=redirect_query)
        for row in rows
    )
    if not rows_html:
        rows_html = '<tr><td colspan="11">Устройств пока нет</td></tr>'
    backend_breakdown_text = ", ".join(
        f'{item["backend_key"]}: {item["total"]}'
        for item in backend_breakdown[:6]
    ) or "Пока только один backend или устройств ещё нет."
    content_html = f"""
    <section class="card">
      <div class="card-title">Поиск и фильтры</div>
      <div class="card-subtitle">Ищите по Telegram ID, username, названию конфига и платформе. Можно быстро открыть все устройства конкретного пользователя.</div>
      <form method="get" action="/admin/devices">
        <div class="toolbar-form">
          <div class="form-row"><label>Поиск</label><input type="text" name="q" value="{q_value}" placeholder="123456789, @user, iOS 2" /></div>
          <div class="form-row"><label>Статус</label><select name="status"><option value="" {status_all}>Все</option><option value="active" {status_active}>Активные</option><option value="inactive" {status_inactive}>Неактивные</option><option value="expired" {status_expired}>Истекшие</option><option value="expiring" {status_expiring}>Истекают скоро</option></select></div>
          <div class="form-row"><label>Платформа</label><select name="os"><option value="" {os_all}>Все</option><option value="Windows" {os_windows}>Windows</option><option value="iOS" {os_ios}>iOS</option><option value="Android" {os_android}>Android</option></select></div>
          <div class="form-row"><label>Backend</label><select name="backend"><option value="" {backend_all}>Все</option>{backend_options_html}</select></div>
          <div class="form-row"><label>Действие</label><button type="submit" class="primary">Применить</button></div>
        </div>
        <div class="toolbar-actions"><a class="btn" href="/admin/devices">Сбросить фильтры</a></div>
      </form>
    </section>
    <section class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Всего устройств</div>
        <div class="stat-value">{stats["total_devices"]}</div>
        <div class="stat-meta">Все записи подписок и конфигов.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Активные</div>
        <div class="stat-value">{stats["active_devices"]}</div>
        <div class="stat-meta">Рабочие подписки с неистекшим сроком.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Истекают скоро</div>
        <div class="stat-value">{stats["expiring_soon"]}</div>
        <div class="stat-meta">Истечение в ближайшие 7 дней.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Пользователи с устройствами</div>
        <div class="stat-value">{stats["users_with_devices"]}</div>
        <div class="stat-meta">Сколько пользователей имеют хотя бы одно устройство.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Подозрение на шаринг</div>
        <div class="stat-value">{stats["suspicious_devices"]}</div>
        <div class="stat-meta">Устройств с числом замеченных IP выше лимита `3`.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Backend-ов</div>
        <div class="stat-value">{len(backend_breakdown)}</div>
        <div class="stat-meta">Сколько серверов уже участвуют в распределении устройств.</div>
      </div>
    </section>
    <section class="section-grid">
      <div class="card">
        <div class="card-title">Раздел устройств</div>
        <div class="card-subtitle">Позволяет быстро увидеть, кто и какие конфиги создал, на какую платформу и с каким сроком действия.</div>
        <ul class="muted-list">
          <li>Выключенные вручную записи: {stats["disabled_devices"]}</li>
          <li>Истекших подписок: {stats["expired_devices"]}</li>
          <li>Список сортируется по дате создания, сверху самые свежие устройства</li>
          <li>Можно использовать как основу для будущих действий: блокировка, ручное продление, поиск пользователя</li>
          <li>Распределение по backend: {escape(backend_breakdown_text)}</li>
          <li>{'IP-аналитика включена: видно историю IP и возможный шаринг.' if stats["share_data_ready"] else 'IP-аналитика недоступна в API текущей версии 3x-ui или пока не вернула данные.'}</li>
        </ul>
      </div>
      <div class="card">
        <div class="card-title">Быстрые действия</div>
        <div class="card-subtitle">Смежные разделы для операционной работы.</div>
        <div class="btn-row">
          <a class="btn" href="/admin">Обзор</a>
          <a class="btn" href="/admin/tariffs">Тарифы</a>
          <a class="btn primary" href="/admin/payments">Платежи</a>
        </div>
      </div>
    </section>
    <section class="card" style="margin-top:16px;">
      <div class="card-title">Последние устройства и подписки</div>
      <div class="card-subtitle">Свежие конфиги пользователей с платформой, тарифом и сроком действия.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Пользователь</th>
              <th>Backend</th>
              <th>Платформа</th>
              <th>Название</th>
              <th>Цена</th>
              <th>Трафик</th>
              <th>Действует до</th>
              <th>Статус</th>
              <th>IP / шаринг</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </section>
    """
    return _admin_layout(
        title="Устройства и подписки",
        subtitle="Операционный список конфигов пользователей: статус, срок действия, платформа и привязка к аккаунту.",
        active_tab="devices",
        content_html=content_html,
        notice_html=notice_html,
    )


def _admin_users_html(data: Dict[str, Any], *, q: str = "", segment: str = "", notice_html: str = "") -> str:
    stats = data["stats"]
    rows = data["rows"]
    q_value = escape(q)
    seg_all = "selected" if not segment else ""
    seg_balance = "selected" if segment == "with_balance" else ""
    seg_devices = "selected" if segment == "with_devices" else ""
    seg_payments = "selected" if segment == "with_payments" else ""
    rows_html = "".join(
        (
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{_admin_person_label(row)}<br><span style=\"color:#94a3b8;font-size:12px;\">TG {row['telegram_id']}</span></td>"
            f"<td>{int(row.get('vpn_balance_stars') or 0)} ⭐</td>"
            f"<td>{int(row.get('active_devices') or 0)} / {int(row.get('total_devices') or 0)}</td>"
            f"<td>{int(row.get('total_transactions') or 0)}</td>"
            f"<td>{row['last_transaction_at'].strftime('%d.%m.%Y %H:%M') if row.get('last_transaction_at') else '—'}</td>"
            "<td><div class=\"cell-actions\">"
            f"<a class=\"btn primary\" href=\"/admin/users/{row['id']}\">Профиль</a>"
            f"<a class=\"btn\" href=\"/admin/devices?q={row['telegram_id']}\">Устройства</a>"
            f"<a class=\"btn\" href=\"/admin/payments?q={row['telegram_id']}\">Платежи</a>"
            "</div></td>"
            "</tr>"
        )
        for row in rows
    )
    if not rows_html:
        rows_html = '<tr><td colspan="7">Пользователи не найдены</td></tr>'
    content_html = f"""
    <section class="card">
      <div class="card-title">Поиск и фильтры</div>
      <div class="card-subtitle">Ищите по Telegram ID, username, имени или фамилии пользователя.</div>
      <form method="get" action="/admin/users">
        <div class="toolbar-form">
          <div class="form-row"><label>Поиск</label><input type="text" name="q" value="{q_value}" placeholder="123456789, @user, Иван" /></div>
          <div class="form-row"><label>Сегмент</label><select name="segment"><option value="" {seg_all}>Все</option><option value="with_balance" {seg_balance}>С балансом</option><option value="with_devices" {seg_devices}>С устройствами</option><option value="with_payments" {seg_payments}>С операциями</option></select></div>
          <div class="form-row"><label>Действие</label><button type="submit" class="primary">Применить</button></div>
          <div class="form-row"><label>Сброс</label><a class="btn" href="/admin/users">Сбросить</a></div>
        </div>
      </form>
    </section>
    <section class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Всего пользователей</div>
        <div class="stat-value">{stats["total_users"]}</div>
        <div class="stat-meta">Аккаунтов в базе данных.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">С балансом</div>
        <div class="stat-value">{stats["users_with_balance"]}</div>
        <div class="stat-meta">Пользователей с положительным Stars-балансом.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Суммарный баланс</div>
        <div class="stat-value">{stats["total_balance"]} ⭐</div>
        <div class="stat-meta">Общий баланс всех пользователей.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Переход</div>
        <div class="stat-value">Profile</div>
        <div class="stat-meta">Откройте профиль пользователя для деталей и быстрых действий.</div>
      </div>
    </section>
    <section class="card">
      <div class="card-title">Пользователи</div>
      <div class="card-subtitle">Список аккаунтов с балансом, количеством устройств и операций.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Пользователь</th>
              <th>Баланс</th>
              <th>Устройства</th>
              <th>Операции</th>
              <th>Последняя активность</th>
              <th>Действия</th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    </section>
    """
    return _admin_layout(
        title="Пользователи",
        subtitle="Раздел для управления аккаунтами: баланс, устройства, платежи и быстрый переход в карточку пользователя.",
        active_tab="users",
        content_html=content_html,
        notice_html=notice_html,
    )


def _admin_user_profile_html(profile: Dict[str, Any], *, notice_html: str = "") -> str:
    user = profile["user"]
    device_stats = profile["device_stats"]
    tx_stats = profile["tx_stats"]
    devices = profile["devices"]
    transactions = profile["transactions"]
    device_rows_html = "".join(
        (
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{escape(str(row.get('device_os') or '—'))}</td>"
            f"<td>{escape(str(row.get('server_label') or '—'))}</td>"
            f"<td>{int(row.get('price_stars') or 0)} ⭐</td>"
            f"<td>{int(row.get('traffic_gb') or 0)} GB</td>"
            f"<td>{row['expires_at'].strftime('%d.%m.%Y') if row.get('expires_at') else '—'}</td>"
            f"<td><a class=\"btn\" href=\"/admin/devices?q={user['telegram_id']}\">Открыть в устройствах</a></td>"
            "</tr>"
        )
        for row in devices
    ) or '<tr><td colspan="7">У пользователя пока нет устройств</td></tr>'
    tx_rows_html = "".join(
        (
            "<tr>"
            f"<td>{row['id']}</td>"
            f"<td>{escape(str(row.get('description') or row.get('kind') or '—'))}</td>"
            f"<td><span class=\"badge {'active' if row.get('status') == 'paid' else 'inactive'}\">{escape(str(row.get('status') or '—'))}</span></td>"
            f"<td>{int(row.get('amount') or 0)} ⭐</td>"
            f"<td>{escape(str(row.get('provider_amount') or '—'))}{(' ' + escape(str(row.get('provider_currency')))) if row.get('provider_currency') else ''}</td>"
            f"<td>{row['created_at'].strftime('%d.%m.%Y %H:%M') if row.get('created_at') else '—'}</td>"
            "</tr>"
        )
        for row in transactions
    ) or '<tr><td colspan="6">Операций пока нет</td></tr>'
    content_html = f"""
    <section class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Пользователь</div>
        <div class="stat-value">{_admin_person_label(user)}</div>
        <div class="stat-meta">Telegram ID: {user["telegram_id"]}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Баланс</div>
        <div class="stat-value">{int(user.get("vpn_balance_stars") or 0)} ⭐</div>
        <div class="stat-meta">Текущий Stars-баланс пользователя.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Устройства</div>
        <div class="stat-value">{device_stats["active_devices"]} / {device_stats["total_devices"]}</div>
        <div class="stat-meta">Активные и всего устройства пользователя.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Операции</div>
        <div class="stat-value">{tx_stats["total_transactions"]}</div>
        <div class="stat-meta">Доход: {tx_stats["total_income"]} ⭐, списания: {tx_stats["total_outcome"]} ⭐</div>
      </div>
    </section>
    <section class="section-grid">
      <div class="card">
        <div class="card-title">Быстрые действия</div>
        <div class="card-subtitle">Переходы в связанные разделы и ручная корректировка баланса пользователя.</div>
        <div class="btn-row" style="margin-bottom:14px;">
          <a class="btn" href="/admin/users">Все пользователи</a>
          <a class="btn" href="/admin/devices?q={user["telegram_id"]}">Все устройства пользователя</a>
          <a class="btn" href="/admin/payments?q={user["telegram_id"]}">Все платежи пользователя</a>
        </div>
        <form method="post" action="/admin/users/{user["id"]}/balance">
          <div class="form-grid">
            <div class="form-row"><label>Тип операции</label><select name="mode"><option value="credit">Начислить</option><option value="debit">Списать</option></select></div>
            <div class="form-row"><label>Сумма (Stars)</label><input type="number" name="amount" min="1" value="10" /></div>
            <div class="form-row" style="grid-column: span 2;"><label>Описание</label><input type="text" name="description" placeholder="Ручная корректировка администратором" /></div>
          </div>
          <div class="btn-row" style="margin-top:8px;">
            <button type="submit" class="primary">Применить</button>
          </div>
        </form>
      </div>
      <div class="card">
        <div class="card-title">Профиль пользователя</div>
        <div class="card-subtitle">Краткая информация об аккаунте.</div>
        <ul class="muted-list">
          <li>ID пользователя в базе: {user["id"]}</li>
          <li>Telegram ID: {user["telegram_id"]}</li>
          <li>Username: {'@' + escape(str(user["username"])) if user.get("username") else '—'}</li>
          <li>Дата регистрации: {user["created_at"].strftime('%d.%m.%Y %H:%M') if user.get("created_at") else '—'}</li>
        </ul>
      </div>
    </section>
    <section class="card" style="margin-top:16px;">
      <div class="card-title">Последние устройства пользователя</div>
      <div class="card-subtitle">Последние созданные конфиги и подписки этого аккаунта.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Платформа</th>
              <th>Название</th>
              <th>Цена</th>
              <th>Трафик</th>
              <th>Действует до</th>
              <th>Действие</th>
            </tr>
          </thead>
          <tbody>{device_rows_html}</tbody>
        </table>
      </div>
    </section>
    <section class="card" style="margin-top:16px;">
      <div class="card-title">Последние операции пользователя</div>
      <div class="card-subtitle">История операций кошелька и ручных корректировок.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Описание</th>
              <th>Статус</th>
              <th>Сумма</th>
              <th>Провайдер</th>
              <th>Дата</th>
            </tr>
          </thead>
          <tbody>{tx_rows_html}</tbody>
        </table>
      </div>
    </section>
    """
    return _admin_layout(
        title="Профиль пользователя",
        subtitle="Карточка пользователя с балансом, устройствами, операциями и быстрыми действиями администратора.",
        active_tab="users",
        content_html=content_html,
        notice_html=notice_html,
    )


def _admin_bar_list_html(items: List[Dict[str, Any]], suffix: str = "") -> str:
    if not items:
        return '<div class="empty-state">Пока недостаточно данных для отображения.</div>'
    max_value = max((int(item.get("value") or 0) for item in items), default=1) or 1
    return "".join(
        (
            '<div class="bar-item">'
            f'<div class="bar-top"><span class="bar-label">{escape(str(item.get("label") or "—"))}</span>'
            f'<span class="bar-value">{int(item.get("value") or 0)}{suffix}</span></div>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{0 if int(item.get("value") or 0) <= 0 else max(8, round((int(item.get("value") or 0) / max_value) * 100))}%;"></div></div>'
            '</div>'
        )
        for item in items
    )


def _admin_analytics_html(data: Dict[str, Any]) -> str:
    core = data["core"]
    days = int(data.get("days") or 14)
    total_users = max(core["total_users"], 1)
    paying_rate = round((core["paying_users"] / total_users) * 100)
    buying_rate = round((core["buying_users"] / total_users) * 100)
    active_rate = round((core["active_users"] / total_users) * 100)
    trend_rows_html = "".join(
        (
            "<tr>"
            f"<td>{escape(row['label'])}</td>"
            f"<td>{int(row['users'])}</td>"
            f"<td>{int(row['topups'])} ⭐</td>"
            "</tr>"
        )
        for row in data["trend"]
    ) or '<tr><td colspan="3">Нет данных</td></tr>'
    funnel_items = [
        {"label": "Пользователи", "value": core["total_users"]},
        {"label": "Пополняли баланс", "value": core["paying_users"]},
        {"label": "Покупали тариф", "value": core["buying_users"]},
        {"label": "Имеют активное устройство", "value": core["active_users"]},
    ]
    period_actions_html = "".join(
        f'<a class="btn{" primary" if value == days else ""}" href="/admin/analytics?days={value}">{value} дней</a>'
        for value in (7, 14, 30)
    )
    content_html = f"""
    <section class="card" style="margin-bottom:16px;">
      <div class="card-title">Период аналитики</div>
      <div class="card-subtitle">Быстрый переключатель окна наблюдения для трендов и роста.</div>
      <div class="toolbar-actions">{period_actions_html}</div>
    </section>
    <section class="stats-grid">
      <div class="stat-card">
        <div class="stat-label">Выручка пополнений</div>
        <div class="stat-value">{core["total_topups"]} ⭐</div>
        <div class="stat-meta">Все успешные пополнения кошелька за всё время.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Покупки тарифов</div>
        <div class="stat-value">{core["total_purchases"]} ⭐</div>
        <div class="stat-meta">Суммарные списания на тарифы.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Рост за {days} дней</div>
        <div class="stat-value">+{core["new_users_period"]}</div>
        <div class="stat-meta">Новых пользователей и +{core["new_devices_period"]} новых устройств.</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">Конверсия в активных</div>
        <div class="stat-value">{active_rate}%</div>
        <div class="stat-meta">Доля пользователей с активными устройствами.</div>
      </div>
    </section>
    <section class="section-grid">
      <div class="card">
        <div class="card-title">Воронка продукта</div>
        <div class="card-subtitle">Простая operational-воронка: от регистрации к активному устройству.</div>
        <div class="bar-list">{_admin_bar_list_html(funnel_items)}</div>
        <div class="bars-grid">
          <div class="module-item">
            <div class="module-title">Paying Rate</div>
            <div class="module-text">{paying_rate}% пользователей хотя бы раз пополняли баланс.</div>
          </div>
          <div class="module-item">
            <div class="module-title">Buying Rate</div>
            <div class="module-text">{buying_rate}% пользователей хотя бы раз покупали тариф.</div>
          </div>
        </div>
      </div>
      <div class="card">
        <div class="card-title">Ключевые выводы</div>
        <div class="card-subtitle">Быстрые ориентиры для управленческих решений.</div>
        <ul class="muted-list">
          <li>Всего пользователей: {core["total_users"]}</li>
          <li>Платящих пользователей: {core["paying_users"]}</li>
          <li>Покупающих пользователей: {core["buying_users"]}</li>
          <li>Пользователей с активным устройством: {core["active_users"]}</li>
          <li>Суммарно создано устройств: {core["total_devices"]}</li>
        </ul>
      </div>
    </section>
    <section class="bars-grid">
      <div class="card">
        <div class="card-title">Топ тарифов</div>
        <div class="card-subtitle">Какие тарифы чаще всего покупают и создают как подписки.</div>
        <div class="bar-list">{_admin_bar_list_html(data["top_tariffs"], " шт.")}</div>
      </div>
      <div class="card">
        <div class="card-title">Топ платформ</div>
        <div class="card-subtitle">На каких устройствах пользователи чаще всего подключают VPN.</div>
        <div class="bar-list">{_admin_bar_list_html(data["top_platforms"], " шт.")}</div>
      </div>
    </section>
    <section class="card" style="margin-top:16px;">
      <div class="card-title">Динамика за последние {days} дней</div>
      <div class="card-subtitle">Новые пользователи и объем успешных пополнений по дням.</div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>День</th>
              <th>Новые пользователи</th>
              <th>Пополнения</th>
            </tr>
          </thead>
          <tbody>{trend_rows_html}</tbody>
        </table>
      </div>
    </section>
    """
    return _admin_layout(
        title="Аналитика",
        subtitle="Сводка по росту продукта, выручке в Stars, воронке и структуре спроса по тарифам и платформам.",
        active_tab="analytics",
        content_html=content_html,
    )


async def handle_admin_index(request: web.Request) -> web.Response:
    """Главная страница админки: overview и навигация по разделам."""
    tariffs = await get_tariffs(active_only=False)
    metrics = await _admin_fetch_overview_metrics()
    return web.Response(
        text=_admin_home_html(tariffs=tariffs, metrics=metrics),
        content_type="text/html",
    )


async def handle_admin_tariffs_index(request: web.Request) -> web.Response:
    """Раздел управления тарифами: отдельная страница внутри админки."""
    tariffs = await get_tariffs(active_only=False)
    return web.Response(
        text=_admin_tariffs_html(
            tariffs=tariffs,
            status=request.query.get("status"),
            error=request.query.get("error"),
        ),
        content_type="text/html",
    )


async def handle_admin_payments_index(request: web.Request) -> web.Response:
    """Раздел админки с платежами и операциями кошелька."""
    q = (request.query.get("q") or "").strip()
    status = (request.query.get("status") or "").strip()
    kind = (request.query.get("kind") or "").strip()
    return web.Response(
        text=_admin_payments_html(
            await _admin_fetch_payments_data(q=q, status=status, kind=kind),
            q=q,
            status=status,
            kind=kind,
            notice_html=_admin_notice_html(status=request.query.get("status"), error=request.query.get("error")),
        ),
        content_type="text/html",
    )


async def handle_admin_devices_index(request: web.Request) -> web.Response:
    """Раздел админки с устройствами и подписками пользователей."""
    q = (request.query.get("q") or "").strip()
    status = (request.query.get("status") or "").strip()
    os_name = (request.query.get("os") or "").strip()
    backend_key = (request.query.get("backend") or "").strip()
    return web.Response(
        text=_admin_devices_html(
            await _admin_fetch_devices_data(
                q=q,
                status=status,
                os_name=os_name,
                backend_key=backend_key,
                threexui_registry=_threexui_registry(request.app),
                default_backend_key=_default_backend_key(request.app),
            ),
            q=q,
            status=status,
            os_name=os_name,
            backend_key=backend_key,
            redirect_query=request.query_string,
            notice_html=_admin_notice_html(status=request.query.get("status"), error=request.query.get("error")),
        ),
        content_type="text/html",
    )


async def handle_admin_users_index(request: web.Request) -> web.Response:
    """Раздел админки со списком пользователей."""
    q = (request.query.get("q") or "").strip()
    segment = (request.query.get("segment") or "").strip()
    return web.Response(
        text=_admin_users_html(
            await _admin_fetch_users_data(q=q, segment=segment),
            q=q,
            segment=segment,
            notice_html=_admin_notice_html(status=request.query.get("status"), error=request.query.get("error")),
        ),
        content_type="text/html",
    )


async def handle_admin_analytics_index(request: web.Request) -> web.Response:
    """Раздел админки с аналитикой продукта и монетизации."""
    days_raw = (request.query.get("days") or "14").strip()
    try:
        days = int(days_raw)
    except ValueError:
        days = 14
    days = max(7, min(days, 90))
    return web.Response(
        text=_admin_analytics_html(await _admin_fetch_analytics_data(days=days)),
        content_type="text/html",
    )


async def handle_admin_user_profile(request: web.Request) -> web.Response:
    """Профиль конкретного пользователя для админки."""
    try:
        user_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPSeeOther("/admin/users?error=user_not_found")
    profile = await _admin_fetch_user_profile(user_id)
    if not profile:
        raise web.HTTPSeeOther("/admin/users?error=user_not_found")
    return web.Response(
        text=_admin_user_profile_html(
            profile,
            notice_html=_admin_notice_html(status=request.query.get("status"), error=request.query.get("error")),
        ),
        content_type="text/html",
    )


async def handle_admin_user_balance_adjust(request: web.Request) -> web.Response:
    try:
        user_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPSeeOther("/admin/users?error=user_not_found")
    data = await request.post()
    try:
        amount = int(data.get("amount") or 0)
    except ValueError:
        amount = 0
    mode = (data.get("mode") or "credit").strip()
    description = (data.get("description") or "").strip() or "Ручная корректировка администратором"
    if amount <= 0 or mode not in {"credit", "debit"}:
        raise web.HTTPSeeOther(f"/admin/users/{user_id}?error=balance_invalid")
    delta = amount if mode == "credit" else -amount
    ok = await _admin_adjust_user_balance(user_id=user_id, delta=delta, description=description)
    if not ok:
        raise web.HTTPSeeOther(f"/admin/users/{user_id}?error=balance_invalid")
    raise web.HTTPSeeOther(f"/admin/users/{user_id}?status=user_balance_updated")


async def handle_admin_device_extend(request: web.Request) -> web.Response:
    try:
        subscription_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPSeeOther("/admin/devices?error=device_not_found")
    data = await request.post()
    redirect = (data.get("redirect") or "").strip()
    base = "/admin/devices" + (f"?{redirect}" if redirect else "")
    try:
        days = int(data.get("days") or 30)
    except ValueError:
        raise web.HTTPSeeOther(base + ("&" if redirect else "?") + "error=device_invalid")
    if days <= 0:
        raise web.HTTPSeeOther(base + ("&" if redirect else "?") + "error=device_invalid")
    try:
        ok = await _admin_extend_subscription_manual(
            subscription_id=subscription_id,
            days=days,
            threexui_registry=_threexui_registry(request.app),
            default_backend_key=_default_backend_key(request.app),
        )
    except Exception:
        ok = False
    if not ok:
        raise web.HTTPSeeOther(base + ("&" if redirect else "?") + "error=server")
    raise web.HTTPSeeOther(base + ("&" if redirect else "?") + "status=device_extended")


async def handle_admin_device_deactivate(request: web.Request) -> web.Response:
    try:
        subscription_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPSeeOther("/admin/devices?error=device_not_found")
    data = await request.post()
    redirect = (data.get("redirect") or "").strip()
    base = "/admin/devices" + (f"?{redirect}" if redirect else "")
    try:
        ok = await _admin_deactivate_subscription(subscription_id)
    except Exception:
        ok = False
    if not ok:
        raise web.HTTPSeeOther(base + ("&" if redirect else "?") + "error=server")
    raise web.HTTPSeeOther(base + ("&" if redirect else "?") + "status=device_deactivated")


async def handle_admin_page_tariffs_create(request: web.Request) -> web.Response:
    """Создать тариф из обычной HTML-формы без зависимости от Telegram WebApp JS."""
    data = await request.post()
    name = (data.get("name") or "").strip()
    months = data.get("months")
    price_stars = data.get("price_stars")
    if not name or months is None or price_stars is None:
        raise web.HTTPSeeOther("/admin/tariffs?error=required")
    try:
        traffic_gb = int(data.get("traffic_gb") or 0)
        sort_order = int(data.get("sort_order") or 0)
        await create_tariff(
            name=name,
            months=int(months),
            price_stars=int(price_stars),
            traffic_gb=traffic_gb,
            badge=((data.get("badge") or "").strip() or None),
            sort_order=sort_order,
        )
    except ValueError:
        raise web.HTTPSeeOther("/admin/tariffs?error=invalid")
    except Exception:
        raise web.HTTPSeeOther("/admin/tariffs?error=server")
    raise web.HTTPSeeOther("/admin/tariffs?status=created")


async def handle_admin_page_tariffs_delete(request: web.Request) -> web.Response:
    """Удалить тариф из обычной HTML-формы без зависимости от Telegram WebApp JS."""
    try:
        tariff_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPSeeOther("/admin/tariffs?error=not_found")
    try:
        deleted = await delete_tariff(tariff_id)
    except Exception:
        raise web.HTTPSeeOther("/admin/tariffs?error=server")
    if not deleted:
        raise web.HTTPSeeOther("/admin/tariffs?error=not_found")
    raise web.HTTPSeeOther("/admin/tariffs?status=deleted")


async def handle_index(request: web.Request) -> web.Response:
    """
    Основной WebApp: вкладки VPN и Кошелек.
    """
    html = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <title>raccaster_vpn</title>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <style>
    * { box-sizing: border-box; }
    :root {
      --surface: #1e293b;
      --surface-raised: #263244;
      --surface-alt: #0f172a;
      --surface-soft: rgba(15, 23, 42, 0.55);
      --surface-soft-border: rgba(148, 163, 184, 0.14);
      --modal-overlay: rgba(2, 6, 23, 0.82);
      --tabbar-bg: rgba(2, 6, 23, 0.95);
      --border: #334155;
      --accent: #38bdf8;
      --accent-soft: rgba(139, 92, 246, 0.18);
      --accent-strong: #c4b5fd;
      --shadow-soft: 0 10px 32px rgba(2, 6, 23, 0.18);
      --summary-start: #312e81;
      --summary-end: #0f4c81;
      --summary-text: #ffffff;
      --summary-muted: rgba(255,255,255,0.78);
      --summary-border: rgba(148, 163, 184, 0.2);
      --wallet-start: #083344;
      --wallet-end: #052e16;
      --wallet-border: rgba(56, 189, 248, 0.18);
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg, #0f172a);
      color: var(--text, #e5e7eb);
      margin: 0;
      padding: 16px;
      padding-bottom: calc(96px + env(safe-area-inset-bottom, 16px));
      min-height: 100vh;
    }
    .header {
      font-size: 24px;
      font-weight: 800;
      margin-bottom: 20px;
      letter-spacing: 0.02em;
    }
    .tab-page { display: none; }
    .tab-page.active { display: block; }
    .section-title { font-size: 15px; font-weight: 600; color: var(--hint, #94a3b8); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
    .summary-card {
      background: linear-gradient(135deg, var(--summary-start), var(--summary-end));
      border: 1px solid var(--summary-border);
      border-radius: 18px;
      padding: 18px;
      margin-bottom: 18px;
      color: var(--summary-text);
      box-shadow: var(--shadow-soft);
    }
    .summary-card .label { color: var(--summary-muted); font-size: 13px; margin-bottom: 8px; }
    .summary-card .value { font-size: 36px; font-weight: 700; margin-bottom: 6px; }
    .summary-card .meta { font-size: 14px; color: var(--summary-muted); }
    .vpn-actions { display: flex; gap: 10px; margin-bottom: 18px; }
    .vpn-actions button {
      flex: 1;
      border: none;
      border-radius: 14px;
      background: var(--button, #8b5cf6);
      color: var(--button-text, #fff);
      padding: 14px 16px;
      font-size: 15px;
      font-weight: 600;
      cursor: pointer;
    }
    .vpn-actions button.secondary {
      background: var(--surface);
      border: 1px solid var(--border, #334155);
      color: var(--text, #e5e7eb);
    }
    .device-chooser {
      display: none;
      background: var(--surface);
      border-radius: 16px;
      padding: 16px;
      border: 1px solid var(--border, #334155);
      margin-bottom: 18px;
      box-shadow: var(--shadow-soft);
    }
    .device-chooser.show { display: block; }
    .device-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    .device-chip, .topup-btn {
      border: 1px solid var(--border, #334155);
      background: var(--surface-alt);
      color: var(--text, #e5e7eb);
      border-radius: 14px;
      padding: 12px 10px;
      text-align: center;
      cursor: pointer;
      font-weight: 600;
    }
    .device-chip.active {
      background: var(--accent-soft);
      border-color: var(--accent-strong);
      color: var(--accent-strong);
    }
    .selected-device {
      display: none;
      margin-bottom: 16px;
      color: var(--hint, #94a3b8);
      font-size: 14px;
    }
    .selected-device.show { display: block; }
    .tariffs-modal {
      display: none;
      position: fixed;
      inset: 0;
      background: var(--modal-overlay);
      z-index: 120;
      padding: 16px;
      overflow-y: auto;
    }
    .tariffs-modal.show { display: block; }
    .tariffs-modal-card {
      max-width: 640px;
      margin: 20px auto 100px auto;
      background: var(--surface-raised);
      border: 1px solid var(--border, #334155);
      border-radius: 18px;
      padding: 16px;
      box-shadow: var(--shadow-soft);
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      margin-bottom: 12px;
    }
    .modal-close {
      border: 1px solid var(--border, #334155);
      background: var(--surface-alt);
      color: var(--text, #e5e7eb);
      border-radius: 12px;
      padding: 8px 12px;
      cursor: pointer;
    }
    .tariffs { display: flex; flex-direction: column; gap: 12px; margin-bottom: 24px; }
    .tariff-card {
      background: var(--surface);
      border-radius: 16px;
      padding: 16px;
      border: 1px solid var(--border, #334155);
      box-shadow: 0 2px 10px rgba(2, 6, 23, 0.04);
    }
    .tariff-card .name { font-size: 17px; font-weight: 600; margin-bottom: 4px; }
    .tariff-card .meta { font-size: 13px; color: var(--hint, #94a3b8); margin-bottom: 12px; }
    .tariff-card .price { font-size: 20px; font-weight: 700; color: var(--accent, #38bdf8); margin-bottom: 12px; }
    .tariff-card .price .badge { font-size: 11px; background: #10b981; color: #fff; padding: 2px 8px; border-radius: 6px; margin-left: 8px; vertical-align: middle; }
    .tariff-card button {
      width: 100%;
      border-radius: 12px;
      padding: 12px 16px;
      border: none;
      cursor: pointer;
      font-size: 15px;
      font-weight: 600;
      background: var(--button, #5288c1);
      color: var(--button-text, #fff);
    }
    .tariff-card button:disabled { opacity: 0.6; cursor: not-allowed; }
    .tariff-card .hint { font-size: 12px; color: var(--hint, #94a3b8); margin-top: 8px; }
    .configs { display: flex; flex-direction: column; gap: 12px; }
    .config-card {
      background: var(--surface);
      border-radius: 12px;
      padding: 14px;
      border: 1px solid var(--border, #334155);
      box-shadow: 0 2px 10px rgba(2, 6, 23, 0.04);
      cursor: pointer;
      transition: transform 0.15s ease, box-shadow 0.15s ease, border-color 0.15s ease;
    }
    .config-card:active { transform: scale(0.995); }
    .config-card:hover { border-color: var(--accent-strong); box-shadow: var(--shadow-soft); }
    .config-card .head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .config-card .label { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
    .config-card .open-hint { font-size: 18px; color: var(--hint, #94a3b8); line-height: 1; }
    .config-card .details { margin: 10px 0; display: grid; gap: 8px; }
    .config-card .detail-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-radius: 10px;
      background: var(--surface-soft);
      border: 1px solid var(--surface-soft-border);
    }
    .config-card .detail-label { font-size: 12px; color: var(--hint, #94a3b8); }
    .config-card .detail-value { font-size: 13px; font-weight: 700; color: var(--text, #f8fafc); }
    .config-card .config-preview { font-size: 11px; color: var(--hint); word-break: break-all; max-height: 40px; overflow: hidden; }
    .config-card .actions { margin-top: 10px; display: flex; gap: 8px; }
    .config-card .actions button {
      flex: 1;
      border-radius: 8px;
      padding: 8px 12px;
      border: none;
      cursor: pointer;
      font-size: 13px;
      font-weight: 500;
      background: var(--surface-alt);
      color: var(--text, #e5e7eb);
    }
    .config-card .actions button.primary { background: var(--button, #5288c1); color: var(--button-text, #fff); }
    .wallet-card {
      background: linear-gradient(135deg, var(--wallet-start), var(--wallet-end));
      border: 1px solid var(--wallet-border);
      border-radius: 18px;
      padding: 18px;
      margin-bottom: 18px;
      color: var(--summary-text);
      box-shadow: var(--shadow-soft);
    }
    .wallet-card .label { color: var(--summary-muted); font-size: 13px; margin-bottom: 8px; }
    .wallet-card .value { font-size: 34px; font-weight: 700; margin-top: 10px; }
    .wallet-note { color: var(--summary-muted); font-size: 13px; margin-bottom: 14px; }
    .topup-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 18px; }
    .history-list { display: flex; flex-direction: column; gap: 10px; }
    .history-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 14px;
      border-radius: 14px;
      background: var(--surface);
      border: 1px solid var(--border, #334155);
      box-shadow: 0 2px 10px rgba(2, 6, 23, 0.04);
    }
    .history-item .meta { font-size: 12px; color: var(--hint, #94a3b8); margin-top: 4px; }
    .history-item .amount { font-size: 15px; font-weight: 700; }
    .empty { text-align: center; color: var(--hint); font-size: 14px; padding: 24px; }
    .toast { position: fixed; bottom: 96px; left: 16px; right: 16px; background: var(--surface-raised); border: 1px solid var(--border, #334155); border-radius: 12px; padding: 12px 16px; text-align: center; font-size: 14px; box-shadow: var(--shadow-soft); z-index: 180; display: none; }
    .toast.show { display: block; }
    .loading { opacity: 0.7; pointer-events: none; }
    .device-detail-screen {
      display: none;
      position: fixed;
      inset: 0;
      z-index: 220;
      background: var(--bg, #0f172a);
      overflow-y: auto;
      padding: 18px 16px calc(110px + env(safe-area-inset-bottom, 0px));
    }
    .device-detail-screen.show { display: block; }
    .device-detail-shell { max-width: 720px; margin: 0 auto; }
    .device-detail-topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      margin-bottom: 18px;
    }
    .device-detail-btn {
      border: 1px solid var(--border, #334155);
      background: var(--surface);
      color: var(--text, #e5e7eb);
      border-radius: 12px;
      padding: 10px 14px;
      font-size: 14px;
      font-weight: 600;
      cursor: pointer;
    }
    .device-detail-hero {
      position: relative;
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      margin: 8px 0 18px;
      padding: 24px 18px 22px;
      border-radius: 28px;
      background:
        radial-gradient(circle at top, rgba(139, 92, 246, 0.28), transparent 42%),
        linear-gradient(180deg, var(--surface-raised), var(--surface));
      border: 1px solid var(--border, #334155);
      box-shadow: var(--shadow-soft);
      overflow: hidden;
    }
    .device-detail-hero::before {
      content: "";
      position: absolute;
      inset: -20% auto auto 50%;
      width: 240px;
      height: 240px;
      transform: translateX(-50%);
      background: radial-gradient(circle, rgba(56, 189, 248, 0.24), transparent 68%);
      pointer-events: none;
    }
    .device-detail-chip {
      position: relative;
      z-index: 1;
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 999px;
      background: rgba(15, 23, 42, 0.22);
      border: 1px solid rgba(255, 255, 255, 0.12);
      color: var(--text, #e5e7eb);
      font-size: 13px;
      font-weight: 700;
      margin-bottom: 14px;
      backdrop-filter: blur(10px);
    }
    .device-detail-chip-dot {
      width: 8px;
      height: 8px;
      border-radius: 999px;
      background: linear-gradient(135deg, #38bdf8, #8b5cf6);
      box-shadow: 0 0 12px rgba(139, 92, 246, 0.45);
    }
    .device-detail-platform {
      position: relative;
      z-index: 1;
      width: 86px;
      height: 86px;
      border-radius: 26px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(135deg, rgba(56, 189, 248, 0.24), rgba(139, 92, 246, 0.26));
      border: 1px solid rgba(255, 255, 255, 0.14);
      box-shadow: 0 16px 36px rgba(15, 23, 42, 0.22);
      margin-bottom: 14px;
      color: #fff;
    }
    .device-detail-platform svg,
    .device-app-icon svg {
      width: 40px;
      height: 40px;
      display: block;
    }
    .device-detail-check {
      position: relative;
      z-index: 1;
      width: 72px;
      height: 72px;
      border-radius: 999px;
      background: linear-gradient(135deg, #10b981, #14b8a6);
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 36px;
      font-weight: 800;
      box-shadow: var(--shadow-soft);
      margin-bottom: 14px;
    }
    .device-detail-check.hidden { display: none; }
    .device-detail-title { position: relative; z-index: 1; font-size: 30px; font-weight: 800; margin-bottom: 8px; }
    .device-detail-subtitle { position: relative; z-index: 1; font-size: 14px; color: var(--hint, #94a3b8); max-width: 540px; line-height: 1.55; }
    .device-app-card {
      display: flex;
      align-items: center;
      gap: 14px;
      background: linear-gradient(180deg, var(--surface-raised), var(--surface));
      border: 1px solid var(--border, #334155);
      border-radius: 20px;
      padding: 16px;
      margin-bottom: 16px;
      box-shadow: var(--shadow-soft);
    }
    .device-app-icon {
      width: 58px;
      height: 58px;
      min-width: 58px;
      border-radius: 18px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(135deg, rgba(56, 189, 248, 0.2), rgba(139, 92, 246, 0.24));
      color: #fff;
      border: 1px solid rgba(255, 255, 255, 0.12);
    }
    .device-app-meta { flex: 1; min-width: 0; }
    .device-app-label { font-size: 12px; color: var(--hint, #94a3b8); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.06em; }
    .device-app-name { font-size: 18px; font-weight: 800; margin-bottom: 4px; }
    .device-app-text { font-size: 14px; color: var(--hint, #94a3b8); line-height: 1.45; }
    .device-app-actions {
      display: grid;
      gap: 10px;
      min-width: 200px;
    }
    .device-app-action {
      width: auto;
      min-width: 176px;
      box-shadow: 0 12px 24px rgba(139, 92, 246, 0.18);
    }
    .device-summary {
      background: var(--surface);
      border: 1px solid var(--border, #334155);
      border-radius: 18px;
      overflow: hidden;
      margin-bottom: 16px;
      box-shadow: var(--shadow-soft);
    }
    .device-summary-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--surface-soft-border);
    }
    .device-summary-row:last-child { border-bottom: none; }
    .device-summary-label { font-size: 14px; color: var(--hint, #94a3b8); }
    .device-summary-value { font-size: 15px; font-weight: 700; color: var(--text, #e5e7eb); text-align: right; }
    .device-summary-value.status-active { color: #10b981; }
    .setup-steps {
      display: grid;
      gap: 12px;
      margin-bottom: 18px;
    }
    .setup-step {
      display: flex;
      gap: 14px;
      align-items: flex-start;
      background: linear-gradient(180deg, var(--surface-raised), var(--surface));
      border: 1px solid var(--border, #334155);
      border-radius: 20px;
      padding: 16px;
      box-shadow: var(--shadow-soft);
      position: relative;
      overflow: hidden;
    }
    .setup-step::after {
      content: "";
      position: absolute;
      inset: auto -30px -36px auto;
      width: 120px;
      height: 120px;
      background: radial-gradient(circle, rgba(139, 92, 246, 0.12), transparent 68%);
      pointer-events: none;
    }
    .setup-step-index {
      min-width: 30px;
      width: 30px;
      height: 30px;
      border-radius: 999px;
      background: linear-gradient(135deg, #8b5cf6, #38bdf8);
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 14px;
      font-weight: 800;
      box-shadow: 0 10px 18px rgba(139, 92, 246, 0.2);
    }
    .setup-step-body { position: relative; z-index: 1; flex: 1; }
    .setup-step-title { font-size: 16px; font-weight: 700; margin-bottom: 4px; }
    .setup-step-text { font-size: 14px; color: var(--hint, #94a3b8); line-height: 1.45; }
    .setup-step-action { margin-top: 10px; }
    .primary-action,
    .secondary-action {
      width: 100%;
      border: none;
      border-radius: 14px;
      padding: 14px 16px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
    }
    .primary-action {
      background: var(--button, #8b5cf6);
      color: var(--button-text, #fff);
    }
    .secondary-action {
      background: var(--surface);
      color: var(--text, #e5e7eb);
      border: 1px solid var(--border, #334155);
    }
    .device-key-label { font-size: 14px; font-weight: 700; margin-bottom: 10px; }
    .device-key-box {
      background: var(--surface);
      border: 1px solid var(--border, #334155);
      border-radius: 16px;
      padding: 14px;
      margin-bottom: 14px;
      box-shadow: var(--shadow-soft);
    }
    .device-key-value {
      font-size: 12px;
      line-height: 1.5;
      color: var(--text, #e5e7eb);
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      word-break: break-all;
      white-space: pre-wrap;
    }
    .device-detail-actions {
      display: grid;
      gap: 10px;
      margin-bottom: 18px;
    }
    @media (max-width: 640px) {
      .device-app-card {
        flex-direction: column;
        align-items: stretch;
      }
      .device-app-actions {
        width: 100%;
        min-width: 0;
      }
      .device-app-action {
        width: 100%;
        min-width: 0;
      }
      .device-detail-title {
        font-size: 26px;
      }
    }
    .bottom-tabs {
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0;
      padding: 8px 12px calc(8px + env(safe-area-inset-bottom, 0px));
      background: var(--tabbar-bg);
      border-top: 1px solid var(--border, #334155);
      backdrop-filter: blur(10px);
      box-shadow: 0 -6px 24px rgba(2, 6, 23, 0.08);
    }
    .tab-btn {
      border: none;
      background: transparent;
      color: var(--hint, #94a3b8);
      padding: 10px 12px;
      font-size: 14px;
      font-weight: 600;
      border-radius: 12px;
      cursor: pointer;
    }
    .tab-btn.active {
      color: var(--accent-strong);
      background: var(--accent-soft);
    }
  </style>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body>
  <div class="header">Raccaster VPN</div>
  <div id="admin-link-wrap" style="display:none; margin-bottom: 16px;">
    <a href="/admin" id="admin-link" style="display:inline-block; padding: 10px 16px; background: var(--surface); color: var(--text, #e5e7eb); border: 1px solid var(--border, #334155); border-radius: 12px; text-decoration: none; font-size: 14px;">⚙ Админ-панель</a>
  </div>

  <div class="tab-page active" id="tab-vpn">
    <div class="summary-card">
      <div class="label">VPN баланс</div>
      <div class="value" id="vpn-balance-main">0 ⭐</div>
      <div class="meta" id="vpn-device-meta">Нет активных устройств</div>
    </div>

    <div class="vpn-actions">
      <button type="button" id="btn-add-device">Добавить устройство</button>
      <button type="button" class="secondary" id="btn-open-wallet">Открыть кошелек</button>
    </div>

    <div class="device-chooser" id="device-chooser">
      <div class="section-title" style="margin-bottom:10px;">Выберите устройство</div>
      <div class="device-grid">
        <button type="button" class="device-chip" data-os="Windows">Windows</button>
        <button type="button" class="device-chip" data-os="iOS">iOS</button>
        <button type="button" class="device-chip" data-os="Android">Android</button>
      </div>
    </div>

    <div class="section-title">Мои устройства</div>
    <div class="configs" id="configs"></div>
    <div class="empty" id="configs-empty" style="display:none;">Нет активных устройств. Нажмите «Добавить устройство», выберите платформу и купите тариф.</div>
  </div>

  <div class="tariffs-modal" id="tariffs-modal">
    <div class="tariffs-modal-card">
      <div class="modal-header">
        <div>
          <div class="section-title" style="margin-bottom:6px;">Тарифы VPN</div>
          <div id="modal-device-title" style="color:var(--hint,#94a3b8);font-size:14px;">Выберите тариф</div>
        </div>
        <button type="button" class="modal-close" id="btn-close-tariffs">Выйти</button>
      </div>
      <div class="tariffs" id="tariffs"></div>
    </div>
  </div>

  <div class="tab-page" id="tab-wallet">
    <div class="wallet-card">
      <div class="label">VPN баланс</div>
      <div class="value" id="wallet-balance">0 ⭐</div>
      <div class="wallet-note" id="wallet-rate-note">Пополните VPN-баланс и оплатите тариф Stars напрямую внутри Telegram.</div>
      <div class="topup-grid">
        <button type="button" class="topup-btn" data-stars="1">1 ⭐</button>
        <button type="button" class="topup-btn" data-stars="55">55 ⭐</button>
        <button type="button" class="topup-btn" data-stars="100">100 ⭐</button>
        <button type="button" class="topup-btn" data-stars="140">140 ⭐</button>
        <button type="button" class="topup-btn" data-stars="250">250 ⭐</button>
        <button type="button" class="topup-btn" data-stars="500">500 ⭐</button>
      </div>
    </div>

    <div class="section-title">История операций</div>
    <div class="history-list" id="wallet-history"></div>
    <div class="empty" id="wallet-empty" style="display:none;">Операций пока нет.</div>
  </div>

  <div class="device-detail-screen" id="device-detail-screen">
    <div class="device-detail-shell">
      <div class="device-detail-topbar">
        <button type="button" class="device-detail-btn" id="device-detail-back">Назад</button>
        <button type="button" class="device-detail-btn" id="device-detail-home">На главную</button>
      </div>
      <div class="device-detail-hero">
        <div class="device-detail-chip" id="device-detail-chip"><span class="device-detail-chip-dot"></span><span id="device-detail-chip-text">Устройство</span></div>
        <div class="device-detail-platform" id="device-detail-platform-icon"></div>
        <div class="device-detail-check" id="device-detail-check">✓</div>
        <div class="device-detail-title" id="device-detail-title">Устройство готово</div>
        <div class="device-detail-subtitle" id="device-detail-subtitle">Настройте приложение, вставьте ключ и подключитесь.</div>
      </div>
      <div class="device-app-card">
        <div class="device-app-icon" id="device-app-icon"></div>
        <div class="device-app-meta">
          <div class="device-app-label">Рекомендуемое приложение</div>
          <div class="device-app-name" id="device-app-name">VPN client</div>
          <div class="device-app-text" id="device-app-text">Установите приложение и импортируйте ключ в пару касаний.</div>
        </div>
        <div class="device-app-actions" id="device-app-actions"></div>
      </div>
      <div class="device-summary" id="device-detail-summary"></div>
      <div class="setup-steps" id="device-detail-steps"></div>
      <div class="device-key-label">Ваш ключ подключения</div>
      <div class="device-key-box">
        <div class="device-key-value" id="device-detail-config"></div>
      </div>
      <div class="device-detail-actions">
        <button type="button" class="primary-action" id="device-detail-copy">Скопировать ключ</button>
        <button type="button" class="secondary-action" id="device-detail-close">Закрыть</button>
      </div>
    </div>
  </div>

  <div class="toast" id="toast"></div>

  <div class="bottom-tabs">
    <button type="button" class="tab-btn active" data-tab="vpn">VPN</button>
    <button type="button" class="tab-btn" data-tab="wallet">Кошелек</button>
  </div>

  <script>
    const tg = window.Telegram && window.Telegram.WebApp;

    function parseHexColor(value) {
      if (!value || typeof value !== "string") return null;
      var hex = value.trim();
      if (!hex) return null;
      if (hex[0] === "#") hex = hex.slice(1);
      if (hex.length === 3) hex = hex.split("").map(function(ch) { return ch + ch; }).join("");
      if (hex.length !== 6) return null;
      var num = parseInt(hex, 16);
      if (isNaN(num)) return null;
      return {
        r: (num >> 16) & 255,
        g: (num >> 8) & 255,
        b: num & 255
      };
    }

    function mixColor(colorA, colorB, ratio) {
      var a = parseHexColor(colorA);
      var b = parseHexColor(colorB);
      if (!a || !b) return colorA || colorB || "#ffffff";
      var t = Math.max(0, Math.min(1, ratio));
      var r = Math.round(a.r + (b.r - a.r) * t);
      var g = Math.round(a.g + (b.g - a.g) * t);
      var bCh = Math.round(a.b + (b.b - a.b) * t);
      return "#" + [r, g, bCh].map(function(v) { return v.toString(16).padStart(2, "0"); }).join("");
    }

    function luminance(color) {
      var rgb = parseHexColor(color);
      if (!rgb) return 0;
      return (0.299 * rgb.r + 0.587 * rgb.g + 0.114 * rgb.b) / 255;
    }

    function setThemeVar(name, value) {
      if (value) document.documentElement.style.setProperty(name, value);
    }

    function applyTelegramTheme(th) {
      th = th || {};
      if (th.bg_color) document.documentElement.style.setProperty("--bg", th.bg_color);
      if (th.text_color) document.documentElement.style.setProperty("--text", th.text_color);
      if (th.hint_color) document.documentElement.style.setProperty("--hint", th.hint_color);
      if (th.button_color) document.documentElement.style.setProperty("--button", th.button_color);
      if (th.button_text_color) document.documentElement.style.setProperty("--button-text", th.button_text_color);
      if (th.secondary_bg_color) {
        document.documentElement.style.setProperty("--card-bg", th.secondary_bg_color);
        document.documentElement.style.setProperty("--surface", th.secondary_bg_color);
      }
      var bg = th.bg_color || "#0f172a";
      var secondary = th.secondary_bg_color || mixColor(bg, "#ffffff", 0.08);
      var accent = th.button_color || "#8b5cf6";
      var isLight = luminance(bg) > 0.72;
      setThemeVar("--accent", accent);
      if (isLight) {
        setThemeVar("--surface", secondary);
        setThemeVar("--surface-raised", mixColor(secondary, "#ffffff", 0.3));
        setThemeVar("--surface-alt", mixColor(secondary, "#dbe4f0", 0.42));
        setThemeVar("--surface-soft", mixColor(secondary, "#dbe4f0", 0.58));
        setThemeVar("--surface-soft-border", "rgba(100, 116, 139, 0.18)");
        setThemeVar("--border", mixColor(secondary, "#94a3b8", 0.46));
        setThemeVar("--modal-overlay", "rgba(15, 23, 42, 0.24)");
        setThemeVar("--tabbar-bg", "rgba(248, 250, 252, 0.94)");
        setThemeVar("--accent-soft", mixColor(bg, accent, 0.18));
        setThemeVar("--accent-strong", mixColor(accent, "#4c1d95", 0.28));
        setThemeVar("--shadow-soft", "0 10px 32px rgba(15, 23, 42, 0.10)");
        setThemeVar("--summary-start", mixColor("#ffffff", "#8b5cf6", 0.2));
        setThemeVar("--summary-end", mixColor("#ffffff", "#0ea5e9", 0.24));
        setThemeVar("--summary-text", th.text_color || "#0f172a");
        setThemeVar("--summary-muted", mixColor(th.text_color || "#0f172a", "#ffffff", 0.35));
        setThemeVar("--summary-border", mixColor("#ffffff", accent, 0.18));
        setThemeVar("--wallet-start", mixColor("#ffffff", "#14b8a6", 0.18));
        setThemeVar("--wallet-end", mixColor("#ffffff", "#22c55e", 0.2));
        setThemeVar("--wallet-border", mixColor("#ffffff", "#14b8a6", 0.2));
      } else {
        setThemeVar("--surface", secondary);
        setThemeVar("--surface-raised", mixColor(secondary, "#334155", 0.18));
        setThemeVar("--surface-alt", mixColor(bg, "#020617", 0.35));
        setThemeVar("--surface-soft", "rgba(15, 23, 42, 0.55)");
        setThemeVar("--surface-soft-border", "rgba(148, 163, 184, 0.14)");
        setThemeVar("--border", mixColor(secondary, "#94a3b8", 0.22));
        setThemeVar("--modal-overlay", "rgba(2, 6, 23, 0.82)");
        setThemeVar("--tabbar-bg", "rgba(2, 6, 23, 0.95)");
        setThemeVar("--accent-soft", mixColor(bg, accent, 0.2));
        setThemeVar("--accent-strong", mixColor(accent, "#ffffff", 0.35));
        setThemeVar("--shadow-soft", "0 10px 32px rgba(2, 6, 23, 0.28)");
        setThemeVar("--summary-border", "rgba(148, 163, 184, 0.2)");
        setThemeVar("--wallet-border", "rgba(56, 189, 248, 0.18)");
      }
    }

    if (tg) {
      tg.expand();
      tg.MainButton.hide();
      applyTelegramTheme(tg.themeParams || {});
      if (typeof tg.onEvent === "function") {
        tg.onEvent("themeChanged", function() {
          applyTelegramTheme(tg.themeParams || {});
        });
      }
    }

    const user = tg && tg.initDataUnsafe && tg.initDataUnsafe.user;
    const telegramId = user ? user.id : null;
    let selectedDeviceOs = null;
    let currentBalance = 0;
    let tariffsModalMode = "buy";
    let renewalSubscriptionId = null;

    function toast(msg) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.classList.add("show");
      setTimeout(function() { el.classList.remove("show"); }, 2500);
    }

    function apiPost(path, payload) {
      return fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload || {})
      }).then(function(r) {
        return r.json().catch(function() { return { ok: false, error: "Bad response" }; })
          .then(function(data) { data.__httpOk = r.ok; return data; });
      });
    }

    function payloadBase(extra) {
      var payload = { telegram_id: telegramId };
      if (user) {
        payload.username = user.username;
        payload.first_name = user.first_name;
        payload.last_name = user.last_name;
      }
      for (var key in (extra || {})) payload[key] = extra[key];
      return payload;
    }

    function setTab(name) {
      document.querySelectorAll(".tab-page").forEach(function(el) {
        el.classList.toggle("active", el.id === "tab-" + name);
      });
      document.querySelectorAll(".tab-btn").forEach(function(el) {
        el.classList.toggle("active", el.dataset.tab === name);
      });
    }

    function escapeHtml(value) {
      return String(value == null ? "" : value)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function formatDateRu(value) {
      if (!value) return "Без срока";
      var date = new Date(value);
      if (isNaN(date.getTime())) return "Без срока";
      return "До " + date.toLocaleDateString("ru");
    }

    function openExternalLink(url) {
      if (!url) return;
      if (tg && typeof tg.openLink === "function") {
        tg.openLink(url);
      } else {
        window.open(url, "_blank", "noopener,noreferrer");
      }
    }

    function setSelectedDevice(osName) {
      selectedDeviceOs = osName || null;
      var modalTitle = document.getElementById("modal-device-title");
      if (modalTitle) modalTitle.textContent = selectedDeviceOs ? ("Устройство: " + selectedDeviceOs) : "Выберите тариф";
      document.querySelectorAll(".device-chip").forEach(function(btn) {
        btn.classList.toggle("active", btn.dataset.os === selectedDeviceOs);
      });
      document.querySelectorAll(".tariff-card button").forEach(function(btn) {
        btn.textContent = tariffsModalMode === "renew" ? "Продлить" : "Купить";
      });
    }

    function updateBalanceViews(balance, deviceCount) {
      currentBalance = balance || 0;
      document.getElementById("vpn-balance-main").textContent = currentBalance + " ⭐";
      document.getElementById("wallet-balance").textContent = currentBalance + " ⭐";
      document.getElementById("vpn-device-meta").textContent = deviceCount > 0
        ? "Активных устройств: " + deviceCount
        : "Нет активных устройств";
    }

    function tariffRubLabel(tariff) {
      var byMonths = {
        1: "100 ₽",
        2: "182 ₽",
        3: "255 ₽",
        6: "455 ₽",
        12: "909 ₽"
      };
      var months = parseInt(tariff.months, 10);
      return byMonths[months] || "";
    }

    function tariffTitle(tariff) {
      var rub = tariffRubLabel(tariff);
      return rub ? (tariff.name + " · " + rub) : tariff.name;
    }

    function renderTariffs(tariffs) {
      const root = document.getElementById("tariffs");
      root.innerHTML = tariffs.map(function(t) {
        const badge = t.badge ? '<span class="badge">' + t.badge + '</span>' : '';
        const buttonLabel = tariffsModalMode === "renew" ? "Продлить" : "Купить";
        return '<div class="tariff-card">' +
          '<div class="name">' + tariffTitle(t) + '</div>' +
          '<div class="meta">' + (t.duration_label || t.name) + ' · ' + t.traffic_gb + ' GB трафика</div>' +
          '<div class="price">' + t.price_stars + ' ⭐' + badge + '</div>' +
          '<button type="button" data-tariff-id="' + t.id + '">' + buttonLabel + '</button>' +
          '<div class="hint">Списывается со Stars-баланса</div>' +
          '</div>';
      }).join("");
      root.querySelectorAll("button").forEach(function(btn) {
        btn.addEventListener("click", function() {
          if (tariffsModalMode === "renew") {
            renewSubscription(btn, renewalSubscriptionId, btn.dataset.tariffId);
          } else {
            buyTariff(btn, btn.dataset.tariffId);
          }
        });
      });
    }

    var configsById = {};
    var currentDetailConfigId = null;
    var DEVICE_GUIDES = {
      "Windows": {
        appName: "Hiddify Next",
        actionLabel: "Открыть загрузки Hiddify",
        actionUrl: "https://github.com/hiddify/hiddify-next/releases",
        links: [
          { label: "Открыть загрузки Hiddify", url: "https://github.com/hiddify/hiddify-next/releases", kind: "primary" }
        ],
        steps: [
          { title: "Скачайте клиент", text: "Откройте страницу загрузки и установите Hiddify Next для Windows." },
          { title: "Скопируйте ключ", text: "Скопируйте ключ подключения ниже в один тап." },
          { title: "Импортируйте профиль", text: "Откройте приложение и вставьте ключ или импортируйте его из буфера обмена." },
          { title: "Подключитесь", text: "Активируйте подключение в приложении. После этого устройство готово к работе." }
        ]
      },
      "Android": {
        appName: "Hiddify Next",
        actionLabel: "Открыть Google Play",
        actionUrl: "https://play.google.com/store/apps/details?id=app.hiddify.com&hl=ru",
        links: [
          { label: "Открыть Google Play", url: "https://play.google.com/store/apps/details?id=app.hiddify.com&hl=ru", kind: "primary" },
          { label: "Скачать APK / Releases", url: "https://github.com/hiddify/hiddify-next/releases", kind: "secondary" }
        ],
        steps: [
          { title: "Скачайте клиент", text: "Установите Hiddify Next из Google Play или через страницу релизов, если это удобнее." },
          { title: "Скопируйте ключ", text: "Скопируйте ключ подключения ниже." },
          { title: "Добавьте профиль", text: "Откройте приложение и импортируйте конфигурацию из буфера обмена." },
          { title: "Подключитесь", text: "Разрешите создание VPN-подключения и включите профиль." }
        ]
      },
      "iOS": {
        appName: "v2RayTun",
        actionLabel: "Открыть App Store",
        actionUrl: "https://apps.apple.com/ru/app/v2raytun/id6476628951",
        links: [
          { label: "Открыть App Store", url: "https://apps.apple.com/ru/app/v2raytun/id6476628951", kind: "primary" }
        ],
        steps: [
          { title: "Установите приложение", text: "Откройте App Store и установите v2RayTun или другой совместимый клиент." },
          { title: "Скопируйте ключ", text: "Скопируйте ваш ключ подключения ниже." },
          { title: "Добавьте профиль", text: "Откройте приложение и вставьте ключ в новый профиль." },
          { title: "Подключитесь", text: "Разрешите VPN-конфигурацию и включите профиль." }
        ]
      }
    };

    function getDeviceGuide(osName) {
      return DEVICE_GUIDES[osName] || {
        appName: "совместимый VPN-клиент",
        actionLabel: "",
        actionUrl: "",
        links: [],
        steps: [
          { title: "Установите приложение", text: "Используйте совместимый клиент для вашего устройства." },
          { title: "Скопируйте ключ", text: "Скопируйте ключ подключения ниже." },
          { title: "Добавьте профиль", text: "Импортируйте конфигурацию из буфера обмена." },
          { title: "Подключитесь", text: "Включите созданный профиль в приложении." }
        ]
      };
    }

    function getPlatformIcon(osName) {
      if (osName === "Windows") {
        return '<svg viewBox="0 0 48 48" fill="none" aria-hidden="true"><rect x="6" y="8" width="16" height="14" rx="2" fill="currentColor"></rect><rect x="26" y="8" width="16" height="14" rx="2" fill="currentColor" opacity="0.92"></rect><rect x="6" y="26" width="16" height="14" rx="2" fill="currentColor" opacity="0.92"></rect><rect x="26" y="26" width="16" height="14" rx="2" fill="currentColor"></rect></svg>';
      }
      if (osName === "Android") {
        return '<svg viewBox="0 0 48 48" fill="none" aria-hidden="true"><rect x="12" y="16" width="24" height="18" rx="7" fill="currentColor"></rect><rect x="15" y="34" width="4" height="8" rx="2" fill="currentColor"></rect><rect x="29" y="34" width="4" height="8" rx="2" fill="currentColor"></rect><circle cx="19" cy="22" r="1.8" fill="#0f172a"></circle><circle cx="29" cy="22" r="1.8" fill="#0f172a"></circle><path d="M18 12L14.5 8.5" stroke="currentColor" stroke-width="3" stroke-linecap="round"></path><path d="M30 12L33.5 8.5" stroke="currentColor" stroke-width="3" stroke-linecap="round"></path></svg>';
      }
      if (osName === "iOS") {
        return '<svg viewBox="0 0 48 48" fill="none" aria-hidden="true"><rect x="14" y="6" width="20" height="36" rx="6" stroke="currentColor" stroke-width="4"></rect><rect x="20" y="10" width="8" height="3" rx="1.5" fill="currentColor"></rect><circle cx="24" cy="35" r="2.5" fill="currentColor"></circle></svg>';
      }
      return '<svg viewBox="0 0 48 48" fill="none" aria-hidden="true"><rect x="10" y="10" width="28" height="28" rx="8" stroke="currentColor" stroke-width="4"></rect><path d="M18 24H30" stroke="currentColor" stroke-width="4" stroke-linecap="round"></path><path d="M24 18V30" stroke="currentColor" stroke-width="4" stroke-linecap="round"></path></svg>';
    }

    function copyConfigText(configValue) {
      if (!configValue) return;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(configValue)
          .then(function() { toast("Ключ скопирован"); })
          .catch(function() { toast("Скопируйте ключ вручную"); });
      } else {
        toast("Скопируйте ключ вручную");
      }
    }

    function hideDeviceDetail() {
      document.getElementById("device-detail-screen").classList.remove("show");
      currentDetailConfigId = null;
    }

    function showDeviceDetail(configId, options) {
      options = options || {};
      var config = typeof configId === "object" ? configId : configsById[configId];
      if (!config) return;
      currentDetailConfigId = config.id;
      var guide = getDeviceGuide(config.device_os);
      var isPurchased = !!options.justPurchased;
      var title = isPurchased ? "Устройство добавлено" : (config.server_label || config.device_os || "Устройство");
      var subtitle = isPurchased
        ? "Осталось скачать приложение, вставить ключ и подключиться."
        : "Все, что нужно для быстрого подключения на этом устройстве.";
      var summaryHtml = [
        '<div class="device-summary-row"><span class="device-summary-label">Статус</span><span class="device-summary-value status-active">Активно</span></div>',
        '<div class="device-summary-row"><span class="device-summary-label">Платформа</span><span class="device-summary-value">' + escapeHtml(config.device_os || "Устройство") + '</span></div>',
        '<div class="device-summary-row"><span class="device-summary-label">Действует до</span><span class="device-summary-value">' + escapeHtml(formatDateRu(config.expires_at)) + '</span></div>',
        '<div class="device-summary-row"><span class="device-summary-label">Осталось трафика</span><span class="device-summary-value">' + escapeHtml(config.traffic_value || "Неизвестно") + '</span></div>'
      ].join("");
      var guideLinks = Array.isArray(guide.links) && guide.links.length ? guide.links : (guide.actionUrl ? [{ label: guide.actionLabel || "Открыть", url: guide.actionUrl, kind: "primary" }] : []);
      var stepsHtml = guide.steps.map(function(step, idx) {
        var actionHtml = "";
        if (idx === 0 && guideLinks.length > 0) {
          actionHtml = '<div class="setup-step-action"><button type="button" class="device-detail-btn device-guide-link" data-url="' + escapeHtml(guideLinks[0].url) + '">' + escapeHtml(guideLinks[0].label) + '</button></div>';
        }
        return '<div class="setup-step">' +
          '<div class="setup-step-index">' + (idx + 1) + '</div>' +
          '<div class="setup-step-body">' +
          '<div class="setup-step-title">' + escapeHtml(step.title) + '</div>' +
          '<div class="setup-step-text">' + escapeHtml(step.text) + '</div>' +
          actionHtml +
          '</div>' +
          '</div>';
      }).join("");
      document.getElementById("device-detail-chip-text").textContent = (config.device_os || "Устройство") + " · " + guide.appName;
      document.getElementById("device-detail-platform-icon").innerHTML = getPlatformIcon(config.device_os);
      document.getElementById("device-app-icon").innerHTML = getPlatformIcon(config.device_os);
      document.getElementById("device-app-name").textContent = guide.appName;
      document.getElementById("device-app-text").textContent = guideLinks.length > 0
        ? "Установите приложение и импортируйте ключ подключения за пару шагов."
        : "Используйте совместимый клиент и импортируйте ключ подключения вручную.";
      document.getElementById("device-app-actions").innerHTML = guideLinks.map(function(link) {
        var btnClass = link.kind === "secondary" ? "secondary-action" : "primary-action";
        return '<button type="button" class="' + btnClass + ' device-app-action device-guide-link" data-url="' + escapeHtml(link.url) + '">' + escapeHtml(link.label) + '</button>';
      }).join("");
      document.getElementById("device-detail-title").textContent = title;
      document.getElementById("device-detail-subtitle").textContent = subtitle;
      document.getElementById("device-detail-summary").innerHTML = summaryHtml;
      document.getElementById("device-detail-steps").innerHTML = stepsHtml;
      document.getElementById("device-detail-config").textContent = config.config || "";
      document.getElementById("device-detail-check").classList.toggle("hidden", !isPurchased);
      document.getElementById("device-detail-screen").classList.add("show");
      document.querySelectorAll(".device-guide-link").forEach(function(btn) {
        btn.addEventListener("click", function() {
          openExternalLink(btn.dataset.url);
        });
      });
    }

    function renderConfigs(configs) {
      const root = document.getElementById("configs");
      const empty = document.getElementById("configs-empty");
      configsById = {};
      if (!configs || configs.length === 0) {
        root.innerHTML = "";
        empty.style.display = "block";
        return;
      }
      empty.style.display = "none";
      configs.forEach(function(c) { configsById[c.id] = c; });
      root.innerHTML = configs.map(function(c) {
        const exp = formatDateRu(c.expires_at);
        const preview = escapeHtml((c.config || "").substring(0, 60) + (c.config && c.config.length > 60 ? "…" : ""));
        const os = c.device_os ? c.device_os + " · " : "";
        var renewBtn = '<button type="button" class="renew-btn">Продлить</button>';
        return '<div class="config-card" data-id="' + c.id + '" data-renew-price="' + (c.renew_price_stars || '') + '" data-renew-months="' + (c.renew_months || '') + '">' +
          '<div class="head"><div class="label">Конфиг · ' + escapeHtml(os + (c.server_label || ("#" + c.id))) + '</div><div class="open-hint">›</div></div>' +
          '<div class="details">' +
          '<div class="detail-row"><span class="detail-label">Действует до</span><span class="detail-value">' + escapeHtml(exp) + '</span></div>' +
          '<div class="detail-row"><span class="detail-label">Осталось трафика</span><span class="detail-value">' + escapeHtml(c.traffic_value || "Неизвестно") + '</span></div>' +
          '</div>' +
          '<div class="config-preview">' + preview + '</div>' +
          '<div class="actions">' +
          '<button type="button" class="primary copy-btn">Скопировать</button>' +
          renewBtn +
          '</div></div>';
      }).join("");
      root.querySelectorAll(".config-card").forEach(function(card) {
        card.addEventListener("click", function() {
          const id = parseInt(card.getAttribute("data-id"), 10);
          showDeviceDetail(id);
        });
      });
      root.querySelectorAll(".copy-btn").forEach(function(btn) {
        btn.addEventListener("click", function(event) {
          event.stopPropagation();
          const id = parseInt(btn.closest(".config-card").getAttribute("data-id"), 10);
          const config = configsById[id] && configsById[id].config;
          if (!config) return;
          copyConfigText(config);
        });
      });
      root.querySelectorAll(".renew-btn").forEach(function(btn) {
        btn.addEventListener("click", function(event) {
          event.stopPropagation();
          const card = btn.closest(".config-card");
          renewalSubscriptionId = parseInt(card.getAttribute("data-id"), 10);
          tariffsModalMode = "renew";
          var modalTitle = document.getElementById("modal-device-title");
          if (modalTitle) modalTitle.textContent = "Продление конфигурации";
          showTariffsModal();
        });
      });
    }

    function loadTariffs() {
      fetch("/api/tariffs")
        .then(function(r) { return r.json(); })
        .then(function(data) { if (data.ok && data.tariffs) renderTariffs(data.tariffs); });
    }

    function loadDashboard() {
      if (!telegramId) {
        updateBalanceViews(0, 0);
        return;
      }
      apiPost("/api/dashboard", payloadBase())
        .then(function(data) {
          if (data.ok) updateBalanceViews(data.vpn_balance_stars || 0, data.device_count || 0);
        });
    }

    function checkAdminLink() {
      if (!telegramId) return;
      fetch("/api/admin/me", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ telegram_id: telegramId }) })
        .then(function(r) { return r.ok ? r.json() : null; })
        .then(function(d) { if (d && d.ok && d.is_admin) { var w = document.getElementById("admin-link-wrap"); if (w) w.style.display = "block"; } })
        .catch(function() {});
    }

    function loadConfigs() {
      if (!telegramId) { renderConfigs([]); return Promise.resolve([]); }
      return apiPost("/api/my-configs", payloadBase())
        .then(function(data) {
          var configs = data.ok ? data.configs : [];
          renderConfigs(configs);
          return configs;
        });
    }

    function renderWallet(transactions) {
      const root = document.getElementById("wallet-history");
      const empty = document.getElementById("wallet-empty");
      if (!transactions || transactions.length === 0) {
        root.innerHTML = "";
        empty.style.display = "block";
        return;
      }
      empty.style.display = "none";
      root.innerHTML = transactions.map(function(tx) {
        const amount = (tx.amount > 0 ? "+" + tx.amount : String(tx.amount)) + " ⭐";
        const created = tx.created_at ? new Date(tx.created_at).toLocaleString("ru") : "";
        const providerMeta = tx.provider_amount && tx.provider_currency ? " · " + tx.provider_amount + " " + tx.provider_currency : "";
        return '<div class="history-item">' +
          '<div><div>' + (tx.description || tx.kind) + '</div><div class="meta">' + created + providerMeta + '</div></div>' +
          '<div class="amount">' + amount + '</div>' +
          '</div>';
      }).join("");
    }

    function loadWallet() {
      if (!telegramId) {
        renderWallet([]);
        return;
      }
      apiPost("/api/wallet", payloadBase())
        .then(function(data) {
          if (!data.ok) return;
          document.getElementById("wallet-balance").textContent = (data.balance_stars || 0) + " ⭐";
          renderWallet(data.transactions || []);
        });
    }

    function buyTariff(btn, tariffId) {
      if (!telegramId) { toast("Ошибка: нет данных пользователя"); return; }
      if (!selectedDeviceOs) {
        document.getElementById("device-chooser").classList.add("show");
        toast("Сначала выберите устройство");
        return;
      }
      btn.disabled = true;
      btn.closest(".tariffs").classList.add("loading");
      apiPost("/api/purchase-tariff", payloadBase({ tariff_id: parseInt(tariffId, 10), device_os: selectedDeviceOs }))
        .then(function(data) {
          btn.closest(".tariffs").classList.remove("loading");
          btn.disabled = false;
          if (data.ok) {
            toast("Устройство добавлено");
            hideTariffsModal();
            loadDashboard();
            loadWallet();
            loadConfigs().then(function() {
              if (data.subscription && data.subscription.id) {
                showDeviceDetail(data.subscription.id, { justPurchased: true });
              }
            });
          } else {
            toast(data.error || "Ошибка");
          }
        })
        .catch(function() {
          btn.closest(".tariffs").classList.remove("loading");
          btn.disabled = false;
          toast("Ошибка сети");
        });
    }

    function renewSubscription(btn, subscriptionId, tariffId) {
      if (!telegramId) { toast("Ошибка: нет данных пользователя"); return; }
      btn.disabled = true;
      apiPost("/api/subscriptions/extend", payloadBase({ subscription_id: subscriptionId, tariff_id: parseInt(tariffId, 10) }))
        .then(function(data) {
          btn.disabled = false;
          if (data.ok) {
            toast("Подписка продлена");
            hideTariffsModal();
            loadDashboard();
            loadWallet();
            loadConfigs();
          } else {
            toast(data.error || "Не удалось продлить");
          }
        })
        .catch(function() {
          btn.disabled = false;
          toast("Ошибка сети");
        });
    }

    function createInvoice(amountStars) {
      if (!telegramId) { toast("Ошибка: нет данных пользователя"); return; }
      apiPost("/api/wallet/create-invoice", payloadBase({ amount_stars: amountStars }))
        .then(function(data) {
          if (!data.ok || !data.invoice_link) {
            toast(data.error || "Не удалось создать счет");
            return;
          }
          if (tg && typeof tg.openInvoice === "function") {
            tg.openInvoice(data.invoice_link, function(status) {
              if (status === "paid") {
                toast("Платеж успешно завершен");
                setTimeout(function() {
                  loadDashboard();
                  loadWallet();
                }, 1200);
              } else if (status === "cancelled") {
                if (data.payload) {
                  apiPost("/api/wallet/cancel-invoice", { payload: data.payload });
                }
                toast("Платеж отменен");
              } else if (status === "failed") {
                if (data.payload) {
                  apiPost("/api/wallet/cancel-invoice", { payload: data.payload });
                }
                toast("Платеж не прошел");
              }
            });
          } else {
            window.location.href = data.invoice_link;
          }
        })
        .catch(function() { toast("Ошибка сети"); });
    }

    document.querySelectorAll(".tab-btn").forEach(function(btn) {
      btn.addEventListener("click", function() { setTab(btn.dataset.tab); });
    });
    function showTariffsModal() {
      document.getElementById("tariffs-modal").classList.add("show");
      loadTariffs();
    }
    function hideTariffsModal() {
      document.getElementById("tariffs-modal").classList.remove("show");
      tariffsModalMode = "buy";
      renewalSubscriptionId = null;
      var modalTitle = document.getElementById("modal-device-title");
      if (modalTitle) modalTitle.textContent = selectedDeviceOs ? ("Устройство: " + selectedDeviceOs) : "Выберите тариф";
    }

    document.getElementById("btn-open-wallet").addEventListener("click", function() { setTab("wallet"); });
    document.getElementById("device-detail-back").addEventListener("click", hideDeviceDetail);
    document.getElementById("device-detail-home").addEventListener("click", function() {
      hideDeviceDetail();
      setTab("vpn");
    });
    document.getElementById("device-detail-close").addEventListener("click", hideDeviceDetail);
    document.getElementById("device-detail-copy").addEventListener("click", function() {
      if (!currentDetailConfigId || !configsById[currentDetailConfigId]) return;
      copyConfigText(configsById[currentDetailConfigId].config || "");
    });
    document.getElementById("btn-add-device").addEventListener("click", function() {
      document.getElementById("device-chooser").classList.toggle("show");
    });
    document.getElementById("btn-close-tariffs").addEventListener("click", hideTariffsModal);
    document.querySelectorAll(".device-chip").forEach(function(btn) {
      btn.addEventListener("click", function() {
        setSelectedDevice(btn.dataset.os);
        document.getElementById("device-chooser").classList.remove("show");
        tariffsModalMode = "buy";
        renewalSubscriptionId = null;
        showTariffsModal();
      });
    });
    document.querySelectorAll(".topup-btn").forEach(function(btn) {
      btn.addEventListener("click", function() {
        createInvoice(parseInt(btn.dataset.stars, 10));
      });
    });

    setSelectedDevice(null);
    loadTariffs();
    loadDashboard();
    loadWallet();
    loadConfigs();
    checkAdminLink();
  </script>
</body>
</html>
    """
    return web.Response(text=html, content_type="text/html")


def create_web_app(
    threexui: ThreeXUIClient,
    threexui_registry: Dict[str, ThreeXUIClient] | None = None,
    threexui_backends: Dict[str, Any] | None = None,
    default_threexui_key: str = "default",
    admin_ids: List[int] | None = None,
    bot: Bot | None = None,
) -> web.Application:
    app = web.Application()
    app["threexui"] = threexui
    app["threexui_registry"] = threexui_registry or {default_threexui_key: threexui}
    app["threexui_backends"] = threexui_backends or {}
    app["default_threexui_key"] = default_threexui_key
    app["admin_ids"] = admin_ids or []
    app["bot"] = bot
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/tariffs", handle_tariffs)
    app.router.add_post("/api/dashboard", handle_dashboard)
    app.router.add_post("/api/wallet", handle_wallet)
    app.router.add_post("/api/wallet/create-invoice", handle_wallet_create_invoice)
    app.router.add_post("/api/wallet/cancel-invoice", handle_wallet_cancel_invoice)
    app.router.add_post("/api/purchase-tariff", handle_purchase_tariff)
    app.router.add_post("/api/subscriptions/extend", handle_extend_subscription)
    app.router.add_post("/api/my-configs", handle_my_configs)
    app.router.add_post("/api/create-test-client", handle_create_test_client)
    # Admin
    app.router.add_get("/admin", handle_admin_index)
    app.router.add_get("/admin/tariffs", handle_admin_tariffs_index)
    app.router.add_get("/admin/payments", handle_admin_payments_index)
    app.router.add_get("/admin/devices", handle_admin_devices_index)
    app.router.add_get("/admin/users", handle_admin_users_index)
    app.router.add_get("/admin/analytics", handle_admin_analytics_index)
    app.router.add_get("/admin/users/{id}", handle_admin_user_profile)
    app.router.add_post("/admin/users/{id}/balance", handle_admin_user_balance_adjust)
    app.router.add_post("/admin/devices/{id}/extend", handle_admin_device_extend)
    app.router.add_post("/admin/devices/{id}/deactivate", handle_admin_device_deactivate)
    app.router.add_post("/admin/tariffs/create", handle_admin_page_tariffs_create)
    app.router.add_post("/admin/tariffs/{id}/delete", handle_admin_page_tariffs_delete)
    app.router.add_post("/api/admin/me", handle_admin_me)
    app.router.add_post("/api/admin/tariffs/list", handle_admin_tariffs_list)
    app.router.add_post("/api/admin/tariffs", handle_admin_tariffs_create)
    app.router.add_patch("/api/admin/tariffs/{id}", handle_admin_tariffs_update)
    app.router.add_delete("/api/admin/tariffs/{id}", handle_admin_tariffs_delete)
    return app

