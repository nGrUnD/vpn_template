from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import json
import time
import uuid

import httpx

from .config import ThreeXUIConfig


@dataclass
class ThreeXUIClientInfo:
    """
    High-level representation of a created VPN client on 3x-ui.
    """

    client_id: str
    config_text: str
    remark: Optional[str] = None
    server_label: Optional[str] = None


class ThreeXUIClient:
    """
    Minimal HTTP client for interacting with 3x-ui panel.

    NOTE: 3x-ui не имеет стабильного публичного API, поэтому
    здесь определён только интерфейс и пример структуры.
    Конкретные URL и поля запросов/ответов нужно будет
    адаптировать под актуальную версию панели.
    """

    def __init__(self, config: ThreeXUIConfig) -> None:
        self._config = config
        self._client = httpx.AsyncClient(base_url=config.base_url, timeout=15.0)
        self._auth_cookies: dict[str, str] = {}

    async def _ensure_login(self) -> None:
        """
        Login to 3x-ui panel (if required) and store cookies/token.
        """
        if self._auth_cookies:
            return

        response = await self._client.post(
            "/login",
            data={
                "username": self._config.username,
                "password": self._config.password,
            },
        )
        response.raise_for_status()
        self._auth_cookies = dict(response.cookies)

    async def create_vless_client(
        self,
        telegram_id: int,
        expire_days: int = 1,
        total_gb: int = 3,
        remark: str | None = None,
    ) -> ThreeXUIClientInfo:
        """
        Create new VLESS client on 3x-ui (for inbound ID=1) and
        return minimal info about created client.

        Важно: 3x-ui не отдаёт готовый VLESS‑линк через API. Здесь мы:
        - создаём клиента в инбаунде с ID=1 через /panel/api/inbounds/addClient;
        - возвращаем ID клиента и текст-заглушку, чтобы бот мог что-то показать.
        Реальный конфиг можно посмотреть в панели.
        """
        await self._ensure_login()

        # ID инбаунда, который используем как основной для клиентов.
        inbound_id = 1

        # Время истечения в миллисекундах (3x-ui в JS использует new Date(expiryTime)).
        expiry_ts_ms = int((time.time() + expire_days * 24 * 60 * 60) * 1000)

        client_uuid = str(uuid.uuid4())
        client_email = remark or f"tg_{telegram_id}"

        client_obj = {
            "id": client_uuid,
            "security": "auto",
            "password": "",
            "flow": "",
            "email": client_email,
            "limitIp": 0,
            "totalGB": int(total_gb),
            "expiryTime": expiry_ts_ms,
            "enable": True,
            "tgId": int(telegram_id),
            "subId": "",
            "comment": client_email,
            "reset": 0,
        }

        # Формат, который использует фронтенд 3x-ui:
        # id: inboundId, settings: '{"clients": [<client JSON>]}' (СТРОКА).
        settings_str = json.dumps({"clients": [client_obj]}, ensure_ascii=False, separators=(",", ":"))

        response = await self._client.post(
            "/panel/api/inbounds/addClient",
            json={"id": inbound_id, "settings": settings_str},
            cookies=self._auth_cookies,
        )
        response.raise_for_status()

        # Панель возвращает стандартный jsonMsg, из которого сложно
        # извлечь линк; поэтому возвращаем клиентский UUID и
        # текст-заглушку. Позже это можно улучшить.
        placeholder_config = (
            "Клиент создан в 3x-ui (inbound ID=1, email="
            f"{client_email}). Скопируй реальный VLESS-конфиг из панели."
        )

        return ThreeXUIClientInfo(
            client_id=client_uuid,
            config_text=placeholder_config,
            remark=client_email,
        )

    async def get_client_config(self, client_id: str) -> str:
        """
        Fetch existing client's config text by ID.
        """
        # На данный момент публичного API для получения готового
        # VLESS-конфига по ID клиента нет. Функция оставлена как заглушка.
        raise NotImplementedError("Fetching client config by ID is not implemented for 3x-ui API")

    async def close(self) -> None:
        await self._client.aclose()

