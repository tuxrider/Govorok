"""Информация о стриме для переменных {uptime}, {game}, {title}, {viewers}.

Данные берутся из Helix ``GET /helix/streams`` и требуют Client-Id приложения и
токен бота. Значения кэшируются: команды отвечают мгновенно, а сеть обновляет
кэш в фоне. Без токена переменные отдают «нет данных».
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any, Callable, Iterable

import httpx

from .config import AppConfig

HELIX_STREAMS = "https://api.twitch.tv/helix/streams"


class StreamInfo:
    """Кэш состояния каналов: жив ли стрим, какая игра, сколько зрителей."""

    def __init__(self, config_provider: Callable[[], AppConfig], *, on_log: Callable[[str], None] | None = None,
                 client: httpx.AsyncClient | None = None, ttl: float = 60.0) -> None:
        self._config = config_provider
        self.on_log = on_log
        self._client = client
        self._own_client = client is None
        self.ttl = ttl
        self._cache: dict[str, dict[str, Any]] = {}
        self._fetched_at: dict[str, float] = {}
        self.last_error = ""
        self.requests = 0

    # ------------------------------------------------------------ утилиты

    def _log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001
                pass

    def enabled(self) -> bool:
        twitch = self._config().twitch
        return bool(twitch.client_id.strip() and twitch.oauth_token.strip())

    def _client_instance(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(12.0),
                                             headers={"User-Agent": "Govorok/0.1 (+local)"})
            self._own_client = True
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None and self._own_client:
            await client.aclose()

    def _headers(self) -> dict[str, str]:
        twitch = self._config().twitch
        token = twitch.normalized_token().removeprefix("oauth:")
        return {"Client-Id": twitch.client_id.strip(), "Authorization": f"Bearer {token}"}

    # -------------------------------------------------------------- данные

    def cached(self, channel: str) -> dict[str, Any]:
        """Последнее известное состояние канала (без обращения к сети)."""
        return self._cache.get((channel or "").lower(), {})

    async def refresh(self, channel: str) -> dict[str, Any]:
        """Обновить состояние одного канала."""
        channel = (channel or "").lower()
        if not channel or not self.enabled():
            return self.cached(channel)
        try:
            response = await self._client_instance().get(
                HELIX_STREAMS, params={"user_login": channel}, headers=self._headers())
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return self.cached(channel)
        self.requests += 1
        if response.status_code != 200:
            self.last_error = (f"токен не подошёл ({response.status_code})"
                               if response.status_code in (401, 403)
                               else f"HTTP {response.status_code}")
            self._log(f"информация о стриме: {self.last_error}")
            return self.cached(channel)
        self.last_error = ""
        data = (response.json() or {}).get("data") or []
        info: dict[str, Any] = {"live": bool(data), "fetched_at": time.time()}
        if data:
            stream = data[0]
            info.update({
                "title": stream.get("title") or "",
                "game": stream.get("game_name") or "",
                "viewers": int(stream.get("viewer_count") or 0),
                "started_at": self._parse_time(stream.get("started_at")),
            })
        self._cache[channel] = info
        self._fetched_at[channel] = time.time()
        return info

    @staticmethod
    def _parse_time(value: Any) -> float:
        if not value:
            return 0.0
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0

    async def poll_loop(self, channels_provider: Callable[[], Iterable[str]],
                        interval: float | None = None) -> None:
        """Периодически обновлять состояние каналов, пока задачу не отменят."""
        while True:
            try:
                if self.enabled():
                    for channel in list(channels_provider()):
                        await self.refresh(channel)
                        await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log(f"информация о стриме: сбой ({exc!r})")
            await asyncio.sleep(interval if interval is not None else self.ttl)
