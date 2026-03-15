from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import base64
import binascii
import json
import secrets
import string
import time
import urllib.parse
import uuid

import httpx

from .config import ThreeXUIConfig

DEFAULT_IP_LIMIT = 3


@dataclass
class ThreeXUIClientInfo:
    """
    High-level representation of a created VPN client on 3x-ui.
    """

    client_id: str
    config_text: str
    remark: Optional[str] = None
    server_label: Optional[str] = None
    sub_id: Optional[str] = None
    subscription_url: Optional[str] = None
    subscription_json_url: Optional[str] = None


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

    def _generate_sub_id(self, length: int = 16) -> str:
        alphabet = string.ascii_lowercase + string.digits
        return "".join(secrets.choice(alphabet) for _ in range(max(length, 8)))

    def _join_url_with_id(self, base: str, item_id: str) -> str:
        return base if not item_id else (base if base.endswith("/") else base + "/") + item_id

    async def _fetch_panel_settings(self) -> dict[str, Any]:
        await self._ensure_login()
        response = await self._client.post("/panel/setting/all", cookies=self._auth_cookies)
        response.raise_for_status()
        payload = self._extract_payload(response.json())
        return payload if isinstance(payload, dict) else {}

    def _build_subscription_urls(self, settings: dict[str, Any], sub_id: str) -> tuple[str | None, str | None]:
        if not sub_id:
            return None, None
        sub_uri = str(settings.get("subURI") or "").strip()
        sub_json_uri = str(settings.get("subJsonURI") or "").strip()
        if not sub_uri or not sub_json_uri:
            parsed = urllib.parse.urlparse(getattr(self._config, "base_url", "") or "")
            base = ""
            if parsed.scheme and parsed.netloc:
                base = f"{parsed.scheme}://{parsed.netloc}"
            sub_path = str(settings.get("subPath") or "/sub/").strip() or "/sub/"
            sub_json_path = str(settings.get("subJsonPath") or "/json/").strip() or "/json/"
            if not sub_path.startswith("/"):
                sub_path = "/" + sub_path
            if not sub_json_path.startswith("/"):
                sub_json_path = "/" + sub_json_path
            if base:
                if not sub_uri:
                    sub_uri = base + sub_path
                if not sub_json_uri:
                    sub_json_uri = base + sub_json_path
        subscription_url = self._join_url_with_id(sub_uri, sub_id) if sub_uri else None
        subscription_json_url = self._join_url_with_id(sub_json_uri, sub_id) if sub_json_uri else None
        return subscription_url, subscription_json_url

    def _decode_subscription_body(self, payload: str) -> list[str]:
        text = (payload or "").strip()
        if not text:
            return []
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        if any(line.startswith(("vless://", "vmess://", "trojan://", "ss://")) for line in raw_lines):
            return raw_lines
        try:
            padding = "=" * (-len(text) % 4)
            decoded = base64.b64decode(text + padding).decode("utf-8", errors="ignore")
        except (binascii.Error, ValueError):
            return raw_lines
        return [line.strip() for line in decoded.splitlines() if line.strip()]

    def _apply_display_name_to_config(self, config_text: str | None, display_name: str) -> str | None:
        text = (config_text or "").strip()
        if not text or not display_name:
            return config_text
        if not text.startswith(("vless://", "trojan://", "ss://")):
            return config_text
        try:
            split = urllib.parse.urlsplit(text)
            return urllib.parse.urlunsplit(
                (
                    split.scheme,
                    split.netloc,
                    split.path,
                    split.query,
                    urllib.parse.quote(display_name, safe=""),
                )
            )
        except Exception:
            base = text.split("#", 1)[0]
            return base + "#" + urllib.parse.quote(display_name, safe="")

    async def _fetch_config_from_subscription(self, subscription_url: str | None) -> str | None:
        if not subscription_url:
            return None
        try:
            response = await self._client.get(
                subscription_url,
                headers={"Accept": "text/plain, */*"},
                follow_redirects=True,
            )
            response.raise_for_status()
        except Exception:
            return None
        lines = self._decode_subscription_body(response.text)
        return lines[0] if lines else None

    async def create_vless_client(
        self,
        telegram_id: int,
        expire_days: int = 1,
        total_gb: int = 3,
        remark: str | None = None,
        limit_ip: int = DEFAULT_IP_LIMIT,
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
        display_name = client_email
        sub_id = self._generate_sub_id()

        # В API 3x-ui totalGB передаётся в БАЙТАХ. 0 = безлимит, иначе total_gb * 1024³
        total_bytes = 0 if total_gb <= 0 else int(total_gb) * (1024**3)

        client_obj = {
            "id": client_uuid,
            "security": "auto",
            "password": "",
            "flow": "",
            "email": client_email,
            "limitIp": max(int(limit_ip), 0),
            "totalGB": total_bytes,
            "expiryTime": expiry_ts_ms,
            "enable": True,
            "tgId": int(telegram_id),
            "subId": sub_id,
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

        subscription_url = None
        subscription_json_url = None
        try:
            settings = await self._fetch_panel_settings()
            subscription_url, subscription_json_url = self._build_subscription_urls(settings, sub_id)
        except Exception:
            subscription_url = None
            subscription_json_url = None

        config_text = await self._fetch_config_from_subscription(subscription_url)
        if not config_text:
            # Фолбэк: генерируем ссылку из тех же полей inbound/client, что использует фронтенд панели.
            config_text = await self._build_client_link_from_inbound(
                inbound_id=inbound_id,
                client_uuid=client_uuid,
                client_email=client_email,
            )
        if not config_text:
            # Последний фолбэк: простая ссылка из env.
            server = getattr(self._config, "vless_server", None) or None
            port = getattr(self._config, "vless_port", None)
            if server and port is not None:
                config_text = f"vless://{client_uuid}@{server}:{port}#{client_email}"
            else:
                config_text = f"Скопируй конфиг из панели 3x-ui (email: {client_email})"
        config_text = self._apply_display_name_to_config(config_text, display_name) or config_text
        return ThreeXUIClientInfo(
            client_id=client_uuid,
            config_text=config_text,
            remark=client_email,
            sub_id=sub_id,
            subscription_url=subscription_url,
            subscription_json_url=subscription_json_url,
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

    def _extract_clients(self, inbound_obj: dict[str, Any]) -> list[dict[str, Any]]:
        settings_raw = inbound_obj.get("settings") or "{}"
        settings = json.loads(settings_raw) if isinstance(settings_raw, str) else (settings_raw or {})
        clients = settings.get("clients") or []
        return clients if isinstance(clients, list) else []

    def _extract_payload(self, data: Any) -> Any:
        if isinstance(data, dict):
            if data.get("obj") is not None:
                return data.get("obj")
            if data.get("data") is not None:
                return data.get("data")
        return data

    def _normalize_ip_string(self, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        lowered = text.lower()
        if lowered in {"no ip record", "error loading ips", "null", "none"}:
            return None
        if " (" in text:
            text = text.split(" (", 1)[0].strip()
        return text or None

    def _parse_client_ips_payload(self, payload: Any) -> dict[str, Any]:
        unique_ips: list[str] = []

        def add_ip(value: Any) -> None:
            ip = self._normalize_ip_string(value)
            if not ip or ip in unique_ips:
                return
            unique_ips.append(ip)

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    add_ip(item.get("ip") or item.get("IP"))
                else:
                    add_ip(item)
        elif isinstance(payload, dict):
            add_ip(payload.get("ip") or payload.get("IP"))
        else:
            add_ip(payload)

        return {"available": True, "ips": unique_ips, "ip_count": len(unique_ips)}

    async def get_client_ips(self, inbound_id: int, client_uuid: str) -> dict[str, Any]:
        """
        Получить список IP клиента из MHSanaei/3x-ui.
        В этой панели endpoint работает по email клиента:
        POST /panel/api/inbounds/clientIps/:email
        """
        await self._ensure_login()
        inbound = await self._get_inbound(inbound_id)
        if not inbound:
            return {"available": False, "ips": [], "ip_count": 0}
        clients = self._extract_clients(inbound)
        target = next((client for client in clients if client.get("id") == client_uuid), None)
        if not target:
            return {"available": False, "ips": [], "ip_count": 0}

        client_email = str(target.get("email") or "").strip()
        if not client_email:
            return {"available": False, "ips": [], "ip_count": 0}
        path = f"/panel/api/inbounds/clientIps/{urllib.parse.quote(client_email, safe='')}"
        try:
            response = await self._client.post(path, cookies=self._auth_cookies)
            if response.status_code == 404:
                return {"available": False, "ips": [], "ip_count": 0}
            response.raise_for_status()
            data = response.json()
            payload = self._extract_payload(data)
            return self._parse_client_ips_payload(payload)
        except Exception:
            return {"available": False, "ips": [], "ip_count": 0}

    async def get_online_clients(self) -> dict[str, Any]:
        """
        Получить список online-клиентов из MHSanaei/3x-ui:
        POST /panel/api/inbounds/onlines
        """
        await self._ensure_login()
        try:
            response = await self._client.post("/panel/api/inbounds/onlines", cookies=self._auth_cookies)
            if response.status_code == 404:
                return {"available": False, "clients": []}
            response.raise_for_status()
            data = response.json()
            payload = self._extract_payload(data)
            if isinstance(payload, list):
                clients = [str(item).strip() for item in payload if str(item).strip()]
            else:
                clients = []
            return {"available": True, "clients": clients}
        except Exception:
            return {"available": False, "clients": []}

    async def get_client_traffic(self, inbound_id: int, client_uuid: str) -> Optional[dict[str, Any]]:
        """
        Вернуть лимит/расход/остаток трафика клиента, если эти поля есть в ответе 3x-ui.
        """
        await self._ensure_login()
        inbound = await self._get_inbound(inbound_id)
        if not inbound:
            return None
        clients = self._extract_clients(inbound)
        target = next((client for client in clients if client.get("id") == client_uuid), None)
        if not target:
            return None

        total_bytes = int(target.get("totalGB") or 0)
        up_bytes = int(target.get("up") or 0)
        down_bytes = int(target.get("down") or 0)
        used_bytes = int(target.get("total") or 0)
        if used_bytes <= 0:
            used_bytes = up_bytes + down_bytes

        if total_bytes <= 0:
            return {
                "is_unlimited": True,
                "limit_bytes": 0,
                "used_bytes": used_bytes,
                "remaining_bytes": None,
            }

        remaining = max(total_bytes - used_bytes, 0)
        return {
            "is_unlimited": False,
            "limit_bytes": total_bytes,
            "used_bytes": used_bytes,
            "remaining_bytes": remaining,
        }

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
        client_flow: str = "",
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
            # Хост и порт для клиента: в панели инбаунд может слушать 8443 (внутренний xray),
            # а снаружи клиенты подключаются к 443 через nginx/stream. Задаём в .env VLESS_SERVER и VLESS_PORT.
            client_port = getattr(self._config, "vless_port", None)
            if client_port is not None:
                port = int(client_port)
            client_host = getattr(self._config, "vless_server", None) or None
            if client_host:
                host = client_host
            else:
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
            params = ["type=" + str(network), "encryption=none"]
            tcp = self._get_nested(stream, "tcpSettings", "tcp_settings") or {}
            if isinstance(tcp, str):
                tcp = json.loads(tcp) if tcp.strip() else {}
            ws = self._get_nested(stream, "wsSettings", "ws_settings") or {}
            if isinstance(ws, str):
                ws = json.loads(ws) if ws.strip() else {}
            grpc = self._get_nested(stream, "grpcSettings", "grpc_settings") or {}
            if isinstance(grpc, str):
                grpc = json.loads(grpc) if grpc.strip() else {}
            httpupgrade = self._get_nested(stream, "httpupgradeSettings", "httpupgrade_settings") or {}
            if isinstance(httpupgrade, str):
                httpupgrade = json.loads(httpupgrade) if httpupgrade.strip() else {}
            xhttp = self._get_nested(stream, "xhttpSettings", "xhttp_settings") or {}
            if isinstance(xhttp, str):
                xhttp = json.loads(xhttp) if xhttp.strip() else {}

            if network == "tcp":
                header = self._get_nested(tcp, "header", "Header") or {}
                request = self._get_nested(header, "request", "Request") or {}
                header_type = self._get_nested(header, "type", "Type")
                if header_type == "http":
                    path_list = request.get("path") or []
                    if isinstance(path_list, list) and path_list:
                        params.append("path=" + urllib.parse.quote(",".join(str(x) for x in path_list), safe=""))
                    headers = request.get("headers") or {}
                    host = headers.get("Host") or headers.get("host") or ""
                    if isinstance(host, list):
                        host = ",".join(str(x) for x in host if x)
                    if host:
                        params.append("host=" + urllib.parse.quote(str(host), safe=""))
                    params.append("headerType=http")
            elif network == "ws":
                path = ws.get("path") or ""
                host_header = ws.get("host") or ws.get("Host") or ""
                if path:
                    params.append("path=" + urllib.parse.quote(str(path), safe="/,"))
                if host_header:
                    params.append("host=" + urllib.parse.quote(str(host_header), safe=",:"))
            elif network == "grpc":
                service_name = grpc.get("serviceName") or grpc.get("service_name") or ""
                authority = grpc.get("authority") or ""
                if service_name:
                    params.append("serviceName=" + urllib.parse.quote(str(service_name), safe=""))
                if authority:
                    params.append("authority=" + urllib.parse.quote(str(authority), safe=""))
                if grpc.get("multiMode") or grpc.get("multi_mode"):
                    params.append("mode=multi")
            elif network == "httpupgrade":
                path = httpupgrade.get("path") or ""
                host_header = httpupgrade.get("host") or httpupgrade.get("Host") or ""
                if path:
                    params.append("path=" + urllib.parse.quote(str(path), safe="/,"))
                if host_header:
                    params.append("host=" + urllib.parse.quote(str(host_header), safe=",:"))
            elif network == "xhttp":
                path = xhttp.get("path") or ""
                host_header = xhttp.get("host") or xhttp.get("Host") or ""
                mode = xhttp.get("mode") or ""
                if path:
                    params.append("path=" + urllib.parse.quote(str(path), safe="/,"))
                if host_header:
                    params.append("host=" + urllib.parse.quote(str(host_header), safe=",:"))
                if mode:
                    params.append("mode=" + urllib.parse.quote(str(mode), safe=""))

            params.append("security=" + str(security))
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
                spider_x = (
                    self._get_nested(reality, "spiderX", "spider_x")
                    or settings.get("spiderX")
                    or settings.get("spider_x")
                    or "/"
                )
                if spider_x:
                    params.append("spx=" + urllib.parse.quote(str(spider_x), safe=""))
                if network == "tcp" and client_flow:
                    params.append("flow=" + urllib.parse.quote(str(client_flow), safe=""))
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
        clients = self._extract_clients(obj)
        target = next((client for client in clients if client.get("id") == client_uuid), None)
        client_flow = str((target or {}).get("flow") or "").strip()
        return self._build_vless_from_inbound(obj, client_uuid, client_email, client_flow=client_flow)

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
            clients = self._extract_clients(obj)
            return any(c.get("id") == client_uuid for c in clients)
        except Exception:
            return True

    async def extend_client(self, inbound_id: int, client_uuid: str, add_days: int, add_total_gb: int) -> bool:
        """
        Продлить существующего клиента: увеличить expiryTime и totalGB.
        """
        await self._ensure_login()
        inbound = await self._get_inbound(inbound_id)
        if not inbound:
            return False

        clients = self._extract_clients(inbound)
        target = next((client for client in clients if client.get("id") == client_uuid), None)
        if not target:
            return False

        now_ms = int(time.time() * 1000)
        current_expiry = int(target.get("expiryTime") or 0)
        base_expiry = current_expiry if current_expiry and current_expiry > now_ms else now_ms
        target["expiryTime"] = base_expiry + max(int(add_days), 1) * 24 * 60 * 60 * 1000

        current_total = int(target.get("totalGB") or 0)
        add_bytes = 0 if int(add_total_gb or 0) <= 0 else int(add_total_gb) * (1024**3)
        if current_total == 0 or add_bytes == 0:
            target["totalGB"] = 0
        else:
            target["totalGB"] = current_total + add_bytes
        target["enable"] = True
        target["limitIp"] = DEFAULT_IP_LIMIT

        payload_obj = {"id": inbound_id, "settings": {"clients": [target]}}
        response = await self._client.post(
            f"/panel/api/inbounds/updateClient/{client_uuid}",
            json=payload_obj,
            cookies=self._auth_cookies,
        )
        response.raise_for_status()
        try:
            data = response.json()
            if data.get("success"):
                return True
        except Exception:
            pass

        payload_str = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [target]}, ensure_ascii=False, separators=(",", ":")),
        }
        response = await self._client.post(
            f"/panel/api/inbounds/updateClient/{client_uuid}",
            json=payload_str,
            cookies=self._auth_cookies,
        )
        response.raise_for_status()
        try:
            return bool(response.json().get("success"))
        except Exception:
            return True

    async def close(self) -> None:
        await self._client.aclose()

