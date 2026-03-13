import asyncio
import logging

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app.config import load_config
from app.db import init_db, close_db
from app.threexui_client import ThreeXUIClient
from app.bot.handlers.basic import router as basic_router
from app.webapp.server import create_web_app


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def run_bot(bot: Bot, dp: Dispatcher, threexui_client: ThreeXUIClient) -> None:
    logger.info("Starting Telegram bot polling...")
    await dp.start_polling(bot, threexui=threexui_client)


async def run_web(threexui_client: ThreeXUIClient) -> None:
    app = create_web_app(threexui_client)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=8080)
    logger.info("Starting WebApp server on http://0.0.0.0:8080")
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

    threexui_client = ThreeXUIClient(config.threexui)

    # В aiogram v3 можно прокидывать объекты через контекст:
    # использование threexui и webapp_url в хендлерах через аргументы функции.
    dp["threexui"] = threexui_client
    dp["webapp_url"] = config.webapp_url

    dp.include_router(basic_router)

    try:
        await asyncio.gather(
            run_bot(bot, dp, threexui_client),
            run_web(threexui_client),
        )
    finally:
        await threexui_client.close()
        await close_db()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

