from __future__ import annotations

from typing import Any, Dict, List

from aiohttp import web

from app.services.subscriptions import create_test_subscription, get_active_subscriptions_by_telegram_id
from app.services.tariffs import (
    create_tariff,
    delete_tariff,
    get_tariffs,
    update_tariff,
)
from app.services.users import get_or_create_user_by_telegram_id
from app.threexui_client import ThreeXUIClient


def _admin_ids(app: web.Application) -> List[int]:
    return list(app.get("admin_ids", []))


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
    configs = [
        {
            "id": r["id"],
            "server_label": r["server_label"],
            "config": r["config"],
            "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows
    ]
    return web.json_response({"ok": True, "configs": configs})


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
    """POST { telegram_id } -> список всех тарифов (включая неактивные)."""
    admin_id, _ = await _require_admin(request)
    if admin_id is None:
        return web.json_response({"ok": False, "error": "Forbidden"}, status=403)
    tariffs = await get_tariffs(active_only=False)
    return web.json_response({"ok": True, "tariffs": tariffs})


async def handle_admin_tariffs_create(request: web.Request) -> web.Response:
    """POST { telegram_id, name, months, price_rub, traffic_gb?, badge?, sort_order? }."""
    admin_id, data = await _require_admin(request)
    if admin_id is None:
        return web.json_response({"ok": False, "error": "Forbidden"}, status=403)
    name = data.get("name")
    months = data.get("months")
    price_rub = data.get("price_rub")
    if not name or months is None or price_rub is None:
        return web.json_response({"ok": False, "error": "name, months, price_rub required"}, status=400)
    try:
        months = int(months)
        price_rub = int(price_rub)
        traffic_gb = int(data.get("traffic_gb", 0))
        sort_order = int(data.get("sort_order", 0))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "Invalid numbers"}, status=400)
    badge = data.get("badge") or None
    row = await create_tariff(name=name, months=months, price_rub=price_rub, traffic_gb=traffic_gb, badge=badge, sort_order=sort_order)
    return web.json_response({"ok": True, "tariff": {"id": row["id"], "name": row["name"], "months": row["months"], "price_rub": row["price_rub"], "traffic_gb": row["traffic_gb"], "badge": row["badge"], "sort_order": row["sort_order"], "is_active": row["is_active"]}})


async def handle_admin_tariffs_update(request: web.Request) -> web.Response:
    """PATCH /api/admin/tariffs/{id} с телом { telegram_id, name?, months?, price_rub?, traffic_gb?, badge?, sort_order?, is_active? }."""
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
    if "price_rub" in data:
        kwargs["price_rub"] = int(data["price_rub"])
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
    return web.json_response({"ok": True, "tariff": {"id": row["id"], "name": row["name"], "months": row["months"], "price_rub": row["price_rub"], "traffic_gb": row["traffic_gb"], "badge": row["badge"], "sort_order": row["sort_order"], "is_active": row["is_active"]}})


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


def _admin_html() -> str:
    return """
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
    .forbidden { color: #f87171; padding: 24px; background: #1e293b; border-radius: 12px; margin-bottom: 16px; line-height: 1.5; }
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
    .toast { position: fixed; bottom: 24px; left: 16px; right: 16px; background: #1e293b; padding: 12px; text-align: center; border-radius: 12px; display: none; }
    .toast.show { display: block; }
  </style>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body>
  <div class="header">Админ-панель</div>
  <div id="status" class="card" style="margin-bottom:16px;min-height:60px;"><span style="color:#94a3b8;">Загрузка…</span></div>
  <div id="forbidden" class="forbidden" style="display:none;"></div>
  <div id="content" style="display:none;">
    <div class="card">
      <div style="margin-bottom:12px;font-weight:600;">Добавить тариф</div>
      <div class="form-grid">
        <div class="form-row"><label>Название</label><input type="text" id="new-name" placeholder="1 месяц" /></div>
        <div class="form-row"><label>Месяцев</label><input type="number" id="new-months" min="1" value="1" /></div>
        <div class="form-row"><label>Цена (₽)</label><input type="number" id="new-price_rub" min="0" value="300" /></div>
        <div class="form-row"><label>Трафик (GB)</label><input type="number" id="new-traffic_gb" min="0" value="30" /></div>
        <div class="form-row"><label>Бейдж</label><input type="text" id="new-badge" placeholder="−17%" /></div>
        <div class="form-row"><label>Порядок</label><input type="number" id="new-sort_order" value="0" /></div>
      </div>
      <button type="button" class="primary" id="btn-create">Добавить</button>
    </div>
    <div class="card">
      <div style="margin-bottom:12px;font-weight:600;">Тарифы</div>
      <table><thead><tr><th>ID</th><th>Название</th><th>Мес</th><th>₽</th><th>GB</th><th>Бейдж</th><th>Активен</th><th></th></tr></thead><tbody id="tariffs-tbody"></tbody></table>
    </div>
  </div>
  <div class="toast" id="toast"></div>
  <script>
    var tg = window.Telegram && window.Telegram.WebApp;
    var telegramId = tg && tg.initDataUnsafe && tg.initDataUnsafe.user ? tg.initDataUnsafe.user.id : null;
    function toast(msg) { var el = document.getElementById("toast"); el.textContent = msg; el.classList.add("show"); setTimeout(function() { el.classList.remove("show"); }, 2500); }
    function payload(extra) { var p = { telegram_id: telegramId }; for (var k in extra) p[k] = extra[k]; return p; }
    function setStatus(html) { var el = document.getElementById("status"); if (el) el.innerHTML = html; }
    function showError(msg) {
      setStatus("");
      var forbiddenEl = document.getElementById("forbidden");
      forbiddenEl.innerHTML = msg;
      forbiddenEl.style.display = "block";
    }
    function checkAdmin() {
      var forbiddenEl = document.getElementById("forbidden");
      var contentEl = document.getElementById("content");
      if (!telegramId) {
        showError("Не удалось получить ваш Telegram ID.<br/>Откройте админку из бота: нажмите /start, затем кнопку «Админ-панель».");
        return;
      }
      setStatus("<span style=\"color:#94a3b8;\">Проверка доступа…</span>");
      var resolved = false;
      var timeoutMs = 12000;
      var fallbackTid = setTimeout(function() {
        if (resolved) return;
        resolved = true;
        showError("Таймаут. Запрос не дошёл до сервера (12 сек). Откройте админку из Telegram (кнопка «Админ-панель»).<br/><br/>Ваш ID: " + telegramId + ".");
      }, timeoutMs);
      function done() { if (!resolved) { resolved = true; clearTimeout(fallbackTid); } }
      fetch("/api/admin/me", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ telegram_id: telegramId }) })
        .then(function(r) {
          done();
          if (!r.ok) { throw new Error("HTTP " + r.status); }
          return r.json().catch(function() { throw new Error("Неверный ответ"); });
        })
        .then(function(d) {
          if (d && d.ok && d.is_admin) {
            setStatus("");
            contentEl.style.display = "block";
            loadTariffs();
          } else {
            showError("Доступ только для администраторов.<br/><br/><strong>Ваш Telegram ID:</strong> " + telegramId + "<br/>Добавьте его в <code>BOT_ADMIN_IDS</code> в .env на сервере и перезапустите бота.");
          }
        })
        .catch(function(err) {
          done();
          var isTimeout = err && err.name === "AbortError";
          showError((isTimeout ? "Таймаут. " : "Ошибка сети. ") + "Ваш ID: " + (telegramId || "—") + ". Откройте из бота (кнопка «Админ-панель»).");
        });
    }
    function loadTariffs() {
      var base = (typeof window !== "undefined" && window.location && window.location.origin) ? window.location.origin : "";
      var controller = new AbortController();
      var tid = setTimeout(function() { controller.abort(); }, 10000);
      fetch(base + "/api/admin/tariffs/list", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ telegram_id: telegramId }), signal: controller.signal })
        .then(function(r) { clearTimeout(tid); return r.ok ? r.json() : Promise.reject(new Error(r.status)); })
        .then(function(d) {
          var tbody = document.getElementById("tariffs-tbody");
          if (!d || !d.ok) { tbody.innerHTML = "<tr><td colspan=\"8\">Ошибка загрузки</td></tr>"; return; }
          var list = d.tariffs || [];
          if (list.length === 0) { tbody.innerHTML = "<tr><td colspan=\"8\">Нет тарифов</td></tr>"; return; }
          tbody.innerHTML = list.map(function(t) {
            return "<tr><td>" + t.id + "</td><td>" + (t.name || "") + "</td><td>" + t.months + "</td><td>" + t.price_rub + "</td><td>" + t.traffic_gb + "</td><td>" + (t.badge || "") + "</td><td>" + (t.is_active ? "да" : "нет") + "</td><td><button class=\"danger\" data-id=\"" + t.id + "\">Удалить</button></td></tr>";
          }).join("");
          tbody.querySelectorAll("button").forEach(function(btn) {
            btn.onclick = function() { deleteTariff(parseInt(btn.dataset.id, 10)); };
          });
        })
        .catch(function() { clearTimeout(tid); var tbody = document.getElementById("tariffs-tbody"); if (tbody) tbody.innerHTML = "<tr><td colspan=\"8\">Ошибка сети или таймаут</td></tr>"; });
    }
    function createTariff() {
      var name = document.getElementById("new-name").value.trim();
      var months = parseInt(document.getElementById("new-months").value, 10);
      var price_rub = parseInt(document.getElementById("new-price_rub").value, 10);
      var traffic_gb = parseInt(document.getElementById("new-traffic_gb").value, 10) || 0;
      var badge = document.getElementById("new-badge").value.trim() || null;
      var sort_order = parseInt(document.getElementById("new-sort_order").value, 10) || 0;
      if (!name || isNaN(months) || isNaN(price_rub)) { toast("Заполните название, месяцы и цену"); return; }
      fetch("/api/admin/tariffs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload({ name: name, months: months, price_rub: price_rub, traffic_gb: traffic_gb, badge: badge, sort_order: sort_order })) })
        .then(function(r) { return r.json(); })
        .then(function(d) { if (d.ok) { toast("Тариф добавлен"); document.getElementById("new-name").value = ""; loadTariffs(); } else toast(d.error || "Ошибка"); });
    }
    function deleteTariff(id) {
      if (!confirm("Удалить тариф?")) return;
      fetch("/api/admin/tariffs/" + id, { method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ telegram_id: telegramId }) })
        .then(function(r) { return r.json(); })
        .then(function(d) { if (d.ok && d.deleted) { toast("Удалён"); loadTariffs(); } else toast(d.error || "Ошибка"); });
    }
    if (tg) tg.expand();
    document.getElementById("btn-create").onclick = createTariff;
    if (document.readyState === "loading") { document.addEventListener("DOMContentLoaded", checkAdmin); }
    else { checkAdmin(); }
  </script>
</body>
</html>
"""


async def handle_admin_index(_: web.Request) -> web.Response:
    """Страница админки: только HTML, доступ проверяется в JS по /api/admin/me."""
    return web.Response(text=_admin_html(), content_type="text/html")


async def handle_index(request: web.Request) -> web.Response:
    """
    MVP WebApp: тарифы, кнопка «Купить (тест)», список активных конфигов.
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
      padding-bottom: env(safe-area-inset-bottom, 16px);
      min-height: 100vh;
    }
    .header { font-size: 22px; font-weight: 700; margin-bottom: 20px; }
    .section-title { font-size: 15px; font-weight: 600; color: var(--hint, #94a3b8); margin-bottom: 12px; text-transform: uppercase; letter-spacing: 0.05em; }
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
    .configs { display: flex; flex-direction: column; gap: 12px; }
    .config-card {
      background: var(--card-bg, #1e293b);
      border-radius: 12px;
      padding: 14px;
      border: 1px solid var(--border, #334155);
    }
    .config-card .label { font-size: 14px; font-weight: 600; margin-bottom: 4px; }
    .config-card .expires { font-size: 12px; color: var(--hint, #94a3b8); margin-bottom: 8px; }
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
    .empty { text-align: center; color: var(--hint); font-size: 14px; padding: 24px; }
    .toast { position: fixed; bottom: 24px; left: 16px; right: 16px; background: var(--card-bg); border-radius: 12px; padding: 12px 16px; text-align: center; font-size: 14px; box-shadow: 0 4px 20px rgba(0,0,0,0.3); z-index: 100; display: none; }
    .toast.show { display: block; }
    .loading { opacity: 0.7; pointer-events: none; }
  </style>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body>
  <div class="header">raccaster_vpn</div>

  <div class="section-title">Тарифы</div>
  <div class="tariffs" id="tariffs"></div>

  <div class="section-title">Мои конфиги</div>
  <div class="configs" id="configs"></div>
  <div class="empty" id="configs-empty" style="display:none;">Нет активных конфигов. Нажми «Купить (тест)» на тарифе.</div>

  <div class="toast" id="toast"></div>

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

    function toast(msg) {
      const el = document.getElementById("toast");
      el.textContent = msg;
      el.classList.add("show");
      setTimeout(function() { el.classList.remove("show"); }, 2500);
    }

    function renderTariffs(tariffs) {
      const root = document.getElementById("tariffs");
      root.innerHTML = tariffs.map(function(t) {
        const badge = t.badge ? '<span class="badge">' + t.badge + '</span>' : '';
        return '<div class="tariff-card">' +
          '<div class="name">' + t.name + '</div>' +
          '<div class="meta">' + t.traffic_gb + ' GB трафика</div>' +
          '<div class="price">' + t.price_rub + ' ₽' + badge + '</div>' +
          '<button type="button" data-tariff-id="' + t.id + '">Купить (тест)</button>' +
          '</div>';
      }).join("");
      root.querySelectorAll("button").forEach(function(btn) {
        btn.addEventListener("click", function() { buyTest(btn, btn.dataset.tariffId); });
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
        return '<div class="config-card" data-id="' + c.id + '">' +
          '<div class="label">' + (c.server_label || "Конфиг #" + c.id) + '</div>' +
          '<div class="expires">' + exp + '</div>' +
          '<div class="config-preview">' + preview + '</div>' +
          '<div class="actions">' +
          '<button type="button" class="primary copy-btn">Скопировать</button>' +
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
    }

    function loadTariffs() {
      fetch("/api/tariffs")
        .then(function(r) { return r.json(); })
        .then(function(data) { if (data.ok && data.tariffs) renderTariffs(data.tariffs); });
    }

    function loadConfigs() {
      if (!telegramId) { renderConfigs([]); return; }
      fetch("/api/my-configs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ telegram_id: telegramId })
      })
        .then(function(r) { return r.json(); })
        .then(function(data) { renderConfigs(data.ok ? data.configs : []); });
    }

    function buyTest(btn, tariffId) {
      if (!telegramId) { toast("Ошибка: нет данных пользователя"); return; }
      btn.disabled = true;
      btn.closest(".tariffs").classList.add("loading");
      var payload = { telegram_id: telegramId };
      if (user) {
        payload.username = user.username;
        payload.first_name = user.first_name;
        payload.last_name = user.last_name;
      }
      fetch("/api/create-test-client", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          btn.closest(".tariffs").classList.remove("loading");
          btn.disabled = false;
          if (data.ok) {
            toast("Тестовый конфиг создан");
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

    loadTariffs();
    loadConfigs();
  </script>
</body>
</html>
    """
    return web.Response(text=html, content_type="text/html")


def create_web_app(threexui: ThreeXUIClient, admin_ids: List[int] | None = None) -> web.Application:
    app = web.Application()
    app["threexui"] = threexui
    app["admin_ids"] = admin_ids or []
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/tariffs", handle_tariffs)
    app.router.add_post("/api/my-configs", handle_my_configs)
    app.router.add_post("/api/create-test-client", handle_create_test_client)
    # Admin
    app.router.add_get("/admin", handle_admin_index)
    app.router.add_post("/api/admin/me", handle_admin_me)
    app.router.add_post("/api/admin/tariffs/list", handle_admin_tariffs_list)
    app.router.add_post("/api/admin/tariffs", handle_admin_tariffs_create)
    app.router.add_patch("/api/admin/tariffs/{id}", handle_admin_tariffs_update)
    app.router.add_delete("/api/admin/tariffs/{id}", handle_admin_tariffs_delete)
    return app

