"""Зрители канала: кто пишет в чат и (по возможности) кто в нём находится.

Источники:

* **активность** — каждый, кто написал хотя бы одно сообщение в этой сессии.
  Работает всегда, в том числе при анонимном чтении чата;
* **официальный список зрителей** — ``GET /helix/chat/chatters``. Требует
  Client-Id приложения, пользовательский токен со scope ``moderator:read:chatters``
  и права модератора в канале. Тогда в списке появляются и молчащие зрители.

Легаси-эндпоинт ``tmi.twitch.tv/group/user/{login}/chatters`` больше не работает
(отдаёт 404), а JOIN/PART-события Twitch присылает только авторизованным
подключениям, поэтому «полного» списка зрителей без токена не существует.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import httpx

from .config import AppConfig
from .irc import ChatMessage

HELIX_CHATTERS = "https://api.twitch.tv/helix/chat/chatters"
HELIX_USERS = "https://api.twitch.tv/helix/users"
MAX_CHATTERS_PAGE = 1000

GROUP_BADGES = {"broadcaster": "STREAMER", "moderators": "MOD", "vips": "VIP",
                "staff": "STAFF", "admins": "ADMIN", "global_mods": "GMOD"}


@dataclass(slots=True)
class Viewer:
    """Один зритель: либо писал в чат, либо есть в официальном списке."""

    login: str
    display: str = ""
    color: str = ""
    badges: list[str] = field(default_factory=list)
    channel: str = ""
    group: str = "viewers"
    messages: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_text: str = ""
    in_chat: bool = False        # подтверждён официальным списком зрителей

    def as_dict(self, *, now: float, idle_seconds: float) -> dict[str, Any]:
        active = self.messages > 0 and (now - self.last_seen) <= idle_seconds
        return {
            "login": self.login,
            "display": self.display or self.login,
            "color": self.color,
            "badges": self.badges,
            "group": self.group,
            "messages": self.messages,
            "last_text": self.last_text[:160],
            "last_seen": round(self.last_seen, 1),
            "idle_seconds": int(max(0.0, now - self.last_seen)) if self.last_seen else -1,
            "active": active,
            "online": bool(self.in_chat) or active,
            "in_chat": bool(self.in_chat),
            "lurker": self.messages == 0,
        }


class ViewerRegistry:
    """Копилка наблюдений за зрителями канала."""

    def __init__(self, *, on_log: Callable[[str], None] | None = None, max_viewers: int = 4000) -> None:
        self.on_log = on_log
        self.max_viewers = max(100, max_viewers)
        self._viewers: dict[tuple[str, str], Viewer] = {}     # (канал, логин) -> зритель
        self._chatters_at: dict[str, float] = {}
        self._chatters_total: dict[str, int] = {}
        self._chatters_error: dict[str, str] = {}
        self.messages_seen = 0

    # ------------------------------------------------------------- запись

    def note(self, message: ChatMessage) -> None:
        """Отметить автора сообщения."""
        login = (message.user or "").lower()
        if not login:
            return
        key = (message.channel, login)
        viewer = self._viewers.get(key)
        now = message.ts or time.time()
        if viewer is None:
            viewer = Viewer(login=login, channel=message.channel, first_seen=now)
            self._viewers[key] = viewer
            self._prune()
        viewer.display = message.display_name or viewer.display or login
        viewer.color = message.color or viewer.color
        if message.badge_labels:
            viewer.badges = message.badge_labels
        if message.is_broadcaster:
            viewer.group = "broadcaster"
        elif message.is_mod:
            viewer.group = "moderators"
        elif message.is_vip:
            viewer.group = "vips"
        viewer.messages += 1
        viewer.last_seen = now
        viewer.last_text = message.text
        self.messages_seen += 1

    def apply_chatters(self, channel: str, entries: Iterable[tuple[str, str]], *,
                       total: int | None = None, error: str = "") -> None:
        """Применить официальный список зрителей: (логин, группа)."""
        now = time.time()
        seen: set[tuple[str, str]] = set()
        for login, group in entries:
            login = (login or "").lower()
            if not login:
                continue
            key = (channel, login)
            seen.add(key)
            viewer = self._viewers.get(key)
            if viewer is None:
                viewer = Viewer(login=login, channel=channel, group=group or "viewers")
                self._viewers[key] = viewer
            viewer.in_chat = True
            if group:
                viewer.group = group
            badge = GROUP_BADGES.get(group or "")
            if badge and badge not in viewer.badges:
                viewer.badges = [*viewer.badges, badge]
            if not viewer.display:
                viewer.display = login
        for key, viewer in self._viewers.items():
            if key[0] == channel and key not in seen:
                viewer.in_chat = False
        self._chatters_at[channel] = now
        if total is not None:
            self._chatters_total[channel] = total
        self._chatters_error[channel] = error
        self._prune()

    def clear(self) -> None:
        self._viewers.clear()
        self._chatters_at.clear()
        self._chatters_total.clear()
        self._chatters_error.clear()
        self.messages_seen = 0

    def _prune(self) -> None:
        """Не даём словарю расти бесконечно: убираем самых старых и молчаливых."""
        if len(self._viewers) <= self.max_viewers:
            return
        ordered = sorted(self._viewers.items(),
                         key=lambda item: (item[1].in_chat, item[1].last_seen), reverse=False)
        for key, _viewer in ordered[: len(self._viewers) - self.max_viewers]:
            self._viewers.pop(key, None)

    # ------------------------------------------------------------- чтение

    def snapshot(self, *, idle_minutes: int = 15, limit: int = 400) -> dict[str, Any]:
        """Снимок для вкладки «Зрители»."""
        now = time.time()
        idle_seconds = max(60, int(idle_minutes) * 60)
        viewers = [viewer.as_dict(now=now, idle_seconds=idle_seconds)
                   for viewer in self._viewers.values()]
        active = [item for item in viewers if item["active"]]
        lurkers = [item for item in viewers if item["lurker"]]
        online = [item for item in viewers if item["online"]]

        def sort_key(item: dict) -> tuple:
            return (0 if item["active"] else (1 if item["in_chat"] or item["online"] else 2),
                    -item["messages"], -item["last_seen"])

        viewers.sort(key=sort_key)
        channels = sorted({viewer.channel for viewer in self._viewers.values() if viewer.channel})
        fetched = max(self._chatters_at.values(), default=0.0)
        errors = {channel: error for channel, error in self._chatters_error.items() if error}
        return {
            "type": "viewers",
            "total": len(viewers),
            "active": len(active),
            "online": len(online),
            "lurkers": len(lurkers),
            "idle_minutes": idle_minutes,
            "channels": channels,
            "chatters_available": bool(self._chatters_at),
            "chatters_fetched_at": fetched,
            "chatters_total": max(self._chatters_total.values(), default=0),
            "chatters_error": next(iter(errors.values()), ""),
            "viewers": viewers[:limit],
            "truncated": max(0, len(viewers) - limit),
        }


class ChattersPoller:
    """Периодически забирает официальный список зрителей через Helix.

    Без Client-Id и модераторского токена не запускается — вкладка «Зрители»
    в этом случае работает на активности чата, о чём сообщает сама.
    """

    def __init__(self, registry: ViewerRegistry, config_provider: Callable[[], AppConfig], *,
                 on_log: Callable[[str], None] | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.registry = registry
        self._config = config_provider
        self.on_log = on_log
        self._client = client
        self._own_client = client is None
        self.bot_user_id = ""
        self.last_error = ""
        self.last_ok_at = 0.0
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
        return bool(twitch.client_id.strip() and twitch.oauth_token.strip()
                    and self._config().viewers.fetch_chatters)

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
        token = twitch.normalized_token()
        return {"Client-Id": twitch.client_id.strip(),
                "Authorization": f"Bearer {token.removeprefix('oauth:')}"}

    # -------------------------------------------------------------- работа

    async def resolve_bot_id(self) -> str:
        """Узнать user id бота: он нужен как moderator_id в запросе."""
        if self.bot_user_id:
            return self.bot_user_id
        try:
            response = await self._client_instance().get(HELIX_USERS, headers=self._headers())
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return ""
        if response.status_code != 200:
            self.last_error = self._describe(response)
            return ""
        data = (response.json() or {}).get("data") or []
        if data:
            self.bot_user_id = str(data[0].get("id") or "")
        return self.bot_user_id

    async def fetch(self, room_id: str, channel: str) -> bool:
        """Забрать список зрителей канала. Возвращает успех."""
        moderator_id = await self.resolve_bot_id()
        if not moderator_id or not room_id:
            self.registry.apply_chatters(channel, [], error=self.last_error or "нет данных бота")
            return False
        params = {"broadcaster_id": str(room_id), "moderator_id": moderator_id,
                  "first": str(MAX_CHATTERS_PAGE)}
        try:
            response = await self._client_instance().get(HELIX_CHATTERS, params=params,
                                                         headers=self._headers())
        except httpx.HTTPError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            self.registry.apply_chatters(channel, [], error=self.last_error)
            return False
        self.requests += 1
        if response.status_code != 200:
            self.last_error = self._describe(response)
            self._log(f"зрители #{channel}: {self.last_error}")
            self.registry.apply_chatters(channel, [], error=self.last_error)
            return False

        payload = response.json() or {}
        entries = [(str(item.get("user_login") or "").lower(), "viewers")
                   for item in payload.get("data") or []]
        total = int(payload.get("total") or len(entries))
        self.registry.apply_chatters(channel, entries, total=total)
        self.last_ok_at = time.time()
        self.last_error = ""
        self._log(f"зрители #{channel}: получено {len(entries)} из {total}")
        return True

    async def poll_loop(self, channels_provider: Callable[[], list[tuple[str, str]]]) -> None:
        """Опрашивать каналы с заданным интервалом, пока задача не отменена."""
        while True:
            try:
                if self.enabled():
                    for room_id, channel in channels_provider():
                        await self.fetch(room_id, channel)
                        await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log(f"зрители: сбой опроса ({exc!r})")
            interval = max(20, int(self._config().viewers.poll_seconds))
            await asyncio.sleep(interval)

    @staticmethod
    def _describe(response: httpx.Response) -> str:
        if response.status_code == 401:
            return "токен не подошёл (401)"
        if response.status_code == 403:
            return "нужен модератор в канале и scope moderator:read:chatters (403)"
        if response.status_code == 404:
            return "канал не найден (404)"
        return f"HTTP {response.status_code}"
