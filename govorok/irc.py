"""Асинхронный клиент чата Twitch.

Работает поверх IRC-over-WebSocket (wss://irc-ws.chat.twitch.tv:443):

* с OAuth-токеном — можно читать и писать в чат;
* без токена — анонимное чтение под ником ``justinfanNNNNN``.

Реализованы: парсинг IRC-тегов, PING/PONG, авто-reconnect с backoff,
лимиты на JOIN и на отправку сообщений (20 сообщений / 30 с).
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover - websockets <= 12
    from websockets.client import connect as ws_connect  # type: ignore

from .config import TwitchConfig

TWITCH_IRC_URL = "wss://irc-ws.chat.twitch.tv:443"

#: обычный аккаунт: 20 JOIN за 10 секунд -> безопасная пауза между JOIN
JOIN_INTERVAL = 0.6
#: лимит отправки: 20 сообщений за 30 секунд на соединение
SEND_WINDOW = 30.0
SEND_LIMIT = 20

StatusCallback = Callable[[str, str], None]      # (state, human text)
MessageCallback = Callable[["ChatMessage"], None]
NoticeCallback = Callable[[str, str], None]      # (kind, text)
RoomCallback = Callable[[str, str], None]        # (room_id, channel login)
LogCallback = Callable[[str], None]

_TAG_UNESCAPE = {
    r"\s": " ",
    r"\:": ";",
    r"\\": "\\",
    r"\r": "\r",
    r"\n": "\n",
}


def unescape_tag(value: str) -> str:
    """Разэкранировать значение IRC-тега."""
    if "\\" not in value:
        return value
    for escaped, plain in _TAG_UNESCAPE.items():
        value = value.replace(escaped, plain)
    return value


def is_privmsg(line: str) -> bool:
    """Является ли строка отправкой сообщения (может быть с тегами впереди)."""
    return "PRIVMSG #" in line


def channel_of(line: str) -> str:
    """Канал из строки отправки: ``@tags PRIVMSG #chan :текст``."""
    if " #" not in line:
        return ""
    return line.split(" #", 1)[1].split(" ", 1)[0].lower()


def parse_privmsg_out(line: str) -> tuple[str, str]:
    """Разобрать исходящую строку PRIVMSG: (канал, текст)."""
    if "PRIVMSG #" not in line:
        return "", ""
    tail = line.split("PRIVMSG #", 1)[1]
    channel, _, rest = tail.partition(" ")
    text = rest[1:] if rest.startswith(":") else rest
    return channel.lower(), text


def parse_irc_line(line: str) -> dict:
    """Разобрать одну строку IRC в словарь tags/prefix/command/params/trailing."""
    line = line.rstrip("\r\n")
    tags: dict[str, str] = {}
    rest = line

    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        for pair in raw_tags.split(";"):
            key, _, value = pair.partition("=")
            if key:
                tags[key] = unescape_tag(value)

    prefix = ""
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")

    trailing = ""
    if " :" in rest:
        rest, _, trailing = rest.partition(" :")
    elif rest.startswith(":"):
        rest, trailing = "", rest[1:]

    parts = rest.split()
    command = parts[0] if parts else ""
    return {
        "tags": tags,
        "prefix": prefix,
        "command": command,
        "params": parts[1:],
        "trailing": trailing,
    }


def parse_emotes(raw: str) -> list[tuple[str, int, int]]:
    """Разобрать тег ``emotes`` в список (emote_id, start, end) включительно."""
    result: list[tuple[str, int, int]] = []
    if not raw:
        return result
    for chunk in raw.split("/"):
        emote_id, _, ranges = chunk.partition(":")
        if not emote_id:
            continue
        for span in ranges.split(","):
            start, _, end = span.partition("-")
            if start.isdigit() and end.isdigit():
                result.append((emote_id, int(start), int(end)))
    result.sort(key=lambda item: item[1])
    return result


def split_by_emotes(text: str, emotes: Sequence[tuple[str, int, int]]) -> list[tuple[str, str]]:
    """Нарезать текст на сегменты: ("text", str) | ("emote", emote_id)."""
    segments: list[tuple[str, str]] = []
    cursor = 0
    for emote_id, start, end in emotes:
        if start < cursor or end >= len(text) or start > end:
            continue
        if start > cursor:
            segments.append(("text", text[cursor:start]))
        segments.append(("emote", emote_id))
        cursor = end + 1
    if cursor < len(text):
        segments.append(("text", text[cursor:]))
    return [seg for seg in segments if seg[1]]


def parse_badges(raw: str) -> list[tuple[str, str]]:
    """Разобрать тег ``badges`` в список (name, version)."""
    badges: list[tuple[str, str]] = []
    for chunk in raw.split(","):
        name, _, version = chunk.partition("/")
        if name:
            badges.append((name, version or "1"))
    return badges


@dataclass(slots=True)
class ChatMessage:
    """Одно сообщение чата."""

    channel: str
    user: str
    display_name: str
    text: str
    color: str | None = None
    emotes: list[tuple[str, int, int]] = field(default_factory=list)
    badges: list[tuple[str, str]] = field(default_factory=list)
    is_mod: bool = False
    is_sub: bool = False
    is_broadcaster: bool = False
    is_vip: bool = False
    msg_id: str = ""
    room_id: str = ""
    ts: float = field(default_factory=time.time)

    @property
    def badge_labels(self) -> list[str]:
        labels: list[str] = []
        names = {name for name, _ in self.badges}
        if self.is_broadcaster or "broadcaster" in names:
            labels.append("STREAMER")
        if self.is_mod or "moderator" in names:
            labels.append("MOD")
        if self.is_vip or "vip" in names:
            labels.append("VIP")
        if self.is_sub or "subscriber" in names or "founder" in names:
            labels.append("SUB")
        return labels


class TwitchChatClient:
    """Клиент чата Twitch с авто-переподключением."""

    def __init__(
        self,
        config: TwitchConfig,
        *,
        on_message: MessageCallback | None = None,
        on_status: StatusCallback | None = None,
        on_notice: NoticeCallback | None = None,
        on_room: RoomCallback | None = None,
        on_sent: Callable[[str, str], None] | None = None,
        on_log: LogCallback | None = None,
    ) -> None:
        self.config = config
        self.on_message = on_message
        self.on_status = on_status
        self.on_notice = on_notice
        self.on_room = on_room
        self.on_sent = on_sent
        self.on_log = on_log

        self._task: asyncio.Task | None = None
        self._ws = None
        self._outbox: asyncio.Queue[str] = asyncio.Queue()
        self._closing = False
        self._joined: set[str] = set()
        self._rooms: dict[str, str] = {}      # room_id -> канал, чтобы не дёргать дважды
        self._send_times: deque[float] = deque()
        self._last_send_per_channel: dict[str, float] = {}
        self._reconnect_delay = 1.0
        self.messages_seen = 0

    # ------------------------------------------------------------------ API

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def joined_channels(self) -> list[str]:
        return sorted(self._joined)

    async def start(self) -> None:
        """Запустить клиент (не блокирует)."""
        if self.is_running:
            return
        self._closing = False
        self._task = asyncio.create_task(self._run_forever(), name="twitch-irc")

    async def stop(self) -> None:
        """Остановить клиент и закрыть соединение."""
        self._closing = True
        # сначала аккуратно закрываем сокет, потом гасим задачу
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        task, self._task = self._task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._joined.clear()
        self._emit_status("offline", "Отключено")

    @property
    def connected(self) -> bool:
        """Есть ли живое соединение (без него отправлять некуда)."""
        return self._ws is not None

    async def send_message(self, text: str, channel: str | None = None, reply_to: str = "") -> None:
        """Поставить сообщение в очередь отправки (с учётом лимитов Twitch).

        ``reply_to`` — id сообщения, на которое отвечаем: тогда Twitch покажет
        ответ веткой (тег ``reply-parent-msg-id``).
        """
        text = text.strip()
        if not text:
            return
        if not self.config.can_send:
            raise RuntimeError("Для отправки сообщений нужны ник бота и OAuth-токен")
        channels = self.config.normalized_channels()
        target = (channel or (channels[0] if channels else "")).lstrip("#@").lower()
        if not target:
            raise RuntimeError("Не выбран канал для отправки")
        tags = f"@reply-parent-msg-id={reply_to} " if reply_to else ""
        await self._outbox.put(f"{tags}PRIVMSG #{target} :{text}")

    def set_channels(self, channels: Iterable[str]) -> None:
        """Обновить список каналов: лишние — PART, новые — JOIN."""
        desired = {c for c in (str(x).lstrip("#@").lower() for x in channels) if c}
        for channel in sorted(self._joined - desired):
            self._outbox.put_nowait(f"PART #{channel}")
            self._joined.discard(channel)
            self._log(f"вышел из #{channel}")
        for channel in sorted(desired - self._joined):
            self._outbox.put_nowait(f"JOIN #{channel}")
            self._joined.add(channel)   # оптимистично: writer подтвердит

    # ------------------------------------------------------------ внутреннее

    def _emit_status(self, state: str, text: str) -> None:
        if self.on_status:
            try:
                self.on_status(state, text)
            except Exception:  # noqa: BLE001
                pass

    def _log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001
                pass

    def _emit_notice(self, kind: str, text: str) -> None:
        if self.on_notice:
            try:
                self.on_notice(kind, text)
            except Exception:  # noqa: BLE001
                pass

    def _emit_room(self, room_id: str, channel: str) -> None:
        """Сообщить о канале один раз за сессию (нужно для смайлов BTTV/7TV)."""
        if not room_id or self._rooms.get(room_id) == channel:
            return
        self._rooms[room_id] = channel
        if self.on_room:
            try:
                self.on_room(room_id, channel)
            except Exception:  # noqa: BLE001
                pass

    async def _run_forever(self) -> None:
        while not self._closing:
            try:
                await self._session()
                self._reconnect_delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._emit_status("error", f"Ошибка соединения: {exc}")
                self._log(f"соединение разорвано: {exc!r}")
            if self._closing:
                break
            delay = self._reconnect_delay
            self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)
            self._emit_status("waiting", f"Переподключение через {delay:.0f} с")
            self._joined.clear()
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        nickname = self.config.nickname.strip()
        token = self.config.normalized_token()
        anonymous = not (nickname and token)
        nick = nickname if not anonymous else f"justinfan{random.randint(10000, 99999)}"

        async with ws_connect(
            TWITCH_IRC_URL,
            ping_interval=None,
            close_timeout=5,
            max_size=2 ** 21,
        ) as ws:
            self._ws = ws
            await ws.send("CAP REQ :twitch.tv/tags twitch.tv/commands")
            if not anonymous:
                await ws.send(f"PASS {token}")
            await ws.send(f"NICK {nick}")

            mode = "анонимно" if anonymous else f"как {nick}"
            self._emit_status("online", f"Подключено к Twitch ({mode})")
            self._log(f"IRC подключён, режим: {mode}")

            for channel in self.config.normalized_channels():
                await ws.send(f"JOIN #{channel}")
                self._joined.add(channel)
                await asyncio.sleep(JOIN_INTERVAL)

            reader = asyncio.create_task(self._reader(ws), name="irc-reader")
            writer = asyncio.create_task(self._writer(ws), name="irc-writer")
            try:
                done, _pending = await asyncio.wait(
                    {reader, writer}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    exc = task.exception()
                    if exc:
                        raise exc
            finally:
                # гасим обе задачи и обязательно забираем их исключения,
                # иначе asyncio ругается «Task exception was never retrieved»
                for task in (reader, writer):
                    if not task.done():
                        task.cancel()
                for task in (reader, writer):
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                    except Exception:  # noqa: BLE001
                        pass
        self._ws = None

    async def _reader(self, ws) -> None:
        async for raw in ws:
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            for line in text.split("\r\n"):
                if line:
                    await self._handle_line(line, ws)

    async def _writer(self, ws) -> None:
        while True:
            line = await self._outbox.get()
            if is_privmsg(line):
                await self._wait_send_slot(line)
            elif line.startswith("JOIN"):
                await asyncio.sleep(JOIN_INTERVAL)   # 20 JOIN за 10 с у обычного аккаунта
            try:
                await ws.send(line)
            except Exception:  # noqa: BLE001
                # соединение умерло — вернуть строку и выйти, reader тоже упадёт
                self._outbox.put_nowait(line)
                raise
            if is_privmsg(line):
                # Twitch не присылает нам наши же сообщения — сообщаем о них сами
                channel, text = parse_privmsg_out(line)
                if self.on_sent:
                    try:
                        self.on_sent(text, channel)
                    except Exception:  # noqa: BLE001
                        pass
            if line.startswith("JOIN"):
                self._log(f"вошёл в #{line.split('#', 1)[-1]}")

    async def _wait_send_slot(self, line: str) -> None:
        """Соблюсти лимиты: 20 сообщений / 30 с на соединение и 1 msg/s на канал."""
        channel = channel_of(line)
        while True:
            now = time.monotonic()
            while self._send_times and now - self._send_times[0] > SEND_WINDOW:
                self._send_times.popleft()
            per_channel_wait = 0.0
            if channel:
                last = self._last_send_per_channel.get(channel, 0.0)
                per_channel_wait = max(0.0, 1.0 - (now - last))
            if len(self._send_times) < SEND_LIMIT and per_channel_wait <= 0:
                self._send_times.append(now)
                if channel:
                    self._last_send_per_channel[channel] = now
                return
            wait = max(per_channel_wait, 1.0 if len(self._send_times) >= SEND_LIMIT else 0.1)
            await asyncio.sleep(min(wait, SEND_WINDOW))

    async def _handle_line(self, line: str, ws) -> None:
        msg = parse_irc_line(line)
        command = msg["command"]
        tags = msg["tags"]
        prefix = msg["prefix"]
        trailing = msg["trailing"]

        if command == "PING":
            await ws.send(f"PONG :{trailing}")
            return

        if command == "RECONNECT":
            self._log("Twitch попросил переподключиться")
            self._emit_status("waiting", "Twitch просит переподключение")
            raise ConnectionError("server requested reconnect")

        if command == "PRIVMSG":
            channel = (msg["params"][0] if msg["params"] else "").lstrip("#").lower()
            user = prefix.split("!", 1)[0]
            badges = parse_badges(tags.get("badges", ""))
            badge_names = {name for name, _ in badges}
            message = ChatMessage(
                channel=channel,
                user=user,
                display_name=tags.get("display-name") or user,
                text=trailing,
                color=tags.get("color") or None,
                emotes=parse_emotes(tags.get("emotes", "")),
                badges=badges,
                is_mod=tags.get("mod") == "1" or "moderator" in badge_names,
                is_sub=tags.get("subscriber") == "1" or bool({"subscriber", "founder"} & badge_names),
                is_broadcaster="broadcaster" in badge_names,
                is_vip="vip" in badge_names or tags.get("vip") == "1",
                msg_id=tags.get("id", ""),
                room_id=tags.get("room-id", ""),
                ts=float(tags["tmi-sent-ts"]) / 1000 if tags.get("tmi-sent-ts", "").isdigit() else time.time(),
            )
            self._emit_room(message.room_id, channel)
            self.messages_seen += 1
            if self.on_message:
                try:
                    self.on_message(message)
                except Exception as exc:  # noqa: BLE001
                    self._log(f"ошибка обработки сообщения: {exc!r}")
            return

        if command == "ROOMSTATE":
            channel = (msg["params"][0] if msg["params"] else "").lstrip("#").lower()
            self._emit_room(tags.get("room-id", ""), channel)
            return

        if command == "USERNOTICE":
            kind = tags.get("msg-id", "notice")
            system_msg = tags.get("system-msg", "").replace(r"\s", " ")
            self._emit_notice(kind, system_msg or trailing)
            return

        if command == "NOTICE":
            notice_id = tags.get("msg-id", "")
            self._emit_notice(f"notice:{notice_id}", trailing)
            if notice_id in {"login_authentication_failed", "msg_channel_suspended", "msg_banned"}:
                self._emit_status("error", f"Twitch: {trailing}")
            return

        if command == "CLEARCHAT":
            target = trailing or ""
            self._emit_notice("clear", f"Чат очищен ({target})" if target else "Чат очищен")
            return

        if command == "CLEARMSG":
            self._emit_notice("delete", f"Удалено сообщение: {trailing}")
            return

        if command == "JOIN" and prefix.startswith(self.config.nickname or "\0"):
            channel = (msg["params"][0] if msg["params"] else "").lstrip("#")
            self._log(f"подтверждён вход в #{channel}")
            return

        if command == "001":
            self._reconnect_delay = 1.0
            return
