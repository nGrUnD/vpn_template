import json
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
    key: str
    base_url: str
    username: str
    password: str
    title: str | None = None
    vless_server: str | None = None  # хост для сборки VLESS-ссылки (опционально)
    vless_port: int | None = None
    inbound_id: int = 1
    enabled: bool = True
    weight: int = 1


@dataclass
class AppConfig:
    bot: BotConfig
    db: DatabaseConfig
    threexui: ThreeXUIConfig
    threexui_backends: dict[str, ThreeXUIConfig]
    default_threexui_key: str
    webapp_url: str | None = None
    webapp_port: int = 8080


def _parse_int_list(value: str | None) -> list[int]:
    if not value:
        return []
    parts = [p.strip() for p in value.split(",")]
    return [int(p) for p in parts if p]


def _parse_optional_int(value: str | None) -> int | None:
    raw = (value or "").strip()
    return int(raw) if raw.isdigit() else None


def _parse_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _backend_from_mapping(data: dict, fallback_key: str) -> ThreeXUIConfig:
    key = str(data.get("key") or fallback_key).strip() or fallback_key
    base_url = str(data.get("base_url") or data.get("baseUrl") or "").strip().rstrip("/")
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "").strip()
    if not base_url or not username or not password:
        raise RuntimeError(f"ThreeXUI backend '{key}' is missing base_url/username/password")
    vless_server = str(data.get("vless_server") or data.get("vlessServer") or "").strip() or None
    vless_port_raw = data.get("vless_port", data.get("vlessPort"))
    try:
        vless_port = int(vless_port_raw) if vless_port_raw not in (None, "") else None
    except (TypeError, ValueError):
        vless_port = None
    inbound_raw = data.get("inbound_id", data.get("inboundId", 1))
    try:
        inbound_id = max(int(inbound_raw), 1)
    except (TypeError, ValueError):
        inbound_id = 1
    weight_raw = data.get("weight", 1)
    try:
        weight = max(int(weight_raw), 1)
    except (TypeError, ValueError):
        weight = 1
    return ThreeXUIConfig(
        key=key,
        title=str(data.get("title") or key).strip() or key,
        base_url=base_url,
        username=username,
        password=password,
        vless_server=vless_server,
        vless_port=vless_port,
        inbound_id=inbound_id,
        enabled=_parse_bool(data.get("enabled"), True),
        weight=weight,
    )


def _load_threexui_backends() -> tuple[dict[str, ThreeXUIConfig], str]:
    raw_json = (os.getenv("THREEXUI_BACKENDS_JSON") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("THREEXUI_BACKENDS_JSON must contain valid JSON") from exc
        if isinstance(parsed, dict):
            items = []
            for key, value in parsed.items():
                if not isinstance(value, dict):
                    raise RuntimeError("Each backend in THREEXUI_BACKENDS_JSON must be an object")
                merged = dict(value)
                merged.setdefault("key", key)
                items.append(merged)
        elif isinstance(parsed, list):
            items = parsed
        else:
            raise RuntimeError("THREEXUI_BACKENDS_JSON must be a JSON array or object")
        backends = {}
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise RuntimeError("Each backend in THREEXUI_BACKENDS_JSON must be an object")
            backend = _backend_from_mapping(item, fallback_key=f"backend_{index}")
            backends[backend.key] = backend
        if not backends:
            raise RuntimeError("THREEXUI_BACKENDS_JSON must contain at least one backend")
        default_key = (os.getenv("THREEXUI_DEFAULT_KEY") or "").strip() or next(iter(backends.keys()))
        if default_key not in backends:
            raise RuntimeError(f"THREEXUI_DEFAULT_KEY '{default_key}' is not defined in THREEXUI_BACKENDS_JSON")
        return backends, default_key

    threexui_base_url = os.getenv("THREEXUI_BASE_URL", "").rstrip("/")
    threexui_username = os.getenv("THREEXUI_USERNAME", "")
    threexui_password = os.getenv("THREEXUI_PASSWORD", "")
    vless_server = (os.getenv("VLESS_SERVER") or "").strip() or None
    vless_port = _parse_optional_int(os.getenv("VLESS_PORT"))
    inbound_id = _parse_optional_int(os.getenv("THREEXUI_INBOUND_ID")) or 1
    title = (os.getenv("THREEXUI_TITLE") or "Default").strip() or "Default"
    default_backend = ThreeXUIConfig(
        key="default",
        title=title,
        base_url=threexui_base_url,
        username=threexui_username,
        password=threexui_password,
        vless_server=vless_server,
        vless_port=vless_port,
        inbound_id=inbound_id,
        enabled=True,
        weight=1,
    )
    return {"default": default_backend}, "default"


def load_config() -> AppConfig:
    bot_token = os.getenv("BOT_TOKEN", "")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is required")

    admin_ids = _parse_int_list(os.getenv("BOT_ADMIN_IDS"))

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        raise RuntimeError("DATABASE_URL is required")

    threexui_backends, default_threexui_key = _load_threexui_backends()
    default_backend = threexui_backends[default_threexui_key]
    if not default_backend.base_url or not default_backend.username or not default_backend.password:
        raise RuntimeError("ThreeXUI backend configuration is required")

    raw_webapp_url = (os.getenv("WEBAPP_URL") or "").strip() or None
    if raw_webapp_url and not raw_webapp_url.startswith(("http://", "https://")):
        raw_webapp_url = "https://" + raw_webapp_url
    webapp_url = raw_webapp_url
    webapp_port = int(os.getenv("WEBAPP_PORT", "8080"))

    return AppConfig(
        bot=BotConfig(token=bot_token, admin_ids=admin_ids),
        db=DatabaseConfig(url=db_url),
        threexui=default_backend,
        threexui_backends=threexui_backends,
        default_threexui_key=default_threexui_key,
        webapp_url=webapp_url,
        webapp_port=webapp_port,
    )

