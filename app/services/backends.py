from __future__ import annotations

from collections.abc import Iterable

from app.config import AppConfig, ThreeXUIConfig
from app.db import get_pool
from app.threexui_client import ThreeXUIClient


ThreeXUIRegistry = dict[str, ThreeXUIClient]


def build_threexui_registry(config: AppConfig) -> ThreeXUIRegistry:
    return {
        key: ThreeXUIClient(backend_config)
        for key, backend_config in config.threexui_backends.items()
        if backend_config.enabled
    }


async def close_threexui_registry(registry: ThreeXUIRegistry) -> None:
    for client in registry.values():
        await client.close()


def get_enabled_backend_configs(config: AppConfig) -> list[ThreeXUIConfig]:
    return [backend for backend in config.threexui_backends.values() if backend.enabled]


def get_default_backend_config(config: AppConfig) -> ThreeXUIConfig:
    return config.threexui_backends[config.default_threexui_key]


def get_default_threexui_client(registry: ThreeXUIRegistry, default_key: str) -> ThreeXUIClient:
    if default_key in registry:
        return registry[default_key]
    if registry:
        return next(iter(registry.values()))
    raise RuntimeError("No enabled 3x-ui backends configured")


def get_registry_client(registry: ThreeXUIRegistry, backend_key: str | None, fallback_key: str) -> ThreeXUIClient:
    key = backend_key or fallback_key
    if key in registry:
        return registry[key]
    return get_default_threexui_client(registry, fallback_key)


def iter_registry_items(registry: ThreeXUIRegistry) -> Iterable[tuple[str, ThreeXUIClient]]:
    return registry.items()


async def pick_backend_for_new_subscription(
    *,
    registry: ThreeXUIRegistry,
    backend_configs: dict[str, ThreeXUIConfig],
    default_key: str,
) -> ThreeXUIConfig:
    enabled_configs = [
        config
        for key, config in backend_configs.items()
        if config.enabled and key in registry
    ]
    if not enabled_configs:
        raise RuntimeError("No enabled 3x-ui backends configured")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT backend_key, COUNT(*) AS total
            FROM subscriptions
            WHERE is_active = TRUE AND (expires_at IS NULL OR expires_at > NOW())
            GROUP BY backend_key
            """
        )
    counts = {str(row["backend_key"] or ""): int(row["total"] or 0) for row in rows}

    def sort_key(config: ThreeXUIConfig) -> tuple[float, int, int, str]:
        active_devices = counts.get(config.key, 0)
        weighted_score = active_devices / max(config.weight, 1)
        is_not_default = 0 if config.key == default_key else 1
        return (weighted_score, active_devices, is_not_default, config.key)

    return min(enabled_configs, key=sort_key)
