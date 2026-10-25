"""Сторонние смайлы: BetterTTV, FrankerFaceZ и 7TV.

Twitch отдаёт позиции только своих смайлов (тег ``emotes``), поэтому
сторонние приходится искать по словам в тексте сообщения.

Что здесь есть:

* чистые парсеры ответов трёх API (их удобно тестировать без сети);
* реестр с приоритетом «канал важнее глобальных» и «BTTV > FFZ > 7TV»;
* ленивая загрузка смайлов канала по ``room-id`` из тегов Twitch;
* дисковый кэш, чтобы после перезапуска смайлы были сразу и работали офлайн.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

BTTV_GLOBAL_URL = "https://api.betterttv.net/3/cached/emotes/global"
BTTV_CHANNEL_URL = "https://api.betterttv.net/3/cached/users/twitch/{room_id}"
FFZ_GLOBAL_URL = "https://api.frankerfacez.com/v1/set/global"
FFZ_ROOM_ID_URL = "https://api.frankerfacez.com/v1/room/id/{room_id}"
FFZ_ROOM_LOGIN_URL = "https://api.frankerfacez.com/v1/room/{login}"
SEVENTV_GLOBAL_URL = "https://7tv.io/v3/emote-sets/global"
SEVENTV_CHANNEL_URL = "https://7tv.io/v3/users/twitch/{room_id}"

#: порядок важен: чем позже — тем выше приоритет при совпадении кода
PROVIDERS = ("7tv", "ffz", "bttv")
PROVIDER_TITLES = {"bttv": "BTTV", "ffz": "FFZ", "7tv": "7TV"}

#: предпочтительные файлы 7TV по убыванию
SEVENTV_FILE_ORDER = ("2x.webp", "2x.gif", "2x.png", "1x.webp", "1x.gif", "1x.png", "3x.webp")

TOKEN_RE = re.compile(r"\S+")
CACHE_VERSION = 1
MAX_CACHED_CHANNELS = 40
#: неудачную (пустую) загрузку канала повторяем не чаще, чем раз в полчаса
CHANNEL_RETRY_AFTER = 1800.0


@dataclass(slots=True)
class ThirdPartyEmote:
    """Один сторонний смайл."""

    code: str
    url: str
    provider: str
    width: int = 0
    height: int = 0
    animated: bool = False

    @property
    def aspect(self) -> float | None:
        """Соотношение сторон (для широких смайлов вроде FFZ WideHard)."""
        if self.width > 0 and self.height > 0:
            return self.width / self.height
        return None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "url": self.url, "provider": self.provider,
                "width": self.width, "height": self.height, "animated": self.animated}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ThirdPartyEmote | None":
        code, url = data.get("code"), data.get("url")
        if not code or not url:
            return None
        try:
            return cls(
                code=str(code),
                url=str(url),
                provider=str(data.get("provider") or "?"),
                width=int(data.get("width") or 0),
                height=int(data.get("height") or 0),
                animated=bool(data.get("animated")),
            )
        except (TypeError, ValueError):
            return None


# ------------------------------------------------------------------ парсеры


def _magnitude(aspect: float, base: int) -> tuple[int, int]:
    """Габариты картинки при заданной высоте base с сохранением пропорций."""
    height = base
    width = max(1, int(round(base * aspect)))
    return width, height


def parse_bttv_emotes(items: Iterable[dict]) -> list[ThirdPartyEmote]:
    """Разобрать список BTTV (глобальный или канальный)."""
    result: list[ThirdPartyEmote] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        emote_id, code = item.get("id"), item.get("code")
        if not emote_id or not code:
            continue
        result.append(ThirdPartyEmote(
            code=str(code),
            url=f"https://cdn.betterttv.net/emote/{emote_id}/2x",
            provider="bttv",
            width=0,
            height=0,
            animated=bool(item.get("animated") or item.get("imageType") == "gif"),
        ))
    return result


def parse_bttv_response(data: dict) -> list[ThirdPartyEmote]:
    """Канальный ответ BTTV: channelEmotes + sharedEmotes."""
    if not isinstance(data, dict):
        return []
    return parse_bttv_emotes(list(data.get("channelEmotes") or []) + list(data.get("sharedEmotes") or []))


def _ffz_url(urls: dict) -> str:
    """Выбрать картинку FFZ (ключи "1"/"2"/"4") и починить протокол."""
    for key in ("2", "1", "4"):
        value = urls.get(key)
        if value:
            url = str(value)
            return f"https:{url}" if url.startswith("//") else url
    return ""


def parse_ffz_sets(sets: Any) -> list[ThirdPartyEmote]:
    """Разобрать словарь сетов FFZ {id: {emoticons: [...]}}."""
    result: list[ThirdPartyEmote] = []
    if not isinstance(sets, dict):
        return result
    for one_set in sets.values():
        if not isinstance(one_set, dict):
            continue
        for item in one_set.get("emoticons") or []:
            if not isinstance(item, dict) or item.get("hidden"):
                continue
            name = item.get("name")
            url = _ffz_url(item.get("urls") or {})
            if not name or not url:
                continue
            result.append(ThirdPartyEmote(
                code=str(name),
                url=url,
                provider="ffz",
                width=int(item.get("width") or 0),
                height=int(item.get("height") or 0),
                animated=bool(item.get("animated")),
            ))
    return result


def parse_ffz_global(data: dict) -> list[ThirdPartyEmote]:
    """Глобальные смайлы FFZ: только сеты из default_sets."""
    if not isinstance(data, dict):
        return []
    sets = data.get("sets") or {}
    default = data.get("default_sets")
    if isinstance(default, list) and default:
        picked = {str(key): sets[str(key)] for key in default if str(key) in sets}
        if picked:
            return parse_ffz_sets(picked)
    return parse_ffz_sets(sets)


def parse_7tv_emotes(emotes: Iterable[dict]) -> list[ThirdPartyEmote]:
    """Разобрать emotes из ответа 7TV (сет или emote_set)."""
    result: list[ThirdPartyEmote] = []
    for item in emotes or []:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        code = item.get("name") or data.get("name")
        host = data.get("host") or {}
        base = str(host.get("url") or "")
        if not code or not base:
            continue
        if base.startswith("//"):
            base = f"https:{base}"
        files = {str(f.get("name")): f for f in (host.get("files") or []) if isinstance(f, dict)}
        chosen: dict | None = None
        for name in SEVENTV_FILE_ORDER:
            if name in files:
                chosen = files[name]
                break
        if chosen is None and files:
            chosen = next(iter(files.values()))
        url = f"{base}/{chosen.get('name')}" if chosen else f"{base}/2x.webp"
        result.append(ThirdPartyEmote(
            code=str(code),
            url=url,
            provider="7tv",
            width=int((chosen or {}).get("width") or 0),
            height=int((chosen or {}).get("height") or 0),
            animated=bool(data.get("animated") or int((chosen or {}).get("frame_count") or 1) > 1),
        ))
    return result


def parse_7tv_set(data: dict) -> list[ThirdPartyEmote]:
    """Сет 7TV: {"emotes": [...]} или ответ пользователя с emote_set."""
    if not isinstance(data, dict):
        return []
    emote_set = data.get("emote_set")
    if isinstance(emote_set, dict):
        return parse_7tv_emotes(emote_set.get("emotes") or [])
    return parse_7tv_emotes(data.get("emotes") or [])


def merge_emotes(*groups: Iterable[ThirdPartyEmote]) -> dict[str, ThirdPartyEmote]:
    """Склеить наборы: побеждает тот, что передан позже (приоритет источников)."""
    merged: dict[str, ThirdPartyEmote] = {}
    for group in groups:
        for emote in group:
            merged[emote.code] = emote
    return merged


# ------------------------------------------------------------------- реестр


class EmoteRegistry:
    """Глобальные и канальные смайлы трёх провайдеров с кэшем на диске."""

    def __init__(
        self,
        config_provider: Callable[[], Any],
        *,
        cache_path: Path | None = None,
        on_log: Callable[[str], None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config_provider
        self.cache_path = cache_path
        self.on_log = on_log
        self._client = client
        self._own_client = client is None

        self._global_by_provider: dict[str, list[ThirdPartyEmote]] = {p: [] for p in PROVIDERS}
        self._channel_by_provider: dict[str, dict[str, list[ThirdPartyEmote]]] = {}
        self._global_lookup: dict[str, ThirdPartyEmote] = {}
        self._channel_lookup: dict[str, dict[str, ThirdPartyEmote]] = {}

        self._globals_loaded_at = 0.0
        self._channel_loaded_at: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self.errors = 0
        self.load_from_disk()

    # -------------------------------------------------------------- утилиты

    def _log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001
                pass

    def _settings(self):
        return self._config().emotes

    def _client_instance(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(12.0),
                headers={"User-Agent": "Govorok/0.1 (+local)"},
                follow_redirects=True,
            )
            self._own_client = True
        return self._client

    async def aclose(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        client, self._client = self._client, None
        if client is not None and self._own_client:
            await client.aclose()

    # ------------------------------------------------------- доступ к данным

    @property
    def global_count(self) -> int:
        return len(self._global_lookup)

    def channel_count(self, room_id: str | None) -> int:
        return len(self._channel_lookup.get(str(room_id or ""), {}))

    def provider_counts(self, room_id: str | None = None) -> dict[str, int]:
        if room_id and str(room_id) in self._channel_by_provider:
            source = self._channel_by_provider[str(room_id)]
        else:
            source = self._global_by_provider
        return {provider: len(source.get(provider, [])) for provider in PROVIDERS}

    @property
    def pending_loads(self) -> int:
        """Сколько каналов ещё грузятся прямо сейчас."""
        return sum(1 for task in self._tasks.values() if not task.done())

    @property
    def loaded_channels(self) -> list[str]:
        return list(self._channel_lookup)

    def total_channel_emotes(self) -> int:
        """Суммарно смайлов по всем загруженным каналам."""
        return sum(len(codes) for codes in self._channel_lookup.values())

    def stats(self, room_id: str | None = None) -> str:
        counts = self.provider_counts(room_id)
        return " + ".join(f"{PROVIDER_TITLES[p]} {counts[p]}" for p in ("bttv", "ffz", "7tv") if counts[p])

    def get(self, code: str, room_id: str | None = None) -> ThirdPartyEmote | None:
        """Найти смайл: сначала канал, потом глобальные."""
        if room_id:
            emote = self._channel_lookup.get(str(room_id), {}).get(code)
            if emote is not None:
                return emote
        return self._global_lookup.get(code)

    def resolve(self, text: str, room_id: str | None = None) -> list[tuple[str, Any]]:
        """Нарезать текст на сегменты: ("text", str) | ("emote", ThirdPartyEmote)."""
        if not text:
            return []
        parts: list[tuple[str, Any]] = []
        cursor = 0
        for match in TOKEN_RE.finditer(text):
            if match.start() > cursor:
                parts.append(("text", text[cursor:match.start()]))
            token = match.group(0)
            emote = self.get(token, room_id)
            parts.append(("emote", emote) if emote else ("text", token))
            cursor = match.end()
        if cursor < len(text):
            parts.append(("text", text[cursor:]))

        merged: list[tuple[str, Any]] = []
        for kind, value in parts:
            if kind == "text" and merged and merged[-1][0] == "text":
                merged[-1] = ("text", merged[-1][1] + value)
            else:
                merged.append((kind, value))
        return merged

    # ------------------------------------------------------------ загрузка

    async def load_globals(self, *, force: bool = False) -> None:
        """Загрузить глобальные смайлы всех включённых провайдеров."""
        settings = self._settings()
        ttl = max(1, int(settings.cache_ttl_hours)) * 3600
        if not force and self._global_lookup and time.time() - self._globals_loaded_at < ttl:
            return

        async with self._lock:
            if not force and self._global_lookup and time.time() - self._globals_loaded_at < ttl:
                return
            jobs: list[tuple[str, str, Callable[[Any], list[ThirdPartyEmote]]]] = []
            if settings.bttv:
                jobs.append(("bttv", BTTV_GLOBAL_URL, parse_bttv_emotes))
            if settings.ffz:
                jobs.append(("ffz", FFZ_GLOBAL_URL, parse_ffz_global))
            if settings.seventv:
                jobs.append(("7tv", SEVENTV_GLOBAL_URL, parse_7tv_set))

            for provider, url, parser in jobs:
                data = await self._fetch_json(url)
                if data is None:
                    continue
                try:
                    self._global_by_provider[provider] = parser(data)
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    self._log(f"смайлы {PROVIDER_TITLES.get(provider, provider)}: ошибка разбора {exc!r}")

            self._rebuild_global_lookup()
            self._globals_loaded_at = time.time()
            if self._global_lookup:
                self._log(f"глобальные смайлы: {self.stats()}")
                self.save_to_disk()

    def _rebuild_global_lookup(self) -> None:
        self._global_lookup = merge_emotes(
            *(self._global_by_provider.get(p, []) for p in PROVIDERS)
        )

    def ensure_channel(self, room_id: str, login: str = "") -> None:
        """Запустить загрузку смайлов канала в фоне (вызывается из цикла чата).

        Метод намеренно синхронный: он лишь ставит задачу, чтобы приём
        сообщений из чата никогда не ждал сеть.
        """
        room_id = str(room_id or "").strip()
        if not room_id or self._channel_is_fresh(room_id):
            return
        task = self._tasks.get(room_id)
        if task is not None and not task.done():
            return
        self._tasks[room_id] = asyncio.create_task(
            self.load_channel(room_id, login), name=f"emotes-{room_id}"
        )

    def _channel_is_fresh(self, room_id: str) -> bool:
        """Свежи ли смайлы канала: пустой набор не должен блокировать повтор."""
        loaded_at = self._channel_loaded_at.get(room_id, 0.0)
        if not loaded_at:
            return False
        if not self._channel_lookup.get(room_id):
            # загрузка не удалась (лимиты/сеть) — попробуем снова, но не сразу
            return time.time() - loaded_at < CHANNEL_RETRY_AFTER
        ttl = max(1, int(self._settings().cache_ttl_hours)) * 3600
        return time.time() - loaded_at < ttl

    async def load_channel(self, room_id: str, login: str = "") -> int:
        """Загрузить смайлы конкретного канала; возвращает их количество."""
        room_id = str(room_id or "").strip()
        if not room_id:
            return 0
        if not self._global_lookup:
            await self.load_globals()

        settings = self._settings()
        by_provider: dict[str, list[ThirdPartyEmote]] = {p: [] for p in PROVIDERS}

        if settings.bttv:
            data = await self._fetch_json(BTTV_CHANNEL_URL.format(room_id=room_id))
            if data is not None:
                by_provider["bttv"] = parse_bttv_response(data)

        if settings.ffz:
            data = await self._fetch_json(FFZ_ROOM_ID_URL.format(room_id=room_id))
            if data is None and login:
                data = await self._fetch_json(FFZ_ROOM_LOGIN_URL.format(login=login.lower()))
            if data is not None:
                by_provider["ffz"] = parse_ffz_sets((data or {}).get("sets") or {})

        if settings.seventv:
            data = await self._fetch_json(SEVENTV_CHANNEL_URL.format(room_id=room_id))
            if data is not None:
                by_provider["7tv"] = parse_7tv_set(data)

        self._channel_by_provider[room_id] = by_provider
        self._channel_lookup[room_id] = merge_emotes(*(by_provider[p] for p in PROVIDERS))
        self._channel_loaded_at[room_id] = time.time()
        total = len(self._channel_lookup[room_id])
        if total:
            self._log(f"смайлы канала {login or room_id}: {self.stats(room_id)} (всего {total})")
        else:
            self._log(f"смайлы канала {login or room_id}: ничего не получено — повтор через 30 мин")
        self.save_to_disk()
        return total

    async def _fetch_json(self, url: str) -> Any | None:
        """GET JSON с мягкой обработкой ошибок: чат из-за смайлов падать не должен."""
        for attempt in range(2):
            try:
                response = await self._client_instance().get(url)
            except httpx.HTTPError as exc:
                self.errors += 1
                self._log(f"смайлы: {type(exc).__name__} для {url}")
                if attempt == 0:
                    await asyncio.sleep(1.0)
                    continue
                return None
            if response.status_code == 404:
                return None
            if response.status_code == 429 or response.status_code >= 500:
                # 7TV и BTTV любят отдавать 429 при залпе запросов — пробуем ещё раз
                self._log(f"смайлы: HTTP {response.status_code} для {url}")
                if attempt == 0:
                    await asyncio.sleep(2.0)
                    continue
                self.errors += 1
                return None
            if response.status_code >= 400:
                self.errors += 1
                self._log(f"смайлы: HTTP {response.status_code} для {url}")
                return None
            try:
                return response.json()
            except ValueError:
                self.errors += 1
                self._log(f"смайлы: не JSON от {url}")
                return None
        return None

    # --------------------------------------------------------------- кэш

    def reset(self) -> None:
        """Сбросить загруженные смайлы — например, после смены провайдеров."""
        for provider in PROVIDERS:
            self._global_by_provider[provider] = []
        self._global_lookup = {}
        self._channel_by_provider.clear()
        self._channel_lookup.clear()
        self._channel_loaded_at.clear()
        self._globals_loaded_at = 0.0

    def load_from_disk(self) -> None:
        """Поднять смайлы из кэша: чат работает и без интернета."""
        if not self.cache_path or not Path(self.cache_path).exists():
            return
        try:
            raw = json.loads(Path(self.cache_path).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict) or raw.get("version") != CACHE_VERSION:
            return

        for provider in PROVIDERS:
            items = [ThirdPartyEmote.from_dict(item) for item in (raw.get("global", {}).get(provider) or [])]
            self._global_by_provider[provider] = [e for e in items if e]
        self._rebuild_global_lookup()
        self._globals_loaded_at = float(raw.get("global_fetched_at") or 0)

        for room_id, payload in (raw.get("channels") or {}).items():
            if not isinstance(payload, dict):
                continue
            by_provider: dict[str, list[ThirdPartyEmote]] = {}
            for provider in PROVIDERS:
                items = [ThirdPartyEmote.from_dict(item) for item in (payload.get(provider) or [])]
                by_provider[provider] = [e for e in items if e]
            merged = merge_emotes(*(by_provider[p] for p in PROVIDERS))
            if not merged:
                # пустой набор (прошлая загрузка сорвалась) не считаем кэшем —
                # в новой сессии канал попробует загрузиться заново
                continue
            self._channel_by_provider[str(room_id)] = by_provider
            self._channel_lookup[str(room_id)] = merged
            self._channel_loaded_at[str(room_id)] = float(payload.get("fetched_at") or 0)

    def save_to_disk(self) -> None:
        if not self.cache_path:
            return
        recent = sorted(self._channel_loaded_at.items(), key=lambda item: item[1], reverse=True)
        # пустые наборы не сохраняем: при следующем запуске канал загрузится заново
        keep = [room_id for room_id, _ in recent[:MAX_CACHED_CHANNELS]
                if self._channel_lookup.get(room_id)]
        payload = {
            "version": CACHE_VERSION,
            "global_fetched_at": self._globals_loaded_at,
            "global": {p: [e.to_dict() for e in self._global_by_provider.get(p, [])] for p in PROVIDERS},
            "channels": {
                room_id: {
                    "fetched_at": self._channel_loaded_at.get(room_id, 0),
                    **{p: [e.to_dict() for e in self._channel_by_provider.get(room_id, {}).get(p, [])]
                       for p in PROVIDERS},
                }
                for room_id in keep
            },
        }
        try:
            Path(self.cache_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.cache_path).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            self._log(f"кэш смайлов не сохранён: {exc}")
