import asyncio
import os

import asyncpg


"""
Скрипт для создания базы данных и пользователя PostgreSQL для raccaster_vpn.

Перед запуском:
1. Установи PostgreSQL под Windows (официальный инсталлятор с postgres.org).
2. Запомни логин/пароль суперпользователя (обычно user: postgres).
3. Задай переменную окружения PG_SUPER_DSN, например:

   postgresql://postgres:your_password@localhost:5432/postgres

Тогда скрипт сам создаст:
- Базу данных: raccaster_vpn
- Пользователя: raccaster со своим паролем
"""


DB_NAME = os.getenv("RACCASTER_DB_NAME", "raccaster_vpn")
DB_USER = os.getenv("RACCASTER_DB_USER", "raccaster")
DB_PASSWORD = os.getenv("RACCASTER_DB_PASSWORD", "strong_password")


async def main() -> None:
    super_dsn = os.getenv("PG_SUPER_DSN")
    if not super_dsn:
        raise RuntimeError(
            "Переменная окружения PG_SUPER_DSN не задана.\n"
            "Пример: postgresql://postgres:your_password@localhost:5432/postgres"
        )

    conn = await asyncpg.connect(dsn=super_dsn)
    try:
        # Создаём пользователя (если ещё нет)
        role_exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = $1",
            DB_USER,
        )
        if not role_exists:
            await conn.execute(
                f"CREATE ROLE {DB_USER} LOGIN PASSWORD '{DB_PASSWORD}';"
            )

        # Создаём базу (если ещё нет)
        db_exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1",
            DB_NAME,
        )
        if not db_exists:
            await conn.execute(
                f"CREATE DATABASE {DB_NAME} OWNER {DB_USER};"
            )

        print(f"✅ База данных '{DB_NAME}' и пользователь '{DB_USER}' готовы.")
        print(
            "Добавь в .env строку подключения:\n"
            f"DATABASE_URL=postgresql://{DB_USER}:{DB_PASSWORD}@localhost:5432/{DB_NAME}"
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

