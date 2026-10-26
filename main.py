"""Govorok — окно настроек, док-панель и оверлей субтитров для OBS.

Чат в этом окне не показывается: он живёт в док-панели OBS
(Виды → Доки → Пользовательские доки браузера), а субтитры твоей речи — в
Браузер-источнике; окно целиком отдано настройкам, статистике и логу. Если окно
вообще не нужно — есть режим ``python main.py --dock``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import math
import re
import sys
import time
from collections import deque

import flet as ft

try:  # в консоли Windows stdout по умолчанию cp1251 и падает на «→» и «·»
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 - stdout может отсутствовать (окно без консоли)
    pass

from govorok.commands import (PERMISSION_TITLES, CommandEngine, TimerEngine,
                             render_template)
from govorok.config import (DATA_DIR, AppConfig, CONFIG_PATH, CommandConfig, STTConfig,
                           TimerConfig)
from govorok.dock import DockHub, DockRunner, message_key, message_payload, self_message_payload
from govorok.emotes import EmoteRegistry
from govorok.fonts import list_system_fonts, resolve_family
from govorok.irc import ChatMessage, TwitchChatClient
from govorok.stats import estimate_cost, stats_payload
from govorok.streaminfo import StreamInfo
from govorok.stt import SubtitleService, device_key
from govorok.translator import Translator
from govorok.viewers import ChattersPoller, ViewerRegistry

# --------------------------------------------------------------------- тема

BG = "#0d1117"
PANEL = "#161b22"
PANEL_ALT = "#1c2230"
BORDER_SOFT = "#262d3a"
ACCENT = "#a78bfa"
ACCENT_DEEP = "#7c5cff"
TEXT = "#e6edf3"
MUTED = "#8b949e"
OK_COLOR = "#3fb950"
WARN_COLOR = "#d29922"
ERR_COLOR = "#f85149"

EMOTES_CACHE_PATH = DATA_DIR / "emotes_cache.json"

PROVIDER_PRESETS: dict[str, tuple[str, str]] = {
    "DeepSeek": ("https://api.deepseek.com/v1", "deepseek-chat"),
    "OpenAI": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "OpenRouter": ("https://openrouter.ai/api/v1", "google/gemini-2.0-flash-001"),
    "Groq": ("https://api.groq.com/openai/v1", "llama-3.3-70b-versatile"),
    "Ollama (локально)": ("http://localhost:11434/v1", "qwen2.5:7b"),
    "Свой вариант": ("", ""),
}

STATUS_STYLE = {
    "offline": (MUTED, "Не подключено"),
    "waiting": (WARN_COLOR, "Переподключение"),
    "online": (OK_COLOR, "Чат читается"),
    "error": (ERR_COLOR, "Ошибка"),
}

#: модели faster-whisper: (значение в конфиге, подпись в списке)
STT_MODELS: list[tuple[str, str]] = [
    ("tiny", "tiny · ~40 МБ, черновик"),
    ("base", "base · ~150 МБ, быстрый старт"),
    ("small", "small · ~500 МБ, быстрый"),
    ("medium", "medium · ~1.5 ГБ, рекомендую"),
    ("large-v3", "large-v3 · ~3 ГБ, максимум качества"),
]
STT_DEVICES: list[tuple[str, str]] = [
    ("auto", "авто (GPU, если есть)"),
    ("cuda", "GPU (CUDA)"),
    ("cpu", "CPU"),
]
STT_COMPUTE: list[tuple[str, str]] = [
    ("auto", "авто"),
    ("float16", "float16 (GPU)"),
    ("int8_float16", "int8_float16 (GPU, меньше VRAM)"),
    ("int8", "int8 (CPU)"),
]
STT_LANGS: list[tuple[str, str]] = [
    ("ru", "русский"),
    ("en", "английский"),
    ("auto", "определять сам"),
]
STT_POSITIONS: list[tuple[str, str]] = [
    ("bottom", "снизу экрана"),
    ("top", "сверху экрана"),
]
STT_MODES: list[tuple[str, str]] = [
    ("both", "речь + перевод"),
    ("en", "только перевод"),
    ("ru", "только речь"),
]
STT_VAD_ENGINES: list[tuple[str, str]] = [
    ("silero", "silero (речь / не речь)"),
    ("rms", "по громкости"),
]


def choice(value: str, pairs: list[tuple[str, str]], fallback: str) -> str:
    """Значение из конфига, если оно есть в списке, иначе запасное."""
    allowed = {key for key, _ in pairs}
    return value if value in allowed else fallback


#: виртуальные «мапперы» PortAudio: ведут на устройство по умолчанию, в списке не нужны
MAPPERS = ("первичный драйвер записи", "primary sound capture", "переназначение звуковых",
           "sound mapper")


#: виртуальные «мапперы» PortAudio: ведут на устройство по умолчанию, в списке не нужны
MAPPERS = ("первичный драйвер записи", "primary sound capture", "переназначение звуковых",
           "sound mapper")


def list_input_devices() -> list[tuple[str, str]]:
    """Микрофоны для списка: (имя для конфига, подпись с host API).

    Один микрофон остаётся в списке один раз, причём в лучшем из доступных API:
    WASAPI → WDM-KS → DirectSound → MME. Виртуальные мапперы («Первичный драйвер
    записи звука») не показываем: они просто дублируют устройство по умолчанию.
    """
    options: list[tuple[str, str]] = [("", "по умолчанию (системный)")]
    try:
        import sounddevice as sd  # noqa: PLC0415 - необязательная зависимость
    except Exception:  # noqa: BLE001
        return options
    best: dict[str, tuple[int, str, str]] = {}       # ключ железа → (ранг, имя, api)
    apis: dict[str, set[str]] = {}                    # сколько API у этого железа
    try:
        for device in sd.query_devices():
            if int(device.get("max_input_channels") or 0) <= 0:
                continue
            name = str(device.get("name") or "").strip()
            if not name or any(marker in name.casefold() for marker in MAPPERS):
                continue
            try:
                api = str(sd.query_hostapis(int(device.get("hostapi") or 0)).get("name", ""))
            except Exception:  # noqa: BLE001
                api = ""
            key = device_key(name)
            apis.setdefault(key, set()).add(api)
            lowered = api.casefold()
            if "wasapi" in lowered:
                rank = 0
            elif "wdm-ks" in lowered:
                rank = 1
            elif "directsound" in lowered:
                rank = 2
            else:
                rank = 3
            current = best.get(key)
            if current is None or rank < current[0]:
                best[key] = (rank, name, api)
    except Exception:  # noqa: BLE001
        pass
    for key, (rank, name, api) in sorted(best.items()):
        # API показываем только когда он единственный: иначе бот сам выберет
        # тот, который принимает 16 кГц, и подпись бы врала
        short = api.replace("Windows ", "").strip() if len(apis.get(key, ())) == 1 else ""
        options.append((name, f"{name} · {short}" if short else name))
    return options


def level_db(rms: float) -> float:
    """Громкость микрофона в дБ (RMS → dBFS)."""
    try:
        level = float(rms)
    except (TypeError, ValueError):
        return -100.0
    if level <= 0:
        return -100.0
    return 20.0 * math.log10(level)


def meter_fraction(rms: float, floor_db: float = -70.0, ceil_db: float = -10.0) -> float:
    """Доля заполнения индикатора 0..1 по логарифмической шкале."""
    db = level_db(rms)
    if db <= floor_db:
        return 0.0
    if db >= ceil_db:
        return 1.0
    return (db - floor_db) / (ceil_db - floor_db)


def stt_dependencies() -> dict[str, bool]:
    """Какие пакеты для субтитров уже установлены (проверка без их импорта)."""
    result: dict[str, bool] = {}
    for name in ("faster_whisper", "sounddevice", "numpy"):
        try:
            result[name] = importlib.util.find_spec(name) is not None
        except (ImportError, ValueError):
            result[name] = False
    return result


def card(title: str, *controls: ft.Control, hint: str = "") -> ft.Container:
    """Карточка настроек: заголовок и поля."""
    rows: list[ft.Control] = [
        ft.Text(title, size=12, weight=ft.FontWeight.BOLD, color=ACCENT),
    ]
    rows.extend(controls)
    if hint:
        rows.append(ft.Text(hint, size=10, color=MUTED))
    return ft.Container(
        content=ft.Column(rows, spacing=8),
        bgcolor=PANEL,
        border=ft.Border.all(1, BORDER_SOFT),
        border_radius=12,
        padding=ft.Padding.symmetric(horizontal=14, vertical=12),
    )


def field(label: str, value: str, *, width: int | None = None, hint: str = "",
          password: bool = False, read_only: bool = False, size: int = 12) -> ft.TextField:
    return ft.TextField(
        label=label,
        value=value,
        hint_text=hint,
        width=width,
        dense=True,
        text_size=size,
        password=password,
        can_reveal_password=password,
        read_only=read_only,
    )


class BotApp:
    """Окно настроек, которое заодно держит бота и док-панель."""

    def __init__(self, page: ft.Page) -> None:
        self.page = page
        #: тесты ставят True, чтобы любое сохранение конфига из окна не писало
        #: настоящие ключи в тестовые копии (см. AppConfig.save(redact=...))
        self.config_save_redacts = False
        self.config = AppConfig.load()
        self.translator = Translator(self.config, on_log=self.log)
        self.emotes = EmoteRegistry(lambda: self.config, cache_path=EMOTES_CACHE_PATH,
                                    on_log=self.log)
        self.viewers = ViewerRegistry(on_log=self.log)
        self.chatters = ChattersPoller(self.viewers, lambda: self.config, on_log=self.log)
        self.stream = StreamInfo(lambda: self.config, on_log=self.log)
        self.commands = CommandEngine(lambda: self.config,
                                      counts_path=DATA_DIR / "counters.json",
                                      on_log=self.log)
        self.timers = TimerEngine(lambda: self.config, on_log=self.log)
        self.stt_service = SubtitleService(lambda: self.config.stt, log=self.log,
                                           translator=self.translator)
        self.dock = DockHub(
            lambda: self.config,
            on_log=self.log,
            stats_provider=self.stats_snapshot,
            viewers_provider=self.viewers_snapshot,
            on_send=self.send_to_chat,
            on_reset_stats=self.reset_stats,
        )
        self.clipboard = ft.Clipboard()
        self.client = TwitchChatClient(
            self.config.twitch,
            on_message=self.on_message,
            on_status=self.on_status,
            on_notice=self.on_notice,
            on_room=self.on_room,
            on_sent=self.on_sent_message,
            on_log=self.log,
        )

        self._tasks: list[asyncio.Task] = []
        self._poller: asyncio.Task | None = None
        self._timer_task: asyncio.Task | None = None
        self._stream_task: asyncio.Task | None = None
        self._stt_tick_task: asyncio.Task | None = None
        self._translate_queue: asyncio.Queue[tuple[ChatMessage, str, bool]] = asyncio.Queue()
        self._echo_sources: deque[str] = deque()
        self._rooms: dict[str, str] = {}

        self.messages_total = 0
        self.translated_total = 0
        self.errors_total = 0
        self.skipped_total = 0
        self.sent_total = 0
        self.started_at = time.time()

        self._build_ui()

    # ------------------------------------------------------------ построение

    def _build_ui(self) -> None:
        cfg = self.config

        # --- шапка: каналы и подключение
        self.channel_field = ft.TextField(
            value=", ".join(cfg.twitch.normalized_channels()),
            hint_text="канал(ы): например shroud, dota2",
            label="Каналы Twitch",
            dense=True,
            width=320,
            text_size=13,
            on_submit=lambda _e: self.page.run_task(self.apply_channels),
        )
        self.connect_button = ft.Button(
            content="Подключить",
            icon=ft.Icons.ROCKET_LAUNCH,
            on_click=lambda _e: self.page.run_task(self.toggle_connection),
        )
        self.status_dot = ft.Container(width=9, height=9, border_radius=5, bgcolor=MUTED)
        self.status_text = ft.Text("Не подключено", size=12, color=MUTED)

        header = ft.Row(
            [
                ft.Column(
                    [
                        ft.Text("Govorok", size=20, weight=ft.FontWeight.BOLD, color=TEXT),
                        ft.Text("настройки бота · чат — в док-панели OBS", size=11, color=MUTED),
                    ],
                    spacing=0,
                ),
                ft.Container(width=12),
                self.channel_field,
                self.connect_button,
                ft.Container(expand=True),
                ft.Row([self.status_dot, self.status_text], spacing=6),
            ],
            spacing=10,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        # --- док-панель
        self.dock_enabled = ft.Switch(label="Включить док-панель", value=cfg.dock.enabled)
        self.dock_host = field("Адрес", cfg.dock.host, width=180)
        self.dock_port = field("Порт", str(cfg.dock.port), width=120)
        self.dock_token = field("Токен", cfg.dock.token, hint="пусто = без проверки (только локально)")
        self.dock_history = field("Сообщений в доке", str(cfg.dock.history), width=200)
        self.dock_only = ft.Switch(label="Только переведённые по умолчанию",
                                   value=cfg.dock.only_translated)
        self.dock_url = field("Ссылка для OBS", "", read_only=True, size=11,
                              hint="включите док-панель, чтобы получить ссылку")
        self.dock_status = ft.Text("док выключен", size=11, color=MUTED)

        dock_card = card(
            "Док-панель OBS",
            self.dock_enabled,
            ft.Row([self.dock_host, self.dock_port, self.dock_history], spacing=8),
            self.dock_token,
            self.dock_only,
            self.dock_url,
            ft.Row(
                [
                    ft.TextButton(content="Скопировать ссылку",
                                  on_click=lambda _e: self.copy_dock_url()),
                    ft.TextButton(content="Перезапустить док",
                                  on_click=lambda _e: self.page.run_task(self.restart_dock)),
                    ft.Container(expand=True),
                    self.dock_status,
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            hint="OBS: Виды → Доки → Пользовательские доки браузера → имя Govorok и эта ссылка",
        )

        # --- перевод
        self.provider = ft.Dropdown(
            label="Провайдер",
            width=220,
            dense=True,
            text_size=12,
            value="Свой вариант",
            options=[ft.DropdownOption(key=name, text=name) for name in PROVIDER_PRESETS],
        )
        self.provider.on_change = self._apply_preset
        self.base_url = field("Base URL", cfg.llm.base_url)
        self.api_key = field("API-ключ", cfg.llm.api_key, password=True)
        self.model = field("Модель", cfg.llm.model)
        self.target_lang = field("Язык перевода", cfg.translation.target_lang, width=150)
        self.concurrency = field("Параллельных запросов", str(cfg.llm.max_concurrency), width=200)
        self.rpm = field("Лимит запросов/мин", str(cfg.llm.max_requests_per_minute), width=180)
        self.price_in = field("Цена входа за 1M", str(cfg.llm.price_in_per_mtok or ""),
                              width=180, hint="0 = не считать")
        self.price_out = field("Цена выхода за 1M", str(cfg.llm.price_out_per_mtok or ""),
                               width=180, hint="0 = не считать")
        self.tr_enabled = ft.Switch(label="Переводить чат", value=cfg.translation.enabled)
        self.json_mode = ft.Switch(label="JSON-режим (response_format)", value=cfg.llm.json_mode)
        self.skip_cyrillic = ft.Switch(label="Не переводить сообщения с кириллицей",
                                       value=cfg.translation.skip_cyrillic)
        self.skip_commands = ft.Switch(label="Не переводить !команды",
                                       value=cfg.translation.skip_commands)

        llm_card = card(
            "Перевод (любой OpenAI-совместимый API)",
            self.provider,
            self.base_url,
            self.api_key,
            ft.Row([self.model, self.target_lang], spacing=8),
            ft.Row([self.concurrency, self.rpm], spacing=8),
            ft.Row([self.price_in, self.price_out], spacing=8),
            self.json_mode,
            self.tr_enabled,
            self.skip_cyrillic,
            self.skip_commands,
            hint="цены нужны только для оценки расхода на вкладке «Статистика» в доке",
        )

        # --- twitch
        self.nickname = field("Ник бота", cfg.twitch.nickname,
                              hint="пусто = анонимное чтение чата")
        self.oauth_token = field("OAuth-токен", cfg.twitch.oauth_token, password=True,
                                 hint="oauth:xxxx — нужен для ответов из дока")
        self.client_id = field("Client-Id приложения", cfg.twitch.client_id,
                               hint="нужен только для списка зрителей (Helix chat/chatters)")
        twitch_card = card(
            "Twitch",
            ft.Row([self.nickname, self.client_id], spacing=8),
            self.oauth_token,
            hint="токен: twitchtokengenerator.com → Bot Chat Token; "
                 "для списка зрителей нужен ещё Client-Id и права модератора",
        )

        # --- шрифт чата в доке
        installed = list_system_fonts()
        current_family = cfg.ui.font_family.strip()
        if current_family and current_family not in installed:
            installed = [current_family, *installed]
        self.font_choice = ft.Dropdown(
            label="Шрифт чата в доке",
            value=current_family or None,
            hint_text="по умолчанию (системный)",
            dense=True,
            enable_filter=True,
            editable=True,
            menu_height=320,
            text_size=12,
            options=[ft.DropdownOption(key=name, text=name) for name in installed],
        )
        self.font_slider = ft.Slider(
            min=cfg.ui.MIN_FONT_SIZE,
            max=cfg.ui.MAX_FONT_SIZE,
            divisions=cfg.ui.MAX_FONT_SIZE - cfg.ui.MIN_FONT_SIZE,
            value=cfg.ui.clamped_size(),
            label="{value}",
            width=260,
        )
        self.font_size_text = ft.Text(f"{cfg.ui.clamped_size()} px", size=12, color=MUTED, width=56)
        self.preview_nick = ft.Text("Viewer", size=cfg.ui.clamped_size(),
                                    weight=ft.FontWeight.BOLD, color="#79c0ff",
                                    font_family=current_family or None)
        self.preview_body = ft.Text("Let's go, this boss is insane Kappa 😅",
                                    size=cfg.ui.clamped_size(), color=TEXT,
                                    font_family=current_family or None)
        self.font_choice.on_change = self._refresh_preview
        self.font_slider.on_change = self._refresh_preview

        font_card = card(
            "Шрифт чата в док-панели",
            self.font_choice,
            ft.Row([self.font_slider, self.font_size_text], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(
                content=ft.Column(
                    [
                        ft.Row([self.preview_nick,
                                ft.Text("#channel", size=10, color=MUTED)], spacing=6),
                        self.preview_body,
                    ],
                    spacing=3,
                ),
                bgcolor=PANEL_ALT, border_radius=10,
                padding=ft.Padding.symmetric(horizontal=10, vertical=8),
            ),
            ft.Row(
                [
                    ft.Text("Список ищется по названию, можно ввести своё (например Consolas).",
                            size=10, color=MUTED),
                    ft.Container(expand=True),
                    ft.TextButton(content="Сбросить шрифт",
                                  on_click=lambda _e: self.reset_font()),
                ],
                spacing=6,
            ),
        )

        # --- смайлы
        self.render_emotes = ft.Switch(label="Рисовать смайлы картинками",
                                       value=cfg.ui.render_emotes)
        self.emote_bttv = ft.Switch(label="BetterTTV (BTTV)", value=cfg.emotes.bttv)
        self.emote_ffz = ft.Switch(label="FrankerFaceZ (FFZ)", value=cfg.emotes.ffz)
        self.emote_7tv = ft.Switch(label="7TV", value=cfg.emotes.seventv)
        self.emote_ttl = field("Кэш смайлов, часов", str(cfg.emotes.cache_ttl_hours), width=200)

        emotes_card = card(
            "Смайлы (Twitch + сторонние)",
            self.render_emotes,
            ft.Row([self.emote_bttv, self.emote_7tv], spacing=8),
            ft.Row([self.emote_ffz, self.emote_ttl], spacing=8),
            ft.Row(
                [
                    ft.Text("Смайлы канала подтягиваются по room-id из Twitch.",
                            size=10, color=MUTED),
                    ft.Container(expand=True),
                    ft.TextButton(content="Обновить смайлы",
                                  on_click=lambda _e: self._reload_emotes()),
                ],
                spacing=6,
            ),
        )

        # --- зрители
        self.viewers_enabled = ft.Switch(label="Собирать зрителей", value=cfg.viewers.enabled)
        self.viewers_idle = field("Считать ушедшим через, мин", str(cfg.viewers.idle_minutes),
                                  width=230)
        self.viewers_max = field("Показывать зрителей", str(cfg.viewers.max_users), width=180)
        self.viewers_poll = field("Опрашивать список, сек", str(cfg.viewers.poll_seconds), width=200)
        self.viewers_fetch = ft.Switch(label="Тянуть официальный список зрителей (Helix)",
                                       value=cfg.viewers.fetch_chatters)
        viewers_card = card(
            "Зрители (вкладка в доке)",
            self.viewers_enabled,
            ft.Row([self.viewers_idle, self.viewers_max, self.viewers_poll], spacing=8),
            self.viewers_fetch,
            hint="официальный список требует Client-Id и токен бота-модератора; "
                 "без него вкладка работает на активности чата",
        )

        # --- субтитры со своего микрофона (пока только настройки)
        stt = cfg.stt
        devices = list_input_devices()
        self.stt_enabled = ft.Switch(label="Включить субтитры", value=stt.enabled)
        self.stt_model = ft.Dropdown(
            label="Модель faster-whisper", width=300, dense=True, text_size=12,
            value=choice(stt.model, STT_MODELS, "base"),
            options=[ft.DropdownOption(key=key, text=title) for key, title in STT_MODELS],
        )
        self.stt_device = ft.Dropdown(
            label="Считать на", width=190, dense=True, text_size=12,
            value=choice(stt.device, STT_DEVICES, "auto"),
            options=[ft.DropdownOption(key=key, text=title) for key, title in STT_DEVICES],
        )
        self.stt_compute = ft.Dropdown(
            label="Точность", width=230, dense=True, text_size=12,
            value=choice(stt.compute_type, STT_COMPUTE, "auto"),
            options=[ft.DropdownOption(key=key, text=title) for key, title in STT_COMPUTE],
        )
        self.stt_lang = ft.Dropdown(
            label="Язык речи", width=190, dense=True, text_size=12,
            value=choice(stt.language, STT_LANGS, "ru"),
            options=[ft.DropdownOption(key=key, text=title) for key, title in STT_LANGS],
        )
        self.stt_beam = field("Beam size (1–5)", str(stt.beam_size), width=170,
                              hint="меньше = быстрее")
        self.stt_mic = ft.Dropdown(
            label="Микрофон", width=340, dense=True, text_size=12, editable=True,
            enable_filter=True,
            value=stt.input_device or None,
            hint_text="по умолчанию (системный)",
            options=[ft.DropdownOption(key=key, text=title) for key, title in devices],
        )
        self.stt_gain = ft.Slider(
            min=1.0, max=8.0, divisions=14, value=stt.clamped_gain(),
            label="{value}×", width=240,
        )
        self.stt_gain_text = ft.Text(f"{stt.clamped_gain():.1f}×", size=12, color=MUTED, width=48)
        self.stt_chunk = ft.Slider(
            min=STTConfig.MIN_CHUNK, max=STTConfig.MAX_CHUNK, divisions=9,
            value=stt.clamped_chunk(), label="{value} c", width=240,
        )
        self.stt_chunk_text = ft.Text(f"{stt.clamped_chunk():.1f} c", size=12, color=MUTED,
                                      width=60)
        self.stt_silence = field("Тишина до конца фразы, мс", str(stt.silence_ms), width=240)
        self.stt_vad = ft.Switch(label="Резать тишину (VAD)", value=stt.vad)
        self.stt_vad_engine = ft.Dropdown(
            label="Детектор речи", width=210, dense=True, text_size=12,
            value=choice(stt.vad_engine, STT_VAD_ENGINES, "silero"),
            options=[ft.DropdownOption(key=key, text=title) for key, title in STT_VAD_ENGINES],
        )
        self.stt_ratio = field("Мин. доля речи в фразе", f"{stt.clamped_ratio():.2f}", width=210,
                               hint="0–0.9")
        self.stt_no_speech = field("Макс. «нет речи»", f"{stt.clamped_no_speech():.2f}",
                                   width=200, hint="меньше — строго")
        self.stt_logprob = field("Мин. уверенность текста", f"{stt.clamped_logprob():.2f}",
                                 width=220, hint="обычно -1.2…-0.8")
        self.stt_mode = ft.Dropdown(
            label="Что показывать", width=230, dense=True, text_size=12,
            value=choice(stt.subtitle_mode, STT_MODES, "both"),
            options=[ft.DropdownOption(key=key, text=title) for key, title in STT_MODES],
        )
        self.stt_translate = field("Язык перевода субтитров", stt.translate_to, width=230,
                                   hint="пусто = язык перевода чата")
        self.stt_listen_button = ft.Button(content="Слушать микрофон", icon=ft.Icons.MIC,
                                           on_click=lambda _e: self.page.run_task(self.toggle_stt))
        self.stt_unload_button = ft.TextButton(
            content="Отпустить память модели",
            on_click=lambda _e: self.page.run_task(self.unload_stt_model))
        self.stt_chunk.on_change = self._refresh_subs_preview
        self.stt_gain.on_change = self._refresh_stt_gain_text

        stt_card = card(
            "Субтитры: распознавание речи (faster-whisper)",
            self.stt_enabled,
            ft.Row([self.stt_model, self.stt_device], spacing=8),
            ft.Row([self.stt_compute, self.stt_lang], spacing=8),
            self.stt_mic,
            ft.Row([self.stt_gain, self.stt_gain_text, self.stt_silence], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([self.stt_vad, self.stt_vad_engine, self.stt_beam], spacing=8),
            ft.Row([self.stt_ratio, self.stt_no_speech, self.stt_logprob], spacing=8),
            ft.Row([self.stt_mode, self.stt_translate], spacing=8),
            ft.Row([self.stt_chunk, self.stt_chunk_text], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row(
                [
                    self.stt_listen_button,
                    ft.TextButton(content="Показать пример в оверлее",
                                  on_click=lambda _e: self.demo_subtitle()),
                    ft.Container(expand=True),
                ],
                spacing=6,
            ),
            hint="«Усиление входа» спасает тихий микрофон, когда не хочется крутить "
                 "системные ползунки. «Детектор речи: silero» не пускает музыку и клики "
                 "в распознавание — иначе Whisper додумывает «спокойная музыка» и "
                 "«татататата»; строгость регулируют три поля ниже",
        )

        # --- как выглядят субтитры на стриме
        self.stt_font = ft.Slider(
            min=STTConfig.MIN_FONT_SIZE, max=STTConfig.MAX_FONT_SIZE,
            divisions=STTConfig.MAX_FONT_SIZE - STTConfig.MIN_FONT_SIZE,
            value=stt.clamped_font(), label="{value}", width=260,
        )
        self.stt_font_text = ft.Text(f"{stt.clamped_font()} px", size=12, color=MUTED, width=60)
        self.stt_lines = field("Строк на экране", str(stt.clamped_lines()), width=180)
        self.stt_max_chars = field("Символов в строке", str(stt.max_chars), width=190)
        self.stt_hold = ft.Slider(
            min=STTConfig.MIN_HOLD, max=STTConfig.MAX_HOLD, divisions=14,
            value=stt.clamped_hold(), label="{value} c", width=240,
        )
        self.stt_hold_text = ft.Text(f"{stt.clamped_hold():.0f} c", size=12, color=MUTED,
                                     width=60)
        self.stt_position = ft.Dropdown(
            label="Где показывать", width=200, dense=True, text_size=12,
            value=choice(stt.position, STT_POSITIONS, "bottom"),
            options=[ft.DropdownOption(key=key, text=title) for key, title in STT_POSITIONS],
        )
        self.stt_font.on_change = self._refresh_subs_preview
        self.stt_hold.on_change = self._refresh_subs_preview
        self.subs_preview = ft.Text(
            "Вот так будет выглядеть твоя речь на стриме",
            size=stt.clamped_font(), color=TEXT, max_lines=stt.clamped_lines(),
            overflow=ft.TextOverflow.ELLIPSIS, text_align=ft.TextAlign.CENTER,
        )

        subs_card = card(
            "Вид субтитров",
            ft.Row([self.stt_font, self.stt_font_text], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Row([self.stt_lines, self.stt_max_chars], spacing=8),
            ft.Row([self.stt_hold, self.stt_hold_text, self.stt_position], spacing=8,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            ft.Container(
                content=self.subs_preview,
                bgcolor=PANEL_ALT, border_radius=10, alignment=ft.Alignment.CENTER,
                padding=ft.Padding.symmetric(horizontal=14, vertical=10),
            ),
            hint="размер и строки применяются к оверлею в OBS, а не к этому окну",
        )

        # --- статус модуля
        self.stt_status = ft.Text("", size=11, color=MUTED)
        self.stt_meter = ft.ProgressBar(value=0, bar_height=10, color=OK_COLOR,
                                        bgcolor=PANEL_ALT, border_radius=5, width=260)
        self.stt_meter_text = ft.Text("движок остановлен", size=11, color=MUTED)
        self.mic_probe_text = ft.Text("", size=11, color=MUTED)
        self.subs_url = field("Ссылка на оверлей для OBS", "", read_only=True, size=11,
                              hint="включите док-панель, чтобы получить ссылку")
        self.stt_status_card = card(
            "Статус модуля и оверлей в OBS",
            self.stt_status,
            ft.Row([self.stt_meter, self.stt_meter_text], spacing=10,
                   vertical_alignment=ft.CrossAxisAlignment.CENTER),
            self.mic_probe_text,
            self.subs_url,
            ft.Row(
                [
                    ft.TextButton(content="Проверить микрофон (3 с)",
                                  on_click=lambda _e: self.page.run_task(self.check_microphone)),
                    ft.TextButton(content="Скопировать ссылку на оверлей",
                                  on_click=lambda _e: self.copy_subs_url()),
                    self.stt_unload_button,
                    ft.TextButton(content="Проверить снова",
                                  on_click=lambda _e: self._refresh_stt_status()),
                    ft.Container(expand=True),
                ],
                spacing=6,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            hint="OBS: Источники → + → Браузер → эта ссылка; снять «Выключать источник, "
                 "когда он не виден», иначе субтитры пропадут при смене сцены",
        )

        # --- команды
        self._editing_command: int | None = None
        self.commands_list = ft.Column(spacing=4)
        self.cmd_triggers = field("Триггеры через запятую", "",
                                  hint="!uptime, !аптайм — регистр не важен")
        self.cmd_response = ft.TextField(label="Ответ", value="", multiline=True, min_lines=2,
                                         max_lines=4, dense=True, text_size=12,
                                         hint_text="Стрим идёт {uptime}, игра: {game}")
        self.cmd_permission = ft.Dropdown(
            label="Кто может", width=180, dense=True, text_size=12, value="everyone",
            options=[ft.DropdownOption(key=key, text=title)
                     for key, title in PERMISSION_TITLES.items()])
        self.cmd_cooldown = field("Кулдаун, сек", "10", width=140)
        self.cmd_enabled = ft.Switch(label="Включена", value=True)
        self.cmd_in_chat = ft.Switch(label="Отвечать в чат", value=True)
        self.cmd_in_dock = ft.Switch(label="Показывать в доке", value=True)
        self.cmd_preview = ft.Text("", size=11, color=OK_COLOR)
        self.cmd_save_button = ft.Button(content="Добавить команду",
                                         on_click=lambda _e: self.save_command())

        commands_card = card(
            "Команды",
            self.commands_list,
            ft.Divider(height=8),
            self.cmd_triggers,
            self.cmd_response,
            ft.Row([self.cmd_permission, self.cmd_cooldown], spacing=8),
            ft.Row([self.cmd_enabled, self.cmd_in_chat, self.cmd_in_dock], spacing=8),
            ft.Row(
                [
                    self.cmd_save_button,
                    ft.TextButton(content="Очистить форму",
                                  on_click=lambda _e: self.clear_command_form()),
                    ft.TextButton(content="Проверить ответ",
                                  on_click=lambda _e: self.preview_command()),
                    ft.Container(expand=True),
                ],
                spacing=6,
            ),
            self.cmd_preview,
            hint="переменные: {user} {display} {channel} {args} {1} {target} {count} "
                 "{date} {time} {uptime} {game} {title} {viewers} {random:a|b|c}",
        )

        # --- таймеры
        self._editing_timer: int | None = None
        self.timers_list = ft.Column(spacing=4)
        self.timer_interval = field("Каждые, минут", "15", width=160)
        self.timer_lines = ft.TextField(label="Строки сообщения", value="", multiline=True,
                                        min_lines=2, max_lines=4, dense=True, text_size=12,
                                        hint_text="Подписывайтесь на канал! | Не забывайте про соцсети")
        self.timer_enabled = ft.Switch(label="Включён", value=True)
        self.timer_active = ft.Switch(label="Только при активном чате", value=True)
        self.timer_window = field("Окно, мин", "5", width=120)
        self.timer_min = field("Мин. сообщений", "3", width=150)
        self.timer_save_button = ft.Button(content="Добавить таймер",
                                           on_click=lambda _e: self.save_timer())

        timers_card = card(
            "Таймеры",
            self.timers_list,
            ft.Divider(height=8),
            self.timer_interval,
            self.timer_lines,
            ft.Row([self.timer_enabled, self.timer_active], spacing=8),
            ft.Row([self.timer_window, self.timer_min], spacing=8),
            ft.Row(
                [
                    self.timer_save_button,
                    ft.TextButton(content="Очистить форму",
                                  on_click=lambda _e: self.clear_timer_form()),
                    ft.Container(expand=True),
                ],
                spacing=6,
            ),
            hint="если строк несколько, они чередуются случайно; "
                 "таймер молчит, когда в чате тишина",
        )

        # --- действия
        self.save_button = ft.Button(content="Сохранить настройки", icon=ft.Icons.SAVE,
                                     on_click=lambda _e: self.apply_settings())
        self.reset_stats_button = ft.TextButton(content="Сбросить статистику",
                                                on_click=lambda _e: self.reset_stats())
        self.warning = ft.Container(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.KEY_OFF, size=14, color=WARN_COLOR),
                    ft.Text("Перевод выключен: укажите API-ключ и модель в настройках.",
                            size=11, color=WARN_COLOR),
                ],
                spacing=8,
            ),
            bgcolor=ft.Colors.with_opacity(0.08, WARN_COLOR),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.35, WARN_COLOR)),
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            visible=not cfg.is_translation_ready(),
        )

        self.stats_text = ft.Text("", size=11, color=MUTED)
        self.log_text = ft.Text("", size=10, color=MUTED, max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS)

        actions = ft.Row(
            [self.save_button, self.reset_stats_button, ft.Container(expand=True)],
            spacing=8,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

        tabs = [
            ft.Tab(label="Подключение", icon=ft.Icons.LINK),
            ft.Tab(label="Агент / Перевод", icon=ft.Icons.AUTO_AWESOME),
            ft.Tab(label="Внешний вид", icon=ft.Icons.PALETTE),
            ft.Tab(label="Субтитры", icon=ft.Icons.SUBTITLES),
            ft.Tab(label="Автоматизация", icon=ft.Icons.SMART_TOY),
        ]

        def view(*cards: ft.Control) -> ft.Control:
            return ft.Column(
                list(cards),
                spacing=10,
                scroll=ft.ScrollMode.AUTO,
                expand=True,
            )

        settings = ft.Tabs(
            length=len(tabs),
            selected_index=0,
            expand=True,
            content=ft.Column(
                expand=True,
                controls=[
                    ft.TabBar(tabs=tabs),
                    ft.TabBarView(
                        expand=True,
                        controls=[
                            view(twitch_card, dock_card, viewers_card),
                            view(llm_card),
                            view(font_card, emotes_card),
                            view(stt_card, subs_card, self.stt_status_card),
                            view(commands_card, timers_card),
                        ],
                    ),
                ],
            ),
        )
        self.settings_column = settings

        self.root = ft.Column(
            [
                header,
                self.warning,
                actions,
                settings,
                ft.Divider(height=8),
                self.stats_text,
                ft.Row(
                    [
                        ft.Text(str(CONFIG_PATH), size=10,
                                color=ft.Colors.with_opacity(0.6, MUTED)),
                        ft.Container(expand=True),
                        self.log_text,
                    ],
                    spacing=8,
                ),
            ],
            spacing=8,
            expand=True,
        )
        self._refresh_stats()
        self._refresh_commands()
        self._refresh_timers()
        self._refresh_stt_status()
        self._refresh_subs_preview()

    # ---------------------------------------------------------- монтирование

    def mount(self) -> None:
        """Показать окно и запустить бота с док-панелью."""
        self.page.bgcolor = BG
        self.page.padding = ft.Padding.all(12)
        self.page.services.append(self.clipboard)
        self.page.add(self.root)
        self._start_tasks()
        self.page.run_task(self.emotes.load_globals)
        if self.config.dock.enabled:
            self.page.run_task(self.start_dock)
        if self.config.viewers.enabled:
            self._poller = asyncio.ensure_future(self.chatters.poll_loop(self.known_rooms))
        if self.config.stt.enabled:
            self.page.run_task(self.start_stt)
        self._timer_task = asyncio.ensure_future(self.timers.run(self.respond))
        if self.stream.enabled():
            self._stream_task = asyncio.ensure_future(self.stream.poll_loop(self.known_channels))
        if self.config.twitch.normalized_channels():
            self.page.run_task(self.connect)

    def known_channels(self) -> list[str]:
        """Каналы для опроса состояния стрима."""
        return self.config.twitch.normalized_channels()

    def _start_tasks(self) -> None:
        workers = max(1, min(8, self.config.llm.max_concurrency))
        self._tasks = [self.page.run_task(self._translation_worker, i) for i in range(workers)]

    async def shutdown(self) -> None:
        for attr in ("_poller", "_timer_task", "_stream_task", "_stt_tick_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task:
                task.cancel()
        for task in self._tasks:
            task.cancel()
        await self.stop_stt()
        await self.client.stop()
        await self.dock.stop()
        await self.chatters.aclose()
        await self.stream.aclose()
        await self.emotes.aclose()
        await self.translator.aclose()

    # ----------------------------------------------------------- док-панель

    async def start_dock(self) -> bool:
        started = await self.dock.start()
        if not started:
            self.log(f"док не запустился: порт {self.config.dock.port} занят?")
        self._refresh_dock_controls()
        return started

    async def stop_dock(self) -> None:
        await self.dock.stop()
        self._refresh_dock_controls()

    async def restart_dock(self) -> None:
        await self.dock.stop()
        await self.start_dock()

    def copy_dock_url(self) -> None:
        if not self.dock.running:
            self.log("док выключен — ссылки пока нет")
            return
        self.page.run_task(self.clipboard.set, self.dock.url())
        self.log("ссылка для OBS скопирована в буфер")

    def _refresh_dock_controls(self) -> None:
        if self.dock.running:
            self.dock_status.value = f"док запущен · клиентов: {self.dock.clients}"
            self.dock_status.color = OK_COLOR
            self.dock_url.value = self.dock.url()
            self.subs_url.value = self.dock.url_subs()
        else:
            self.dock_status.value = "док выключен"
            self.dock_status.color = MUTED
            self.dock_url.value = ""
            self.subs_url.value = ""
        try:
            self.page.update()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------ статистика

    def stats_snapshot(self) -> dict:
        return stats_payload(
            started_at=self.started_at,
            messages=self.messages_total,
            translated=self.translated_total,
            skipped=self.skipped_total,
            errors=self.errors_total,
            translator=self.translator,
            config=self.config,
            channels=self.config.twitch.normalized_channels(),
            clients=self.dock.clients,
            can_send=self.config.twitch.can_send,
            sent=self.sent_total,
            commands_fired=self.commands.fired,
            timers_fired=self.timers.sent,
            emotes={"global": self.emotes.global_count,
                    "channels": self.emotes.total_channel_emotes()},
        )

    def viewers_snapshot(self) -> dict:
        return self.viewers.snapshot(idle_minutes=self.config.viewers.idle_minutes,
                                     limit=self.config.viewers.max_users)

    def reset_stats(self) -> None:
        self.messages_total = self.translated_total = 0
        self.errors_total = self.skipped_total = self.sent_total = 0
        self.commands.fired = 0
        self.timers.sent = 0
        self.started_at = time.time()
        self.translator.reset_usage()
        self.viewers.clear()
        self._refresh_stats()
        self.log("статистика сброшена")
        self.page.update()

    def viewers_count(self) -> int:
        return self.viewers.snapshot()["total"]

    def _refresh_stats(self) -> None:
        cost = estimate_cost(self.translator.prompt_tokens, self.translator.completion_tokens,
                             self.config.llm.price_in_per_mtok, self.config.llm.price_out_per_mtok)
        money = (f"   ·   API: {cost:.4f} {self.config.llm.currency or 'USD'}"
                 if cost is not None else "")
        emotes = f"смайлы: {self.emotes.global_count} глоб."
        channel_total = self.emotes.total_channel_emotes()
        if channel_total:
            emotes += f" + {channel_total} канальных"
        elif self.emotes.pending_loads:
            emotes += " + канальные грузятся…"
        self.stats_text.value = (
            f"сообщений: {self.messages_total}   ·   переведено: {self.translated_total}   ·   "
            f"ошибок: {self.errors_total}   ·   кэш: {self.translator.cache_size}   ·   "
            f"запросов к API: {self.translator.requests_sent}{money}   ·   "
            f"зрителей: {self.viewers_count()}   ·   {emotes}"
        )

    # ---------------------------------------------------------- события чата

    def known_rooms(self) -> list[tuple[str, str]]:
        return list(self._rooms.items())

    def on_room(self, room_id: str, channel: str) -> None:
        if room_id:
            self._rooms[room_id] = channel
        self.emotes.ensure_channel(room_id, channel)

    def on_message(self, message: ChatMessage) -> None:
        """Сообщение из чата: уходит в док, окно только считает статистику."""
        self.messages_total += 1
        self.viewers.note(message)
        self.timers.observe(message)
        should, _reason = self.translator.should_translate(message.text)
        if should:
            self._translate_queue.put_nowait((message, message_key(message), False))
        else:
            self.skipped_total += 1
        self.dock.publish_message(message_payload(message, self.emotes, pending=should,
                                                  config=self.config))
        self.handle_command(message)

    def handle_command(self, message: ChatMessage) -> None:
        """Проверить сообщение на `!команду` и ответить, если положено."""
        if not self.config.commands:
            return
        result = self.commands.handle(message, stream=self.stream.cached(message.channel))
        if result is None:
            return
        task = self.page.run_task(self.respond, message.channel, result.text, result.source,
                                  in_chat=result.command.reply_in_chat,
                                  in_dock=result.command.show_in_dock)
        if isinstance(task, asyncio.Future):
            task.add_done_callback(self._report_task)

    def _report_task(self, task: asyncio.Future) -> None:
        """Не терять ошибки фоновых ответов: иначе они пропадают молча."""
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.log(f"ответ бота сорвался: {error!r}")

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
            self.dock.publish_message(self_message_payload(text, channel, self.config,
                                                           source=source))
            reason = "нет связи с Twitch" if self.config.twitch.can_send else "нет OAuth-токена"
            self.log(f"ответ только в доке ({reason}): {text[:60]}")

    def on_notice(self, kind: str, text: str) -> None:
        if text:
            self.dock.publish_notice(text)
            self.log(text)

    def on_sent_message(self, text: str, channel: str) -> None:
        """Наше сообщение ушло в чат: Twitch его не вернёт, показываем сами."""
        self.sent_total += 1
        source, in_dock = self._echo_sources.popleft() if self._echo_sources else ("", True)
        if in_dock:
            self.dock.publish_message(self_message_payload(text, channel, self.config,
                                                           source=source))
        self.log(f"отправлено в #{channel}: {text[:60]}")
        self._refresh_stats()

    def on_status(self, state: str, text: str) -> None:
        color, _ = STATUS_STYLE.get(state, (MUTED, ""))
        self.status_dot.bgcolor = color
        self.status_text.value = text
        self.status_text.color = color
        self.connect_button.content = "Отключить" if state in {"online", "waiting"} else "Подключить"
        self.dock.publish_status(state, text)
        self._refresh_dock_controls()

    def log(self, text: str) -> None:
        self.log_text.value = f"{time.strftime('%H:%M:%S')}  {text}"
        try:
            self.page.update()
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- конвейер

    async def _translation_worker(self, index: int) -> None:
        while True:
            message, key, force = await self._translate_queue.get()
            result = await self.translator.translate(message.text, force=force)
            if result.error:
                self.errors_total += 1
                self.log(f"перевод не удался: {result.error}")
            elif result.text:
                self.translated_total += 1
            self.dock.publish_translation(key, result)
            self.dock.publish(self.stats_snapshot())
            self._refresh_stats()

    async def send_to_chat(self, text: str, *, reply_to: str = "",
                           channel: str = "") -> tuple[bool, str]:
        """Ответ в чат из док-панели (веткой, если передан reply_to)."""
        if not self.config.twitch.can_send:
            return False, "нужны ник бота и OAuth-токен (раздел Twitch)"
        try:
            await self.client.send_message(text, channel=channel or None, reply_to=reply_to)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        return True, ""

    # ---------------------------------------------------------- подключение

    async def toggle_connection(self) -> None:
        if self.client.is_running:
            await self.disconnect()
        else:
            await self.connect()

    async def apply_channels(self) -> None:
        """Enter в поле каналов: подключиться или до-подписаться на новые каналы."""
        self.config.twitch.channels = [self.channel_field.value or ""]
        self.config.save(redact=self.config_save_redacts)
        desired = self.config.twitch.normalized_channels()
        if not desired:
            self.log("укажите хотя бы один канал")
            return
        if self.client.is_running:
            self.client.config = self.config.twitch
            self.client.set_channels(desired)
            self.log(f"каналы обновлены: {', '.join(desired)}")
        else:
            await self.connect()
        self.page.update()

    async def connect(self) -> None:
        self.config.twitch.channels = [self.channel_field.value or ""]
        self.config.save(redact=self.config_save_redacts)
        if not self.config.twitch.normalized_channels():
            self.log("укажите хотя бы один канал")
            return
        self.client.config = self.config.twitch
        await self.client.start()
        self._refresh_stats()

    async def disconnect(self) -> None:
        await self.client.stop()

    # ------------------------------------------------------------- настройки

    def _apply_preset(self, _e=None) -> None:
        url, name = PROVIDER_PRESETS.get(self.provider.value or "", ("", ""))
        if url:
            self.base_url.value = url
            self.model.value = name
        self.base_url.update()
        self.model.update()

    def _refresh_preview(self, _e=None) -> None:
        family = (self.font_choice.value or "").strip() or None
        size = int(self.font_slider.value or self.config.ui.clamped_size())
        self.font_size_text.value = f"{size} px"
        for control in (self.preview_nick, self.preview_body):
            control.font_family = family
            control.size = size
        self.page.update()

    def reset_font(self) -> None:
        self.font_choice.value = None
        self.font_slider.value = 14
        self._refresh_preview()

    # ------------------------------------------------------------- субтитры

    def _refresh_subs_preview(self, _e=None) -> None:
        """Живой предпросмотр: размер шрифта, число строк, чанк, удержание."""
        stt = self.config.stt
        size = max(STTConfig.MIN_FONT_SIZE, min(STTConfig.MAX_FONT_SIZE,
                   _int_or(self.stt_font.value, stt.clamped_font())))
        lines = max(STTConfig.MIN_LINES, min(STTConfig.MAX_LINES,
                   _int_or(self.stt_lines.value, stt.clamped_lines())))
        hold = max(STTConfig.MIN_HOLD, min(STTConfig.MAX_HOLD,
                   _float_or(self.stt_hold.value, stt.clamped_hold())))
        chunk = max(STTConfig.MIN_CHUNK, min(STTConfig.MAX_CHUNK,
                   _float_or(self.stt_chunk.value, stt.clamped_chunk())))
        self.stt_font_text.value = f"{size} px"
        self.stt_hold_text.value = f"{hold:.0f} c"
        self.stt_chunk_text.value = f"{chunk:.1f} c"
        self.subs_preview.size = size
        self.subs_preview.max_lines = lines
        try:
            self.page.update()
        except Exception:  # noqa: BLE001 - окно ещё не смонтировано
            pass

    def _refresh_stt_gain_text(self, _e=None) -> None:
        """Подпись рядом с ползунком усиления: сколько раз усиливаем сигнал."""
        value = _float_or(self.stt_gain.value, self.config.stt.clamped_gain())
        self.stt_gain_text.value = f"{max(1.0, min(8.0, value)):.1f}×"
        try:
            self.stt_gain_text.update()
        except Exception:  # noqa: BLE001 - окно ещё не смонтировано
            pass

    def _refresh_stt_status(self, _e=None) -> None:
        """Пакеты, состояние движка и ссылка на оверлей — одной плашкой."""
        deps = stt_dependencies()
        titles = (("faster_whisper", "faster-whisper"), ("sounddevice", "sounddevice"),
                  ("numpy", "numpy"))
        missing = [title for name, title in titles if not deps[name]]
        header = ("пакеты: всё на месте" if not missing
                  else "пакеты: нет " + ", ".join(missing))
        stt = self.config.stt
        stats = self.stt_service.stats()
        threshold_db = level_db(stats.get("threshold") or 0.0)
        peak_db = level_db(stats.get("peak") or 0.0)
        planned = (f"настройки: {stt.model} · язык {stt.language} · "
                   f"VAD {'вкл' if stt.vad else 'выкл'} · тишина {stt.silence_ms} мс · "
                   f"усиление {stt.clamped_gain():.1f}×")
        lines = [header, self._stt_runtime_line()]
        if stats["running"]:
            lines.append(f"вход: {stats.get('input') or 'по умолчанию'} · "
                         f"пик {peak_db:.0f} дБ · порог {threshold_db:.0f} дБ · "
                         f"оверлеев {self.dock.subs_clients}")
            garbage = int(stats.get("garbage_blocks") or 0)
            lines.append(f"детектор речи: {stats.get('speech_engine')} · речь "
                         f"{float(stats.get('speech_prob') or 0):.2f} · "
                         f"отброшено: не-речь {stats.get('rejected_speech')}, "
                         f"плохой текст {stats.get('rejected_quality')}"
                         + (f", мусорных блоков {garbage}" if garbage else ""))
            target = (stt.translate_to or "").strip()
            mode = stt.clamped_mode()
            label_mode = {"ru": "только речь", "en": "только перевод",
                          "both": "речь + перевод"}.get(mode, mode)
            if mode == "ru":
                lines.append(f"субтитры: {label_mode} — перевод не запрашивается")
            if target:
                same = target[:2].casefold() == (stt.language or "ru")[:2].casefold()
                lines.append(("перевод второй строки: → " + target
                              + f" · запросов {stats.get('translate_requests')} · "
                                f"готово {stats.get('translate_done')}"
                              + (f" · «{stats.get('last_translation')}»"
                                 if stats.get("last_translation") else ""))
                             if not same else
                             (f"перевод субтитров: язык речи и цель совпадают ({target}) — "
                              "вторая строка не появится"))
        lines.append(planned)
        self.stt_status.value = "\n".join(lines)
        self.stt_status.color = (ERR_COLOR if stats["error"]
                                 else OK_COLOR if stats["running"] else MUTED)
        self.subs_url.value = self.dock.url_subs() if self.dock.running else ""
        listening = bool(stats["running"])
        if not listening:
            self.stt_meter.value = 0
            self.stt_meter_text.value = "движок остановлен"
        self.stt_listen_button.content = "Стоп" if listening else "Слушать микрофон"
        self.stt_listen_button.icon = ft.Icons.STOP if listening else ft.Icons.MIC
        loaded = self.stt_service.recognizer.loaded
        self.stt_unload_button.visible = bool(loaded)
        try:
            self.page.update()
        except Exception:  # noqa: BLE001 - окно ещё не смонтировано
            pass

    def _stt_runtime_line(self) -> str:
        stats = self.stt_service.stats()
        if stats["error"]:
            return f"движок: ошибка — {stats['error']}"
        if not stats["running"]:
            return "движок: остановлен"
        return (f"движок: слушаю · {stats['device']} · реплик {stats['utterances']} · "
                f"субтитров {stats['subtitles']} · задержка {stats['latency_ms']} мс")

    # ---------------------------------------------------- проверка микрофона

    def probe_microphone(self, seconds: float = 3.0) -> tuple[bool, str]:
        """Замерить уровень с микрофона и сказать, хватает ли его для VAD.

        Читаем через коллбэк, а не блокирующим read: у DirectSound блокирующее
        чтение отдаёт одни нули, и проба врала «слишком тихо» на рабочем микрофоне.
        """
        try:
            import numpy as np
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            return False, f"sounddevice не установлен: {exc}"
        from govorok.stt import BLOCK_SIZE, SAMPLE_RATE, device_label, input_candidates

        stats = self.stt_service.stats()
        threshold = float(stats.get("threshold") or 0.0067)
        candidates = input_candidates(self.config.stt.input_device)
        per_device = max(0.8, seconds / max(1, len(candidates)))
        best: tuple[float, str] = (0.0, "")
        last_error = ""
        for index in candidates:
            levels: list[float] = []

            def collect(data, _frames, _time, _status) -> None:  # noqa: ANN001 - PortAudio
                if len(data):
                    chunk = np.asarray(data, dtype=np.float32)
                    levels.append(float(np.sqrt(np.mean(np.square(chunk)))))

            try:
                stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                        blocksize=BLOCK_SIZE, device=index, callback=collect)
                stream.start()
            except Exception as exc:  # noqa: BLE001
                last_error = f"{device_label(index)}: {exc}"
                continue
            try:
                time.sleep(per_device)
            finally:
                try:
                    stream.stop()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            peak = max(levels) if levels else 0.0
            label = device_label(index)
            if peak > best[0]:
                best = (peak, label)
            if peak >= threshold:                     # нашли рабочий вариант
                return True, (f"микрофон слышно: {label} · пик {level_db(peak):.0f} дБ "
                              f"при пороге {level_db(threshold):.0f} дБ")
        if not best[1]:
            return False, f"не удалось открыть микрофон: {last_error}"
        peak, label = best
        if peak <= 1e-6:
            return False, (f"{label} отдаёт тишину — микрофон выключен, замьючен "
                           f"или закрыт доступ к нему в Windows")
        return False, (f"слишком тихо: {label} · пик {level_db(peak):.0f} дБ при пороге "
                       f"{level_db(threshold):.0f} дБ — поднимите гейн или выберите другой вход")

    async def check_microphone(self) -> None:
        """Кнопка «Проверить микрофон»: 3 секунды слушаем и показываем уровень."""
        self.log("проверяю микрофон 3 секунды — говори что-нибудь…")
        self.mic_probe_text.value = "слушаю 3 секунды…"
        self.mic_probe_text.color = MUTED
        self.page.update()
        if self.stt_service.running:
            ok, text = await self._probe_from_engine(3.0)
        else:
            ok, text = await asyncio.to_thread(self.probe_microphone, 3.0)
        self.mic_probe_text.value = text
        self.mic_probe_text.color = OK_COLOR if ok else WARN_COLOR
        self.log(text)
        self.page.update()

    async def _probe_from_engine(self, seconds: float) -> tuple[bool, str]:
        """Замер без второго потока: движок уже держит микрофон."""
        peak = 0.0
        deadline = time.time() + max(1.0, seconds)
        while time.time() < deadline:
            await asyncio.sleep(0.1)
            peak = max(peak, float(self.stt_service.stats().get("level") or 0.0))
        stats = self.stt_service.stats()
        threshold = float(stats.get("threshold") or 0.0067)
        name = stats.get("input") or "микрофон"
        if peak >= threshold:
            return True, (f"микрофон слышно: {name} · пик {level_db(peak):.0f} дБ "
                          f"при пороге {level_db(threshold):.0f} дБ")
        return False, (f"слишком тихо: {name} · пик {level_db(peak):.0f} дБ при пороге "
                       f"{level_db(threshold):.0f} дБ — поднимите гейн или выберите другой вход")

    def _refresh_meter(self) -> None:
        """Прыгающая полоска уровня микрофона (обновляется 10 раз в секунду)."""
        stats = self.stt_service.stats()
        running = bool(stats["running"])
        level = float(stats.get("level") or 0.0)
        self.stt_meter.value = meter_fraction(level) if running else 0
        self.stt_meter.color = OK_COLOR if running else MUTED
        if running:
            name = (stats.get("input") or "микрофон").strip()
            self.stt_meter_text.value = f"{name} · {level_db(level):.0f} дБ"
        else:
            self.stt_meter_text.value = "движок остановлен"
        try:                                   # только свои контролы: дешевле page.update
            self.stt_meter.update()
            self.stt_meter_text.update()
        except Exception:  # noqa: BLE001 - окно ещё не смонтировано
            pass

    # -------------------------------------------------------------- движок

    async def start_stt(self) -> bool:
        """Открыть микрофон и начать распознавание."""
        if self.stt_service.running:
            return True
        ok, reason = await self.stt_service.start(self.dock.publish)
        if not ok:
            self.log(f"субтитры не запустились: {reason}")
        else:
            self.log(f"субтитры слушают: {self.stt_service.device_label}")
            if self._stt_tick_task is None or self._stt_tick_task.done():
                self._stt_tick_task = asyncio.ensure_future(self._stt_tick())
        self._refresh_stt_status()
        return ok

    async def unload_stt_model(self) -> None:
        """Освободить видеопамять модели, не перезапуская процесс бота."""
        message = await self.stt_service.unload(to_cpu=False)
        self.log(f"субтитры: {message}")
        self._refresh_stt_status()

    async def stop_stt(self) -> None:
        await self.stt_service.stop()
        if self._stt_tick_task is not None:
            self._stt_tick_task.cancel()
            self._stt_tick_task = None
        self._refresh_stt_status()

    async def restart_stt(self) -> None:
        await self.stop_stt()
        if self.config.stt.enabled:
            await self.start_stt()

    async def toggle_stt(self) -> None:
        if self.stt_service.running:
            await self.stop_stt()
        else:
            await self.start_stt()

    async def _stt_tick(self) -> None:
        """10 раз в секунду — полоска уровня, раз в секунду — строка статуса."""
        ticks = 0
        while self.stt_service.running:
            self._refresh_meter()
            ticks += 1
            if ticks % 10 == 0:
                self._refresh_stt_status()
            await asyncio.sleep(0.1)

    def copy_subs_url(self) -> None:
        if not self.dock.running:
            self.log("док выключен — ссылки на оверлей нет")
            return
        self.page.run_task(self.clipboard.set, self.dock.url_subs())
        self.log("ссылка на оверлей субтитров скопирована в буфер")

    def demo_subtitle(self) -> None:
        """Показать в оверлее пример: удобно выставлять позицию в OBS."""
        if not self.dock.running:
            self.log("док выключен: оверлею некуда отправить пример")
            return
        self.dock.publish_subtitle({"id": "demo", "final": True,
                                    "text": "Пример субтитров: так будет выглядеть твоя речь",
                                    "ts": time.time()})
        self.log("в оверлей отправлен пример субтитров")

    def apply_settings(self) -> None:
        """Сохранить всё, что в окне, и применить на лету."""
        cfg = self.config
        cfg.llm.base_url = (self.base_url.value or "").strip()
        cfg.llm.api_key = (self.api_key.value or "").strip()
        cfg.llm.model = (self.model.value or "").strip()
        cfg.llm.json_mode = self.json_mode.value
        cfg.llm.max_concurrency = max(1, _int_or(self.concurrency.value, cfg.llm.max_concurrency))
        cfg.llm.max_requests_per_minute = max(1, _int_or(self.rpm.value,
                                                         cfg.llm.max_requests_per_minute))
        cfg.llm.price_in_per_mtok = _float_or(self.price_in.value, cfg.llm.price_in_per_mtok)
        cfg.llm.price_out_per_mtok = _float_or(self.price_out.value, cfg.llm.price_out_per_mtok)

        cfg.translation.enabled = self.tr_enabled.value
        cfg.translation.skip_cyrillic = self.skip_cyrillic.value
        cfg.translation.skip_commands = self.skip_commands.value
        cfg.translation.target_lang = (self.target_lang.value or "ru").strip() or "ru"

        cfg.twitch.nickname = (self.nickname.value or "").strip()
        cfg.twitch.oauth_token = (self.oauth_token.value or "").strip()
        cfg.twitch.client_id = (self.client_id.value or "").strip()

        new_family = resolve_family((self.font_choice.value or "").strip())
        new_size = max(cfg.ui.MIN_FONT_SIZE,
                       min(cfg.ui.MAX_FONT_SIZE,
                           int(self.font_slider.value or cfg.ui.clamped_size())))
        font_changed = (cfg.ui.font_family != new_family) or (cfg.ui.clamped_size() != new_size)
        cfg.ui.font_family = "" if new_family.casefold() in {"", "по умолчанию", "default"} \
            else new_family
        cfg.ui.font_size = new_size
        cfg.ui.render_emotes = self.render_emotes.value

        emotes_changed = (cfg.emotes.bttv != self.emote_bttv.value
                          or cfg.emotes.ffz != self.emote_ffz.value
                          or cfg.emotes.seventv != self.emote_7tv.value)
        cfg.emotes.bttv = self.emote_bttv.value
        cfg.emotes.ffz = self.emote_ffz.value
        cfg.emotes.seventv = self.emote_7tv.value
        cfg.emotes.cache_ttl_hours = max(1, _int_or(self.emote_ttl.value,
                                                    cfg.emotes.cache_ttl_hours))

        cfg.viewers.enabled = self.viewers_enabled.value
        cfg.viewers.idle_minutes = max(1, _int_or(self.viewers_idle.value,
                                                  cfg.viewers.idle_minutes))
        cfg.viewers.max_users = max(10, _int_or(self.viewers_max.value, cfg.viewers.max_users))
        cfg.viewers.poll_seconds = max(20, _int_or(self.viewers_poll.value,
                                                   cfg.viewers.poll_seconds))
        cfg.viewers.fetch_chatters = self.viewers_fetch.value

        stt = cfg.stt
        stt_before = (stt.enabled, stt.model, stt.device, stt.compute_type, stt.language,
                      stt.input_device, stt.chunk_seconds, stt.silence_ms, stt.translate_to,
                      stt.vad, stt.vad_engine, stt.input_gain, stt.subtitle_mode)
        stt.enabled = self.stt_enabled.value
        stt.model = str(self.stt_model.value or stt.model)
        stt.device = str(self.stt_device.value or stt.device)
        stt.compute_type = str(self.stt_compute.value or stt.compute_type)
        stt.language = str(self.stt_lang.value or stt.language)
        stt.beam_size = max(1, min(5, _int_or(self.stt_beam.value, stt.beam_size)))
        stt.chunk_seconds = max(STTConfig.MIN_CHUNK, min(STTConfig.MAX_CHUNK,
                                _float_or(self.stt_chunk.value, stt.clamped_chunk())))
        stt.silence_ms = max(100, min(3000, _int_or(self.stt_silence.value, stt.silence_ms)))
        stt.vad = self.stt_vad.value
        stt.vad_engine = str(self.stt_vad_engine.value or stt.vad_engine)
        stt.min_speech_ratio = max(0.0, min(0.9, _float_or(self.stt_ratio.value,
                                     stt.clamped_ratio())))
        stt.max_no_speech_prob = max(0.0, min(1.0, _float_or(self.stt_no_speech.value,
                                       stt.clamped_no_speech())))
        stt.min_avg_logprob = max(-4.0, min(0.0, _float_or(self.stt_logprob.value,
                                     stt.clamped_logprob())))
        stt.input_device = (self.stt_mic.value or "").strip()
        stt.input_gain = max(1.0, min(8.0, _float_or(self.stt_gain.value, stt.clamped_gain())))
        stt.translate_to = (self.stt_translate.value or "").strip()
        stt.subtitle_mode = str(self.stt_mode.value or stt.clamped_mode())
        if (stt.translate_to[:2].casefold() == stt.language[:2].casefold()
                if stt.translate_to else False):
            self.log(f"перевод субтитров: цель «{stt.translate_to}» совпадает с языком "
                     f"речи — показать перевод не получится")
        stt.font_size = max(STTConfig.MIN_FONT_SIZE, min(STTConfig.MAX_FONT_SIZE,
                           _int_or(self.stt_font.value, stt.clamped_font())))
        stt.lines = max(STTConfig.MIN_LINES, min(STTConfig.MAX_LINES,
                        _int_or(self.stt_lines.value, stt.clamped_lines())))
        stt.max_chars = max(20, min(200, _int_or(self.stt_max_chars.value, stt.max_chars)))
        stt.hold_seconds = max(STTConfig.MIN_HOLD, min(STTConfig.MAX_HOLD,
                              _float_or(self.stt_hold.value, stt.clamped_hold())))
        stt.position = str(self.stt_position.value or stt.position)
        stt_after = (stt.enabled, stt.model, stt.device, stt.compute_type, stt.language,
                     stt.input_device, stt.chunk_seconds, stt.silence_ms, stt.translate_to,
                     stt.vad, stt.vad_engine, stt.input_gain, stt.subtitle_mode)
        stt_changed = stt_before != stt_after

        new_host = (self.dock_host.value or "").strip() or "127.0.0.1"
        new_port = max(1024, min(65535, _int_or(self.dock_port.value, cfg.dock.port)))
        new_token = (self.dock_token.value or "").strip()
        dock_changed = (cfg.dock.enabled != self.dock_enabled.value
                        or cfg.dock.host != new_host
                        or cfg.dock.port != new_port
                        or cfg.dock.token != new_token)
        cfg.dock.enabled = self.dock_enabled.value
        cfg.dock.host = new_host
        cfg.dock.port = new_port
        cfg.dock.token = new_token
        cfg.dock.history = max(10, min(500, _int_or(self.dock_history.value, cfg.dock.history)))
        cfg.dock.only_translated = self.dock_only.value

        path = cfg.save(redact=self.config_save_redacts)
        self.translator.configure(cfg)
        self.client.config = cfg.twitch
        if emotes_changed:
            self._reload_emotes()
        if font_changed and self.dock.running:
            # док-панель подхватывает шрифт без перезагрузки страницы
            self.dock.publish({"type": "settings", "font": cfg.ui.font_family,
                               "size": cfg.ui.clamped_size()})
        if dock_changed:
            if cfg.dock.enabled:
                self.page.run_task(self.restart_dock)
            else:
                self.page.run_task(self.stop_dock)
        if cfg.viewers.enabled and self._poller is None:
            self._poller = asyncio.ensure_future(self.chatters.poll_loop(self.known_rooms))
        elif not cfg.viewers.enabled and self._poller is not None:
            self._poller.cancel()
            self._poller = None
        if self.dock.running:
            # оверлей субтитров подхватывает размер, позицию и число строк на лету
            self.dock.publish({"type": "settings", "subs": self.dock.subs_settings()})
        if stt_changed:
            if cfg.stt.enabled:
                self.page.run_task(self.restart_stt)
            else:
                # выключили — весы модели больше не нужны, пусть занимают 0 ГБ VRAM
                self.page.run_task(self.unload_stt_model)

        self.warning.visible = not cfg.is_translation_ready()
        self._refresh_stats()
        self._refresh_commands()
        self._refresh_timers()
        self._refresh_subs_preview()
        self._refresh_stt_status()
        if self.stream.enabled() and self._stream_task is None:
            self._stream_task = asyncio.ensure_future(self.stream.poll_loop(self.known_channels))
        self.log(f"настройки сохранены: {path} · команд: {len(cfg.commands)}, "
                 f"таймеров: {len(cfg.timers)}")
        if font_changed:
            self.log(f"шрифт дока: {cfg.ui.font_family or 'по умолчанию'}, "
                     f"{cfg.ui.clamped_size()} px")
        if stt.enabled:
            self.log(f"субтитры: {stt.model} · {stt.language} · "
                     f"{stt.clamped_chunk():.1f} c (модуль ещё не подключён)")
        self.page.update()

    # ------------------------------------------------------------ команды

    def _refresh_commands(self) -> None:
        rows: list[ft.Control] = [self._command_row(index, command)
                                  for index, command in enumerate(self.config.commands)]
        if not rows:
            rows.append(ft.Text("команд пока нет — добавьте первую ниже", size=10, color=MUTED))
        self.commands_list.controls = rows

    def _command_row(self, index: int, command: CommandConfig) -> ft.Control:
        flags: list[str] = []
        if not command.enabled:
            flags.append("выключена")
        if command.permission != "everyone":
            flags.append(PERMISSION_TITLES.get(command.permission, command.permission))
        if command.cooldown:
            flags.append(f"{command.cooldown} с")
        count = self.commands.counts.get(self.commands.key_of(command), 0)
        if count:
            flags.append(f"×{count}")
        return ft.Container(
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(command.title, size=12, weight=ft.FontWeight.BOLD, color=TEXT),
                            ft.Text(command.response[:100], size=10, color=MUTED, max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS),
                        ],
                        spacing=1,
                        expand=True,
                    ),
                    ft.Text(" · ".join(flags), size=10, color=MUTED),
                    ft.IconButton(icon=ft.Icons.EDIT, icon_size=16, tooltip="Изменить",
                                  on_click=lambda _e, i=index: self.edit_command(i)),
                    ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, icon_size=16, tooltip="Удалить",
                                  on_click=lambda _e, i=index: self.delete_command(i)),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=PANEL_ALT,
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
        )

    def edit_command(self, index: int) -> None:
        if not 0 <= index < len(self.config.commands):
            return
        command = self.config.commands[index]
        self._editing_command = index
        self.cmd_triggers.value = ", ".join(command.triggers)
        self.cmd_response.value = command.response
        self.cmd_permission.value = (command.permission if command.permission in PERMISSION_TITLES
                                     else "everyone")
        self.cmd_cooldown.value = str(command.cooldown)
        self.cmd_enabled.value = command.enabled
        self.cmd_in_chat.value = command.reply_in_chat
        self.cmd_in_dock.value = command.show_in_dock
        self.cmd_save_button.content = "Сохранить команду"
        self.cmd_preview.value = ""
        self.page.update()

    def delete_command(self, index: int) -> None:
        if not 0 <= index < len(self.config.commands):
            return
        removed = self.config.commands.pop(index)
        self.clear_command_form()
        self._refresh_commands()
        self.log(f"команда {removed.title} удалена (не забудьте сохранить настройки)")
        self.page.update()

    def save_command(self) -> None:
        raw = (self.cmd_triggers.value or "").replace(";", ",")
        triggers = [item.strip() for item in raw.split(",") if item.strip()]
        response = (self.cmd_response.value or "").strip()
        if not triggers or not response:
            self.cmd_preview.value = "нужен хотя бы один триггер и текст ответа"
            self.cmd_preview.color = ERR_COLOR
            self.page.update()
            return
        command = CommandConfig(
            triggers=triggers,
            response=response,
            permission=self.cmd_permission.value or "everyone",
            cooldown=max(0, _int_or(self.cmd_cooldown.value, 10)),
            enabled=self.cmd_enabled.value,
            reply_in_chat=self.cmd_in_chat.value,
            show_in_dock=self.cmd_in_dock.value,
        )
        edited = self._editing_command is not None and 0 <= self._editing_command < len(self.config.commands)
        if edited:
            self.config.commands[self._editing_command] = command
        else:
            self.config.commands.append(command)
        self.clear_command_form()
        self._refresh_commands()
        self.cmd_preview.value = ("команда обновлена" if edited else "команда добавлена") + \
                                 " — нажмите «Сохранить настройки»"
        self.cmd_preview.color = OK_COLOR
        self.page.update()

    def clear_command_form(self) -> None:
        self._editing_command = None
        self.cmd_triggers.value = ""
        self.cmd_response.value = ""
        self.cmd_permission.value = "everyone"
        self.cmd_cooldown.value = "10"
        self.cmd_enabled.value = True
        self.cmd_in_chat.value = True
        self.cmd_in_dock.value = True
        self.cmd_save_button.content = "Добавить команду"
        self.cmd_preview.value = ""
        self.page.update()

    def preview_command(self) -> None:
        """Показать, как будет выглядеть ответ с подставленными переменными."""
        channels = self.config.twitch.normalized_channels()
        channel = channels[0] if channels else "канал"
        sample = ChatMessage(channel=channel, user="зритель", display_name="Зритель",
                             text="!команда привет мир")
        text = render_template(self.cmd_response.value or "", message=sample,
                               args=["привет", "мир"], count=1,
                               stream=self.stream.cached(channel), channel=channel).strip()
        self.cmd_preview.value = f"ответ: {text}" if text else "ответ пустой — проверьте шаблон"
        self.cmd_preview.color = OK_COLOR if text else ERR_COLOR
        self.page.update()

    # ------------------------------------------------------------ таймеры

    def _refresh_timers(self) -> None:
        rows: list[ft.Control] = [self._timer_row(index, timer)
                                  for index, timer in enumerate(self.config.timers)]
        if not rows:
            rows.append(ft.Text("таймеров пока нет — добавьте первый ниже", size=10, color=MUTED))
        self.timers_list.controls = rows

    def _timer_row(self, index: int, timer: TimerConfig) -> ft.Control:
        flags = [f"каждые {timer.interval_minutes} мин",
                 f"строк: {len(timer.lines)}"]
        if not timer.enabled:
            flags.append("выключен")
        if timer.only_when_active:
            flags.append(f"при {timer.min_messages}+ сообщ за {timer.active_window_minutes} мин")
        if timer.sent:
            flags.append(f"отправлен ×{timer.sent}")
        return ft.Container(
            content=ft.Row(
                [
                    ft.Column(
                        [
                            ft.Text(timer.title or "(пусто)", size=12,
                                    weight=ft.FontWeight.BOLD, color=TEXT),
                            ft.Text(" · ".join(flags), size=10, color=MUTED),
                        ],
                        spacing=1,
                        expand=True,
                    ),
                    ft.IconButton(icon=ft.Icons.EDIT, icon_size=16, tooltip="Изменить",
                                  on_click=lambda _e, i=index: self.edit_timer(i)),
                    ft.IconButton(icon=ft.Icons.DELETE_OUTLINE, icon_size=16, tooltip="Удалить",
                                  on_click=lambda _e, i=index: self.delete_timer(i)),
                ],
                spacing=4,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=PANEL_ALT,
            border_radius=8,
            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
        )

    def edit_timer(self, index: int) -> None:
        if not 0 <= index < len(self.config.timers):
            return
        timer = self.config.timers[index]
        self._editing_timer = index
        self.timer_interval.value = str(timer.interval_minutes)
        self.timer_lines.value = " | ".join(timer.lines)
        self.timer_enabled.value = timer.enabled
        self.timer_active.value = timer.only_when_active
        self.timer_window.value = str(timer.active_window_minutes)
        self.timer_min.value = str(timer.min_messages)
        self.timer_save_button.content = "Сохранить таймер"
        self.page.update()

    def delete_timer(self, index: int) -> None:
        if not 0 <= index < len(self.config.timers):
            return
        self.config.timers.pop(index)
        self.clear_timer_form()
        self._refresh_timers()
        self.log("таймер удалён (не забудьте сохранить настройки)")
        self.page.update()

    def save_timer(self) -> None:
        raw = self.timer_lines.value or ""
        lines = [item.strip() for item in raw.split("|") if item.strip()]
        if not lines:
            self.log("таймеру нужна хотя бы одна строка сообщения")
            return
        timer = TimerConfig(
            lines=lines,
            interval_minutes=max(1, _int_or(self.timer_interval.value, 15)),
            enabled=self.timer_enabled.value,
            only_when_active=self.timer_active.value,
            active_window_minutes=max(1, _int_or(self.timer_window.value, 5)),
            min_messages=max(1, _int_or(self.timer_min.value, 3)),
        )
        edited = self._editing_timer is not None and 0 <= self._editing_timer < len(self.config.timers)
        if edited:
            self.config.timers[self._editing_timer] = timer
        else:
            self.config.timers.append(timer)
        self.clear_timer_form()
        self._refresh_timers()
        self.log(("таймер обновлён" if edited else "таймер добавлен")
                 + " — нажмите «Сохранить настройки»")
        self.page.update()

    def clear_timer_form(self) -> None:
        self._editing_timer = None
        self.timer_interval.value = "15"
        self.timer_lines.value = ""
        self.timer_enabled.value = True
        self.timer_active.value = True
        self.timer_window.value = "5"
        self.timer_min.value = "3"
        self.timer_save_button.content = "Добавить таймер"
        self.page.update()

    def _reload_emotes(self) -> None:
        self.emotes.reset()
        self.page.run_task(self.emotes.load_globals)
        for room_id, channel in self._rooms.items():
            self.page.run_task(self.emotes.load_channel, room_id, channel)
        self.log("смайлы перезагружаются…")
        self._refresh_stats()
        self.page.update()


def _int_or(value: str | None, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _float_or(value: str | None, fallback: float) -> float:
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return fallback


def main(page: ft.Page) -> None:
    page.title = "Govorok — настройки"
    page.theme_mode = ft.ThemeMode.DARK
    page.theme = ft.Theme(color_scheme_seed=ACCENT_DEEP)
    try:
        page.window.width = 1000
        page.window.height = 860
        page.window.min_width = 760
        page.window.min_height = 560
    except Exception:  # noqa: BLE001 - старые сборки Flet
        pass

    app = BotApp(page)
    app.mount()
    page.on_close = lambda _e: page.run_task(app.shutdown)


async def run_dock_only(channels: list[str], port: int | None) -> int:
    """Режим без окна: чат и перевод уходят только в док-панель OBS."""
    config = AppConfig.load()
    if channels:
        config.twitch.channels = channels
    if port:
        config.dock.port = port
    config.dock.enabled = True

    runner = DockRunner(config)
    if not await runner.start():
        print(f"Не удалось занять {config.dock.host}:{config.dock.port}. "
              f"Закройте другую копию бота или смените порт (--port N).")
        await runner.stop()
        return 1

    print("Govorok работает без окна: чат и перевод идут только в док-панель OBS.")
    print("Ссылка для OBS (Виды → Доки → Пользовательские доки браузера):")
    print(f"  {runner.hub.url()}")
    print("Остановка — Ctrl+C")
    try:
        await runner.run_forever()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        await runner.stop()
    return 0


def parse_cli(argv: list[str]) -> tuple[bool, list[str], int | None]:
    """Разобрать аргументы: --dock/--no-window, --channel/-c, --port."""
    dock_only = any(arg in {"--dock", "--no-window", "--headless"} for arg in argv)
    channels: list[str] = []
    port: int | None = None
    for index, arg in enumerate(argv):
        nxt = argv[index + 1] if index + 1 < len(argv) else ""
        if arg in {"--channel", "-c", "--channels"} and nxt and not nxt.startswith("-"):
            channels = [nxt]
        elif arg == "--port" and nxt.isdigit():
            port = int(nxt)
    return dock_only, channels, port


USAGE = """Govorok — перевод чата Twitch и субтитры из микрофона, док-панелью в OBS.

Запуск из исходников (нужен Python) или из portable-пакета (python.exe внутри):

  Govorok.cmd                          окно настроек + бот + док-панель
  Govorok.cmd dock                     без окна: только бот и док (для автозапуска)
  Govorok.cmd dock -c xqc --port 9000   то же, но со своим каналом и портом
  Govorok.cmd test                      автотесты (сеть и ключи не нужны)
  Govorok.cmd check                     диагностика микрофона и субтитров
  Govorok.cmd models                    скачать веса faster-whisper заранее

Напрямую через python то же самое:
  python main.py                         окно настроек
  python main.py --dock                  без окна

Аргументы main.py:
  -c, --channel <канал>   канал (можно несколько через запятую)
      --port <номер>      порт док-панели (0 — любой свободный)
      --dock              не открывать окно настроек
  -h, --help              эта справка
"""


if __name__ == "__main__":
    if any(arg in {"-h", "--help"} for arg in sys.argv[1:]):
        print(USAGE)
        raise SystemExit(0)
    dock_only, cli_channels, cli_port = parse_cli(sys.argv[1:])
    if dock_only:
        try:
            raise SystemExit(asyncio.run(run_dock_only(cli_channels, cli_port)))
        except KeyboardInterrupt:
            raise SystemExit(0)
    ft.run(main, name="Govorok")
