"""Док-панель для OBS: локальный сервер со страницей чата.

OBS умеет показывать произвольную веб-страницу в доке
(«Виды → Доки → Пользовательские доки браузера»). Здесь живёт всё, что для
этого нужно:

* :class:`DockHub` — один порт: HTML-страница дока и WebSocket-поток сообщений;
* :func:`message_payload` — сериализация сообщения чата для браузера;
* :class:`DockRunner` — работа бота вообще без окна Flet, только док.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Response

from .config import APP_DIR, AppConfig, DATA_DIR
from .commands import CommandEngine, TimerEngine
from .emotes import EmoteRegistry
from .irc import ChatMessage, TwitchChatClient
from .render import build_segments
from .stats import stats_payload
from .streaminfo import StreamInfo
from .stt import SubtitleService
from .translator import Translation, Translator
from .viewers import ChattersPoller, ViewerRegistry

DOCK_PAGE = APP_DIR / "assets" / "dock" / "index.html"
SUBS_PAGE = APP_DIR / "assets" / "subs" / "index.html"
MAX_MESSAGE_LEN = 1200
#: цвет своих сообщений в доке (совпадает с акцентом страницы)
ACCENT_COLOR = "#a78bfa"


def message_key(message: ChatMessage) -> str:
    """Стабильный идентификатор сообщения: по нему док находит перевод."""
    return message.msg_id or f"{message.channel}-{message.ts:.3f}"


def message_payload(message: ChatMessage, emotes: EmoteRegistry | None, *, pending: bool = False,
                    config: AppConfig | None = None) -> dict:
    """Превратить сообщение чата в JSON для страницы дока."""
    if config is not None and not config.ui.render_emotes:
        segments = [{"t": "text", "v": message.text}]        # смайлы выключены в настройках
    else:
        segments = [segment.as_dict() for segment in build_segments(message, emotes)]
    return {
        "type": "message",
        "id": message_key(message),
        "ts": message.ts,
        "channel": message.channel,
        "user": message.user,
        "display": message.display_name,
        "color": message.color or "",
        "badges": message.badge_labels,
        "segments": segments,
        "pending": pending,
    }


def self_message_payload(text: str, channel: str, config: AppConfig, *, source: str = "") -> dict:
    """Пакет для сообщения, которое отправил сам бот.

    Twitch не возвращает соединению его собственные сообщения (вместо PRIVMSG
    приходит только USERSTATE), поэтому док показывает их локальным эхом —
    иначе ответ из дока выглядел бы так, будто его не отправили.
    """
    nick = (config.twitch.nickname or "").strip()
    channels = config.twitch.normalized_channels()
    now = time.time()
    return {
        "type": "message",
        "id": f"self-{int(now * 1000)}-{secrets.token_hex(3)}",
        "ts": now,
        "channel": channel or (channels[0] if channels else ""),
        "user": (nick or "bot").lower(),
        "display": nick or "бот",
        "color": ACCENT_COLOR,
        "badges": ["BOT"],
        "segments": [{"t": "text", "v": text}],
        "pending": False,
        "own": True,
        "source": source,
    }


def translation_payload(message_id: str, result: Translation) -> dict:
    """Обновление перевода для уже показанного сообщения."""
    return {
        "type": "translation",
        "id": message_id,
        "lang": result.source_lang or "",
        "target": result.target_lang or "",
        "text": result.text or "",
        "error": result.error or "",
        "cached": bool(result.from_cache),
    }


class DockHub:
    """HTTP + WebSocket сервер для док-панели OBS."""

    def __init__(
        self,
        config_provider: Callable[[], AppConfig],
        *,
        on_log: Callable[[str], None] | None = None,
        stats_provider: Callable[[], dict] | None = None,
        viewers_provider: Callable[[], dict] | None = None,
        on_send: Callable[[str], Awaitable[tuple[bool, str]]] | None = None,
        on_reset_stats: Callable[[], None] | None = None,
    ) -> None:
        self._config = config_provider
        self.on_log = on_log
        self.stats_provider = stats_provider
        self.viewers_provider = viewers_provider
        self.on_send = on_send
        self.on_reset_stats = on_reset_stats
        self.stats_interval = 5.0
        self._stats_task: asyncio.Task | None = None
        self._server = None
        self._actual_port = 0
        self._clients: set[ServerConnection] = set()
        self._subs_clients: set[ServerConnection] = set()   # подключены как оверлей субтитров
        self._pages: dict[str, list] = {}          # slot -> [mtime, bytes]
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._history: deque[dict] = deque(maxlen=200)
        self.started_at = 0.0
        self.sent_messages = 0

    # ------------------------------------------------------------- свойства

    @property
    def clients(self) -> int:
        return len(self._clients)

    @property
    def subs_clients(self) -> int:
        """Сколько оверлеев субтитров подключено (док-панель не считается)."""
        return len(self._subs_clients)

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int:
        """Фактический порт: если в конфиге 0, его выбрала система."""
        return self._actual_port or int(self._config().dock.port)

    @property
    def host(self) -> str:
        return self._config().dock.host.strip() or "127.0.0.1"

    def url(self) -> str:
        """Ссылка для «Пользовательских доков браузера» в OBS."""
        dock = self._config().dock
        ui = self._config().ui
        host = self.host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        query = [f"limit={max(10, min(500, int(dock.history)))}"]
        if dock.token:
            query.append(f"token={dock.token}")
        if ui.font_family.strip():
            query.append(f"font={quote(ui.font_family.strip())}")
        query.append(f"size={ui.clamped_size()}")
        if dock.only_translated:
            query.append("only=1")
        return f"http://{host}:{self.port}{dock.path}?{'&'.join(query)}"

    def url_subs(self) -> str:
        """Ссылка на оверлей субтитров для Браузер-источника в OBS."""
        dock = self._config().dock
        stt = self._config().stt
        host = self.host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        query = [f"font={stt.clamped_font()}", f"lines={stt.clamped_lines()}",
                 f"chars={max(10, min(200, int(stt.max_chars)))}",
                 f"hold={stt.clamped_hold():.0f}", f"pos={stt.position}",
                 f"mode={stt.clamped_mode()}", "page=subs"]
        if dock.token:
            query.append(f"token={dock.token}")
        return f"http://{host}:{self.port}/subs?{'&'.join(query)}"

    # -------------------------------------------------------------- запуск

    async def start(self) -> bool:
        """Поднять сервер. Возвращает False, если порт занят."""
        if self.running:
            return True
        self._ensure_token()
        try:
            self._server = await serve(
                self._on_client,
                self.host,
                self.port,
                process_request=self._process_request,
                ping_interval=20,
                ping_timeout=20,
            )
        except OSError as exc:
            self._log(f"док: не удалось занять {self.host}:{self.port} — {exc}")
            self._server = None
            return False
        self._actual_port = self._bound_port()
        self.started_at = time.time()
        self._pump = asyncio.create_task(self._pump_loop(), name="dock-pump")
        self._stats_task = asyncio.create_task(self._stats_loop(), name="dock-stats")
        self._log(f"док запущен: {self.url()}")
        return True

    async def stop(self) -> None:
        stats_task, self._stats_task = self._stats_task, None
        if stats_task:
            stats_task.cancel()
            try:
                await stats_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        pump, self._pump = self._pump, None
        if pump:
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for client in list(self._clients):
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients.clear()
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.05)   # дать loop завершить отмену accept-задач
        self._log("док остановлен")

    # ------------------------------------------------------------ рассылка

    def publish(self, payload: dict) -> None:
        """Положить событие в очередь (можно звать из любого sync-контекста)."""
        if self.running:
            self._queue.put_nowait(payload)

    def publish_message(self, payload: dict) -> None:
        self.sent_messages += 1
        self.publish(payload)

    def publish_translation(self, message_id: str, result: Translation) -> None:
        self.publish(translation_payload(message_id, result))

    def publish_notice(self, text: str) -> None:
        self.publish({"type": "notice", "text": text[:300], "ts": time.time()})

    def subs_settings(self) -> dict:
        """Настройки вида субтитров для оверлея (уходят в hello и в settings)."""
        stt = self._config().stt
        return {
            "font": stt.clamped_font(),
            "lines": stt.clamped_lines(),
            "chars": max(10, min(200, int(stt.max_chars))),
            "hold": stt.clamped_hold(),
            "pos": "top" if stt.position == "top" else "bottom",
            "mode": stt.clamped_mode(),
            "lang": stt.language,
            "target": (stt.translate_to or "").strip(),
        }

    def publish_subtitle(self, payload: dict) -> None:
        """Пакет субтитра: ``{"text":…, "final":true|false}``."""
        self.publish({"type": "subtitle", **payload})

    def publish_status(self, state: str, text: str) -> None:
        self.publish({"type": "status", "state": state, "text": text[:200]})
    def publish_stats(self, **stats: Any) -> None:
        self.publish({"type": "stats", **stats})

    def push_stats(self) -> None:
        """Разослать свежий снимок статистики (если он есть у владельца дока)."""
        if not self.stats_provider or not self._clients:
            return
        try:
            payload = self.stats_provider()
        except Exception as exc:  # noqa: BLE001
            self._log(f"док: статистика не собралась ({exc!r})")
            return
        self.publish(payload)

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(self.stats_interval)
            self.push_stats()

    async def _pump_loop(self) -> None:
        """Раздаёт события подписчикам и ведёт историю для новых подключений."""
        while True:
            payload = await self._queue.get()
            kind = payload.get("type")
            if kind == "message":
                self._history.append(payload)
            elif kind == "translation":
                for item in reversed(self._history):
                    if item.get("type") == "message" and item.get("id") == payload.get("id"):
                        item["pending"] = False
                        item["translation"] = payload
                        break
            await self._broadcast(payload)

    async def _broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        data = json.dumps(payload, ensure_ascii=False)
        for client in list(self._clients):
            try:
                await client.send(data)
            except Exception:  # noqa: BLE001
                self._clients.discard(client)

    # -------------------------------------------------------------- сервер

    def _bound_port(self) -> int:
        """Узнать порт, который реально занял сервер (важно при port = 0)."""
        sockets = getattr(self._server, "sockets", None) or []
        for sock in sockets:
            try:
                return int(sock.getsockname()[1])
            except (OSError, IndexError, TypeError):
                continue
        return int(self._config().dock.port)

    def _ensure_token(self) -> None:
        """Для доступа не с localhost токен обязателен — создаём и сохраняем."""
        config = self._config()
        dock = config.dock
        if dock.token or dock.host.strip() in {"", "127.0.0.1", "localhost", "::1"}:
            return
        dock.token = secrets.token_urlsafe(12)
        try:
            config.save()
        except OSError as exc:
            self._log(f"док: токен не сохранён ({exc})")
        self._log("док: сгенерирован токен доступа (адрес не локальный)")

    def _authorized(self, params: dict[str, list[str]]) -> bool:
        token = self._config().dock.token
        if not token:
            return True
        return (params.get("token") or [""])[0] == token

    def _page(self) -> bytes:
        """HTML страницы дока (с перечитыванием файла при правках)."""
        return self._static_page(DOCK_PAGE, "<h1>Govorok</h1>"
                                 "<p>Файл assets/dock/index.html не найден.</p>",
                                 "dock")

    def _subs_page(self) -> bytes:
        """HTML оверлея субтитров для OBS."""
        return self._static_page(SUBS_PAGE, "<h1>Govorok</h1>"
                                 "<p>Файл assets/subs/index.html не найден.</p>",
                                 "subs")

    def _static_page(self, path: Path, missing: str, slot: str) -> bytes:
        """Отдать файл с диска, перечитывая его только при изменении."""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return missing.encode("utf-8")
        cache = self._pages.setdefault(slot, [0.0, b""])
        if mtime != cache[0] or not cache[1]:
            cache[0] = mtime
            cache[1] = path.read_bytes()
        return cache[1]

    def _process_request(self, connection, request) -> Response | None:
        """Отдать страницу дока по HTTP; сам WebSocket обслуживает обработчик."""
        path, _, query = (request.path or "/").partition("?")
        params = parse_qs(query)
        dock = self._config().dock

        if path == "/ws":
            if not self._authorized(params):
                return Response(403, "Forbidden", Headers([("Content-Type", "text/plain; charset=utf-8")]),
                                "неверный токен".encode("utf-8"))
            return None                      # продолжаем рукопожатие WebSocket

        if path in ("/", dock.path, "/dock", "/index.html"):
            if not self._authorized(params):
                return Response(403, "Forbidden", Headers([("Content-Type", "text/plain; charset=utf-8")]),
                                "нужен токен: откройте ссылку из Govorok".encode("utf-8"))
            return Response(200, "OK", Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-store"),
            ]), self._page())

        if path in ("/subs", "/subtitles", "/subs.html"):
            if not self._authorized(params):
                return Response(403, "Forbidden", Headers([("Content-Type", "text/plain; charset=utf-8")]),
                                "нужен токен: откройте ссылку из Govorok".encode("utf-8"))
            return Response(200, "OK", Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Cache-Control", "no-store"),
            ]), self._subs_page())

        if path == "/health":
            body = json.dumps({"ok": True, "clients": self.clients,
                               "messages": self.sent_messages,
                               "uptime": round(time.time() - self.started_at, 1)}).encode("utf-8")
            return Response(200, "OK", Headers([("Content-Type", "application/json")]), body)

        return Response(404, "Not Found", Headers([("Content-Type", "text/plain; charset=utf-8")]),
                        "не найдено".encode("utf-8"))

    async def _on_client(self, ws: ServerConnection) -> None:
        """Обслужить подключение страницы дока."""
        request = getattr(ws, "request", None)
        path, _, query = ((getattr(request, "path", "") or "") or "/ws").partition("?")
        params = parse_qs(query)
        if path != "/ws" or not self._authorized(params):
            await ws.close(code=1008, reason="forbidden")
            return

        self._clients.add(ws)
        request_params = params
        if (request_params.get("page") or [""])[0] == "subs":
            self._subs_clients.add(ws)
        dock = self._config().dock
        try:
            hello = {
                "type": "hello",
                "channels": self._config().twitch.normalized_channels(),
                "history": list(self._history)[-max(1, int(dock.history)):],
                "only_translated": bool(dock.only_translated),
                "can_send": self._config().twitch.can_send,
                "server_time": time.time(),
                "subs": self.subs_settings(),
            }
            if self.stats_provider:
                try:
                    hello["stats"] = self.stats_provider()
                except Exception:  # noqa: BLE001
                    pass
            if self.viewers_provider:
                try:
                    snapshot = self.viewers_provider()
                    hello["viewers_summary"] = {key: snapshot.get(key) for key in
                                                ("total", "active", "online", "lurkers",
                                                 "chatters_available", "chatters_error")}
                except Exception:  # noqa: BLE001
                    pass
            await ws.send(json.dumps(hello, ensure_ascii=False))
            self._log(f"док: подключился клиент (всего {self.clients})")
            async for raw in ws:
                await self._handle_command(ws, raw)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._clients.discard(ws)
            self._subs_clients.discard(ws)
            self._log(f"док: клиент отключился (осталось {self.clients})")

    # ------------------------------------------------- команды со страницы

    async def _handle_command(self, ws: ServerConnection, raw) -> None:
        """Разобрать сообщение от страницы дока."""
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        text = text.strip()
        if not text:
            return
        if text == "ping":                         # простой keep-alive
            await self._reply(ws, {"type": "pong"})
            return
        try:
            payload = json.loads(text)
        except ValueError:
            return
        if not isinstance(payload, dict):
            return

        kind = str(payload.get("t") or payload.get("type") or "").lower()
        if kind == "ping":
            await self._reply(ws, {"type": "pong"})
        elif kind == "stats":
            if self.stats_provider:
                try:
                    await self._reply(ws, self.stats_provider())
                except Exception:  # noqa: BLE001
                    pass
        elif kind == "viewers":
            if self.viewers_provider:
                try:
                    await self._reply(ws, self.viewers_provider())
                except Exception as exc:  # noqa: BLE001
                    self._log(f"док: список зрителей не собрался ({exc!r})")
        elif kind == "reset_stats":
            if self.on_reset_stats:
                try:
                    self.on_reset_stats()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"док: сброс статистики не удался ({exc!r})")
            if self.stats_provider:
                try:
                    await self._reply(ws, self.stats_provider())
                except Exception:  # noqa: BLE001
                    pass
        elif kind == "send":
            await self._handle_send(ws, str(payload.get("text") or ""),
                                    reply_to=str(payload.get("reply_to") or ""),
                                    channel=str(payload.get("channel") or ""))

    async def _handle_send(self, ws: ServerConnection, text: str, *, reply_to: str = "",
                           channel: str = "") -> None:
        """Отправить сообщение в чат от имени бота (ответ из дока)."""
        text = " ".join(text.split())[:500]
        if not text:
            return
        if not self.on_send:
            await self._reply(ws, {"type": "send_result", "ok": False, "text": text,
                                   "reply_to": reply_to,
                                   "error": "отправка не поддерживается"})
            return
        try:
            ok, error = await self.on_send(text, reply_to=reply_to, channel=channel)
        except TypeError:                      # старый обработчик без новых аргументов
            ok, error = await self.on_send(text)
        except Exception as exc:  # noqa: BLE001
            ok, error = False, f"{type(exc).__name__}: {exc}"
        if ok:
            suffix = f" (ответ на {reply_to[:8]})" if reply_to else ""
            self._log(f"док: отправлено в чат — {text[:60]}{suffix}")
        else:
            self._log(f"док: сообщение не отправлено — {error}")
        await self._reply(ws, {"type": "send_result", "ok": ok, "text": text,
                               "reply_to": reply_to, "error": error or ""})

    async def _reply(self, ws: ServerConnection, payload: dict) -> None:
        try:
            await ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            self._clients.discard(ws)

    def _log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001
                pass


class DockRunner:
    """Бот без окна: чат Twitch → перевод → док-панель OBS."""

    def __init__(self, config: AppConfig, *, verbose: bool = True,
                 cache_path: Path | None = None) -> None:
        self.config = config
        self.verbose = verbose
        self.emotes = EmoteRegistry(lambda: self.config,
                                    cache_path=cache_path if cache_path is not None
                                    else DATA_DIR / "emotes_cache.json",
                                    on_log=self.log if verbose else None)
        self.translator = Translator(config, on_log=self.log if verbose else None)
        self.viewers = ViewerRegistry(on_log=self.log if verbose else None,
                                      max_viewers=max(500, config.viewers.max_users * 5))
        self.chatters = ChattersPoller(self.viewers, lambda: self.config,
                                       on_log=self.log if verbose else None)
        self.stream = StreamInfo(lambda: self.config, on_log=self.log if verbose else None)
        self.commands = CommandEngine(lambda: self.config,
                                      counts_path=DATA_DIR / "counters.json",
                                      on_log=self.log if verbose else None)
        self.timers = TimerEngine(lambda: self.config, on_log=self.log if verbose else None)
        self.stt = SubtitleService(lambda: self.config.stt,
                                   log=self.log if verbose else None,
                                   translator=self.translator)
        self.hub = DockHub(
            lambda: self.config,
            on_log=self.log,
            stats_provider=self.stats_snapshot,
            viewers_provider=self.viewers_snapshot,
            on_send=self.send_to_chat,
            on_reset_stats=self.reset_stats,
        )
        self.client = TwitchChatClient(
            config.twitch,
            on_message=self.on_message,
            on_status=self.on_status,
            on_notice=self.on_notice,
            on_room=self.on_room,
            on_sent=self.on_sent_message,
            on_log=self.log if verbose else None,
        )
        self._tasks: list[asyncio.Task] = []
        self._poller: asyncio.Task | None = None
        self._timer_task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._echo_sources: deque[str] = deque()
        self._rooms: dict[str, str] = {}
        self._translation_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self.messages = 0
        self.translated = 0
        self.skipped = 0
        self.errors = 0
        self.sent = 0
        self.started_at = time.time()

    # ------------------------------------------------------------ зрители

    def viewers_snapshot(self) -> dict:
        """Снимок для вкладки «Зрители»."""
        return self.viewers.snapshot(idle_minutes=self.config.viewers.idle_minutes,
                                     limit=self.config.viewers.max_users)

    def known_rooms(self) -> list[tuple[str, str]]:
        return list(self._rooms.items())

    # ---------------------------------------------------------- статистика

    def stats_snapshot(self) -> dict:
        """Снимок для вкладки «Статистика» в доке."""
        return stats_payload(
            started_at=self.started_at,
            messages=self.messages,
            translated=self.translated,
            skipped=self.skipped,
            errors=self.errors,
            translator=self.translator,
            config=self.config,
            channels=self.config.twitch.normalized_channels(),
            clients=self.hub.clients,
            can_send=self.config.twitch.can_send,
            sent=self.sent,
            commands_fired=self.commands.fired,
            timers_fired=self.timers.sent,
            emotes={"global": self.emotes.global_count,
                    "channels": self.emotes.total_channel_emotes()},
        )

    def reset_stats(self) -> None:
        self.messages = self.translated = self.skipped = self.errors = 0
        self.sent = 0
        self.commands.fired = 0
        self.timers.sent = 0
        self.started_at = time.time()
        self.translator.reset_usage()
        self.log("статистика сброшена")

    async def send_to_chat(self, text: str, *, reply_to: str = "", channel: str = "") -> tuple[bool, str]:
        """Ответ в чат из док-панели (с веткой, если указан reply_to)."""
        if not self.config.twitch.can_send:
            return False, "нужны ник бота и OAuth-токен"
        try:
            await self.client.send_message(text, channel=channel or None, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, ""

    # -------------------------------------------------------------- логи

    def log(self, text: str) -> None:
        if self.verbose:
            print(f"{time.strftime('%H:%M:%S')}  {text}", flush=True)

    # ------------------------------------------------------------ запуск

    async def start(self, *, connect_chat: bool = True) -> bool:
        hub_started = await self.hub.start()
        if not hub_started:
            return False
        await self.emotes.load_globals()
        workers = max(1, min(8, self.config.llm.max_concurrency))
        self._tasks = [asyncio.create_task(self._translation_worker(i), name=f"translate-{i}")
                       for i in range(workers)]
        if self.config.viewers.enabled:
            self._poller = asyncio.create_task(self.chatters.poll_loop(self.known_rooms),
                                               name="chatters-poll")
        self._timer_task = asyncio.create_task(self.timers.run(self.respond), name="timers")
        if self.stream.enabled():
            self._stream_task = asyncio.create_task(
                self.stream.poll_loop(self.known_channels), name="stream-info")
        if connect_chat:
            await self.client.start()
        if self.config.stt.enabled:
            ok, reason = await self.stt.start(self.hub.publish)
            if not ok:
                self.log(f"субтитры не запустились: {reason}")
        return True

    def known_channels(self) -> list[str]:
        """Каналы для опроса состояния стрима."""
        return self.config.twitch.normalized_channels()

    async def stop(self) -> None:
        for attr in ("_poller", "_timer_task", "_stream_task"):
            task, setattr_target = getattr(self, attr), None
            setattr(self, attr, None)
            if task:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        for task in self._tasks:
            task.cancel()
        await self.stt.stop()
        await self.client.stop()
        await self.hub.stop()
        await self.chatters.aclose()
        await self.stream.aclose()
        await self.emotes.aclose()
        await self.translator.aclose()

    async def run_forever(self) -> None:
        """Держать процесс живым (Ctrl+C для выхода)."""
        while True:
            await asyncio.sleep(3600)

    # ------------------------------------------------------- события чата

    def on_room(self, room_id: str, channel: str) -> None:
        if room_id:
            self._rooms[room_id] = channel
        self.emotes.ensure_channel(room_id, channel)

    def on_message(self, message: ChatMessage) -> None:
        self.messages += 1
        self.viewers.note(message)
        self.timers.observe(message)
        message_id = message.msg_id or f"{message.channel}-{message.ts:.3f}"
        should, _reason = self.translator.should_translate(message.text)
        if not should:
            self.skipped += 1
        self.hub.publish_message(message_payload(message, self.emotes, pending=should,
                                                 config=self.config))
        if should:
            self._translation_queue.put_nowait((message_id, message.text))
        self.handle_command(message)

    def handle_command(self, message: ChatMessage) -> None:
        """Проверить сообщение на `!команду` и ответить, если положено."""
        if not self.config.commands:
            return
        result = self.commands.handle(message, stream=self.stream.cached(message.channel))
        if result is None:
            return
        task = asyncio.create_task(self.respond(
            message.channel, result.text, result.source,
            in_chat=result.command.reply_in_chat, in_dock=result.command.show_in_dock))
        task.add_done_callback(self._report_task)

    def _report_task(self, task: asyncio.Task) -> None:
        """Не терять ошибки фоновых ответов: иначе они пропадают молча."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.log(f"ответ бота сорвался: {error!r}")

    def on_notice(self, kind: str, text: str) -> None:
        if text:
            self.hub.publish_notice(text)

    def on_sent_message(self, text: str, channel: str) -> None:
        """Наше сообщение ушло в чат: показываем его в доке локальным эхом."""
        self.sent += 1
        source, in_dock = self._echo_sources.popleft() if self._echo_sources else ("", True)
        if in_dock:
            self.hub.publish_message(self_message_payload(text, channel, self.config,
                                                          source=source))
        self.log(f"отправлено в #{channel}: {text[:60]}")

    async def respond(self, channel: str, text: str, source: str = "", *,
                      in_chat: bool = True, in_dock: bool = True) -> None:
        """Ответ бота: в чат, если есть токен и связь, и в док-панель."""
        text = " ".join((text or "").split())
        if not text:
            return
        sent = False
        if in_chat and self.config.twitch.can_send and self.client.connected:
            self._echo_sources.append((source, in_dock))
            try:
                await self.client.send_message(text, channel=channel or None)
                sent = True
            except Exception as exc:  # noqa: BLE001
                if self._echo_sources:
                    self._echo_sources.pop()
                self.log(f"ответ не отправлен: {exc}")
        if not sent and in_dock:
            self.hub.publish_message(self_message_payload(text, channel, self.config,
                                                          source=source))
            reason = "нет связи с Twitch" if self.config.twitch.can_send else "нет OAuth-токена"
            self.log(f"ответ только в доке ({reason}): {text[:60]}")

    def on_status(self, state: str, text: str) -> None:
        self.hub.publish_status(state, text)
        self.log(f"статус: {text}")

    async def _translation_worker(self, index: int) -> None:
        while True:
            message_id, text = await self._translation_queue.get()
            result = await self.translator.translate(text)
            if result.error:
                self.errors += 1
                self.log(f"перевод не удался: {result.error}")
            elif result.text:
                self.translated += 1
                self.log(f"→ {result.text}")
            self.hub.publish_translation(message_id, result)
            self.hub.publish(self.stats_snapshot())
