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
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg, #0f172a);
      color: var(--text, #e5e7eb);
      margin: 0;
      padding: 16px;
      padding-bottom: calc(96px + env(safe-area-inset-bottom, 16px));
      min-height: 100vh;
    }
    .header { font-size: 22px; font-weight: 700; margin-bottom: 20px; }
    .tab-page { display: none; }
    .tab-page.active { display: block; }
    .section-title { font-size: 15px; font-weight: 600; color: var(--hint, #94a3b8); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
    .summary-card {
      background: linear-gradient(135deg, #312e81, #0f4c81);
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 18px;
      padding: 18px;
      margin-bottom: 18px;
    }
    .summary-card .label { color: rgba(255,255,255,0.8); font-size: 13px; margin-bottom: 8px; }
    .summary-card .value { font-size: 36px; font-weight: 700; margin-bottom: 6px; }
    .summary-card .meta { font-size: 14px; color: rgba(255,255,255,0.75); }
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
      background: var(--secondary, #1e293b);
      border: 1px solid var(--border, #334155);
      color: var(--text, #e5e7eb);
    }
    .device-chooser {
      display: none;
      background: var(--card-bg, #1e293b);
      border-radius: 16px;
      padding: 16px;
      border: 1px solid var(--border, #334155);
      margin-bottom: 18px;
    }
    .device-chooser.show { display: block; }
    .device-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
    .device-chip, .topup-btn {
      border: 1px solid var(--border, #334155);
      background: var(--secondary, #0f172a);
      color: var(--text, #e5e7eb);
      border-radius: 14px;
      padding: 12px 10px;
      text-align: center;
      cursor: pointer;
      font-weight: 600;
    }
    .device-chip.active {
      background: rgba(139, 92, 246, 0.18);
      border-color: #8b5cf6;
      color: #c4b5fd;
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
      background: rgba(2, 6, 23, 0.82);
      z-index: 120;
      padding: 16px;
      overflow-y: auto;
    }
    .tariffs-modal.show { display: block; }
    .tariffs-modal-card {
      max-width: 640px;
      margin: 20px auto 100px auto;
      background: var(--bg, #0f172a);
      border: 1px solid var(--border, #334155);
      border-radius: 18px;
      padding: 16px;
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
      background: var(--secondary, #1e293b);
      color: var(--text, #e5e7eb);
      border-radius: 12px;
      padding: 8px 12px;
      cursor: pointer;
    }
    .tariffs { display: flex; flex-direction: column; gap: 12px; margin-bottom: 24px; }
    .tariff-card {
      background: var(--card-bg, #1e293b);
      border-radius: 16px;
      padding: 16px;
      border: 1px solid var(--border, #334155);
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
      background: var(--card-bg, #1e293b);
      border-radius: 12px;
      padding: 14px;
      border: 1px solid var(--border, #334155);
    }
    .config-card .label { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
    .config-card .details { margin: 10px 0; display: grid; gap: 8px; }
    .config-card .detail-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 10px 12px;
      border-radius: 10px;
      background: rgba(15, 23, 42, 0.55);
      border: 1px solid rgba(148, 163, 184, 0.14);
    }
    .config-card .detail-label { font-size: 12px; color: #cbd5e1; }
    .config-card .detail-value { font-size: 13px; font-weight: 700; color: #f8fafc; }
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
      background: var(--secondary, #334155);
      color: var(--text, #e5e7eb);
    }
    .config-card .actions button.primary { background: var(--button, #5288c1); color: var(--button-text, #fff); }
    .wallet-card {
      background: linear-gradient(135deg, #083344, #052e16);
      border: 1px solid rgba(56, 189, 248, 0.18);
      border-radius: 18px;
      padding: 18px;
      margin-bottom: 18px;
    }
    .wallet-card .value { font-size: 34px; font-weight: 700; margin-top: 10px; }
    .wallet-note { color: var(--hint, #94a3b8); font-size: 13px; margin-bottom: 14px; }
    .topup-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 18px; }
    .history-list { display: flex; flex-direction: column; gap: 10px; }
    .history-item {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      padding: 14px;
      border-radius: 14px;
      background: var(--card-bg, #1e293b);
      border: 1px solid var(--border, #334155);
    }
    .history-item .meta { font-size: 12px; color: var(--hint, #94a3b8); margin-top: 4px; }
    .history-item .amount { font-size: 15px; font-weight: 700; }
    .empty { text-align: center; color: var(--hint); font-size: 14px; padding: 24px; }
    .toast { position: fixed; bottom: 24px; left: 16px; right: 16px; background: var(--card-bg); border-radius: 12px; padding: 12px 16px; text-align: center; font-size: 14px; box-shadow: 0 4px 20px rgba(0,0,0,0.3); z-index: 100; display: none; }
    .toast.show { display: block; }
    .loading { opacity: 0.7; pointer-events: none; }
    .bottom-tabs {
      position: fixed;
      left: 0;
      right: 0;
      bottom: 0;
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0;
      padding: 8px 12px calc(8px + env(safe-area-inset-bottom, 0px));
      background: rgba(2, 6, 23, 0.95);
      border-top: 1px solid var(--border, #334155);
      backdrop-filter: blur(10px);
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
      color: #c4b5fd;
      background: rgba(139, 92, 246, 0.14);
    }
  </style>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body>
  <div class="header">raccaster_vpn</div>
  <div id="admin-link-wrap" style="display:none; margin-bottom: 16px;">
    <a href="/admin" id="admin-link" style="display:inline-block; padding: 10px 16px; background: var(--secondary, #334155); color: var(--hint, #94a3b8); border-radius: 12px; text-decoration: none; font-size: 14px;">⚙ Админ-панель</a>
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
      <div class="wallet-note" id="wallet-rate-note">Баланс, тарифы и продление работают напрямую в Telegram Stars.</div>
      <div class="topup-grid">
        <button type="button" class="topup-btn" data-stars="100">100 ⭐</button>
        <button type="button" class="topup-btn" data-stars="300">300 ⭐</button>
        <button type="button" class="topup-btn" data-stars="500">500 ⭐</button>
      </div>
    </div>

    <div class="section-title">История операций</div>
    <div class="history-list" id="wallet-history"></div>
    <div class="empty" id="wallet-empty" style="display:none;">Операций пока нет.</div>
  </div>

  <div class="toast" id="toast"></div>

  <div class="bottom-tabs">
    <button type="button" class="tab-btn active" data-tab="vpn">VPN</button>
    <button type="button" class="tab-btn" data-tab="wallet">Кошелек</button>
  </div>

  <script>
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg) {
      tg.expand();
      tg.MainButton.hide();
      var th = tg.themeParams || {};
      if (th.bg_color) document.documentElement.style.setProperty("--bg", th.bg_color);
      if (th.text_color) document.documentElement.style.setProperty("--text", th.text_color);
      if (th.hint_color) document.documentElement.style.setProperty("--hint", th.hint_color);
      if (th.button_color) document.documentElement.style.setProperty("--button", th.button_color);
      if (th.button_text_color) document.documentElement.style.setProperty("--button-text", th.button_text_color);
      if (th.secondary_bg_color) document.documentElement.style.setProperty("--card-bg", th.secondary_bg_color);
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

    function renderTariffs(tariffs) {
      const root = document.getElementById("tariffs");
      root.innerHTML = tariffs.map(function(t) {
        const badge = t.badge ? '<span class="badge">' + t.badge + '</span>' : '';
        const buttonLabel = tariffsModalMode === "renew" ? "Продлить" : "Купить";
        return '<div class="tariff-card">' +
          '<div class="name">' + t.name + '</div>' +
          '<div class="meta">' + t.months + ' мес. · ' + t.traffic_gb + ' GB трафика</div>' +
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
      configs.forEach(function(c) { configsById[c.id] = c.config || ""; });
      root.innerHTML = configs.map(function(c) {
        const exp = c.expires_at ? "До " + new Date(c.expires_at).toLocaleDateString("ru") : "Без срока";
        const preview = (c.config || "").substring(0, 60) + (c.config && c.config.length > 60 ? "…" : "");
        const os = c.device_os ? c.device_os + " · " : "";
        var renewBtn = '<button type="button" class="renew-btn">Продлить</button>';
        return '<div class="config-card" data-id="' + c.id + '" data-renew-price="' + (c.renew_price_stars || '') + '" data-renew-months="' + (c.renew_months || '') + '">' +
          '<div class="label">Конфиг · ' + os + (c.server_label || ("#" + c.id)) + '</div>' +
          '<div class="details">' +
          '<div class="detail-row"><span class="detail-label">Действует до</span><span class="detail-value">' + exp + '</span></div>' +
          '<div class="detail-row"><span class="detail-label">Осталось трафика</span><span class="detail-value">' + (c.traffic_value || "Неизвестно") + '</span></div>' +
          '</div>' +
          '<div class="config-preview">' + preview + '</div>' +
          '<div class="actions">' +
          '<button type="button" class="primary copy-btn">Скопировать</button>' +
          renewBtn +
          '</div></div>';
      }).join("");
      root.querySelectorAll(".copy-btn").forEach(function(btn) {
        btn.addEventListener("click", function() {
          const id = parseInt(btn.closest(".config-card").getAttribute("data-id"), 10);
          const config = configsById[id];
          if (!config) return;
          navigator.clipboard && navigator.clipboard.writeText(config).then(function() { toast("Скопировано"); }).catch(function() { toast("Скопируйте вручную"); });
        });
      });
      root.querySelectorAll(".renew-btn").forEach(function(btn) {
        btn.addEventListener("click", function() {
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
      if (!telegramId) { renderConfigs([]); return; }
      apiPost("/api/my-configs", payloadBase())
        .then(function(data) { renderConfigs(data.ok ? data.configs : []); });
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
            loadConfigs();
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

