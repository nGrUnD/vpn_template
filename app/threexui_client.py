from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import json
import time
import urllib.parse
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

        # Пытаемся получить конфиг из данных инбаунда (listen, port, streamSettings) — как делает сама панель.
        config_text = await self._build_client_link_from_inbound(
            inbound_id=inbound_id,
            client_uuid=client_uuid,
            client_email=client_email,
        )
        if not config_text:
            server = getattr(self._config, "vless_server", None) or None
            port = getattr(self._config, "vless_port", None)
            if server and port is not None:
                config_text = f"vless://{client_uuid}@{server}:{port}#{client_email}"
            else:
                config_text = f"Скопируй конфиг из панели 3x-ui (email: {client_email})"
        return ThreeXUIClientInfo(
            client_id=client_uuid,
            config_text=config_text,
            remark=client_email,
        )

    async def _get_inbound(self, inbound_id: int) -> Optional[dict[str, Any]]:
        """Получить инбаунд по ID (для сборки VLESS из streamSettings и т.д.)."""
        await self._ensure_login()
        for path in (f"/panel/api/inbounds/get/{inbound_id}", f"/panel/api/inbound/get/{inbound_id}"):
            try:
                resp = await self._client.get(path, cookies=self._auth_cookies)
                resp.raise_for_status()
                data = resp.json()
                if not data.get("success"):
                    continue
                obj = data.get("obj") or data.get("data")
                if obj:
                    return obj
            except Exception:
                continue
        return None

    def _get_nested(self, d: dict, *keys: str) -> Any:
        """Взять значение по первому существующему ключу (camelCase или snake_case)."""
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        return None

    def _build_vless_from_inbound(
        self,
        obj: dict[str, Any],
        client_uuid: str,
        client_email: str,
    ) -> Optional[str]:
        """
        Собрать VLESS-ссылку из объекта инбаунда (listen, port, streamSettings).
        Поддерживаем camelCase и snake_case в ответе API 3x-ui.
        """
        try:
            listen = (obj.get("listen") or obj.get("Listen") or "").strip()
            port = self._get_nested(obj, "port", "Port")
            if port is None:
                return None
            port = int(port) if isinstance(port, (int, float)) else None
            if port is None:
                return None
            # Хост: listen "0.0.0.0" или пустой — берём из base_url панели
            host = listen if listen and listen not in ("0.0.0.0", "::") else None
            if not host:
                base = getattr(self._config, "base_url", "") or ""
                parsed = urllib.parse.urlparse(base)
                host = parsed.hostname or "localhost"
            # streamSettings может быть stream_settings (snake_case) или строка JSON
            stream_raw = self._get_nested(obj, "streamSettings", "stream_settings") or "{}"
            if isinstance(stream_raw, str):
                stream = json.loads(stream_raw) if stream_raw.strip() else {}
            else:
                stream = stream_raw or {}
            network = self._get_nested(stream, "network", "Network") or "tcp"
            security = self._get_nested(stream, "security", "Security") or "none"
            # VLESS требует encryption=none
            params = ["type=" + str(network), "encryption=none", "security=" + str(security)]
            if security == "reality":
                reality = self._get_nested(stream, "realitySettings", "reality_settings") or {}
                if isinstance(reality, str):
                    reality = json.loads(reality) if reality.strip() else {}
                if not reality and stream:
                    reality = stream
                settings = reality.get("settings") or reality.get("Settings") or {}
                pbk = (
                    self._get_nested(reality, "publicKey", "public_key")
                    or settings.get("publicKey")
                    or settings.get("public_key")
                    or ""
                )
                if isinstance(pbk, str):
                    pbk = pbk.strip()
                sni = ""
                for key in ("serverNames", "server_names", "serverName", "server_name", "dest", "Dest"):
                    v = reality.get(key)
                    if isinstance(v, list) and v:
                        sni = str(v[0]).split(":")[0] if ":" in str(v[0]) else str(v[0])
                        break
                    if isinstance(v, str) and v.strip():
                        sni = v.split(":")[0].strip()
                        break
                short_ids = (
                    self._get_nested(reality, "shortIds", "short_ids")
                    or settings.get("shortIds")
                    or settings.get("short_ids")
                    or []
                )
                if isinstance(short_ids, str):
                    short_ids = [s.strip() for s in short_ids.split(",") if s.strip()]
                sid = short_ids[0] if short_ids else ""
                fp = (
                    self._get_nested(reality, "fingerprint", "fingerprint")
                    or settings.get("fingerprint")
                    or "random"
                )
                params.append("fp=" + str(fp))
                if pbk:
                    params.append("pbk=" + urllib.parse.quote(str(pbk), safe=""))
                if sni:
                    params.append("sni=" + urllib.parse.quote(sni, safe=""))
                if sid:
                    params.append("sid=" + urllib.parse.quote(str(sid), safe=""))
                params.append("spx=%2F")
            query = "&".join(params)
            frag = urllib.parse.quote(client_email, safe="")
            return f"vless://{client_uuid}@{host}:{port}/?{query}#{frag}"
        except Exception:
            return None

    async def _build_client_link_from_inbound(
        self,
        inbound_id: int,
        client_uuid: str,
        client_email: str,
    ) -> Optional[str]:
        """Получить VLESS-конфиг клиента из данных инбаунда в панели (по сути «из API по данным панели»)."""
        obj = await self._get_inbound(inbound_id)
        if not obj:
            return None
        return self._build_vless_from_inbound(obj, client_uuid, client_email)

    async def get_client_config(self, client_id: str) -> str:
        """
        Fetch existing client's config text by ID.
        """
        raise NotImplementedError("Fetching client config by ID is not implemented for 3x-ui API")

    async def client_exists(self, inbound_id: int, client_uuid: str) -> bool:
        """
        Проверить, есть ли клиент с данным UUID в инбаунде (для синхронизации с БД).
        """
        await self._ensure_login()
        try:
            resp = await self._client.get(
                f"/panel/api/inbounds/get/{inbound_id}",
                cookies=self._auth_cookies,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("success"):
                return False
            obj = data.get("obj") or {}
            settings_str = obj.get("settings") or "{}"
            settings = json.loads(settings_str) if isinstance(settings_str, str) else settings_str
            clients = settings.get("clients") or []
            return any(c.get("id") == client_uuid for c in clients)
        except Exception:
            return True

    async def close(self) -> None:
        await self._client.aclose()

