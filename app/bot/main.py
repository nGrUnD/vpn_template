import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import load_config
from app.db import init_db, close_db
from app.bot.handlers.basic import router as basic_router
from app.services.backends import (
    build_threexui_registry,
    close_threexui_registry,
    get_default_threexui_client,
)
from app.threexui_client import ThreeXUIClient
from app.webapp.server import create_web_app


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run_bot(bot: Bot, dp: Dispatcher, threexui_client: ThreeXUIClient) -> None:
    logger.info("Starting Telegram bot polling...")
    await dp.start_polling(bot, threexui=threexui_client)


async def run_web(
    bot: Bot,
    threexui_client: ThreeXUIClient,
    threexui_registry: dict[str, ThreeXUIClient],
    threexui_backends,
    default_threexui_key: str,
    port: int,
    admin_ids: list[int],
) -> None:
    app = create_web_app(threexui_client, threexui_registry, threexui_backends, default_threexui_key, admin_ids, bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    logger.info("Starting WebApp server on http://0.0.0.0:%s", port)
    await site.start()

    # Keep running until cancelled
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()


async def main() -> None:
    config = load_config()

    await init_db(config.db)

    bot = Bot(
        token=config.bot.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    threexui_registry = build_threexui_registry(config)
    threexui_client = get_default_threexui_client(threexui_registry, config.default_threexui_key)

    # В aiogram v3 можно прокидывать объекты через контекст:
    # использование threexui и webapp_url в хендлерах через аргументы функции.
    dp["threexui"] = threexui_client
    dp["threexui_registry"] = threexui_registry
    dp["default_threexui_key"] = config.default_threexui_key
    dp["webapp_url"] = config.webapp_url
    dp["admin_ids"] = config.bot.admin_ids

    dp.include_router(basic_router)

    try:
        await asyncio.gather(
            run_bot(bot, dp, threexui_client),
            run_web(
                bot,
                threexui_client,
                threexui_registry,
                config.threexui_backends,
                config.default_threexui_key,
                config.webapp_port,
                config.bot.admin_ids,
            ),
        )
    finally:
        await close_threexui_registry(threexui_registry)
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

