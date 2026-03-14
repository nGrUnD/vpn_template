from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)

from app.services.users import get_or_create_user
from app.services.subscriptions import create_test_subscription
from app.threexui_client import ThreeXUIClient


router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, webapp_url: str | None = None) -> None:
    """
    Register user in DB (if needed) and greet.
    """
    db_user = await get_or_create_user(message.from_user)

    text_lines = [
        "👋 Добро пожаловать в raccaster_vpn!",
        "",
        f"Ваш Telegram ID: `{message.from_user.id}`",
        f"Внутренний ID пользователя: `{db_user['id']}`",
        "",
        "Доступные команды:",
        "/buy — открыть меню покупки подписки",
        "/test_config — получить тестовый VPN-конфиг на 1 день",
    ]

    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="/buy")],
            [KeyboardButton(text="/test_config")],
        ],
        resize_keyboard=True,
    )

    await message.answer("\n".join(text_lines), parse_mode="Markdown", reply_markup=keyboard)

    if webapp_url:
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=webapp_url))]
            ]
        )
        await message.answer("Открой мини-приложение для управления VPN:", reply_markup=inline_kb)


@router.message(Command("test_config"))
async def cmd_test_config(message: Message, threexui: ThreeXUIClient | None = None) -> None:
    """
    Provision a test VPN config via 3x-ui and send it to the user.

    Если 3x-ui ещё не настроен или недоступен, отправляем
    заглушку с фейковым конфигом, чтобы команда что‑то делала.
    """
    db_user = await get_or_create_user(message.from_user)

    config_text: str

    try:
        if threexui is None:
            raise RuntimeError("ThreeXUI client is not configured")

        subscription = await create_test_subscription(
            db_user_id=db_user["id"],
            telegram_id=message.from_user.id,
            threexui=threexui,
        )
        config_text = subscription["config"]
    except Exception:
        # Фолбэк: заглушка, если панель не отвечает или не настроена.
        config_text = "vless://test-config-placeholder@your-server:443?security=reality#raccaster_vpn_test"

    text_lines = [
        "✅ Тестовый VPN-конфиг (MVP).",
        "",
        "Срок действия: 1 день (логика заложена в бэкенде).",
        "Трафик: ~3GB (точные лимиты настраиваются в панели 3x-ui).",
        "",
        "Вот ваш конфиг (или заглушка, если панель не настроена):",
        "```",
        config_text,
        "```",
        "",
        "Его можно импортировать в клиент (например, v2rayNG / Nekobox).",
    ]

    await message.answer("\n".join(text_lines), parse_mode="Markdown")


@router.message(Command("buy"))
async def cmd_buy(message: Message) -> None:
    """
    Простое MVP-меню покупки подписки.

    Пока без реальной оплаты: только демонстрация UX.
    """
    text_lines = [
        "💳 Покупка подписки raccaster_vpn (MVP).",
        "",
        "Планы (пример):",
        "- 1 месяц — 300₽",
        "- 3 месяца — 750₽",
        "- 12 месяцев — 2400₽",
        "",
        "Сейчас это демонстрационный режим без реальной оплаты.",
        "На следующем шаге сюда будет добавлена оплата через Telegram Payments или внешний провайдер.",
    ]

    await message.answer("\n".join(text_lines))


