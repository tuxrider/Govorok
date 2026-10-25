"""Команды и таймеры бота.

* :class:`CommandEngine` — распознаёт `!команды` в чате, проверяет права и
  кулдауны, подставляет переменные и возвращает готовый ответ;
* :class:`TimerEngine` — раз в N минут отправляет одну из заготовленных строк,
  умеет молчать, когда в чате тишина.

Оба движка не знают, как отправлять сообщения: готовый текст забирает владелец
(окно настроек или режим без окна) и решает, писать в чат или только в док.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .config import AppConfig, CommandConfig, TimerConfig
from .irc import ChatMessage

PERMISSION_LEVELS = {"everyone": 0, "subscriber": 1, "moderator": 2, "broadcaster": 3}
PERMISSION_TITLES = {"everyone": "все", "subscriber": "подписчики",
                     "moderator": "модераторы", "broadcaster": "стример"}

#: переменные шаблона: {имя} или {имя:параметр}; поддерживаются и {1}, {args.2}
VARIABLE_RE = re.compile(r"\{([a-zA-Zа-яА-Я_0-9][\w.]*)(?::([^}]*))?\}")

DOCK_SOURCE_TITLES = {"command": "команда", "timer": "таймер"}


def format_duration(seconds: float) -> str:
    """Секунды в «2 ч 05 мин» — для переменной {uptime}."""
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {secs:02d} с"
    return f"{secs} с"


def user_level(message: ChatMessage) -> int:
    """Уровень прав автора сообщения."""
    if message.is_broadcaster:
        return PERMISSION_LEVELS["broadcaster"]
    if message.is_mod:
        return PERMISSION_LEVELS["moderator"]
    if message.is_sub:
        return PERMISSION_LEVELS["subscriber"]
    return PERMISSION_LEVELS["everyone"]


@dataclass(slots=True)
class CommandResult:
    """Готовый ответ на команду."""

    command: CommandConfig
    trigger: str
    text: str
    source: str = "command"


def render_template(template: str, *, message: ChatMessage | None = None, args: Iterable[str] = (),
                    count: int = 0, stream: dict | None = None,
                    channel: str = "", now: float | None = None) -> str:
    """Подставить переменные в шаблон ответа."""
    arg_list = [str(a) for a in args if str(a) != ""]
    stream = stream or {}
    moment = time.localtime(now if now is not None else time.time())

    def value(name: str, parameter: str | None) -> str:
        key = name.lower()
        if key in {"user", "user.name", "ник"}:
            return message.user if message else ""
        if key in {"display", "display.name", "имя"}:
            return (message.display_name or message.user) if message else ""
        if key in {"channel", "канал"}:
            return channel or (message.channel if message else "")
        if key in {"args", "аргументы"}:
            return " ".join(arg_list)
        if key in {"target", "цель"}:
            return arg_list[0] if arg_list else (message.user if message else "")
        if key in {"count", "счётчик"}:
            return str(count)
        if key in {"date", "дата"}:
            return time.strftime("%d.%m.%Y", moment)
        if key in {"time", "время"}:
            return time.strftime("%H:%M:%S", moment)
        if key in {"datetime", "датавремя"}:
            return time.strftime("%d.%m.%Y %H:%M", moment)
        if key in {"uptime", "аптайм"}:
            started = stream.get("started_at")
            return format_duration(time.time() - started) if started else "нет данных"
        if key in {"game", "игра"}:
            return str(stream.get("game") or "нет данных")
        if key in {"title", "заголовок"}:
            return str(stream.get("title") or "нет данных")
        if key in {"viewers", "зрители"}:
            viewers = stream.get("viewers")
            return str(viewers) if viewers is not None else "нет данных"
        if key in {"random", "рандом"}:
            variants = [item.strip() for item in (parameter or "").split("|") if item.strip()]
            return random.choice(variants) if variants else ""
        if key.isdigit() and parameter is None:            # {1}, {2} — аргументы по номеру
            index = int(key) - 1
            return arg_list[index] if 0 <= index < len(arg_list) else ""
        if key.startswith("args.") and key[5:].isdigit():
            index = int(key[5:]) - 1
            return arg_list[index] if 0 <= index < len(arg_list) else ""
        return ""                                          # неизвестную переменную убираем

    return VARIABLE_RE.sub(lambda match: value(match.group(1), match.group(2)), template or "")


class CommandEngine:
    """Обработка `!команд` из чата."""

    def __init__(self, config_provider: Callable[[], AppConfig], *, counts_path: Path | None = None,
                 on_log: Callable[[str], None] | None = None, global_cooldown: float = 3.0) -> None:
        self._config = config_provider
        self.counts_path = counts_path
        self.on_log = on_log
        self.global_cooldown = global_cooldown
        self.counts: dict[str, int] = {}
        self._user_cooldowns: dict[tuple[str, str], float] = {}
        self._global_until: dict[str, float] = {}
        self.fired = 0
        self.blocked = 0
        self.load_counts()

    # ------------------------------------------------------------ утилиты

    def _log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def key_of(command: CommandConfig) -> str:
        """Стабильный ключ счётчика — первый триггер."""
        return (command.triggers[0] if command.triggers else "").strip().casefold()

    def split_args(self, message: ChatMessage) -> tuple[str, list[str]]:
        """Разделить сообщение на триггер и аргументы."""
        parts = message.text.strip().split()
        if not parts:
            return "", []
        return parts[0], parts[1:]

    def match(self, message: ChatMessage) -> CommandConfig | None:
        """Найти команду по первому слову сообщения."""
        if not message.text or message.text.startswith(" "):
            return None
        trigger, _args = self.split_args(message)
        if not trigger:
            return None
        wanted = trigger.casefold()
        for command in self._config().commands:
            for item in command.triggers:
                if item.strip().casefold() == wanted:
                    return command
        return None

    def check(self, command: CommandConfig, message: ChatMessage, now: float | None = None) -> tuple[bool, str]:
        """Проверить права и кулдауны. Возвращает (можно, причина отказа)."""
        moment = now if now is not None else time.time()
        if not command.enabled:
            return False, "команда выключена"
        need = PERMISSION_LEVELS.get(command.permission, 0)
        if user_level(message) < need:
            return False, f"нужны права: {PERMISSION_TITLES.get(command.permission, command.permission)}"
        key = self.key_of(command)
        if moment < self._global_until.get(key, 0.0):
            return False, "слишком часто (общий кулдаун)"
        until = self._user_cooldowns.get((key, message.user), 0.0)
        if moment < until:
            return False, f"подожди {int(until - moment) + 1} с"
        return True, ""

    def handle(self, message: ChatMessage, *, stream: dict | None = None,
               now: float | None = None) -> CommandResult | None:
        """Обработать сообщение: вернуть готовый ответ или None."""
        command = self.match(message)
        if command is None:
            return None
        moment = now if now is not None else time.time()
        allowed, reason = self.check(command, message, moment)
        if not allowed:
            self.blocked += 1
            self._log(f"команда {command.title}: {reason}")
            return None

        trigger, args = self.split_args(message)
        key = self.key_of(command)
        self.counts[key] = self.counts.get(key, 0) + 1
        self.save_counts()
        self._user_cooldowns[(key, message.user)] = moment + max(0, int(command.cooldown))
        self._global_until[key] = moment + self.global_cooldown
        self.fired += 1

        text = render_template(command.response, message=message, args=args,
                               count=self.counts[key], stream=stream,
                               channel=message.channel, now=moment).strip()
        if not text:
            return None
        return CommandResult(command=command, trigger=trigger, text=text)

    def reset_counts(self) -> None:
        self.counts.clear()
        self.save_counts()

    # ------------------------------------------------------------- счётчики

    def load_counts(self) -> None:
        if not self.counts_path or not Path(self.counts_path).exists():
            return
        try:
            raw = json.loads(Path(self.counts_path).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    self.counts[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue

    def save_counts(self) -> None:
        if not self.counts_path:
            return
        try:
            Path(self.counts_path).parent.mkdir(parents=True, exist_ok=True)
            Path(self.counts_path).write_text(
                json.dumps(self.counts, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self._log(f"счётчики команд не сохранены: {exc}")


class TimerEngine:
    """Таймеры: сообщение раз в N минут, с проверкой активности чата."""

    def __init__(self, config_provider: Callable[[], AppConfig], *, on_log: Callable[[str], None] | None = None,
                 tick: float = 5.0, rng: random.Random | None = None,
                 activity_window: int = 300) -> None:
        self._config = config_provider
        self.on_log = on_log
        self.tick = tick
        self.rng = rng or random.Random()
        self._schedule: dict[int, tuple[tuple, float]] = {}
        self._activity: dict[str, deque[float]] = {}
        self._activity_window = activity_window
        self.sent = 0

    def _log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001
                pass

    # ----------------------------------------------------------- активность

    def observe(self, message: ChatMessage) -> None:
        """Запомнить время сообщения — для проверки «в чате тишина»."""
        bucket = self._activity.setdefault(message.channel, deque(maxlen=200))
        bucket.append(message.ts or time.time())
        cutoff = time.time() - self._activity_window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def is_active(self, channel: str, *, window_minutes: int, min_messages: int) -> bool:
        now = time.time()
        window = max(60, int(window_minutes) * 60)
        bucket = self._activity.get(channel) or deque()
        recent = [ts for ts in bucket if now - ts <= window]
        return len(recent) >= max(1, int(min_messages))

    # ------------------------------------------------------------ расписание

    @staticmethod
    def _signature(timer: TimerConfig) -> tuple:
        return (tuple(timer.lines), int(timer.interval_minutes), bool(timer.only_when_active),
                int(timer.active_window_minutes), int(timer.min_messages))

    def due(self, now: float | None = None) -> list[tuple[str, TimerConfig, str]]:
        """Что пора отправить: список (канал, таймер, текст)."""
        cfg = self._config()
        moment = now if now is not None else time.time()
        channel = (cfg.twitch.normalized_channels() or [""])[0]
        result: list[tuple[str, TimerConfig, str]] = []

        seen: set[int] = set()
        for index, timer in enumerate(cfg.timers):
            seen.add(index)
            signature = self._signature(timer)
            saved = self._schedule.get(index)
            if saved is None or saved[0] != signature:
                self._schedule[index] = (signature, moment + max(1, int(timer.interval_minutes)) * 60)
                continue
            if not timer.enabled or not timer.lines:
                continue
            if moment < saved[1]:
                continue
            self._schedule[index] = (signature, moment + max(1, int(timer.interval_minutes)) * 60)
            if timer.only_when_active and not self.is_active(
                    channel, window_minutes=timer.active_window_minutes,
                    min_messages=timer.min_messages):
                self._log(f"таймер пропущен: в #{channel} тишина")
                continue
            text = self.rng.choice([line for line in timer.lines if line.strip()] or [""]).strip()
            if not text:
                continue
            timer.sent += 1
            self.sent += 1
            result.append((channel, timer, text))
        for index in list(self._schedule):
            if index not in seen:
                self._schedule.pop(index, None)
        return result

    async def run(self, sender: Callable[[str, str, str], Awaitable[Any]]) -> None:
        """Цикл таймеров: sender(канал, текст, источник)."""
        while True:
            await asyncio.sleep(self.tick)
            try:
                for channel, timer, text in self.due():
                    await sender(channel, text, "timer")
                    self._log(f"таймер ({timer.interval_minutes} мин): {text[:60]}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._log(f"таймеры: сбой ({exc!r})")

    def reset(self) -> None:
        self._schedule.clear()
        self._activity.clear()
        self.sent = 0
