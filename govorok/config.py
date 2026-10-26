"""Конфигурация приложения: чтение/запись config.json.

Секреты (OAuth-токен Twitch, ключ LLM API) лежат только в локальном
config.json, который исключён из git через .gitignore.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any

try:  # консоль Windows живёт в cp1251 и падает на «→», «·», «×»: переводим stdout в UTF-8
    for _stream in (sys.stdout, sys.stderr):
        if _stream is not None:
            _stream.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - у GUI-процесса потоков может не быть
    pass

#: чем заменяются секреты в дампах конфигов (логов, тестов, отчётов диагностики)
REDACTED = "<redacted>"

#: папка с кодом и ресурсами (assets/dock/index.html и т.п.)
APP_DIR = Path(__file__).resolve().parent.parent

#: имя проекта: оно же имя папки данных, оно же заголовок окна
APP_NAME = "Govorok"
#: как называлась папка данных до переименования проекта (было BotTTV)
LEGACY_APP_NAME = "BotTTV"


def _data_base() -> Path:
    """Родительская папка для данных: %APPDATA ~/ Library ~/ XDG_CONFIG_HOME."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config"))


def _migrate_data_dir(new: Path, old: Path) -> None:
    """Перенести данные старого имени на новое: конфиг, кэш смайлов, весы моделей.

    Переименование папки — операция атомарная и дешёвая (в отличие от копирования
    гигабайт моделей). Если папки на разных томах или её держит другой процесс —
    оставляем как есть: читать будем со старого места, ничего не теряется.
    """
    if new.exists() or not old.exists():
        return
    try:
        old.rename(new)
    except OSError:
        try:
            shutil.copytree(old, new)
        except OSError:
            return
        try:
            old.rename(old.with_name(old.name + "-old"))
        except OSError:
            pass


def _data_dir() -> Path:
    """Папка пользовательских данных, не зависящая от расположения кода.

    Настройки и секреты (config.json), кэш смайлов, счётчики команд и скачанные
    модели живут здесь, чтобы пересоздание/перенос папки с кодом их не затирал.
    Старая папка ``BotTTV`` переносится в ``Govorok`` при первом запуске.
    """
    base = _data_base()
    new = base / APP_NAME
    legacy = base / LEGACY_APP_NAME
    if new.exists() or not legacy.exists():
        return new
    _migrate_data_dir(new, legacy)
    return new if new.exists() else legacy


#: папка пользовательских данных (переживает переделки кода)
DATA_DIR = _data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
#: старое место config.json (рядом с кодом) — для миграции на новую схему
LEGACY_CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_SYSTEM_PROMPT = (
    "You are a translation engine for a Twitch chat window.\n"
    "You receive ONE chat message and must detect its language and translate it into {target_lang}.\n"
    "Rules:\n"
    "- If the message is already in {target_lang}, or contains no translatable words "
    "(only emotes, nicknames, numbers, links, punctuation), return translation as null.\n"
    "- Keep nicknames, @mentions, emote names, URLs, commands and numbers unchanged.\n"
    "- Preserve slang, memes and tone; never explain, never add notes, never censor.\n"
    "- Keep the translation short — it is a chat line, not prose.\n"
    'Answer with strict JSON only: {{"lang": "<ISO 639-1 code>", "translation": "<text or null>"}}'
)


@dataclass
class TwitchConfig:
    """Параметры подключения к чату Twitch."""

    channels: list[str] = field(default_factory=list)
    nickname: str = ""          # логин бота; пусто = анонимное чтение (justinfan)
    oauth_token: str = ""       # "oauth:xxxx" или просто токен
    client_id: str = ""         # Client-Id приложения: нужен только для списка зрителей

    def masked(self) -> "TwitchConfig":
        """Копия без токена и Client-Id: для тестовых дампов, логов и скриншотов."""
        return replace(self, oauth_token="", client_id="")

    @property
    def can_send(self) -> bool:
        return bool(self.nickname.strip() and self.oauth_token.strip())

    def normalized_token(self) -> str:
        token = self.oauth_token.strip()
        if not token:
            return ""
        return token if token.startswith("oauth:") else f"oauth:{token}"

    def normalized_channels(self) -> list[str]:
        """Каналы в нижнем регистре, без '#', '@' и дублей, в порядке ввода."""
        seen: set[str] = set()
        result: list[str] = []
        for raw in self.channels:
            for chunk in str(raw).replace(";", ",").split(","):
                name = chunk.strip().lstrip("#@").lower()
                if name and name not in seen:
                    seen.add(name)
                    result.append(name)
        return result


@dataclass
class LLMConfig:
    """Провайдер перевода — любой OpenAI-совместимый endpoint."""

    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""

    def masked(self) -> "LLMConfig":
        """Копия с выключенным API-ключом."""
        return replace(self, api_key="")
    model: str = "deepseek-chat"
    timeout: float = 25.0
    max_concurrency: int = 4
    json_mode: bool = True      # response_format={"type": "json_object"}
    temperature: float = 0.0
    max_requests_per_minute: int = 120   # защита от разорения на платном API
    price_in_per_mtok: float = 0.0       # цена за 1M входных токенов (0 = не считать)
    price_out_per_mtok: float = 0.0      # цена за 1M выходных токенов
    currency: str = "USD"

    @property
    def chat_completions_url(self) -> str:
        base = self.base_url.strip().rstrip("/")
        if not base:
            return ""
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"


@dataclass
class TranslationConfig:
    """Правила, что и как переводить."""

    enabled: bool = True
    target_lang: str = "ru"
    skip_cyrillic: bool = True      # сообщения с кириллицей не отправляем в API
    skip_commands: bool = True      # "!команды" не переводим
    min_length: int = 3
    max_length: int = 500
    cache_size: int = 3000
    system_prompt: str = ""         # пусто = DEFAULT_SYSTEM_PROMPT


@dataclass
class UIConfig:
    """Настройки окна и отображения чата."""

    render_emotes: bool = True
    max_messages: int = 400
    font_size: int = 14
    font_family: str = ""          # пусто = системный шрифт по умолчанию

    #: разумные границы размера шрифта чата
    MIN_FONT_SIZE = 9
    MAX_FONT_SIZE = 40

    def clamped_size(self) -> int:
        try:
            size = int(self.font_size)
        except (TypeError, ValueError):
            return 14
        return max(self.MIN_FONT_SIZE, min(self.MAX_FONT_SIZE, size))


@dataclass
class EmotesConfig:
    """Сторонние смайлы: BetterTTV, FrankerFaceZ, 7TV."""

    bttv: bool = True
    ffz: bool = True
    seventv: bool = True
    cache_ttl_hours: int = 12      # сколько держать смайлы в кэше на диске


@dataclass
class DockConfig:
    """Док-панель для OBS (Виды → Доки → Пользовательские доки браузера)."""

    enabled: bool = True
    host: str = "127.0.0.1"      # 0.0.0.0 — если OBS на другом компьютере (нужен токен)
    port: int = 8765
    token: str = ""              # проверяется в ссылке; для не-loopback создаётся сам

    def masked(self) -> "DockConfig":
        """Копия без токена доступа к доку."""
        return replace(self, token="")
    path: str = "/dock"
    history: int = 60            # сколько сообщений отдавать док-панели
    only_translated: bool = True # начальное состояние фильтра «только перевод»


@dataclass
class ViewersConfig:
    """Вкладка «Зрители» в док-панели."""

    enabled: bool = True
    idle_minutes: int = 15        # сколько минут без сообщений считать «ушёл»
    max_users: int = 400          # сколько зрителей отдавать странице
    fetch_chatters: bool = True   # тянуть официальный список (нужен токен модератора)
    poll_seconds: int = 60        # как часто опрашивать список зрителей


@dataclass
class STTConfig:
    """Субтитры со своего микрофона (faster-whisper + VAD).

    Пока только настройки: модуль распознавания подключается отдельно, но поля
    описывают ровно то, что ему понадобится — модель, устройство, нарезка аудио
    и вид субтитров на стриме.
    """

    enabled: bool = False
    model: str = "base"              # tiny | base | small | medium | large-v3
    device: str = "auto"             # auto | cuda | cpu
    compute_type: str = "auto"       # auto | float16 | int8_float16 | int8
    language: str = "ru"             # ru | en | auto
    beam_size: int = 2               # меньше = быстрее, но грубее
    chunk_seconds: float = 2.0       # кусок аудио, который уходит в распознавание
    vad: bool = True                 # резать тишину (silero-vad / webrtcvad)
    silence_ms: int = 500            # тишина, которая закрывает фразу
    input_device: str = ""           # часть имени микрофона; пусто = по умолчанию
    input_gain: float = 3.0           # усиление входа в разах (1…8) для тихих микрофонов
    translate_to: str = ""           # вторая строка субтитров через переводчик (пусто = нет)
    subtitle_mode: str = "both"      # ru | en | both — что показывать зрителю

    # --- защита от «субтитров не по смыслу»: фильтр не-речи
    vad_engine: str = "silero"       # silero | rms
    min_speech_ratio: float = 0.25   # доля «речевых» блоков в фразе
    max_no_speech_prob: float = 0.75 # выше — Whisper сам считает, что речи нет
    min_avg_logprob: float = -1.05   # ниже — текст подобран неуверенно (галлюцинация)

    # --- как субтитры выглядят на стриме
    font_size: int = 28
    lines: int = 2                   # сколько строк держать на экране
    max_chars: int = 60              # сколько символов в строке
    hold_seconds: float = 4.0        # сколько держать строку после последней фразы
    position: str = "bottom"         # bottom | top

    #: разумные границы (используются в окне настроек и при загрузке конфига)
    MIN_FONT_SIZE = 12
    MAX_FONT_SIZE = 64
    MIN_CHUNK = 0.5
    MAX_CHUNK = 5.0
    MIN_LINES = 1
    MAX_LINES = 4
    MIN_HOLD = 1.0
    MAX_HOLD = 15.0

    def clamped_font(self) -> int:
        try:
            size = int(self.font_size)
        except (TypeError, ValueError):
            return 28
        return max(self.MIN_FONT_SIZE, min(self.MAX_FONT_SIZE, size))
    def clamped_chunk(self) -> float:
        try:
            value = float(self.chunk_seconds)
        except (TypeError, ValueError):
            return 2.0
        return max(self.MIN_CHUNK, min(self.MAX_CHUNK, value))

    def clamped_lines(self) -> int:
        try:
            value = int(self.lines)
        except (TypeError, ValueError):
            return 2
        return max(self.MIN_LINES, min(self.MAX_LINES, value))

    def clamped_hold(self) -> float:
        try:
            value = float(self.hold_seconds)
        except (TypeError, ValueError):
            return 4.0
        return max(self.MIN_HOLD, min(self.MAX_HOLD, value))

    def clamped_mode(self) -> str:
        mode = str(self.subtitle_mode or "both").strip().lower()
        return mode if mode in {"ru", "en", "both"} else "both"

    def clamped_gain(self) -> float:
        try:
            value = float(self.input_gain)
        except (TypeError, ValueError):
            return 3.0
        return max(1.0, min(8.0, value))

    def clamped_ratio(self) -> float:
        try:
            value = float(self.min_speech_ratio)
        except (TypeError, ValueError):
            return 0.25
        return max(0.0, min(0.9, value))

    def clamped_no_speech(self) -> float:
        try:
            value = float(self.max_no_speech_prob)
        except (TypeError, ValueError):
            return 0.75
        return max(0.0, min(1.0, value))

    def clamped_logprob(self) -> float:
        try:
            value = float(self.min_avg_logprob)
        except (TypeError, ValueError):
            return -1.05
        return max(-4.0, min(0.0, value))


@dataclass
class CommandConfig:
    """Команда бота: триггер (или несколько) и ответ с переменными."""

    triggers: list[str] = field(default_factory=list)
    response: str = ""
    permission: str = "everyone"     # everyone | subscriber | moderator | broadcaster
    cooldown: int = 10               # секунд на одного пользователя
    enabled: bool = True
    reply_in_chat: bool = True       # отправлять ответ в чат (нужен OAuth-токен)
    show_in_dock: bool = True        # показывать ответ в док-панели
    count: int = 0                   # сколько раз сработала (ведётся на лету)

    @property
    def title(self) -> str:
        return ", ".join(self.triggers) or "(без триггера)"


@dataclass
class TimerConfig:
    """Таймер: сообщение раз в N минут, можно несколько вариантов."""

    lines: list[str] = field(default_factory=list)
    interval_minutes: int = 15
    enabled: bool = True
    only_when_active: bool = True    # молчать, если в чате тишина
    active_window_minutes: int = 5   # окно, за которое считаем активность
    min_messages: int = 3            # сколько сообщений нужно за окно
    sent: int = 0                    # сколько раз отправлен за сессию

    @property
    def title(self) -> str:
        first = (self.lines[0] if self.lines else "").strip()
        return first[:48] + ("…" if len(first) > 48 else "")


@dataclass
class AppConfig:
    twitch: TwitchConfig = field(default_factory=TwitchConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    translation: TranslationConfig = field(default_factory=TranslationConfig)
    ui: UIConfig = field(default_factory=UIConfig)
    emotes: EmotesConfig = field(default_factory=EmotesConfig)
    dock: DockConfig = field(default_factory=DockConfig)
    viewers: ViewersConfig = field(default_factory=ViewersConfig)
    stt: STTConfig = field(default_factory=STTConfig)
    commands: list[CommandConfig] = field(default_factory=list)
    timers: list[TimerConfig] = field(default_factory=list)

    # ---------- сериализация ----------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def redacted(self) -> dict[str, Any]:
        """Словарь без секретов: для тестовых дампов, логов и отчётов диагностики.

        Нужен, чтобы копия конфига (её пишут selftest и tools/) не утаскивала за
        собой OAuth-токен Twitch, API-ключ провайдера и токен доступа к доку.
        Рабочий конфиг в %APPDATA% при этом не трогается: маскируется только
        возвращаемый словарь, а не сам объект.
        """
        data = asdict(self)
        for section, field in (("twitch", ("oauth_token", "client_id")),
                               ("llm", ("api_key",)),
                               ("dock", ("token",))):
            block = data.get(section)
            if isinstance(block, dict):
                for key in field:
                    block[key] = REDACTED
        return data

    def save(self, path: Path | None = None, *, redact: bool = False) -> Path:
        """Сохранить конфиг. `redact=True` — выжать секреты (для тестовых копий)."""
        target = Path(path) if path else CONFIG_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = self.redacted() if redact else self.to_dict()
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        source = Path(path) if path else CONFIG_PATH
        migrated = False
        # миграция: до выноса данных config.json лежал рядом с кодом; если нового
        # файла ещё нет, а старый есть — подхватываем его, чтобы не потерять ключи.
        if source == CONFIG_PATH and not source.exists() and LEGACY_CONFIG_PATH.exists():
            source = LEGACY_CONFIG_PATH
            migrated = True
        if not source.exists():
            return cls()
        try:
            # utf-8-sig: Блокнот и PowerShell любят писать BOM, из-за него
            # json.loads падал и настройки молча терялись
            raw = json.loads(source.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        result = cls(
            twitch=_build(TwitchConfig, raw.get("twitch")),
            llm=_build(LLMConfig, raw.get("llm")),
            translation=_build(TranslationConfig, raw.get("translation")),
            ui=_build(UIConfig, raw.get("ui")),
            emotes=_build(EmotesConfig, raw.get("emotes")),
            dock=_build(DockConfig, raw.get("dock")),
            viewers=_build(ViewersConfig, raw.get("viewers")),
            stt=_build(STTConfig, raw.get("stt")),
            commands=_build_list(CommandConfig, raw.get("commands")),
            timers=_build_list(TimerConfig, raw.get("timers")),
        )
        if migrated:
            # сразу переносим в новое (надёжное) место
            try:
                result.save()
            except OSError:
                pass
        return result

    def command_by_trigger(self, trigger: str) -> "CommandConfig | None":
        """Найти команду по триггеру (регистр не важен)."""
        wanted = (trigger or "").strip().casefold()
        for command in self.commands:
            for item in command.triggers:
                if item.strip().casefold() == wanted:
                    return command
        return None

    # ---------- удобные геттеры ----------

    def system_prompt(self) -> str:
        template = self.translation.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
        if "{target_lang}" in template:
            return template.replace("{target_lang}", self.translation.target_lang)
        return f"{template}\nTarget language: {self.translation.target_lang}."

    def is_translation_ready(self) -> bool:
        """Хватает ли настроек, чтобы перевод реально работал."""
        return bool(
            self.translation.enabled
            and self.llm.api_key.strip()
            and self.llm.model.strip()
            and self.llm.chat_completions_url
        )


def _build(dc_type: type, data: Any):
    """Собрать dataclass из словаря, игнорируя неизвестные и битые поля."""
    if not is_dataclass(dc_type):
        raise TypeError(f"{dc_type!r} не dataclass")
    instance = dc_type()
    if not isinstance(data, dict):
        return instance
    known = {f.name: f for f in fields(dc_type)}
    for key, value in data.items():
        spec = known.get(key)
        if spec is None:
            continue
        current = getattr(instance, key)
        try:
            if isinstance(current, bool):
                setattr(instance, key, bool(value))
            elif isinstance(current, int) and not isinstance(value, bool):
                setattr(instance, key, int(value))
            elif isinstance(current, float):
                setattr(instance, key, float(value))
            elif isinstance(current, list):
                setattr(instance, key, [str(v) for v in value] if isinstance(value, (list, tuple)) else current)
            elif isinstance(current, str):
                setattr(instance, key, str(value))      # "5" вместо 5 в строковом поле
            else:
                setattr(instance, key, value if value is not None else current)
        except (TypeError, ValueError):
            continue
    return instance


def _build_list(dc_type: type, items: Any) -> list:
    """Собрать список dataclass-ов (команды, таймеры), пропуская мусор."""
    result: list = []
    if not isinstance(items, (list, tuple)):
        return result
    for item in items:
        if isinstance(item, dc_type):
            result.append(item)
        elif isinstance(item, dict):
            result.append(_build(dc_type, item))
    return result
