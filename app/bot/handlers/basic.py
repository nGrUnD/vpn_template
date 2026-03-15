from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    PreCheckoutQuery,
)

from app.services.users import get_or_create_user
from app.services.wallet import mark_topup_paid


router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, webapp_url: str | None = None) -> None:
    """
    Register user in DB (if needed) and greet.
    """
    await get_or_create_user(message.from_user)

    text_lines = [
        "Raccaster VPN.",
        "",
        "Безопасный и удобный доступ к VPN прямо внутри Telegram.",
        "",
        "В мини-приложении вы сможете пополнить баланс, подключить новое устройство, продлить подписку и открыть конфиг для настройки.",
    ]
    await message.answer("\n".join(text_lines))

    if webapp_url:
        inline_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Открыть VPN-приложение", web_app=WebAppInfo(url=webapp_url))]
            ]
        )
        await message.answer("Откройте мини-приложение, чтобы начать пользоваться VPN.", reply_markup=inline_kb)
    else:
        await message.answer("Мини-приложение пока недоступно. Проверьте настройку `WEBAPP_URL` в конфигурации бота.")


@router.pre_checkout_query()
async def handle_pre_checkout_query(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def handle_successful_payment(message: Message) -> None:
    payment = message.successful_payment
    if not payment:
        return
    payload = payment.invoice_payload or ""
    applied = await mark_topup_paid(
        payload=payload,
        telegram_payment_charge_id=getattr(payment, "telegram_payment_charge_id", None),
        total_amount=int(payment.total_amount or 0),
        currency=payment.currency or "XTR",
    )
    if applied:
        await message.answer("Баланс VPN пополнен.")


