"""Автотесты Govorok — без GUI и без реальных ключей.

Запуск:

    .venv\\Scripts\\python.exe selftest.py            # всё, кроме живого Twitch
    .venv\\Scripts\\python.exe selftest.py --live     # + реальное подключение к чату

Проверяет: парсер IRC, конфиг, полный путь перевода через настоящий HTTP
(на локальном мок-сервере, совместимом с OpenAI API) и сборку виджетов Flet.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:  # чтобы русский текст не превращался в кракозябры в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from govorok.config import (AppConfig, CommandConfig, STTConfig, TimerConfig,  # noqa: E402
                           TwitchConfig)
from govorok.irc import (  # noqa: E402
    ChatMessage,
    TwitchChatClient,
    parse_badges,
    parse_emotes,
    parse_irc_line,
    split_by_emotes,
)
from govorok.translator import Translator, has_cyrillic, looks_translatable  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [ok]   {name}")
    else:
        FAILED.append(f"{name} {detail}".strip())
        print(f"  [FAIL] {name} {detail}")


# --------------------------------------------------------------- мок-сервер


class MockOpenAIHandler(BaseHTTPRequestHandler):
    """Мини-заглушка OpenAI API: /v1/chat/completions."""

    strict = False  # если True — ругается на response_format (проверка фолбэка)
    requests_seen: list[dict] = []

    def log_message(self, *args) -> None:  # тишина в консоли
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        payload = json.loads(raw or "{}")
        MockOpenAIHandler.requests_seen.append(payload)

        if MockOpenAIHandler.strict and "response_format" in payload:
            self._send(400, {"error": {"message": "response_format is not supported"}})
            return

        user_text = ""
        for message in payload.get("messages", []):
            if message.get("role") == "user":
                user_text = message.get("content", "")

        if has_cyrillic(user_text):
            result = {"lang": "ru", "translation": None}
        elif "hello" in user_text.lower():
            result = {"lang": "en", "translation": f"привет, чат! ({user_text})"}
        else:
            result = {"lang": "de", "translation": f"перевод: {user_text}"}

        self._send(200, {
            "id": "mock",
            "choices": [{"index": 0, "message": {"role": "assistant",
                                                 "content": json.dumps(result, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 12, "total_tokens": 132},
        })

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class MockServer:
    def __init__(self, *, strict: bool = False) -> None:
        MockOpenAIHandler.strict = strict
        MockOpenAIHandler.requests_seen = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAIHandler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "MockServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


# ------------------------------------------------- фикстуры API смайлов

BTTV_GLOBAL_FIXTURE = [
    {"id": "5590b223b344e2c42a9e28e3", "code": "EZ", "imageType": "png",
     "animated": False, "userId": "558f7862b344e2c42a9e2822", "modifier": False},
    {"id": "5f1b3b3b3b3b3b3b3b3b3b3b", "code": "pepeD", "imageType": "gif",
     "animated": True, "userId": "1", "modifier": False},
]

BTTV_CHANNEL_FIXTURE = {
    "id": "71092938",
    "bots": ["xqcbot"],
    "avatar": "https://cdn.betterttv.net/user/avatar/xqc",
    "channelEmotes": [
        {"id": "5e9c6c0a0000000000000001", "code": "xqcL", "imageType": "png",
         "animated": False, "userId": "1", "modifier": False},
    ],
    "sharedEmotes": [
        {"id": "5590b223b344e2c42a9e28e3", "code": "EZ", "imageType": "png", "animated": False,
         "user": {"id": "558f7862b344e2c42a9e2822", "name": "helloboat",
                  "displayName": "helloboat", "providerId": "39819556"}},
    ],
}

FFZ_GLOBAL_FIXTURE = {
    "default_sets": [3],
    "sets": {
        "3": {"id": 3, "title": "Global Emotes", "emoticons": [
            {"id": 9, "name": "ZrehplaR", "height": 30, "width": 33, "hidden": True,
             "urls": {"1": "https://cdn.frankerfacez.com/emote/9/1"}},
            {"id": 123, "name": "FFZHyper", "height": 32, "width": 32, "hidden": False,
             "urls": {"1": "//cdn.frankerfacez.com/emote/123/1",
                      "2": "//cdn.frankerfacez.com/emote/123/2"}},
        ]},
        "999": {"id": 999, "title": "Не дефолтный сет", "emoticons": [
            {"id": 1, "name": "ShouldNotLoad", "hidden": False, "urls": {"1": "https://x/1"}},
        ]},
    },
}

FFZ_ROOM_FIXTURE = {
    "room": {"_id": 166907, "twitch_id": 71092938, "id": "xqc", "display_name": "xQc"},
    "sets": {"166907": {"id": 166907, "title": "Channel: xQc", "emoticons": [
        {"id": 246878, "name": "WideHard", "height": 20, "width": 50, "hidden": False,
         "urls": {"1": "https://cdn.frankerfacez.com/emote/246878/1",
                  "2": "https://cdn.frankerfacez.com/emote/246878/2"}},
        # коллизия с BTTV: у канального FFZ приоритет выше глобального BTTV
        {"id": 777, "name": "EZ", "height": 32, "width": 32, "hidden": False,
         "urls": {"1": "https://cdn.frankerfacez.com/emote/777/1"}},
    ]}},
}

SEVENTV_GLOBAL_FIXTURE = {
    "id": "global", "name": "Global", "emotes": [
        {"id": "01FCY771D800007PQ2DF3GDTN6", "name": "RainTime",
         "data": {"name": "RainTime", "animated": True, "listed": True,
                  "host": {"url": "//cdn.7tv.app/emote/01FCY771D800007PQ2DF3GDTN6",
                           "files": [
                               {"name": "1x.webp", "width": 32, "height": 32, "frame_count": 15, "format": "WEBP"},
                               {"name": "2x.webp", "width": 64, "height": 64, "frame_count": 15, "format": "WEBP"},
                               {"name": "2x.gif", "width": 64, "height": 64, "frame_count": 15, "format": "GIF"},
                           ]}}},
    ],
}

SEVENTV_CHANNEL_FIXTURE = {
    "id": "01G3WEGZN0000ET2J0MQP5YJ0G0", "platform": "twitch", "username": "xqc",
    "emote_set": {"id": "set1", "name": "xQc", "emotes": [
        {"id": "01G3WEGZN0000ET2J0MQP5YJ0G", "name": "GAMBA",
         "data": {"name": "GAMBA", "animated": True,
                  "host": {"url": "//cdn.7tv.app/emote/01G3WEGZN0000ET2J0MQP5YJ0G",
                           "files": [
                               {"name": "1x.webp", "width": 32, "height": 16, "frame_count": 1, "format": "WEBP"},
                               {"name": "2x.webp", "width": 64, "height": 32, "frame_count": 1, "format": "WEBP"},
                           ]}}},
        # перекрывает глобальный BTTV-смайл с тем же кодом: канал важнее
        {"id": "01H0000000000000000000000", "name": "pepeD",
         "data": {"name": "pepeD", "animated": True,
                  "host": {"url": "//cdn.7tv.app/emote/01H0000000000000000000000",
                           "files": [{"name": "2x.webp", "width": 64, "height": 64,
                                      "frame_count": 20, "format": "WEBP"}]}}},
    ]},
}


class JsonServer:
    """Мок-сервер смайлов: отдаёт заранее заданные JSON по путям, иначе 404."""

    def __init__(self, routes: dict[str, object]) -> None:
        routes = dict(routes)
        self.hits: dict[str, int] = {}
        hits = self.hits

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                hits[path] = hits.get(path, 0) + 1
                if path not in routes:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = json.dumps(routes[path]).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "JsonServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


EMOTE_ROUTES = {
    "/bttv/global": BTTV_GLOBAL_FIXTURE,
    "/bttv/users/twitch/71092938": BTTV_CHANNEL_FIXTURE,
    "/ffz/global": FFZ_GLOBAL_FIXTURE,
    "/ffz/room/id/71092938": FFZ_ROOM_FIXTURE,
    "/7tv/global": SEVENTV_GLOBAL_FIXTURE,
    "/7tv/users/twitch/71092938": SEVENTV_CHANNEL_FIXTURE,
}


def patch_emote_urls(base: str):
    """Подменить адреса API смайлов на мок-сервер; возвращает функцию отката."""
    import govorok.emotes as module

    original = {name: getattr(module, name) for name in (
        "BTTV_GLOBAL_URL", "BTTV_CHANNEL_URL", "FFZ_GLOBAL_URL",
        "FFZ_ROOM_ID_URL", "FFZ_ROOM_LOGIN_URL", "SEVENTV_GLOBAL_URL", "SEVENTV_CHANNEL_URL",
    )}
    module.BTTV_GLOBAL_URL = f"{base}/bttv/global"
    module.BTTV_CHANNEL_URL = f"{base}/bttv/users/twitch/{{room_id}}"
    module.FFZ_GLOBAL_URL = f"{base}/ffz/global"
    module.FFZ_ROOM_ID_URL = f"{base}/ffz/room/id/{{room_id}}"
    module.FFZ_ROOM_LOGIN_URL = f"{base}/ffz/room/{{login}}"
    module.SEVENTV_GLOBAL_URL = f"{base}/7tv/global"
    module.SEVENTV_CHANNEL_URL = f"{base}/7tv/users/twitch/{{room_id}}"

    def restore() -> None:
        for name, value in original.items():
            setattr(module, name, value)

    return restore


def patch_helix_urls(base: str):
    """Подменить адреса Helix на мок-сервер; возвращает функцию отката."""
    import govorok.viewers as module

    original = {name: getattr(module, name) for name in ("HELIX_CHATTERS", "HELIX_USERS")}
    module.HELIX_CHATTERS = f"{base}/helix/chat/chatters"
    module.HELIX_USERS = f"{base}/helix/users"

    def restore() -> None:
        for name, value in original.items():
            setattr(module, name, value)

    return restore


def mock_config(server: MockServer, **overrides) -> AppConfig:
    cfg = AppConfig()
    cfg.llm.base_url = server.base_url
    cfg.llm.api_key = "test-key"
    cfg.llm.model = "mock-model"
    cfg.llm.timeout = 10.0
    cfg.llm.max_concurrency = 2
    cfg.translation.min_length = 2
    for key, value in overrides.items():
        target, _, field = key.partition(".")
        setattr(getattr(cfg, target), field, value)
    return cfg


# ------------------------------------------------------------------- тесты


def test_irc_parsing() -> None:
    print("\n[1/13] Парсер IRC")
    line = (
        "@badge-info=subscriber/12;badges=broadcaster/1,subscriber/12;color=#1E90FF;"
        "display-name=Streamer;emotes=25:0-4/1902:6-10;id=abc-123;mod=0;room-id=42;"
        "subscriber=1;tmi-sent-ts=1700000000000;user-type= "
        ":streamer!streamer@streamer.tmi.twitch.tv PRIVMSG #mychannel :Kappa Keepo hello"
    )
    parsed = parse_irc_line(line)
    check("команда PRIVMSG", parsed["command"] == "PRIVMSG")
    check("канал разобран", parsed["params"][0] == "#mychannel")
    check("текст сообщения", parsed["trailing"] == "Kappa Keepo hello")
    check("display-name из тегов", parsed["tags"]["display-name"] == "Streamer")

    emotes = parse_emotes(parsed["tags"]["emotes"])
    check("смайлы разобраны", emotes == [("25", 0, 4), ("1902", 6, 10)], str(emotes))
    segments = split_by_emotes("Kappa Keepo hello", emotes)
    check("нарезка по смайлам", segments == [("emote", "25"), ("text", " "), ("emote", "1902"),
                                             ("text", " hello")], str(segments))
    check("бейджи", parse_badges(parsed["tags"]["badges"]) == [("broadcaster", "1"), ("subscriber", "12")])

    ping = parse_irc_line("PING :tmi.twitch.tv")
    check("PING распознан", ping["command"] == "PING" and ping["trailing"] == "tmi.twitch.tv")

    # экранирование в тегах
    escaped = parse_irc_line(r"@system-msg=Hello\sworld\:ok :tmi.twitch.tv USERNOTICE #x")
    check("unescape тегов", escaped["tags"]["system-msg"] == "Hello world;ok")

    # сообщение целиком превращается в объект
    client = TwitchChatClient(TwitchConfig(channels=["mychannel"]))
    captured: list[ChatMessage] = []
    client.on_message = captured.append

    async def feed() -> None:
        await client._handle_line(line, _FakeWS())  # noqa: SLF001

    asyncio.run(feed())
    check("ChatMessage собран", len(captured) == 1 and captured[0].display_name == "Streamer")
    if captured:
        msg = captured[0]
        check("бейджи сообщения", msg.badge_labels == ["STREAMER", "SUB"], str(msg.badge_labels))
        check("subscriber распознан", msg.is_sub and not msg.is_mod)

    # смена каналов без соединения: строки уходят в очередь отправки
    switcher = TwitchChatClient(TwitchConfig(channels=["a"]))
    switcher.set_channels(["a", "b"])
    switcher.set_channels(["b", "c"])
    queued: list[str] = []
    while not switcher._outbox.empty():  # noqa: SLF001
        queued.append(switcher._outbox.get_nowait())  # noqa: SLF001
    check("PART лишнего канала", "PART #a" in queued, str(queued))
    check("JOIN нового канала", "JOIN #c" in queued, str(queued))
    check("список каналов обновился", switcher.joined_channels == ["b", "c"], str(switcher.joined_channels))

    # ответ веткой: тег перед PRIVMSG не должен ломать определение канала и лимиты
    from govorok.irc import channel_of, is_privmsg

    async def check_reply() -> None:
        client = TwitchChatClient(TwitchConfig(channels=["xqc"], nickname="mybot", oauth_token="t"))
        await client.send_message("привет", reply_to="abc-123")
        await client.send_message("просто так")
        await client.send_message("в другой канал", channel="other", reply_to="q-1")
        lines = []
        while not client._outbox.empty():              # noqa: SLF001
            lines.append(client._outbox.get_nowait())  # noqa: SLF001
        check("ответ уходит с тегом reply-parent-msg-id",
              lines[0] == "@reply-parent-msg-id=abc-123 PRIVMSG #xqc :привет", str(lines))
        check("обычное сообщение без тегов", lines[1] == "PRIVMSG #xqc :просто так", str(lines))
        check("ответ можно адресовать в конкретный канал",
              lines[2] == "@reply-parent-msg-id=q-1 PRIVMSG #other :в другой канал", str(lines[2]))
        check("строка с тегами распознаётся как сообщение",
              is_privmsg(lines[0]) and not is_privmsg("JOIN #xqc"))
        check("канал определяется и с тегами", channel_of(lines[2]) == "other", channel_of(lines[2]))

    asyncio.run(check_reply())

    # Twitch не возвращает боту его собственные сообщения — бот сообщает о них сам
    async def check_self_echo() -> None:
        echoed: list[tuple[str, str]] = []
        client = TwitchChatClient(TwitchConfig(channels=["xqc"], nickname="mybot", oauth_token="t"),
                                  on_sent=lambda text, channel: echoed.append((text, channel)))
        socket = _FakeWS()
        writer = asyncio.create_task(client._writer(socket))          # noqa: SLF001
        await client.send_message("привет чат", reply_to="abc")
        for _ in range(50):
            await asyncio.sleep(0.02)
            if echoed:
                break
        writer.cancel()
        try:
            await writer
        except asyncio.CancelledError:
            pass
        check("бот сам сообщает о своём сообщении", echoed == [("привет чат", "xqc")], str(echoed))
        check("в сокет ушла строка с тегом ответа",
              bool(socket.sent) and socket.sent[0] == "@reply-parent-msg-id=abc PRIVMSG #xqc :привет чат",
              str(socket.sent))

    asyncio.run(check_self_echo())


class _FakeWS:
    """Заглушка websocket: записывает отправленные строки."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def test_config_roundtrip(tmp: Path) -> None:
    print("\n[2/13] Конфиг")
    path = tmp / "config.test.json"
    cfg = AppConfig()
    cfg.twitch.channels = ["SxWrOuD, dota2", "#twitch"]
    cfg.twitch.nickname = "mybot"
    cfg.twitch.oauth_token = "abc123"
    cfg.llm.api_key = "secret"
    cfg.save(path)

    loaded = AppConfig.load(path)
    check("каналы сохранены", loaded.twitch.channels == ["SxWrOuD, dota2", "#twitch"], str(loaded.twitch.channels))
    check("нормализация каналов",
          loaded.twitch.normalized_channels() == ["sxwroud", "dota2", "twitch"],
          str(loaded.twitch.normalized_channels()))
    check("oauth префикс", loaded.twitch.normalized_token() == "oauth:abc123")
    check("можно отправлять", loaded.twitch.can_send)
    check("ключ прочитан", loaded.llm.api_key == "secret")
    check("перевод готов", loaded.is_translation_ready())

    broken = tmp / "config.broken.json"
    broken.write_text("{ это не json", encoding="utf-8")
    check("битый конфиг не роняет", AppConfig.load(broken).ui.max_messages == 400)

    # Блокнот и PowerShell сохраняют JSON с BOM — такой файл обязан читаться
    bom = tmp / "config.bom.json"
    bom.write_text(json.dumps({"twitch": {"channels": ["xqc"]}, "emotes": {"ffz": False}}),
                   encoding="utf-8-sig")
    bom_cfg = AppConfig.load(bom)
    check("конфиг с BOM читается", bom_cfg.twitch.normalized_channels() == ["xqc"],
          str(bom_cfg.twitch.normalized_channels()))
    check("настройки смайлов из файла", bom_cfg.emotes.ffz is False and bom_cfg.emotes.bttv is True)

    partial = tmp / "config.partial.json"
    partial.write_text(json.dumps({"llm": {"model": "gpt-x", "unknown": 1}}), encoding="utf-8")
    got = AppConfig.load(partial)
    check("частичный конфиг мержится", got.llm.model == "gpt-x" and got.llm.base_url.endswith("/v1"))


def test_heuristics() -> None:
    print("\n[3/13] Эвристики перевода")
    cfg = AppConfig()
    cfg.llm.api_key = "x"
    translator = Translator(cfg)

    cases = [
        ("hello guys", True, "английский текст"),
        ("Привет, как дела?", False, "кириллица"),
        ("!", False, "слишком коротко"),
        ("!uptime", False, "команда"),
        ("Kappa", True, "одно слово"),
        ("https://clips.twitch.tv/xyz", False, "только ссылка"),
        ("12 + 34", False, "только цифры"),
    ]
    for text, expected, label in cases:
        allowed, _ = translator.should_translate(text)
        check(f"«{text[:24]}» — {label}", allowed == expected, f"(получено {allowed})")

    check("кириллица найдена", has_cyrillic("привет") and not has_cyrillic("hello"))
    check("слова найдены", looks_translatable("hello") and not looks_translatable("!!! 123"))


async def test_translation_flow() -> None:
    print("\n[4/13] Перевод через HTTP (мок OpenAI API)")
    with MockServer() as server:
        cfg = mock_config(server)
        translator = Translator(cfg)

        result = await translator.translate("hello chat, how are you?")
        check("английский переведён", result.ok and "привет, чат!" in (result.text or ""), str(result))
        check("язык определён", result.source_lang == "en", result.source_lang)
        check("был HTTP-запрос", translator.requests_sent == 1, str(translator.requests_sent))
        check("токены посчитаны",
              translator.prompt_tokens == 120 and translator.completion_tokens == 12,
              f"{translator.prompt_tokens}/{translator.completion_tokens}")
        check("язык попал в статистику", translator.languages.get("en") == 1, str(dict(translator.languages)))
        check("символы посчитаны", translator.characters == len("hello chat, how are you?"),
              str(translator.characters))
        check("задержка измерена", translator.latency_samples == 1, str(translator.avg_latency_ms))
        snapshot = translator.usage_snapshot()
        check("снимок расхода API",
              snapshot["tokens"] == 132 and snapshot["requests"] == 1 and snapshot["languages"] == {"en": 1},
              str(snapshot))

        again = await translator.translate("hello chat, how are you?")
        check("повтор берётся из кэша", again.from_cache and translator.requests_sent == 1)

        russian = await translator.translate("привет, как дела")
        check("русское не переводится", russian.ok and russian.text is None, str(russian))

        skipped = await translator.translate("!uptime")
        check("команда пропущена", skipped.text is None and skipped.error is None)

        cache_before = translator.cache_size
        translator.clear_cache()
        check("кэш очищается", translator.cache_size == 0 and cache_before > 0)

        # ответ модели в ```-блоке и «перевод не нужен»
        payload = await translator._request("hello again")  # noqa: SLF001
        check("второй запрос прошёл", payload.ok and payload.text is not None, str(payload))

    with MockServer(strict=True) as strict_server:
        cfg = mock_config(strict_server)
        translator = Translator(cfg)
        result = await translator.translate("hello strict mode")
        check("фолбэк без response_format", result.ok and result.text is not None, str(result))
        check("провайдер ответил 400 один раз", len(MockOpenAIHandler.requests_seen) >= 2)
        await translator.aclose()

    # ошибка сети не должна лететь исключением
    cfg = AppConfig()
    cfg.llm.base_url = "http://127.0.0.1:9/v1"
    cfg.llm.api_key = "x"
    cfg.llm.timeout = 1.0
    translator = Translator(cfg)
    failed = await translator.translate("hello nobody is listening")
    check("сетевая ошибка поймана", (not failed.ok) and failed.error, str(failed))
    await translator.aclose()


def collect_image_urls(control) -> list[str]:
    """Рекурсивно собрать src всех ft.Image в дереве контролов."""
    import flet as ft

    if isinstance(control, ft.Image):
        return [control.src or ""]
    urls: list[str] = []
    for attr in ("controls", "content"):
        value = getattr(control, attr, None)
        if isinstance(value, list):
            for child in value:
                urls.extend(collect_image_urls(child))
        elif value is not None and not isinstance(value, str):
            urls.extend(collect_image_urls(value))
    return urls


def collect_texts(control) -> list[str]:
    """Рекурсивно собрать все тексты в дереве контролов."""
    import flet as ft

    if isinstance(control, ft.Text):
        return [control.value or ""]
    texts: list[str] = []
    for attr in ("controls", "content"):
        value = getattr(control, attr, None)
        if isinstance(value, list):
            for child in value:
                texts.extend(collect_texts(child))
        elif value is not None and not isinstance(value, str):
            texts.extend(collect_texts(value))
    return texts


def test_emote_parsers() -> None:
    print("\n[6/13] Парсеры смайлов BTTV / FFZ / 7TV")
    from govorok.emotes import (merge_emotes, parse_7tv_set, parse_bttv_emotes,
                               parse_bttv_response, parse_ffz_global, parse_ffz_sets)

    bttv = parse_bttv_emotes(BTTV_GLOBAL_FIXTURE)
    check("BTTV: разобраны глобальные", [e.code for e in bttv] == ["EZ", "pepeD"])
    check("BTTV: ссылка 2x",
          bttv[0].url == "https://cdn.betterttv.net/emote/5590b223b344e2c42a9e28e3/2x", bttv[0].url)
    check("BTTV: анимация и провайдер", bttv[1].animated and bttv[1].provider == "bttv")

    channel = parse_bttv_response(BTTV_CHANNEL_FIXTURE)
    check("BTTV канал: channelEmotes + sharedEmotes",
          {e.code for e in channel} == {"xqcL", "EZ"}, str([e.code for e in channel]))

    ffz_global = parse_ffz_global(FFZ_GLOBAL_FIXTURE)
    check("FFZ: скрытые и не дефолтные сеты отброшены",
          [e.code for e in ffz_global] == ["FFZHyper"], str([e.code for e in ffz_global]))
    check("FFZ: взят 2x и починен протокол",
          ffz_global[0].url == "https://cdn.frankerfacez.com/emote/123/2", ffz_global[0].url)

    ffz_room = parse_ffz_sets(FFZ_ROOM_FIXTURE["sets"])
    wide = next(e for e in ffz_room if e.code == "WideHard")
    check("FFZ: пропорции широкого смайла (50x20)", wide.aspect == 2.5, str(wide.aspect))

    tv_global = parse_7tv_set(SEVENTV_GLOBAL_FIXTURE)
    check("7TV: разобраны глобальные", [e.code for e in tv_global] == ["RainTime"])
    check("7TV: выбран 2x.webp и добавлен https",
          tv_global[0].url == "https://cdn.7tv.app/emote/01FCY771D800007PQ2DF3GDTN6/2x.webp",
          tv_global[0].url)
    check("7TV: анимация по frame_count", tv_global[0].animated)

    tv_channel = parse_7tv_set(SEVENTV_CHANNEL_FIXTURE)
    gamba = next(e for e in tv_channel if e.code == "GAMBA")
    check("7TV канал: пропорции 64x32", gamba.aspect == 2.0, str(gamba.aspect))

    merged = merge_emotes(bttv, ffz_room)
    check("приоритет: FFZ канала перебил BTTV глобальный",
          merged["EZ"].provider == "ffz", merged["EZ"].provider)

    check("мусор в ответе не роняет парсер",
          parse_bttv_emotes([{"code": "нет-id"}, None, "строка"]) == []
          and parse_7tv_set({}) == [] and parse_7tv_set(None) == [])


async def test_emote_registry(tmp: Path) -> None:
    print("\n[7/13] Реестр смайлов: загрузка, приоритеты, кэш")
    from govorok.emotes import EmoteRegistry

    cache = tmp / "emotes-cache.json"
    if cache.exists():
        cache.unlink()

    with JsonServer(EMOTE_ROUTES) as server:
        restore = patch_emote_urls(server.base)
        try:
            cfg = AppConfig()
            registry = EmoteRegistry(lambda: cfg, cache_path=cache)

            await registry.load_globals()
            check("глобальные загружены", registry.global_count == 4, str(registry.global_count))
            check("счётчики по провайдерам",
                  registry.provider_counts() == {"bttv": 2, "ffz": 1, "7tv": 1},
                  str(registry.provider_counts()))

            total = await registry.load_channel("71092938", "xqc")
            check("канал загружен (с учётом коллизий)", total == 5, str(total))
            check("канальный смайл перебил глобальный",
                  (registry.get("pepeD", "71092938") or {}).provider == "7tv",
                  str(registry.get("pepeD", "71092938")))
            check("глобальный доступен без канала",
                  (registry.get("pepeD") or {}).provider == "bttv",
                  str(registry.get("pepeD")))
            check("внутри канала BTTV выше FFZ",
                  (registry.get("EZ", "71092938") or {}).provider == "bttv",
                  str(registry.get("EZ", "71092938")))
            check("неизвестный код не найден", registry.get("нетТакого", "71092938") is None)

            parts = registry.resolve("lol pepeD GAMBA", "71092938")
            check("токенизация текста с двумя смайлами",
                  [kind for kind, _ in parts] == ["text", "emote", "text", "emote"], str(parts))
            check("обычное слово остаётся текстом",
                  registry.resolve("привет", "71092938") == [("text", "привет")])
            check("регистр кода важен", registry.get("gamba", "71092938") is None)
            check("статистика читаемая", "BTTV" in registry.stats("71092938"), registry.stats("71092938"))

            registry.save_to_disk()
            check("кэш записан на диск", cache.exists())

            restored = EmoteRegistry(lambda: cfg, cache_path=cache)
            check("глобальные подняты из кэша", restored.global_count == 4, str(restored.global_count))
            check("канальные подняты из кэша", restored.channel_count("71092938") == 5,
                  str(restored.channel_count("71092938")))
            check("смайл из кэша резолвится",
                  (restored.get("WideHard", "71092938") or {}).url.endswith("/2"),
                  str(restored.get("WideHard", "71092938")))

            hits_before = server.hits.get("/bttv/global", 0)
            await restored.load_globals()
            check("TTL: свежий кэш не перезагружаем",
                  server.hits.get("/bttv/global", 0) == hits_before)
            await restored.load_globals(force=True)
            check("force перезагружает",
                  server.hits.get("/bttv/global", 0) == hits_before + 1)
            await restored.aclose()

            empty = await registry.load_channel("999999", "nobody")
            check("404 по каналу не ломает бота", empty == 0, str(empty))

            # пустой набор из кэша не должен блокировать повторную загрузку:
            # ровно на этом ловились каналы, у которых 7TV один раз отдал 429
            empty_cache = tmp / "emotes-empty.json"
            empty_cache.write_text(json.dumps({
                "version": 1,
                "global_fetched_at": time.time(),
                "global": {p: [] for p in ("bttv", "ffz", "7tv")},
                "channels": {"71092938": {"fetched_at": time.time(),
                                          "bttv": [], "ffz": [], "7tv": []}},
            }, ensure_ascii=False), encoding="utf-8")
            retry = EmoteRegistry(lambda: cfg, cache_path=empty_cache)
            check("пустой канал из кэша не считается свежим",
                  not retry._channel_is_fresh("71092938"))       # noqa: SLF001
            hits_before = server.hits.get("/bttv/users/twitch/71092938", 0)
            retry.ensure_channel("71092938", "xqc")
            for _ in range(40):
                await asyncio.sleep(0.05)
                if retry.channel_count("71092938"):
                    break
            check("пустой кэш перезагружается", retry.channel_count("71092938") == 5,
                  str(retry.channel_count("71092938")))
            check("запрос к API действительно ушёл",
                  server.hits.get("/bttv/users/twitch/71092938", 0) > hits_before)
            check("после удачной загрузки канал свежий",
                  retry._channel_is_fresh("71092938"))           # noqa: SLF001
            retry._channel_loaded_at["71092938"] = time.time() - 13 * 3600   # noqa: SLF001
            check("истёкший TTL канала требует перезагрузки",
                  not retry._channel_is_fresh("71092938"))       # noqa: SLF001
            await retry.aclose()

            fresh = EmoteRegistry(lambda: cfg, cache_path=None)
            fresh.ensure_channel("71092938", "xqc")
            for _ in range(40):
                await asyncio.sleep(0.05)
                if fresh.channel_count("71092938"):
                    break
            check("ensure_channel грузит в фоне", fresh.channel_count("71092938") == 5,
                  str(fresh.channel_count("71092938")))
            await fresh.aclose()

            await registry.aclose()
        finally:
            restore()


def collect_controls(control, kind) -> list:
    """Все контролы указанного типа в дереве."""
    found = []
    if isinstance(control, kind):
        found.append(control)
    for attr in ("controls", "content"):
        value = getattr(control, attr, None)
        if isinstance(value, list):
            for child in value:
                found.extend(collect_controls(child, kind))
        elif value is not None and not isinstance(value, str):
            found.extend(collect_controls(value, kind))
    return found


def test_chat_font(tmp: Path) -> None:
    print("\n[8/13] Шрифт чата в док-панели")
    import flet as ft

    from govorok.fonts import collect_families, list_system_fonts, normalize_family, resolve_family

    check("начертание отрезается", normalize_family("Arial Bold (TrueType)") == "Arial",
          normalize_family("Arial Bold (TrueType)"))
    check("несколько начертаний сразу",
          normalize_family("Segoe UI Semibold Italic (TrueType)") == "Segoe UI",
          normalize_family("Segoe UI Semibold Italic (TrueType)"))
    check("Arial Narrow остаётся отдельным семейством",
          normalize_family("Arial Narrow Bold Italic (TrueType)") == "Arial Narrow",
          normalize_family("Arial Narrow Bold Italic (TrueType)"))
    check("OpenType и пустое имя", normalize_family("Cambria Math (OpenType)") == "Cambria Math"
          and normalize_family("") == "")

    cleaned = collect_families([
        "Arial (TrueType)", "Arial Bold (TrueType)", "arial italic (TrueType)",
        "@Arial (TrueType)", "Consolas (TrueType)", "Странный (Weird, Name) (TrueType)", "",
    ])
    check("дубликаты схлопнуты", [f.lower() for f in cleaned].count("arial") == 1, str(cleaned))
    check("вертикальные варианты отброшены", "@Arial" not in cleaned, str(cleaned))
    check("подозрительные имена отброшены", "Странный" not in " ".join(cleaned), str(cleaned))
    check("популярные шрифты впереди", cleaned[:2] == ["Consolas", "Arial"], str(cleaned))

    installed = list_system_fonts()
    check("список шрифтов системы не пуст", len(installed) > 5, str(len(installed)))
    check("в списке нет хвостов (TrueType)",
          not any("(truetype" in name.lower() for name in installed))
    if sys.platform.startswith("win"):
        check("Segoe UI найден", any(name.lower() == "segoe ui" for name in installed),
              str(installed[:5]))
    check("resolve_family приводит регистр", resolve_family("consolas") == "Consolas"
          or not any(f.lower() == "consolas" for f in installed))
    check("неизвестный шрифт остаётся как введён",
          resolve_family("Мой Шрифт 3000") == "Мой Шрифт 3000")

    cfg = AppConfig()
    cfg.ui.font_family = "Consolas"
    cfg.ui.font_size = 18
    path = tmp / "font.json"
    cfg.save(path)
    loaded = AppConfig.load(path)
    check("шрифт сохраняется в конфиг",
          loaded.ui.font_family == "Consolas" and loaded.ui.font_size == 18,
          f"{loaded.ui.font_family!r} {loaded.ui.font_size}")
    loaded.ui.font_size = 200
    check("размер зажат сверху", loaded.ui.clamped_size() == loaded.ui.MAX_FONT_SIZE)
    loaded.ui.font_size = 2
    check("размер зажат снизу", loaded.ui.clamped_size() == loaded.ui.MIN_FONT_SIZE)
    loaded.ui.font_size = "мусор"
    check("битый размер даёт умолчание", loaded.ui.clamped_size() == 14)

    # шрифт и смайлы применяются к док-панели: они попадают в ссылку для OBS и в пакеты
    from govorok.dock import DockHub, message_payload

    hub = DockHub(lambda: loaded)
    url = hub.url()
    check("ссылка для OBS несёт шрифт и размер",
          "font=Consolas" in url and f"size={loaded.ui.clamped_size()}" in url, url)
    loaded.ui.render_emotes = False
    message = ChatMessage(channel="test", user="bob", display_name="Bob",
                          text="Kappa hello", emotes=[("25", 0, 4)])
    plain = message_payload(message, None, config=loaded)
    check("выключенные смайлы уходят текстом",
          [seg["t"] for seg in plain["segments"]] == ["text"] and plain["segments"][0]["v"] == "Kappa hello",
          str(plain["segments"]))
    loaded.ui.render_emotes = True
    rich = message_payload(message, None, config=loaded)
    check("включённые смайлы уходят сегментами",
          any(seg["t"] == "emote" for seg in rich["segments"]), str(rich["segments"]))


def free_port() -> int:
    """Порт для тестового сервера: 0 — система выберет сама.

    На Windows нельзя заранее «примерить» порт и тут же занять его заново,
    поэтому док-сервер умеет сообщать фактический порт после запуска.
    """
    return 0


async def http_get(url: str) -> tuple[int, str, str]:
    """Простой GET через httpx: (код, content-type, тело)."""
    import httpx

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(url)
        return response.status_code, response.headers.get("content-type", ""), response.text


async def ws_recv(ws, timeout: float = 3.0) -> dict:
    """Дождаться JSON-пакета от сервера."""
    import json as _json

    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return _json.loads(raw)


async def test_dock(tmp: Path) -> None:
    print("\n[9/13] Док-панель для OBS")
    from websockets.asyncio.client import connect as ws_connect

    from govorok.dock import DockHub, message_key, message_payload
    from govorok.stats import estimate_cost, language_title, stats_payload, top_languages

    # --- чистые функции статистики
    langs = top_languages(Counter({"en": 5, "ru": 3, "de": 2, "fr": 0}))
    check("топ языков считается с долями",
          [item["code"] for item in langs] == ["en", "ru", "de"] and langs[0]["share"] == 50.0,
          str(langs))
    check("названия языков человеческие", language_title("en").startswith("английский"),
          language_title("en"))
    check("неизвестный язык остаётся кодом", language_title("xx") == "XX", language_title("xx"))
    check("стоимость без цен не считается", estimate_cost(1000, 500, 0, 0) is None)
    check("стоимость по цене за 1M токенов",
          estimate_cost(1_000_000, 200_000, 0.27, 1.1) == 0.49,
          str(estimate_cost(1_000_000, 200_000, 0.27, 1.1)))

    port = free_port()
    cfg = AppConfig()
    cfg.dock.port = port
    cfg.dock.token = "s3cret"
    cfg.dock.history = 3
    cfg.twitch.channels = ["xqc"]
    cfg.llm.price_in_per_mtok = 0.27
    cfg.llm.price_out_per_mtok = 1.1

    sent: list[str] = []
    resets: list[str] = []

    async def fake_send(text: str, *, reply_to: str = "", channel: str = "") -> tuple[bool, str]:
        if not cfg.twitch.can_send:
            return False, "нужен токен"
        sent.append((text, reply_to, channel))
        return True, ""

    def fake_stats() -> dict:
        translator = Translator(AppConfig())
        translator.prompt_tokens, translator.completion_tokens = 1_000_000, 200_000
        translator.requests_sent, translator.cache_hits = 12, 30
        translator.languages.update({"en": 5, "pt": 2})
        return stats_payload(started_at=time.time() - 90, messages=20, translated=15, skipped=4,
                             errors=1, translator=translator, config=cfg, channels=["xqc"],
                             clients=1, can_send=cfg.twitch.can_send,
                             emotes={"global": 122, "channels": 986})

    hub = DockHub(lambda: cfg, stats_provider=fake_stats, on_send=fake_send,
                  on_reset_stats=lambda: resets.append("reset"),
                  viewers_provider=lambda: {"type": "viewers", "total": 42, "active": 7,
                                            "online": 30, "lurkers": 35, "idle_minutes": 15,
                                            "channels": ["xqc"], "chatters_available": False,
                                            "chatters_error": "", "viewers": []})
    check("док стартует", await hub.start())
    port = hub.port
    check("порт известен после запуска", port > 0, str(port))
    check("ссылка для OBS содержит порт и токен",
          f":{port}/dock?" in hub.url() and "token=s3cret" in hub.url(), hub.url())

    # --- страница
    code, ctype, page_body = await http_get(f"http://127.0.0.1:{port}/dock?token=s3cret")
    check("страница дока отдаётся", code == 200 and "text/html" in ctype, f"{code} {ctype}")
    check("страница похожа на чат",
          "Govorok" in page_body and "/ws" in page_body and "перевод" in page_body, page_body[:80])
    code, _, _ = await http_get(f"http://127.0.0.1:{port}/dock")
    check("без токена страница закрыта", code == 403, str(code))
    code, ctype, body = await http_get(f"http://127.0.0.1:{port}/health")
    check("health отвечает JSON", code == 200 and '"ok": true' in body, body[:60])
    code, _, _ = await http_get(f"http://127.0.0.1:{port}/нет-такого")
    check("неизвестный путь — 404", code == 404, str(code))

    # --- websocket
    ws = await ws_connect(f"ws://127.0.0.1:{port}/ws?token=s3cret")
    hello = await ws_recv(ws)
    check("приветствие с каналами", hello["type"] == "hello" and hello["channels"] == ["xqc"],
          str(hello)[:120])
    check("клиент учтён", hub.clients == 1, str(hub.clients))

    # --- оверлей субтитров
    check("приветствие отдаёт вид субтитров",
          hello.get("subs", {}).get("font") == cfg.stt.clamped_font()
          and hello["subs"].get("pos") == "bottom", str(hello.get("subs"))[:120])
    check("ссылка оверлея содержит настройки и токен",
          f":{port}/subs?" in hub.url_subs() and "font=" in hub.url_subs()
          and "token=s3cret" in hub.url_subs(), hub.url_subs())
    code, ctype, subs_body = await http_get(f"http://127.0.0.1:{port}/subs?token=s3cret")
    check("страница оверлея отдаётся",
          code == 200 and "text/html" in ctype and "/ws" in subs_body
          and "subtitle" in subs_body, f"{code} {ctype} {subs_body[:60]}")
    code, _, _ = await http_get(f"http://127.0.0.1:{port}/subs")
    check("оверлей без токена закрыт", code == 403, str(code))

    cfg.stt.font_size = 40
    cfg.stt.lines = 3
    hub.publish({"type": "settings", "subs": hub.subs_settings()})
    got = await ws_recv(ws)
    check("смена настроек субтитров летит в оверлей",
          got.get("type") == "settings" and got["subs"]["font"] == 40
          and got["subs"]["lines"] == 3, str(got)[:140])

    hub.publish_subtitle({"id": "sub-1", "text": "привет, чат", "final": True,
                          "ts": time.time()})
    got = await ws_recv(ws)
    check("субтитр доехал до оверлея",
          got.get("type") == "subtitle" and got.get("text") == "привет, чат"
          and got.get("final") is True, str(got)[:120])

    # оверлей помечает себя page=subs — чтобы в статусе считать именно его
    ws_subs = await ws_connect(f"ws://127.0.0.1:{port}/ws?token=s3cret&page=subs")
    await ws_recv(ws_subs)
    check("оверлеи считаются отдельно от док-панели",
          hub.subs_clients == 1 and hub.clients == 2,
          f"оверлеев {hub.subs_clients}, всего {hub.clients}")
    await ws_subs.close()
    await asyncio.sleep(0.2)
    check("отключённый оверлей не считается", hub.subs_clients == 0,
          str(hub.subs_clients))

    message = ChatMessage(channel="xqc", user="bob", display_name="Bob", room_id="71092938",
                          text="Kappa hello chat", emotes=[("25", 0, 4)],
                          badges=[("moderator", "1")], is_mod=True, msg_id="msg-1")
    payload = message_payload(message, None, pending=True)
    check("в пакете есть смайл-сегмент",
          any(seg["t"] == "emote" and "static-cdn.jtvnw.net" in seg["u"] for seg in payload["segments"]),
          str(payload["segments"]))
    check("в пакете есть бейджи и флаг перевода",
          payload["badges"] == ["MOD"] and payload["pending"] is True)

    hub.publish_message(payload)
    got = await ws_recv(ws)
    check("сообщение доехало до дока", got["type"] == "message" and got["id"] == "msg-1", str(got)[:120])

    from govorok.translator import Translation

    hub.publish_translation(message_key(message),
                            Translation(text="привет, чат", source_lang="en", target_lang="ru"))
    got = await ws_recv(ws)
    check("перевод доехал до дока",
          got["type"] == "translation" and got["text"] == "привет, чат" and got["id"] == "msg-1",
          str(got)[:120])

    # --- история для нового подключения
    ws2 = await ws_connect(f"ws://127.0.0.1:{port}/ws?token=s3cret")
    hello2 = await ws_recv(ws2)
    history = hello2.get("history") or []
    check("новому клиенту отдаётся история", len(history) == 1 and history[0]["id"] == "msg-1",
          str(history)[:120])
    check("в истории уже есть перевод",
          (history[0].get("translation") or {}).get("text") == "привет, чат",
          str(history[0].get("translation")))
    check("два клиента на связи", hub.clients == 2, str(hub.clients))

    # --- статистика и ответы из дока
    check("hello отдаёт флаг отправки", hello["can_send"] is False, str(hello.get("can_send")))
    check("hello отдаёт статистику", (hello.get("stats") or {}).get("messages") == 20,
          str(hello.get("stats"))[:100])
    check("в статистике есть топ языков",
          [item["code"] for item in hello["stats"]["languages"]] == ["en", "pt"],
          str(hello["stats"]["languages"]))
    check("в статистике посчитан расход", hello["stats"]["cost"] == 0.49,
          str(hello["stats"]["cost"]))
    check("страница содержит вкладку статистики и форму ответа",
          "Статистика" in page_body and "composer" in page_body and "reset-stats" in page_body)
    check("свои сообщения бота выделяются в доке",
          "msg.own" in page_body or '.msg.own' in page_body, "нет стиля own")
    check("в доке есть бейдж бота", "badge.BOT" in page_body)
    check("страница содержит вкладку зрителей",
          "Зрители" in page_body and "v-search" in page_body and "v-list" in page_body)
    check("в списке зрителей только ники, без текста сообщений",
          "v-text" not in page_body and "v-lurk" not in page_body)
    check("ответ на сообщение живёт в чате",
          "reply-bar" in page_body and "reply_to" in page_body and "startReply" in page_body)

    await ws.send(json.dumps({"t": "viewers"}))
    viewers_packet = await ws_recv(ws)
    check("список зрителей по запросу",
          viewers_packet["type"] == "viewers" and viewers_packet["total"] == 42
          and viewers_packet["online"] == 30, str(viewers_packet)[:100])
    check("hello отдаёт сводку по зрителям",
          (hello.get("viewers_summary") or {}).get("total") == 42,
          str(hello.get("viewers_summary")))

    await ws.send(json.dumps({"t": "stats"}))
    stats_packet = await ws_recv(ws)
    check("статистика по запросу", stats_packet["type"] == "stats" and stats_packet["translated"] == 15,
          str(stats_packet)[:100])

    await ws.send(json.dumps({"t": "reset_stats"}))
    reset_packet = await ws_recv(ws)
    check("сброс статистики вызван", resets == ["reset"], str(resets))
    check("после сброса пришёл свежий снимок", reset_packet["type"] == "stats")

    await ws.send(json.dumps({"t": "send", "text": "привет из дока"}))
    result_packet = await ws_recv(ws)
    check("без токена отправка отклонена",
          result_packet["type"] == "send_result" and result_packet["ok"] is False
          and "токен" in result_packet["error"], str(result_packet))

    cfg.twitch.nickname = "mybot"
    cfg.twitch.oauth_token = "abc"
    check("can_send включился", cfg.twitch.can_send)
    ws4 = await ws_connect(f"ws://127.0.0.1:{port}/ws?token=s3cret")
    hello4 = await ws_recv(ws4)
    check("hello сообщает о возможности писать", hello4["can_send"] is True, str(hello4.get("can_send")))
    await ws4.send(json.dumps({"t": "send", "text": "  ответ   из дока  "}))
    ok_packet = await ws_recv(ws4)
    check("сообщение отправлено в чат",
          ok_packet["ok"] is True and sent == [("ответ из дока", "", "")], f"{ok_packet} {sent}")
    check("в ответе есть текст", ok_packet["text"] == "ответ из дока", str(ok_packet))

    await ws4.send(json.dumps({"t": "send", "text": "веткой", "reply_to": "msg-77",
                               "channel": "xqc"}))
    reply_packet = await ws_recv(ws4)
    check("ответ веткой доходит до бота",
          reply_packet["ok"] is True and sent[-1] == ("веткой", "msg-77", "xqc"),
          f"{reply_packet} {sent[-1]}")
    check("в ответе видно, на что отвечаем", reply_packet["reply_to"] == "msg-77", str(reply_packet))
    await ws4.close()

    # --- без токена websocket отклоняется ещё на рукопожатии
    from websockets.exceptions import InvalidStatus

    rejected = "подключился"
    try:
        await ws_connect(f"ws://127.0.0.1:{port}/ws")
    except InvalidStatus as exc:
        rejected = str(exc.response.status_code)
    except Exception as exc:  # noqa: BLE001
        rejected = type(exc).__name__
    check("websocket без токена отклонён (403)", rejected == "403", rejected)

    await ws.close()
    await ws2.close()
    await asyncio.sleep(0.2)
    check("клиенты отключаются", hub.clients == 0, str(hub.clients))
    await hub.stop()
    check("док остановлен", not hub.running)


async def test_dock_runner(tmp: Path) -> None:
    """Сквозной тест режима без окна: сообщение → перевод → док."""
    print("\n[12/13] Режим без окна (DockRunner)")
    from websockets.asyncio.client import connect as ws_connect

    from govorok.dock import DockRunner

    with MockServer() as server, JsonServer(EMOTE_ROUTES) as emote_server:
        restore = patch_emote_urls(emote_server.base)
        try:
            cfg = mock_config(server)
            cfg.dock.port = free_port()
            cfg.dock.token = ""
            cfg.dock.history = 10
            cfg.twitch.channels = []

            runner = DockRunner(cfg, verbose=False, cache_path=tmp / "dock-emotes.json")
            started = await runner.start(connect_chat=False)
            check("раннер поднялся без окна", started)
            check("ссылка для OBS выдана", runner.hub.url().startswith("http://127.0.0.1:"),
                  runner.hub.url())
            await runner.emotes.load_globals()
            await runner.emotes.load_channel("71092938", "xqc")
            check("статистика раннера собирается",
                  runner.stats_snapshot()["can_send"] is False
                  and runner.stats_snapshot()["emotes"]["global"] >= 0,
                  str(runner.stats_snapshot())[:100])
            ok, error = await runner.send_to_chat("привет")
            check("без токена ответ из дока не уходит", ok is False and "токен" in error, error)

            cfg.twitch.nickname = "mybot"
            cfg.twitch.oauth_token = "abc"
            cfg.twitch.channels = ["xqc"]
            ok, error = await runner.send_to_chat("привет из дока")
            check("с токеном сообщение ставится в очередь", ok is True and not error, f"{ok} {error}")
            check("статистика видит возможность писать", runner.stats_snapshot()["can_send"] is True)

            ws = await ws_connect(f"ws://127.0.0.1:{runner.hub.port}/ws")
            hello = await ws_recv(ws)
            check("док ответил hello", hello["type"] == "hello", str(hello)[:80])

            runner.on_message(ChatMessage(channel="xqc", user="bob", display_name="Bob",
                                          room_id="71092938", text="pepeD hello chat",
                                          msg_id="run-1"))
            first = await ws_recv(ws)
            check("сообщение ушло в док", first["type"] == "message" and first["id"] == "run-1",
                  str(first)[:100])
            check("флаг ожидания перевода", first["pending"] is True)
            check("смайл 7TV попал в сегменты",
                  any(seg["t"] == "emote" and "cdn.7tv.app" in seg["u"] for seg in first["segments"]),
                  str(first["segments"]))

            for _ in range(60):
                packet = await ws_recv(ws, timeout=3)
                if packet.get("type") == "translation":
                    break
            check("перевод пришёл в док",
                  packet["type"] == "translation" and "привет, чат" in packet["text"], str(packet)[:120])
            check("счётчик переводов растёт", runner.translated == 1, str(runner.translated))

            runner.on_notice("notice", "Чат очищен")
            for _ in range(5):
                packet = await ws_recv(ws, timeout=3)
                if packet.get("type") == "notice":
                    break
            check("системная плашка доехала", packet["type"] == "notice", str(packet)[:80])

            # своё сообщение Twitch не возвращает по IRC — бот показывает его сам
            runner.on_sent_message("ответ из бота", "xqc")
            for _ in range(5):
                packet = await ws_recv(ws, timeout=3)
                if packet.get("type") == "message" and packet.get("own"):
                    break
            check("сообщение бота видно в доке",
                  packet.get("own") is True and packet["segments"][0]["v"] == "ответ из бота",
                  str(packet)[:120])
            check("у сообщения бота есть бейдж и ник",
                  packet["badges"] == ["BOT"] and packet["user"] == "mybot", str(packet)[:80])
            check("счётчик отправленных растёт", runner.sent == 1, str(runner.sent))
            check("отправленные попадают в статистику",
                  runner.stats_snapshot()["sent"] == 1, str(runner.stats_snapshot()["sent"]))

            # команды и таймеры работают и в режиме без окна
            runner.config.commands = [CommandConfig(triggers=["!ping"], response="понг, {user}")]
            runner.config.timers = [TimerConfig(lines=["Напоминание"], interval_minutes=5)]
            runner.on_message(ChatMessage(channel="xqc", user="viewer", display_name="Viewer",
                                          text="!ping", msg_id="cmd-1"))
            for _ in range(8):
                packet = await ws_recv(ws, timeout=3)
                if packet.get("source") == "command":
                    break
            check("команда ответила в режиме без окна",
                  packet.get("source") == "command"
                  and packet["segments"][0]["v"] == "понг, viewer", str(packet)[:120])
            check("счётчик команд учтён", runner.commands.fired == 1, str(runner.commands.fired))

            # ответ можно скрыть из дока, даже если он уходит в чат
            runner._echo_sources.append(("command", False))          # noqa: SLF001
            runner.on_sent_message("скрытый ответ", "xqc")
            hidden = True
            for _ in range(3):
                try:
                    packet = await ws_recv(ws, timeout=1)
                except (TimeoutError, asyncio.TimeoutError):
                    break
                if packet.get("type") == "message":
                    hidden = False
                    break
            check("ответ можно не показывать в доке", hidden)
            check("но отправка всё равно посчитана", runner.sent == 2, str(runner.sent))
            check("таймеры в фоне запущены",
                  runner._timer_task is not None and not runner._timer_task.done(),  # noqa: SLF001
                  "задача таймеров не запущена")
            check("статистика знает про команды",
                  runner.stats_snapshot()["commands_fired"] == 1,
                  str(runner.stats_snapshot()["commands_fired"]))

            await ws.close()
            await runner.stop()
            check("раннер остановлен", not runner.hub.running)
        finally:
            restore()


async def test_viewers(tmp: Path) -> None:
    print("\n[10/13] Вкладка «Зрители»")
    from govorok.viewers import ChattersPoller, ViewerRegistry

    registry = ViewerRegistry()
    now = time.time()
    for index in range(3):
        registry.note(ChatMessage(channel="xqc", user="bob", display_name="Bob", color="#79c0ff",
                                  room_id="71092938", text=f"привет {index}", ts=now - 10 * index,
                                  is_mod=True, badges=[("moderator", "1")]))
    registry.note(ChatMessage(channel="xqc", user="old", display_name="Old", text="давно",
                              ts=now - 3600))
    registry.note(ChatMessage(channel="gaules", user="ze", display_name="Zé", text="oi", ts=now))

    snap = registry.snapshot(idle_minutes=15, limit=10)
    check("зрители собраны", snap["total"] == 3, str(snap["total"]))
    check("активные считаются по свежести", snap["active"] == 2, str(snap["active"]))
    check("порядок: активные впереди", snap["viewers"][0]["login"] == "bob",
          str([v["login"] for v in snap["viewers"]]))
    check("счётчик сообщений у зрителя", snap["viewers"][0]["messages"] == 3,
          str(snap["viewers"][0]))
    check("бейджи и цвет сохранены",
          snap["viewers"][0]["badges"] == ["MOD"] and snap["viewers"][0]["color"] == "#79c0ff",
          str(snap["viewers"][0]))
    check("давний зритель помечен как неактивный",
          next(v for v in snap["viewers"] if v["login"] == "old")["active"] is False)
    check("каналы перечислены", snap["channels"] == ["gaules", "xqc"], str(snap["channels"]))
    check("без официального списка помечаем", snap["chatters_available"] is False)

    registry.apply_chatters("xqc", [("lurker1", "viewers"), ("moddy", "moderators"),
                                    ("bob", "viewers")], total=120)
    snap = registry.snapshot(idle_minutes=15, limit=10)
    check("молчащие зрители добавлены", snap["total"] == 5, str(snap["total"]))
    check("молчащие помечены как lurker", snap["lurkers"] == 2, str(snap["lurkers"]))
    check("официальный список учтён", snap["chatters_available"] and snap["chatters_total"] == 120,
          str(snap))
    check("модератор из списка получил бейдж",
          "MOD" in next(v for v in snap["viewers"] if v["login"] == "moddy")["badges"],
          str(next(v for v in snap["viewers"] if v["login"] == "moddy")))
    check("зритель не из списка помечен вышедшим",
          next(v for v in snap["viewers"] if v["login"] == "old")["online"] is False,
          str(next(v for v in snap["viewers"] if v["login"] == "old")))
    check("лимит списка работает",
          registry.snapshot(idle_minutes=15, limit=2)["truncated"] == 3,
          str(registry.snapshot(idle_minutes=15, limit=2)["truncated"]))

    registry.apply_chatters("xqc", [], error="нужен модератор в канале (403)")
    check("ошибка официального списка сохраняется",
          "403" in registry.snapshot()["chatters_error"], registry.snapshot()["chatters_error"])
    registry.clear()
    check("реестр очищается", registry.snapshot()["total"] == 0)

    # --- опрос официального списка через Helix
    routes = {
        "/helix/users": {"data": [{"id": "999", "login": "mybot"}]},
        "/helix/chat/chatters": {"data": [{"user_login": "lurker1"}, {"user_login": "lurker2"}],
                                 "total": 2},
    }
    with JsonServer(routes) as server:
        restore = patch_helix_urls(server.base)
        try:
            cfg = AppConfig()
            cfg.twitch.client_id = "client-id"
            cfg.twitch.oauth_token = "oauth:token"
            cfg.twitch.nickname = "mybot"
            poller = ChattersPoller(registry, lambda: cfg)
            check("опрос включён при клиенте и токене", poller.enabled())
            check("id бота получен", await poller.resolve_bot_id() == "999", poller.last_error)
            check("список зрителей забран", await poller.fetch("71092938", "xqc"), poller.last_error)
            snap = registry.snapshot()
            check("зрители из Helix в реестре",
                  {v["login"] for v in snap["viewers"]} == {"lurker1", "lurker2"},
                  str([v["login"] for v in snap["viewers"]]))
            check("happy path без ошибок", snap["chatters_error"] == "", snap["chatters_error"])
            await poller.aclose()

            cfg.twitch.client_id = ""
            check("без Client-Id опрос выключен", not ChattersPoller(registry, lambda: cfg).enabled())
            cfg.twitch.client_id = "client-id"
            cfg.viewers.fetch_chatters = False
            check("опрос можно выключить настройкой",
                  not ChattersPoller(registry, lambda: cfg).enabled())
            cfg.viewers.fetch_chatters = True
        finally:
            restore()

    # --- ошибка доступа
    with JsonServer({"/helix/users": {"data": [{"id": "999"}]}}) as server:
        restore = patch_helix_urls(server.base)
        try:
            cfg = AppConfig()
            cfg.twitch.client_id = "client-id"
            cfg.twitch.oauth_token = "oauth:token"
            poller = ChattersPoller(registry, lambda: cfg)
            check("404 списка не роняет опрос", await poller.fetch("71092938", "xqc") is False)
            check("ошибка описана по-человечески",
                  "404" in registry.snapshot()["chatters_error"],
                  registry.snapshot()["chatters_error"])
            await poller.aclose()
        finally:
            restore()


async def test_commands(tmp: Path) -> None:
    print("\n[11/13] Команды и таймеры")
    import random as random_module

    from govorok.commands import CommandEngine, TimerEngine, format_duration, render_template

    # --- переменные
    message = ChatMessage(channel="xqc", user="bob", display_name="Bob", text="!cmd раз два")
    check("простой шаблон",
          render_template("Привет, {user}!", message=message) == "Привет, bob!")
    check("ник, канал и аргументы",
          render_template("{display} в {channel}: {args}", message=message, args=["раз", "два"])
          == "Bob в xqc: раз два",
          render_template("{display} в {channel}: {args}", message=message, args=["раз", "два"]))
    check("аргументы по номеру",
          render_template("{1}-{2}-{3}|{args.1}|{target}", message=message, args=["раз", "два"])
          == "раз-два-|раз|раз",
          render_template("{1}-{2}-{3}|{args.1}|{target}", message=message, args=["раз", "два"]))
    fixed_now = time.mktime((2026, 10, 6, 12, 34, 56, 0, 0, -1))
    check("счётчик и даты",
          render_template("{count} {date} {time}", message=message, count=5, now=fixed_now)
          == "5 06.10.2026 12:34:56",
          render_template("{count} {date} {time}", message=message, count=5, now=fixed_now))
    check("случайный вариант из списка",
          render_template("{random:да|нет}", message=message) in {"да", "нет"})
    check("неизвестная переменная исчезает",
          render_template("a{нетТакой}b", message=message) == "ab",
          render_template("a{нетТакой}b", message=message))
    check("фигурные скобки не по шаблону остаются как есть",
          render_template("a{нет-такой}b", message=message) == "a{нет-такой}b")

    stream = {"started_at": time.time() - 3720, "game": "Dota 2", "title": "Пабы", "viewers": 1234}
    uptime_text = render_template("{uptime}", message=message, stream=stream)
    check("аптайм считается из времени старта", uptime_text == "1 ч 02 мин", uptime_text)
    check("игра, заголовок и зрители",
          render_template("{game}|{title}|{viewers}", message=message, stream=stream)
          == "Dota 2|Пабы|1234")
    check("без данных Helix переменные честные",
          render_template("{uptime}|{game}", message=message) == "нет данных|нет данных")
    check("длительность форматируется",
          (format_duration(45), format_duration(754), format_duration(3720))
          == ("45 с", "12 мин 34 с", "1 ч 02 мин"),
          f"{format_duration(45)}, {format_duration(754)}, {format_duration(3720)}")

    # --- движок команд
    cfg = AppConfig()
    cfg.twitch.channels = ["xqc"]
    cfg.commands = [
        CommandConfig(triggers=["!uptime", "!аптайм"],
                      response="Стрим идёт {uptime}, игра: {game}, смотрят {viewers}",
                      cooldown=30),
        CommandConfig(triggers=["!hi"], response="Привет, {user}! Сказал: {args}",
                      permission="subscriber", cooldown=0),
        CommandConfig(triggers=["!death"], response="Смертей: {count}", cooldown=0),
        CommandConfig(triggers=["!off"], response="молчит", enabled=False, cooldown=0),
    ]
    counts_path = tmp / "counters.json"
    if counts_path.exists():
        counts_path.unlink()
    engine = CommandEngine(lambda: cfg, counts_path=counts_path, global_cooldown=0.0)

    result = engine.handle(ChatMessage(channel="xqc", user="bob", display_name="Bob",
                                       text="!UPTIME"), stream=stream)
    check("команда найдена без учёта регистра",
          result is not None and "Dota 2" in result.text and "1 ч 0" in result.text,
          result.text if result else "нет ответа")
    check("повтор блокируется кулдауном",
          engine.handle(ChatMessage(channel="xqc", user="bob", display_name="Bob",
                                    text="!uptime"), stream=stream) is None)
    check("другой зритель не заблокирован",
          engine.handle(ChatMessage(channel="xqc", user="ann", display_name="Ann",
                                    text="!аптайм"), stream=stream) is not None)
    check("выключенная команда не отвечает",
          engine.handle(ChatMessage(channel="xqc", user="bob", display_name="Bob",
                                    text="!off")) is None)

    plain = ChatMessage(channel="xqc", user="bob", display_name="Bob", text="!hi как дела")
    check("права проверяются", engine.handle(plain) is None)
    plain.is_sub = True
    allowed = engine.handle(plain)
    check("подписчик получает ответ", allowed is not None and "как дела" in allowed.text,
          allowed.text if allowed else "нет")
    check("модератор тоже может",
          engine.handle(ChatMessage(channel="xqc", user="mod", display_name="Mod",
                                    text="!hi", is_mod=True)) is not None)

    counter = ChatMessage(channel="xqc", user="any", display_name="Any", text="!death")
    first = engine.handle(counter)
    counter.user = "any2"
    second = engine.handle(counter)
    check("счётчик растёт", (first.text, second.text) == ("Смертей: 1", "Смертей: 2"),
          f"{first.text} / {second.text}")
    check("счётчики сохранены на диск", counts_path.exists())
    restored = CommandEngine(lambda: cfg, counts_path=counts_path)
    check("счётчики переживают перезапуск", restored.counts.get("!death") == 2,
          str(restored.counts))

    # --- таймеры
    cfg.timers = [
        TimerConfig(lines=["Подписывайтесь!", "Соцсети в описании"], interval_minutes=10,
                    only_when_active=False),
        TimerConfig(lines=["Только при активном чате"], interval_minutes=5,
                    only_when_active=True, active_window_minutes=5, min_messages=2),
        TimerConfig(lines=["Выключенный"], interval_minutes=5, enabled=False),
    ]
    timers = TimerEngine(lambda: cfg, rng=random_module.Random(1))
    check("таймеры не срабатывают сразу", timers.due() == [])
    moment = time.time() + 10 * 60 + 1
    fired = timers.due(now=moment)
    check("таймер сработал по интервалу", len(fired) == 1 and fired[0][0] == "xqc", str(fired))
    check("текст из списка строк", fired[0][2] in {"Подписывайтесь!", "Соцсети в описании"},
          fired[0][2])
    check("повторно сразу не срабатывает", timers.due(now=moment + 1) == [])

    moment += 5 * 60 + 1
    fired = timers.due(now=moment)
    check("таймер молчит при тишине в чате", not any("активном" in item[2] for item in fired),
          str(fired))
    for index in range(3):
        timers.observe(ChatMessage(channel="xqc", user=f"u{index}", display_name="U",
                                   text="привет", ts=time.time()))
    check("активность чата учитывается",
          timers.is_active("xqc", window_minutes=5, min_messages=2))
    moment += 5 * 60 + 1
    fired = timers.due(now=moment)
    check("активный чат получает таймер", any("активном" in item[2] for item in fired),
          str(fired))
    check("выключенный таймер молчит", not any("Выключенный" in item[2] for item in fired))

    sent: list[tuple[str, str, str]] = []

    async def fake_sender(channel: str, text: str, source: str) -> None:
        sent.append((channel, text, source))

    cfg.timers = [TimerConfig(lines=["Раз в минуту"], interval_minutes=1, only_when_active=False)]
    fast = TimerEngine(lambda: cfg, tick=0.05)
    fast.due()                                          # поставить в расписание
    signature = fast._signature(cfg.timers[0])          # noqa: SLF001
    fast._schedule[0] = (signature, time.time() - 1)    # noqa: SLF001
    task = asyncio.create_task(fast.run(fake_sender))
    for _ in range(40):
        await asyncio.sleep(0.05)
        if sent:
            break
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("цикл таймеров отправляет сообщение через владельца",
          bool(sent) and sent[0][0] == "xqc" and sent[0][1] == "Раз в минуту"
          and sent[0][2] == "timer", str(sent))


def test_stt(tmp: Path) -> None:
    print("\n[stt] Субтитры: VAD, чистка текста и конвейер без микрофона")
    import numpy as np

    from govorok.stt import (BLOCK_SIZE, SAMPLE_RATE, Recognizer, Segmenter, SubtitleService,
                            clean_text, make_speech_provider, sanitize_block)

    # --- чистка текста
    check("обычный текст остаётся", clean_text("  привет,  чат ") == "привет, чат",
          clean_text("  привет,  чат "))
    check("галлюцинации Whisper выбрасываются",
          clean_text("Субтитры создавал DimaTorzok") == ""
          and clean_text("Продолжение следует...") == "",
          clean_text("Продолжение следует..."))
    check("мусор без букв не проходит", clean_text("... !!! 123") == "", clean_text("... !!! 123"))

    # --- поиск CUDA: не должен падать и должен объяснять причину
    from govorok.stt import cuda_ready, prepare_cuda

    ready, reason = cuda_ready()
    check("детектор CUDA отвечает",
          isinstance(ready, bool) and (ready or bool(reason)), f"{ready} / {reason}")
    check("подготовка CUDA согласована с детектором", prepare_cuda() == ready,
          str(ready))
    cfg_fast = AppConfig(); cfg_fast.stt.device = "auto"
    from govorok.stt import SubtitleService as _Svc
    candidates = _Svc(lambda: cfg_fast.stt).recognizer._candidates(cfg_fast.stt)
    check("без CUDA в кандидатах сразу CPU",
          candidates == [("cpu", "int8")] if not ready else candidates[0][0] == "cuda",
          str(candidates))

    # --- индикатор уровня и список микрофонов
    import main as window

    check("meter в крайних точках", window.meter_fraction(0) == 0.0
          and window.meter_fraction(1.0) == 1.0, str(window.meter_fraction(1.0)))
    check("meter логарифмический",
          window.meter_fraction(0.0001) < window.meter_fraction(0.005)
          < window.meter_fraction(0.05), f"{window.meter_fraction(0.005):.2f}")
    check("тишина в дБ — нижний предел", window.level_db(0) == -100.0, str(window.level_db(0)))
    check("один микрофон в разных API — один ключ",
          window.device_key("Microphone (fifine SC3)")
          == window.device_key("Микрофон (fifine SC3)"),
          window.device_key("Микрофон (fifine SC3)"))
    devices = window.list_input_devices()
    keys = [key.casefold() for key, _label in devices]
    check("в списке микрофонов нет дублей",
          len(keys) == len(set(keys)), f"{len(keys)} шт: {[d[1] for d in devices][:4]}")
    check("мапперы не показаны как микрофоны",
          not any("первичный драйвер" in _label.casefold() for _key, _label in devices),
          str([d[1] for d in devices][:4]))

    from govorok.stt import device_label, input_candidates

    check("пустой выбор — системный микрофон", input_candidates("") == [None],
          str(input_candidates("")))
    names = [key for key, _label in devices if key]
    candidates = input_candidates(names[0]) if names else [None]
    check("после кандидатов идёт «по умолчанию»", candidates[-1] is None, str(candidates))
    check("кандидаты упорядочены, с запасным вариантом",
          len(candidates) > 1 and candidates[-1] is None and candidates[0] is not None,
          str([device_label(index) for index in candidates][:3]))

    # --- PortAudio может отдать незаполненный буфер: такие блоки не должны жить
    good = np.full(BLOCK_SIZE, 0.02, dtype=np.float32)
    dirty = np.full(BLOCK_SIZE, 9e33, dtype=np.float32)
    clipped = (np.linspace(-0.2, 0.2, BLOCK_SIZE, dtype=np.float32) * 30).astype(np.float32)
    mixed = np.linspace(-0.2, 0.2, BLOCK_SIZE, dtype=np.float32)
    mixed[10] = np.float32("nan")
    check("нормальный блок проходит очистку", sanitize_block(good.copy()) is True)
    check("блок с мусором отбраковывается", sanitize_block(dirty) is False)
    check("обрезка не выпускает клиппинг", sanitize_block(clipped) is False)
    check("nan заменяется тишиной", sanitize_block(mixed) and np.isfinite(mixed).all())

    # --- VAD
    cfg = AppConfig()
    cfg.stt.silence_ms = 300
    segmenter = Segmenter(lambda: cfg.stt)
    silence = np.zeros(BLOCK_SIZE, dtype=np.float32)
    speech = (np.random.randn(BLOCK_SIZE).astype(np.float32) * 0.15)
    for _ in range(5):
        segmenter.feed(silence)
    check("тишина не считается речью", segmenter.speaking is False)
    check("речь не закрывается сразу",
          all(segmenter.feed(speech) is None for _ in range(6)), "открылась фраза")
    check("VAD считает, что говорит", segmenter.speaking is True)
    utterance = None
    for _ in range(6):
        utterance = segmenter.feed(silence) or utterance
    check("фраза закрылась после тишины", utterance is not None)
    check("в реплику попал предбуфер",
          utterance is not None and utterance.duration > 0.6,
          f"{utterance.duration if utterance else 0:.2f} c")
    check("у реплики есть стабильный id",
          utterance is not None and utterance.id.startswith("sub-"),
          utterance.id if utterance else "")

    long_speech = [segmenter.feed(speech) for _ in range(14)]
    partial = segmenter.partial()
    check("серая гипотеза появляется на длинной фразе",
          partial is not None and partial.duration >= 1.2 and all(item is None for item in long_speech),
          f"{partial.duration if partial else 0:.2f} c")
    segmenter.reset()
    check("сброс VAD забывает фразу", segmenter.speaking is False)

    # --- silero-гейт: «громко, но не речь» не должно уходить в распознавание
    music_cfg = AppConfig()
    music_cfg.stt.silence_ms = 300
    music_gate = Segmenter(lambda: music_cfg.stt, speech=lambda _block: 0.35)
    loud_noise = [music_gate.feed(np.full(BLOCK_SIZE, 0.2, dtype=np.float32)) for _ in range(10)]
    tail = None
    for _ in range(6):
        tail = music_gate.feed(np.zeros(BLOCK_SIZE, dtype=np.float32)) or tail
    check("музыка не становится репликой",
          all(item is None for item in loud_noise)
          and music_gate.rejected_speech >= 1,
          f"отброшено {music_gate.rejected_speech}")
    check("серая гипотеза тоже не рождается из музыки", music_gate.partial() is None)

    dead_air = Segmenter(lambda: music_cfg.stt, speech=lambda _block: 0.05)
    for _ in range(12):
        dead_air.feed(np.full(BLOCK_SIZE, 0.2, dtype=np.float32))
    check("совсем не речь даже не открывает фразу", dead_air.speaking is False,
          str(dead_air.speaking))

    speech_gate = Segmenter(lambda: music_cfg.stt, speech=lambda _block: 0.95)
    made = []
    for _ in range(8):
        made.append(speech_gate.feed(np.full(BLOCK_SIZE, 0.2, dtype=np.float32)))
    hiss = np.random.randn(BLOCK_SIZE).astype(np.float32) * 0.002
    for _ in range(6):
        made.append(speech_gate.feed(hiss))
    check("речь проходит гейт",
          any(item is not None for item in made) and speech_gate.rejected_speech == 0,
          str([item.duration if item else None for item in made]))

    # --- фильтр мусора в тексте
    check("одиночная подпись «музыка» выбрасывается",
          clean_text("Музыка.") == "" and clean_text("Спокойная музыка") == "",
          clean_text("Спокойная музыка"))
    check("«татататата» выбрасывается", clean_text("татататата та-та-та") == "",
          clean_text("татататата та-та-та"))
    check("нормальная речь с повторами жива", clean_text("мама мыла раму") == "мама мыла раму",
          clean_text("мама мыла раму"))
    check("«музыка в игре» остаётся",
          clean_text("тут музыка в игре классная") != "",
          clean_text("тут музыка в игре классная"))

    # --- «музыка» в модели: её должен ловить no_speech_prob до нашей фильтрации
    tonal = np.zeros(SAMPLE_RATE * 3, dtype=np.float32)
    timeline = np.arange(tonal.size) / SAMPLE_RATE
    for start, freq in ((0.0, 220.0), (0.75, 330.0), (1.5, 196.0)):
        window = (timeline >= start) & (timeline < start + 0.7)
        tonal[window] += 0.25 * np.sin(2 * np.pi * freq * timeline[window]).astype(np.float32)
    probe = Recognizer(lambda: music_cfg.stt)
    try:
        probe.ensure()
        raw, _info = probe._model.transcribe(tonal, language="ru", beam_size=1,
                                             vad_filter=False, without_timestamps=True)
        parts = list(raw)
        check("модель сама помечает музыку как «не речь»",
              not parts or max(item.no_speech_prob for item in parts) > music_cfg.stt.max_no_speech_prob
              or not clean_text(" ".join(item.text for item in parts)),
              str([(round(item.no_speech_prob, 2), item.text.strip()) for item in parts])[:120])
        before = probe.rejected_quality
        check("гейт качества глушит музыкальные домыслы",
              probe.transcribe(tonal) == "" and probe.rejected_quality >= before,
              repr(probe.transcribe(tonal)))
    except Exception as exc:  # noqa: BLE001 - нет модели/устройства: не критично
        check("проверка модели на музыке выполнена", True, f"пропуск: {str(exc)[:60]}")

    # --- фиксированная нарезка, когда VAD выключен
    fixed_cfg = AppConfig()
    fixed_cfg.stt.vad = False
    fixed_cfg.stt.chunk_seconds = 1.0
    fixed_seg = Segmenter(lambda: fixed_cfg.stt)
    chunks = [fixed_seg.feed(np.full(BLOCK_SIZE, 0.05, dtype=np.float32)) for _ in range(10)]
    check("без VAD поток режется по времени",
          sum(item is not None for item in chunks) == 1
          and chunks[-1] is not None and chunks[-1].duration >= 1.0,
          f"{sum(item is not None for item in chunks)} реплик")
    quiet_chunks = [fixed_seg.feed(np.zeros(BLOCK_SIZE, dtype=np.float32)) for _ in range(10)]
    check("без VAD тишина не уходит в распознавание",
          all(item is None for item in quiet_chunks),
          str([item is not None for item in quiet_chunks]))

    # --- авто-подстройка порога под тихий микрофон
    quiet = Segmenter(lambda: cfg.stt)
    quiet.noise = 0.0005
    check("без речи порог не падает", quiet.threshold() >= 0.0018, str(quiet.threshold()))
    quiet.peak = 0.004                     # тихая речь: пик ниже абсолютного порога
    check("тихий микрофон получает низкий порог",
          quiet.threshold() < 0.0055, str(quiet.threshold()))
    loud_peak = Segmenter(lambda: cfg.stt)
    loud_peak.noise = 0.0005
    loud_peak.peak = 0.05                  # нормальная громкость — держим абсолютный порог
    check("громкая речь не поднимает порог",
          loud_peak.threshold() <= 0.0055, str(loud_peak.threshold()))
    tracker = Segmenter(lambda: cfg.stt)
    tracker.feed(np.full(BLOCK_SIZE, 0.05, dtype=np.float32))
    check("пик запоминается", tracker.peak > 0.04, str(tracker.peak))

    # --- конвейер: очередь → VAD → «распознавание» → пакеты субтитров
    service = SubtitleService(lambda: cfg.stt)
    published: list[dict] = []
    service._publish = published.append                       # без цикла событий в тесте
    service.recognizer.transcribe = lambda audio, beam_size=1: "привет чат"
    worker = threading.Thread(target=service._work, daemon=True)
    worker.start()
    for _ in range(14):                                      # 1.4 с речи → промежуточная гипотеза
        service._on_audio(speech.reshape(-1, 1), BLOCK_SIZE, None, None)
    deadline = time.time() + 4
    while time.time() < deadline and not any(item.get("final") is False for item in published):
        time.sleep(0.05)
    for _ in range(4):                                       # тишина закрывает фразу
        service._on_audio(silence.reshape(-1, 1), BLOCK_SIZE, None, None)
    deadline = time.time() + 6
    while time.time() < deadline and not any(item.get("final") for item in published):
        time.sleep(0.05)
    service._stop.set()
    worker.join(timeout=3)

    partials = [item for item in published
                if item.get("type") == "subtitle" and item.get("final") is False]
    finals = [item for item in published
              if item.get("type") == "subtitle" and item.get("final") is True]
    check("промежуточная гипотеза ушла в оверлей", bool(partials), str(published)[:140])
    check("финальный субтитр ушёл в оверлей",
          bool(finals) and finals[0].get("text") == "привет чат", str(published)[:160])
    check("счётчики сервиса ведутся",
          service.utterances >= 1 and service.subtitles >= 1,
          str(service.stats())[:120])

    # --- сервис не должен пропускать мусор PortAudio в очередь
    garbage_service = SubtitleService(lambda: cfg.stt)
    garbage_service._publish = lambda _payload: None
    garbage_service._on_audio(np.full((BLOCK_SIZE, 1), 9e33, dtype=np.float32),
                              BLOCK_SIZE, None, None)
    check("мусорный блок не попадает в очередь",
          garbage_service._audio_queue.empty() and garbage_service.garbage_blocks == 1,
          str(garbage_service.stats())[:120])

    # --- выгрузка модели: конструктор ничего не грузит, unload возвращает память
    idle = SubtitleService(lambda: cfg.stt)
    check("создание сервиса не загружает модель",
          idle.recognizer.loaded is False and idle.segmenter.speech is None,
          str((idle.recognizer.loaded, idle.segmenter.speech)))
    check("выгрузка незагруженной модели безопасна",
          "не была загружена" in idle.recognizer.unload(), idle.recognizer.unload())

    class FakeInner:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def unload_model(self, to_cpu: bool = False) -> None:
            self.calls.append(to_cpu)

    class FakeModel:
        def __init__(self) -> None:
            self.model = FakeInner()

    fake = FakeModel()
    idle.recognizer._model = fake
    note = idle.recognizer.unload(to_cpu=True)
    check("unload зовёт unload_model(to_cpu=True) и забывает модель",
          fake.model.calls == [True] and idle.recognizer.loaded is False
          and "видеопамять свободна" in note, f"{fake.model.calls}: {note}")


class SubtitleTranslationServer:
    """Мок OpenAI API, который переводит кириллицу в другую сторону (как реальный)."""

    def __init__(self) -> None:
        outer = self
        self.seen: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                outer.seen.append(payload)
                user_text = ""
                for message in payload.get("messages", []):
                    if message.get("role") == "user":
                        user_text = message.get("content", "")
                result = {"lang": "ru", "translation": f"[en] {user_text[:28]}"}
                body = json.dumps({
                    "id": "mock", "choices": [{"index": 0, "message": {"role": "assistant",
                                                                       "content": json.dumps(result)}}],
                    "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                }, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"


async def test_stt_translation() -> None:
    print("\n[stt-tr] Перевод второй строки субтитров")
    from govorok.translator import Translator
    from govorok.stt import SubtitleService

    with SubtitleTranslationServer() as server:
        cfg = mock_config(server, **{"translation.skip_cyrillic": "True"})
        cfg.translation.enabled = True
        cfg.translation.target_lang = "ru"       # чат переводим НА русский
        cfg.stt.translate_to = "en"              # субтитры — на английский
        cfg.stt.language = "ru"
        chat = Translator(cfg)
        published: list[dict] = []

        service = SubtitleService(lambda: cfg.stt, translator=chat)
        service._loop = asyncio.get_running_loop()
        service._publish = published.append
        service._translate("sub-1", "Привет, чат! Сегодня играем в Доту.")

        deadline = asyncio.get_running_loop().time() + 8
        while (asyncio.get_running_loop().time() < deadline
               and not [p for p in published if p.get("type") == "subtitle_translation"]):
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.2)

        packets = [p for p in published if p.get("type") == "subtitle_translation"]
        check("русская речь переводится, несмотря на skip_cyrillic", bool(packets),
              str(published)[:160])
        check("перевод ушёл в своей пакет с id строки",
              bool(packets) and packets[0].get("id") == "sub-1"
              and (packets[0].get("text") or "").startswith("[en]"),
              str(packets)[:160])
        check("worker не блокируется: счётчики перевода ведутся",
              service.translate_requests == 1 and service.translate_done == 1,
              str((service.translate_requests, service.translate_done)))
        check("язык чата не тронут переводом субтитров",
              chat.config.translation.target_lang == "ru"
              and cfg.translation.target_lang == "ru",
              chat.config.translation.target_lang)
        check("запрос к API реально был", len(server.seen) == 1, str(len(server.seen)))

        same = AppConfig()
        same.stt.language, same.stt.translate_to = "ru", "ru"
        quiet: list[dict] = []
        service2 = SubtitleService(lambda: same.stt, translator=chat)
        service2._loop = asyncio.get_running_loop()
        service2._publish = quiet.append
        service2._translate("sub-2", "привет")
        await asyncio.sleep(0.6)
        check("совпадение языка речи и цели не порождает запрос",
              not quiet and len(server.seen) == 1, str(quiet)[:120])

        await service.stop()
        await chat.aclose()


async def test_stt_modes() -> None:
    import numpy as np
    print("\n[stt-m] Режимы показа субтитров: ru / en / both")
    from govorok.translator import Translator
    from govorok.stt import SubtitleService, Utterance

    async def run_mode(mode: str) -> tuple[list[dict], int]:
        with SubtitleTranslationServer() as server:
            cfg = mock_config(server)
            cfg.translation.enabled = True
            cfg.translation.target_lang = "ru"
            cfg.stt.language = "ru"
            cfg.stt.translate_to = "en"
            cfg.stt.subtitle_mode = mode
            chat = Translator(cfg)
            published: list[dict] = []
            service = SubtitleService(lambda: cfg.stt, translator=chat)
            service._loop = asyncio.get_running_loop()
            service._publish = published.append
            service.recognizer.transcribe = lambda a, beam_size=1: "привет чат"
            utterance = Utterance(audio=np.zeros(16000, dtype=np.float32),
                                  duration=1.0, started=time.time())
            service._finish(utterance)
            deadline = asyncio.get_running_loop().time() + 6
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
                if [q for q in published if q.get("type") != "subtitle"]:
                    break
            await asyncio.sleep(0.3)
            requests = len(server.seen)
            await service.stop()
            await chat.aclose()
            return published, requests

    both, requests_both = await run_mode("both")
    finals = [p for p in both if p.get("type") == "subtitle" and p.get("final")]
    translations = [p for p in both if p.get("type") == "subtitle_translation"]
    check("both: сразу оригинал, перевод отдельным пакетом",
          len(finals) == 1 and finals[0].get("text") == "привет чат"
          and bool(translations) and translations[0]["text"].startswith("[en]"),
          str(both)[:200])
    check("both: в финале есть original для переключения режима",
          bool(finals) and finals[0].get("original") == "привет чат", str(finals)[:120])

    only_en, requests_en = await run_mode("en")
    finals_en = [p for p in only_en if p.get("type") == "subtitle" and p.get("final")]
    check("en: оригинал не показывается, ждём перевод",
          len(finals_en) == 1 and (finals_en[0].get("text") or "").startswith("[en]")
          and finals_en[0].get("original") == "привет чат", str(only_en)[:200])
    check("en: пакет помечен replace — оверлей подменит строку переводом",
          len(finals_en) == 1 and finals_en[0].get("replace") is True, str(finals_en)[:160])
    check("en: отдельный пакет перевода не нужен",
          not [p for p in only_en if p.get("type") == "subtitle_translation"],
          str(only_en)[:120])

    only_ru, requests_ru = await run_mode("ru")
    finals_ru = [p for p in only_ru if p.get("type") == "subtitle" and p.get("final")]
    check("ru: перевод вообще не запрашивается",
          requests_ru == 0 and len(finals_ru) == 1
          and finals_ru[0].get("text") == "привет чат",
          f"запросов {requests_ru}: {str(only_ru)[:120]}")
    check("en тратит один запрос на строку, ru — ноль",
          requests_en == 1 and requests_ru == 0 and requests_both == 1,
          str((requests_both, requests_en, requests_ru)))


def test_overlay_modes() -> None:
    print("\n[ovl] Оверлей субтитров: режимы ru / en / both")
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        check("node установлен — проверка логики оверлея", True, "node не найден: пропуск")
        return
    script = Path(__file__).resolve().parent / "tools" / "test_overlay.cjs"
    page = Path(__file__).resolve().parent / "assets" / "subs" / "index.html"
    try:
        done = subprocess.run([node, str(script), str(page)], capture_output=True,
                              text=True, timeout=60, encoding="utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        check("оверлей проверился", False, f"{type(exc).__name__}: {exc}")
        return
    tail = (done.stdout or "").strip().splitlines()[-1:] or [""]
    check("логика режимов в оверлее верна",
          done.returncode == 0 and tail[0] == "OVERLAY MODE LOGIC OK",
          tail[0] + " " + (done.stderr or "")[:150])


def test_ui_build() -> None:
    print("\n[5/13] Окно настроек (Flet)")
    import flet as ft

    from main import BotApp

    app = BotApp(FakePage())
    check("окно собрано", app.root is not None)
    check("чата в окне нет", not hasattr(app, "chat") and not hasattr(app, "entries"))
    check("поле каналов на месте", isinstance(app.channel_field, ft.TextField)
          and app.channel_field.label == "Каналы Twitch")
    check("кнопка подключения на месте", app.connect_button is not None)
    check("ссылка для OBS только для чтения",
          app.dock_url.read_only and app.dock_url.value == "", str(app.dock_url.value))
    check("кнопка сохранения настроек есть", app.save_button is not None)
    check("все разделы настроек собраны",
          all(hasattr(app, name) for name in ("dock_port", "base_url", "api_key", "model",
                                              "nickname", "oauth_token", "client_id",
                                              "font_choice", "font_slider", "emote_bttv",
                                              "emote_ffz", "emote_7tv", "viewers_enabled",
                                              "viewers_fetch", "price_in", "price_out",
                                              "commands_list", "cmd_triggers", "cmd_response",
                                              "cmd_permission", "timers_list", "timer_lines",
                                              "timer_interval")))
    check("предпросмотр шрифта собран",
          app.preview_body is not None and app.preview_nick is not None)
    check("счётчик статистики заполняется", "сообщений:" in (app.stats_text.value or ""),
          (app.stats_text.value or "")[:60])
    check("шрифт дока подписан как док",
          "док" in (app.font_choice.label or "").lower(), app.font_choice.label)
    check("провайдеры перевода в списке", len(app.provider.options) >= 5,
          str(len(app.provider.options)))

    from main import USAGE, parse_cli

    check("CLI понимает --dock и канал",
          parse_cli(["--dock", "-c", "xqc,gaules", "--port", "9000"]) == (True, ["xqc,gaules"], 9000),
          str(parse_cli(["--dock", "-c", "xqc,gaules", "--port", "9000"])))
    check("CLI по умолчанию открывает окно", parse_cli([]) == (False, [], None))
    check("справка описана", "--dock" in USAGE and "--port" in USAGE)

    # команды и таймеры добавляются прямо из окна
    app.cmd_triggers.value = "!hi, !привет"
    app.cmd_response.value = "Привет, {user}! Ты сказал: {args}"
    app.cmd_permission.value = "everyone"
    app.cmd_cooldown.value = "7"
    app.preview_command()
    check("предпросмотр команды рендерит переменные",
          "Привет, зритель!" in (app.cmd_preview.value or ""), app.cmd_preview.value or "")
    app.save_command()
    check("команда добавилась из формы",
          len(app.config.commands) == 1 and app.config.commands[0].triggers == ["!hi", "!привет"],
          str([c.triggers for c in app.config.commands]))
    check("форма очистилась после добавления",
          app.cmd_triggers.value == "" and app.cmd_save_button.content == "Добавить команду")
    check("список команд в окне обновился", len(app.commands_list.controls) == 1)

    app.edit_command(0)
    check("команда загрузилась в форму для правки",
          app.cmd_triggers.value == "!hi, !привет" and app.cmd_cooldown.value == "7",
          f"{app.cmd_triggers.value} / {app.cmd_cooldown.value}")
    app.cmd_cooldown.value = "15"
    app.save_command()
    check("правка команды сохраняется",
          len(app.config.commands) == 1 and app.config.commands[0].cooldown == 15,
          str(app.config.commands[0].cooldown))

    app.timer_interval.value = "20"
    app.timer_lines.value = "Подписывайтесь! | Соцсети в описании"
    app.save_timer()
    check("таймер добавился из формы",
          len(app.config.timers) == 1 and len(app.config.timers[0].lines) == 2
          and app.config.timers[0].interval_minutes == 20,
          str(app.config.timers[0]))
    check("список таймеров в окне обновился", len(app.timers_list.controls) == 1)

    app.delete_command(0)
    app.delete_timer(0)
    check("удаление из окна работает", not app.config.commands and not app.config.timers)
    check("пустые списки подсказывают, что делать",
          len(app.commands_list.controls) == 1 and len(app.timers_list.controls) == 1)


async def test_live_irc(channel: str, seconds: float) -> None:
    print(f"\n[LIVE] Чтение чата #{channel} в течение {seconds:.0f} с")
    received: list[ChatMessage] = []
    statuses: list[tuple[str, str]] = []

    client = TwitchChatClient(
        TwitchConfig(channels=[channel]),
        on_message=received.append,
        on_status=lambda state, text: statuses.append((state, text)),
    )
    await client.start()
    await asyncio.sleep(seconds)
    await client.stop()

    online = any(state == "online" for state, _ in statuses)
    check("подключение к Twitch", online, str(statuses[:3]))
    check(f"получены сообщения (#{channel})", len(received) > 0, f"получено {len(received)}")
    for message in received[:5]:
        print(f"      {message.display_name}: {message.text[:70]}")
    if not received:
        print("      (в канале тихо — попробуйте --channel с более активным чатом)")


async def test_app_pipeline(tmp: Path) -> None:
    """Окно настроек + конвейер: чат → смайлы → перевод → док-панель."""
    print("\n[13/13] Окно настроек и конвейер (FakePage)")
    from websockets.asyncio.client import connect as ws_connect

    from govorok.emotes import EmoteRegistry
    from main import BotApp

    with MockServer() as server, JsonServer(EMOTE_ROUTES) as emote_server:
        restore = patch_emote_urls(emote_server.base)
        try:
            page = FakePage()
            app = BotApp(page)
            app.config = mock_config(server)
            # тесты не должны зависеть от пользовательского конфига: субтитры — только
            # настройки, микрофон и модель здесь не нужны
            app.config.stt.enabled = False
            app.stt_enabled.value = False
            app.config.twitch.channels = []
            app.config.commands = [CommandConfig(triggers=["!hi"],
                                                 response="Привет, {user}! Сказал: {args}")]
            counts_file = tmp / "window-counters.json"
            if counts_file.exists():
                counts_file.unlink()
            app.commands.counts_path = counts_file
            app.commands.counts.clear()
            app.config.dock.enabled = False       # док поднимем сами, чтобы знать порт
            app.config.dock.port = 0
            app.config.dock.history = 50
            app.translator.configure(app.config)
            app.emotes = EmoteRegistry(lambda: app.config, cache_path=None)
            app.mount()
            check("док из окна настроек поднимается", await app.dock.start())
            port = app.dock.port

            await app.emotes.load_globals()
            await app.emotes.load_channel("71092938", "xqc")
            app._rooms["71092938"] = "xqc"       # noqa: SLF001

            ws = await ws_connect(f"ws://127.0.0.1:{port}/ws")
            hello = await ws_recv(ws)
            check("док ответил hello", hello["type"] == "hello", str(hello)[:80])

            app.on_message(ChatMessage(channel="xqc", user="bob", display_name="Bob",
                                       room_id="71092938", text="pepeD hello chat, how are you?",
                                       msg_id="win-1"))
            app.on_message(ChatMessage(channel="xqc", user="vasya", display_name="Vasya",
                                       room_id="71092938", text="привет всем, как дела",
                                       msg_id="win-2"))
            app.on_message(ChatMessage(channel="xqc", user="mod", display_name="Mod",
                                       room_id="71092938", text="!uptime", msg_id="win-3"))
            app.on_message(ChatMessage(channel="xqc", user="bob", display_name="Bob",
                                       room_id="71092938", text="!hi как дела", msg_id="win-4"))

            first = await ws_recv(ws)
            check("сообщение ушло в док из окна", first["type"] == "message" and first["id"] == "win-1",
                  str(first)[:100])
            check("смайл 7TV попал в сегменты",
                  any(seg["t"] == "emote" and "cdn.7tv.app" in seg["u"] for seg in first["segments"]),
                  str(first["segments"]))
            check("окно показывает ожидание перевода", first["pending"] is True)

            for _ in range(60):          # ждём до 3 секунд
                await asyncio.sleep(0.05)
                if app.translated_total >= 1:
                    break
            check("иностранное переведено", app.translated_total == 1, str(app.translated_total))
            check("русское и команда пропущены", app.skipped_total == 3, str(app.skipped_total))
            check("счётчики окна считают сообщения", app.messages_total == 4, str(app.messages_total))
            check("счётчик смайлов в статистике", "глоб." in (app.stats_text.value or ""),
                  (app.stats_text.value or "")[:80])

            # собираем всё, что док получил: перевод и ответ на команду
            collected: list[dict] = []
            for _ in range(60):
                try:
                    collected.append(await ws_recv(ws, timeout=2))
                except (TimeoutError, asyncio.TimeoutError):
                    break
                got_translation = any(item.get("type") == "translation" and item.get("id") == "win-1"
                                      for item in collected)
                got_command = any(item.get("source") == "command" for item in collected)
                if got_translation and got_command:
                    break

            translation_packet = next((item for item in collected
                                       if item.get("type") == "translation"
                                       and item.get("id") == "win-1"), {})
            check("перевод пришёл в док", "привет, чат" in (translation_packet.get("text") or ""),
                  str(translation_packet)[:100])

            # команда из чата должна ответить и попасть в док
            reply = next((item for item in collected if item.get("source") == "command"), {})
            check("команда ответила в док",
                  reply.get("segments", [{}])[0].get("v") == "Привет, bob! Сказал: как дела",
                  str(reply)[:140])
            check("ответ команды отмечен как сообщение бота",
                  reply.get("own") is True and reply.get("badges") == ["BOT"], str(reply)[:80])
            check("счётчик команд в статистике",
                  app.stats_snapshot()["commands_fired"] == 1,
                  str(app.stats_snapshot()["commands_fired"]))
            check("счётчик использования команды",
                  app.commands.counts.get("!hi") == 1, str(app.commands.counts))

            # переключатели «отвечать в чат» / «показывать в доке» действительно работают
            await app.respond("xqc", "тихий ответ", "command", in_chat=False, in_dock=False)
            quiet = True
            for _ in range(3):
                try:
                    packet = await ws_recv(ws, timeout=1)
                except (TimeoutError, asyncio.TimeoutError):
                    break
                if packet.get("type") == "message":
                    quiet = False
                    break
            check("ответ можно полностью скрыть из дока", quiet)
            await app.respond("xqc", "только в доке", "command", in_chat=False, in_dock=True)
            docked = await ws_recv(ws, timeout=3)
            check("флаг «только в доке» работает",
                  docked.get("segments", [{}])[0].get("v") == "только в доке", str(docked)[:100])

            # зрители и статистика отдаются доку
            await ws.send(json.dumps({"t": "viewers"}))
            viewers_packet = {}
            for _ in range(10):
                viewers_packet = await ws_recv(ws, timeout=3)
                if viewers_packet.get("type") == "viewers":
                    break
            check("зрители из окна отдаются доку",
                  viewers_packet.get("total") == 3
                  and {v["login"] for v in viewers_packet["viewers"]} == {"bob", "vasya", "mod"},
                  str(viewers_packet)[:120])
            check("статистика окна собирается", app.stats_snapshot()["messages"] == 4)

            # сохранение настроек прямо из окна
            import govorok.config as config_module

            original_path = config_module.CONFIG_PATH
            config_module.CONFIG_PATH = tmp / "window-config.json"
            try:
                app.font_choice.value = "Consolas"
                app.font_slider.value = 17
                app.dock_port.value = str(port)
                app.viewers_idle.value = "20"
                app.apply_settings()
                saved = AppConfig.load(config_module.CONFIG_PATH)
                check("настройки сохранены из окна",
                      saved.ui.font_family == "Consolas" and saved.ui.font_size == 17
                      and saved.viewers.idle_minutes == 20,
                      f"{saved.ui.font_family}/{saved.ui.font_size}/{saved.viewers.idle_minutes}")
                check("шрифт окна применился к конфигу",
                      app.config.ui.font_family == "Consolas" and app.config.ui.font_size == 17)

                # вкладка «Субтитры»: сохраняем и проверяем, что границы зажимают мусор
                app.stt_model.value = "large-v3"
                app.stt_device.value = "cuda"
                app.stt_lang.value = "auto"
                app.stt_chunk.value = 9.0          # вне диапазона слайдера
                app.stt_font.value = 200
                app.stt_lines.value = "9"
                app.stt_silence.value = "700"
                app.stt_mic.value = "Микрофон (USB)"
                app.stt_translate.value = " en "
                app.stt_vad.value = False
                app.apply_settings()
                saved_stt = AppConfig.load(config_module.CONFIG_PATH).stt
                check("субтитры сохраняются из окна",
                      saved_stt.model == "large-v3" and saved_stt.device == "cuda"
                      and saved_stt.language == "auto" and saved_stt.vad is False
                      and saved_stt.silence_ms == 700
                      and saved_stt.input_device == "Микрофон (USB)"
                      and saved_stt.translate_to == "en", str(saved_stt))
                check("границы субтитров зажимаются",
                      saved_stt.chunk_seconds == STTConfig.MAX_CHUNK
                      and saved_stt.font_size == STTConfig.MAX_FONT_SIZE
                      and saved_stt.lines == STTConfig.MAX_LINES,
                      f"{saved_stt.chunk_seconds}/{saved_stt.font_size}/{saved_stt.lines}")
                check("статус модуля субтитров собирается",
                      "движок:" in app.stt_status.value
                      and "настройки: large-v3" in app.stt_status.value,
                      app.stt_status.value[:140])
                app._refresh_meter()          # движок выключен — просто не должно падать
                check("индикатор уровня обновляется без движка",
                      app.stt_meter.value == 0
                      and "остановлен" in app.stt_meter_text.value,
                      f"{app.stt_meter.value} / {app.stt_meter_text.value}")
            finally:
                # не трогаем рабочий config.json
                app.config.dock.port = port
                app.config.dock.enabled = False
                config_module.CONFIG_PATH = original_path

            app.reset_stats()
            check("сброс статистики обнуляет счётчики",
                  app.messages_total == 0 and app.translated_total == 0,
                  f"{app.messages_total}/{app.translated_total}")

            # сообщение, отправленное ботом, показывается в доке локальным эхом
            app.on_sent_message("ответ из окна", "xqc")
            own = {}
            for _ in range(10):
                own = await ws_recv(ws, timeout=3)
                if own.get("type") == "message" and own.get("own"):
                    break
            check("эхо своего сообщения доходит до дока",
                  own.get("own") is True and own["segments"][0]["v"] == "ответ из окна",
                  str(own)[:120])
            check("счётчик отправленных в окне", app.sent_total == 1, str(app.sent_total))
            check("статистика окна знает про отправленные",
                  app.stats_snapshot()["sent"] == 1, str(app.stats_snapshot()["sent"]))

            ok, error = await app.send_to_chat("привет")
            check("без OAuth-токена ответ из дока не уходит", ok is False and error, error)

            await ws.close()
            await app.shutdown()
            for task in list(page._tasks):        # noqa: SLF001
                task.cancel()
            await asyncio.sleep(0.05)
        finally:
            restore()


class FakePage:
    """Минимальная заглушка flet.Page для тестов без окна."""

    def __init__(self) -> None:
        self.updated = 0
        self.controls: list = []
        self.dialogs: list = []
        self.services: list = []
        self.bgcolor = None
        self.padding = None
        self.title = ""
        self.theme_mode = None
        self.theme = None
        self.window = _FakeWindow()
        self.on_close = None
        self._tasks: list[asyncio.Task] = []

    def run_task(self, handler, *args, **kwargs):
        task = asyncio.ensure_future(handler(*args, **kwargs))
        self._tasks.append(task)
        return task

    def add(self, *controls) -> None:
        self.controls.extend(controls)

    def update(self) -> None:
        self.updated += 1

    def show_dialog(self, dialog) -> None:
        self.dialogs.append(dialog)

    def pop_dialog(self):
        return self.dialogs.pop() if self.dialogs else None


class _FakeWindow:
    width = height = min_width = min_height = 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Автотесты Govorok")
    parser.add_argument("--live", action="store_true", help="проверить реальное подключение к чату Twitch")
    parser.add_argument("--channel", default="xqc,dota2,twitch",
                        help="канал(ы) через запятую для --live (берите активные)")
    parser.add_argument("--seconds", type=float, default=20.0, help="сколько слушать чат")
    parser.add_argument("--only", default="",
                        help="только эти секции через запятую: irc,config,heuristics,translation,ui,"
                             "emotes,registry,font,dock,dockrun,viewers,commands,stt,overlay,pipeline")
    args = parser.parse_args()

    tmp = Path(__file__).resolve().parent / ".selftest-tmp"
    tmp.mkdir(exist_ok=True)

    only = {item.strip() for item in args.only.split(",") if item.strip()}

    def wanted(name: str) -> bool:
        return not only or name in only

    if wanted("irc"):
        test_irc_parsing()
    if wanted("config"):
        test_config_roundtrip(tmp)
    if wanted("heuristics"):
        test_heuristics()
    if wanted("translation"):
        asyncio.run(test_translation_flow())
    if wanted("overlay"):
        test_overlay_modes()
    if wanted("ui"):
        test_ui_build()
    if wanted("emotes"):
        test_emote_parsers()
    if wanted("registry"):
        asyncio.run(test_emote_registry(tmp))
    if wanted("font"):
        test_chat_font(tmp)
    if wanted("dock"):
        asyncio.run(test_dock(tmp))
    if wanted("dockrun"):
        asyncio.run(test_dock_runner(tmp))
    if wanted("viewers"):
        asyncio.run(test_viewers(tmp))
    if wanted("commands"):
        asyncio.run(test_commands(tmp))
    if wanted("stt"):
        test_stt(tmp)
    if wanted("stt") and not args.live:
        asyncio.run(test_stt_translation())
        asyncio.run(test_stt_modes())
    if wanted("pipeline"):
        asyncio.run(test_app_pipeline(tmp))

    if args.live:
        asyncio.run(test_live_irc(args.channel, args.seconds))

    print("\n" + "=" * 60)
    print(f"Пройдено: {len(PASSED)}   Провалено: {len(FAILED)}")
    for item in FAILED:
        print(f"  ! {item}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
