from __future__ import annotations

import json
from typing import Any, Dict

from aiohttp import web

from app.threexui_client import ThreeXUIClient


async def handle_health(_: web.Request) -> web.Response:
    return web.Response(text="ok", content_type="text/plain")


async def handle_create_test_client(request: web.Request) -> web.Response:
    """
    HTTP endpoint for WebApp: create a test VLESS client in 3x-ui (inbound ID=1).

    Expects JSON:
    {
      "telegram_id": 123456789
    }
    """
    threexui: ThreeXUIClient = request.app["threexui"]
    data: Dict[str, Any] = await request.json()

    telegram_id = int(data.get("telegram_id", 0))
    if not telegram_id:
        return web.json_response({"ok": False, "error": "telegram_id is required"}, status=400)

    info = await threexui.create_vless_client(
        telegram_id=telegram_id,
        expire_days=1,
        total_gb=3,
        remark=f"webapp_{telegram_id}",
    )

    return web.json_response(
        {
            "ok": True,
            "client_id": info.client_id,
            "remark": info.remark,
            "message": info.config_text,
        }
    )


async def handle_index(request: web.Request) -> web.Response:
    """
    Simple static HTML for Telegram WebApp (MVP).
    """
    html = """
<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <title>raccaster_vpn WebApp</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      body { font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
             background: #0f172a; color: #e5e7eb; margin: 0; padding: 16px; }
      .card { background: #020617; border-radius: 16px; padding: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.4); }
      .title { font-size: 20px; font-weight: 600; margin-bottom: 8px; }
      .subtitle { font-size: 13px; color: #9ca3af; margin-bottom: 16px; }
      button { width: 100%; border-radius: 999px; padding: 10px 16px; border: none; cursor: pointer;
               font-size: 15px; font-weight: 600; background: #10b981; color: #022c22; }
      button:disabled { opacity: .6; cursor: default; }
      .log { margin-top: 16px; font-size: 13px; white-space: pre-wrap; word-break: break-word;
             background: #020617; border-radius: 12px; padding: 12px; border: 1px solid #1e293b; }
    </style>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
  </head>
  <body>
    <div class="card">
      <div class="title">raccaster_vpn</div>
      <div class="subtitle">
        Тестовое мини-приложение.<br/>
        Нажми кнопку, чтобы создать тестовый VPN-клиент в 3x-ui (inbound #1).
      </div>
      <button id="btn" type="button">Создать тестовый VPN</button>
      <div class="log" id="log"></div>
    </div>
    <script>
      const tg = window?.Telegram?.WebApp;
      if (tg) {
        tg.expand();
        tg.MainButton.hide();
      }

      const btn = document.getElementById("btn");
      const logEl = document.getElementById("log");

      function log(msg) {
        logEl.textContent = msg;
      }

      async function createTestClient() {
        if (!tg || !tg.initDataUnsafe || !tg.initDataUnsafe.user) {
          log("Не удалось получить данные пользователя из Telegram WebApp.");
          return;
        }

        const telegramId = tg.initDataUnsafe.user.id;

        btn.disabled = true;
        log("Создаём тестового клиента для Telegram ID " + telegramId + "...");

        try {
          const resp = await fetch("/api/create-test-client", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ telegram_id: telegramId })
          });
          const data = await resp.json();
          if (!data.ok) {
            log("Ошибка: " + (data.error || "неизвестная ошибка"));
          } else {
            log("Клиент создан:\\nID: " + data.client_id + "\\nКомментарий: " + data.remark + "\\n\\n" + data.message);
          }
        } catch (e) {
          log("Ошибка запроса: " + e);
        } finally {
          btn.disabled = false;
        }
      }

      btn.addEventListener("click", createTestClient);
    </script>
  </body>
</html>
    """
    return web.Response(text=html, content_type="text/html")


def create_web_app(threexui: ThreeXUIClient) -> web.Application:
    app = web.Application()
    app["threexui"] = threexui
    app.router.add_get("/health", handle_health)
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/create-test-client", handle_create_test_client)
    return app

