from __future__ import annotations

from html import escape
from typing import Any, Dict, List

from aiohttp import ClientSession, web
from aiogram import Bot
from aiogram.types import LabeledPrice

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
    threexui = request.app.get("threexui")
    rows = await get_active_subscriptions_by_telegram_id(telegram_id, threexui=threexui)
    configs = []

    def format_gb(value: float) -> str:
        rounded = round(float(value), 2)
        if rounded.is_integer():
            return f"{int(rounded)} GB"
        return f"{rounded:.2f}".rstrip("0").rstrip(".") + " GB"

    for r in rows:
        traffic = None
        if threexui and r.get("threexui_client_id"):
            try:
                traffic = await threexui.get_client_traffic(1, r["threexui_client_id"])
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
    configs = await get_active_subscriptions_by_telegram_id(telegram_id, threexui=request.app.get("threexui"))
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
        subscription = await create_subscription_from_tariff(
            db_user_id=user["id"],
            telegram_id=telegram_id,
            threexui=request.app["threexui"],
            months=int(tariff["months"]),
            traffic_gb=int(tariff["traffic_gb"]),
            tariff_name=str(tariff["name"]),
            tariff_id=int(tariff["id"]),
            tariff_price_stars=int(tariff["price_stars"]),
            device_os=device_os,
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
        subscription = await extend_subscription_for_user(
            subscription_id=subscription_id,
            telegram_id=telegram_id,
            threexui=request.app["threexui"],
            months=renew_months,
            traffic_gb=renew_traffic_gb,
            tariff_id=effective_tariff_id,
            tariff_price_stars=renew_price_stars,
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
    threexui: ThreeXUIClient = request.app["threexui"]
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
    subscription = await create_test_subscription(
        db_user_id=user["id"],
        telegram_id=telegram_id,
        threexui=threexui,
    )

    return web.json_response(
        {
            "ok": True,
            "client_id": subscription["threexui_client_id"],
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


def _admin_html(tariffs: List[Dict[str, Any]], status: str | None = None, error: str | None = None) -> str:
    rows_html = "".join(
        (
            "<tr>"
            f"<td>{t['id']}</td>"
            f"<td>{escape(str(t.get('name') or ''))}</td>"
            f"<td>{t['months']}</td>"
            f"<td>{t['price_stars']}</td>"
            f"<td>{t['traffic_gb']}</td>"
            f"<td>{escape(str(t.get('badge') or ''))}</td>"
            f"<td>{'да' if t.get('is_active') else 'нет'}</td>"
            "<td>"
            f"<form method=\"post\" action=\"/admin/tariffs/{t['id']}/delete\" class=\"inline-form\" onsubmit=\"return confirm('Удалить тариф?');\">"
            "<button type=\"submit\" class=\"danger\">Удалить</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
        for t in tariffs
    )
    if not rows_html:
        rows_html = '<tr><td colspan="8">Нет тарифов</td></tr>'

    notice_html = ""
    if status == "created":
        notice_html = '<div class="notice success">Тариф добавлен</div>'
    elif status == "deleted":
        notice_html = '<div class="notice success">Тариф удален</div>'
    elif error == "required":
        notice_html = '<div class="notice error">Заполните название, месяцы и цену</div>'
    elif error == "invalid":
        notice_html = '<div class="notice error">Поля месяцев, цены, трафика и порядка должны быть числами</div>'
    elif error == "not_found":
        notice_html = '<div class="notice error">Тариф не найден</div>'
    elif error == "server":
        notice_html = '<div class="notice error">Внутренняя ошибка сервера</div>'

    html = """
<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <title>Админ — raccaster_vpn</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0f172a; color: #e5e7eb; margin: 0; padding: 16px; }
    .header { font-size: 20px; font-weight: 700; margin-bottom: 16px; }
    table { width: 100%; border-collapse: collapse; margin-bottom: 16px; }
    th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #334155; }
    th { color: #94a3b8; font-weight: 600; font-size: 12px; text-transform: uppercase; }
    input, button { padding: 8px 12px; border-radius: 8px; border: 1px solid #475569; background: #1e293b; color: #e5e7eb; }
    button { cursor: pointer; font-size: 13px; }
    button.danger { background: #7f1d1d; border-color: #991b1b; }
    button.primary { background: #166534; border-color: #15803d; }
    .form-row { margin-bottom: 12px; }
    .form-row label { display: block; font-size: 12px; color: #94a3b8; margin-bottom: 4px; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .card { background: #1e293b; border-radius: 12px; padding: 16px; margin-bottom: 16px; }
    .notice { border-radius: 12px; padding: 12px 14px; margin-bottom: 16px; }
    .notice.success { background: #052e16; color: #86efac; border: 1px solid #166534; }
    .notice.error { background: #450a0a; color: #fca5a5; border: 1px solid #991b1b; }
    .inline-form { display: inline; margin: 0; }
  </style>
</head>
<body>
  <div class="header" style="display:flex;align-items:center;gap:12px;">
    <a href="/" style="color:var(--hint,#94a3b8);text-decoration:none;font-size:14px;">← В приложение</a>
    <span>Админ-панель</span>
  </div>
  __NOTICE_HTML__
  <div id="content">
    <form class="card" method="post" action="/admin/tariffs/create">
      <div style="margin-bottom:12px;font-weight:600;">Добавить тариф</div>
      <div class="form-grid">
        <div class="form-row"><label>Название</label><input type="text" name="name" placeholder="1 месяц" /></div>
        <div class="form-row"><label>Месяцев</label><input type="number" name="months" min="1" value="1" /></div>
        <div class="form-row"><label>Цена (Stars)</label><input type="number" name="price_stars" min="0" value="300" /></div>
        <div class="form-row"><label>Трафик (GB)</label><input type="number" name="traffic_gb" min="0" value="30" /></div>
        <div class="form-row"><label>Бейдж</label><input type="text" name="badge" placeholder="-17%" /></div>
        <div class="form-row"><label>Порядок</label><input type="number" name="sort_order" value="0" /></div>
      </div>
      <button type="submit" class="primary">Добавить</button>
    </form>
    <div class="card">
      <div style="margin-bottom:12px;font-weight:600;">Тарифы</div>
      <table><thead><tr><th>ID</th><th>Название</th><th>Мес</th><th>Stars</th><th>GB</th><th>Бейдж</th><th>Активен</th><th></th></tr></thead><tbody>__ROWS_HTML__</tbody></table>
    </div>
  </div>
</body>
</html>
"""
    return html.replace("__NOTICE_HTML__", notice_html).replace("__ROWS_HTML__", rows_html)


async def handle_admin_index(request: web.Request) -> web.Response:
    """Страница админки: полностью серверный рендер без JS-зависимости."""
    tariffs = await get_tariffs(active_only=False)
    return web.Response(
        text=_admin_html(
            tariffs=tariffs,
            status=request.query.get("status"),
            error=request.query.get("error"),
        ),
        content_type="text/html",
    )


async def handle_admin_page_tariffs_create(request: web.Request) -> web.Response:
    """Создать тариф из обычной HTML-формы без зависимости от Telegram WebApp JS."""
    data = await request.post()
    name = (data.get("name") or "").strip()
    months = data.get("months")
    price_stars = data.get("price_stars")
    if not name or months is None or price_stars is None:
        raise web.HTTPSeeOther("/admin?error=required")
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
        raise web.HTTPSeeOther("/admin?error=invalid")
    except Exception:
        raise web.HTTPSeeOther("/admin?error=server")
    raise web.HTTPSeeOther("/admin?status=created")


async def handle_admin_page_tariffs_delete(request: web.Request) -> web.Response:
    """Удалить тариф из обычной HTML-формы без зависимости от Telegram WebApp JS."""
    try:
        tariff_id = int(request.match_info["id"])
    except (KeyError, TypeError, ValueError):
        raise web.HTTPSeeOther("/admin?error=not_found")
    try:
        deleted = await delete_tariff(tariff_id)
    except Exception:
        raise web.HTTPSeeOther("/admin?error=server")
    if not deleted:
        raise web.HTTPSeeOther("/admin?error=not_found")
    raise web.HTTPSeeOther("/admin?status=deleted")


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
        <button type="button" class="primary-action device-app-action" id="device-app-action">Открыть</button>
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
        steps: [
          { title: "Скачайте клиент", text: "Откройте страницу загрузки и установите Hiddify Next для Windows." },
          { title: "Скопируйте ключ", text: "Скопируйте ключ подключения ниже в один тап." },
          { title: "Импортируйте профиль", text: "Откройте приложение и вставьте ключ или импортируйте его из буфера обмена." },
          { title: "Подключитесь", text: "Активируйте подключение в приложении. После этого устройство готово к работе." }
        ]
      },
      "Android": {
        appName: "Hiddify Next",
        actionLabel: "Открыть загрузки Hiddify",
        actionUrl: "https://github.com/hiddify/hiddify-next/releases",
        steps: [
          { title: "Скачайте клиент", text: "Установите Hiddify Next для Android с официальной страницы загрузки." },
          { title: "Скопируйте ключ", text: "Скопируйте ключ подключения ниже." },
          { title: "Добавьте профиль", text: "Откройте приложение и импортируйте конфигурацию из буфера обмена." },
          { title: "Подключитесь", text: "Разрешите создание VPN-подключения и включите профиль." }
        ]
      },
      "iOS": {
        appName: "v2RayTun",
        actionLabel: "Открыть App Store",
        actionUrl: "https://apps.apple.com/us/search?term=v2RayTun",
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
      var stepsHtml = guide.steps.map(function(step, idx) {
        var actionHtml = "";
        if (idx === 0 && guide.actionLabel && guide.actionUrl) {
          actionHtml = '<div class="setup-step-action"><button type="button" class="device-detail-btn device-guide-link" data-url="' + escapeHtml(guide.actionUrl) + '">' + escapeHtml(guide.actionLabel) + '</button></div>';
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
      document.getElementById("device-app-text").textContent = guide.actionLabel
        ? "Установите приложение и импортируйте ключ подключения за пару шагов."
        : "Используйте совместимый клиент и импортируйте ключ подключения вручную.";
      var appActionBtn = document.getElementById("device-app-action");
      appActionBtn.textContent = guide.actionLabel || "Открыть";
      appActionBtn.style.display = guide.actionUrl ? "" : "none";
      appActionBtn.onclick = guide.actionUrl ? function() { openExternalLink(guide.actionUrl); } : null;
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
    admin_ids: List[int] | None = None,
    bot: Bot | None = None,
) -> web.Application:
    app = web.Application()
    app["threexui"] = threexui
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
    app.router.add_post("/admin/tariffs/create", handle_admin_page_tariffs_create)
    app.router.add_post("/admin/tariffs/{id}/delete", handle_admin_page_tariffs_delete)
    app.router.add_post("/api/admin/me", handle_admin_me)
    app.router.add_post("/api/admin/tariffs/list", handle_admin_tariffs_list)
    app.router.add_post("/api/admin/tariffs", handle_admin_tariffs_create)
    app.router.add_patch("/api/admin/tariffs/{id}", handle_admin_tariffs_update)
    app.router.add_delete("/api/admin/tariffs/{id}", handle_admin_tariffs_delete)
    return app

