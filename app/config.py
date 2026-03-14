import os
from dataclasses import dataclass

from dotenv import load_dotenv


load_dotenv()


@dataclass
class BotConfig:
    token: str
    admin_ids: list[int]


@dataclass
class DatabaseConfig:
    url: str


@dataclass
class ThreeXUIConfig:
    base_url: str
    username: str
    password: str
    vless_server: str | None = None  # хост для сборки VLESS-ссылки (опционально)
    vless_port: int | None = None


@dataclass
class AppConfig:
    bot: BotConfig
    db: DatabaseConfig
    threexui: ThreeXUIConfig
    webapp_url: str | None = None
    webapp_port: int = 8080


def _parse_int_list(value: str | None) -> list[int]:
    if not value:
        return []
    parts = [p.strip() for p in value.split(",")]
    return [int(p) for p in parts if p]


def load_config() -> AppConfig:
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required")

    admin_ids = _parse_int_list(os.getenv("BOT_ADMIN_IDS"))

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is required")

    threexui_base_url = os.getenv("THREEXUI_BASE_URL", "").rstrip("/")
    threexui_username = os.getenv("THREEXUI_USERNAME", "")
    threexui_password = os.getenv("THREEXUI_PASSWORD", "")
    vless_server = (os.getenv("VLESS_SERVER") or "").strip() or None
    vless_port_str = os.getenv("VLESS_PORT", "").strip()
    vless_port = int(vless_port_str) if vless_port_str.isdigit() else None

    raw_webapp_url = (os.getenv("WEBAPP_URL") or "").strip() or None
    if raw_webapp_url and not raw_webapp_url.startswith(("http://", "https://")):
        raw_webapp_url = "https://" + raw_webapp_url
    webapp_url = raw_webapp_url
    webapp_port = int(os.getenv("WEBAPP_PORT", "8080"))

    return AppConfig(
        bot=BotConfig(token=bot_token, admin_ids=admin_ids),
        db=DatabaseConfig(url=db_url),
        threexui=ThreeXUIConfig(
            base_url=threexui_base_url,
            username=threexui_username,
            password=threexui_password,
            vless_server=vless_server,
            vless_port=vless_port,
        ),
        webapp_url=webapp_url,
        webapp_port=webapp_port,
    )

