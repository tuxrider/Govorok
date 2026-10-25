"""Перевод сообщений чата через LLM API (любой OpenAI-совместимый endpoint).

Один запрос = одно сообщение. Модель сама определяет язык и возвращает
строгий JSON вида ``{"lang": "en", "translation": "..."}``.

Экономим запросы: кэш по тексту, эвристики (кириллица, команды, смайлы),
ограничение параллельности и лимит запросов в минуту.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass

import httpx

from .config import AppConfig

CYRILLIC_RE = re.compile(r"[а-яёА-ЯЁ]")
LETTER_WORD_RE = re.compile(r"[^\W\d_]{2,}", re.UNICODE)
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"\s+")
JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


class TranslationSkipped(Exception):
    """Сообщение не нужно переводить (эвристика)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class Translation:
    """Результат перевода одного сообщения."""

    text: str | None
    source_lang: str = ""
    target_lang: str = "ru"
    from_cache: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def has_cyrillic(text: str) -> bool:
    return bool(CYRILLIC_RE.search(text))


def looks_translatable(text: str) -> bool:
    """Есть ли в сообщении хоть одно слово из 2+ букв."""
    return bool(LETTER_WORD_RE.search(URL_RE.sub(" ", text)))


class Translator:
    """Асинхронный переводчик с кэшем и лимитами."""

    def __init__(self, config: AppConfig, *, on_log=None) -> None:
        self.config = config
        self.on_log = on_log
        self._client: httpx.AsyncClient | None = None
        self._semaphore: asyncio.Semaphore | None = None
        self._cache: OrderedDict[tuple[str, str, str], Translation] = OrderedDict()
        self._request_times: deque[float] = deque()
        self._lock = asyncio.Lock()

        # статистика за сессию (используется док-панелью и счётчиками окна)
        self.started_at = time.time()
        self.requests_sent = 0
        self.cache_hits = 0
        self.failures = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.characters = 0
        self.languages: Counter[str] = Counter()
        self.total_latency = 0.0
        self.latency_samples = 0

    # ------------------------------------------------------------------ API

    def configure(self, config: AppConfig) -> None:
        """Подхватить изменённые настройки без перезапуска приложения."""
        self.config = config
        self._semaphore = None  # пересоздадим с новой конкурентностью

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def clear_cache(self) -> None:
        self._cache.clear()

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    def should_translate(self, text: str) -> tuple[bool, str]:
        """Эвристика: стоит ли вообще тратить запрос к API."""
        cfg = self.config.translation
        stripped = text.strip()
        if not cfg.enabled:
            return False, "перевод выключен"
        if len(stripped) < cfg.min_length:
            return False, "слишком коротко"
        if len(stripped) > cfg.max_length:
            return False, "слишком длинно"
        if cfg.skip_commands and stripped.startswith("!"):
            return False, "команда"
        if cfg.skip_cyrillic and has_cyrillic(stripped):
            return False, "уже кириллица"
        if not looks_translatable(stripped):
            return False, "нет слов"
        if not self.config.is_translation_ready():
            return False, "не настроен API-ключ"
        return True, ""

    async def translate(self, text: str, *, force: bool = False) -> Translation:
        """Перевести текст. Исключения наружу не летят — ошибка в поле error."""
        stripped = WHITESPACE_RE.sub(" ", text).strip()
        llm = self.config.llm
        target = self.config.translation.target_lang

        if not force:
            allowed, reason = self.should_translate(stripped)
            if not allowed:
                return Translation(text=None, target_lang=target, error=None)

        key = (llm.model, target, stripped)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            self.cache_hits += 1
            return Translation(
                text=cached.text,
                source_lang=cached.source_lang,
                target_lang=cached.target_lang,
                from_cache=True,
            )

        await self._acquire_slot()
        try:
            if self._semaphore is None:
                self._semaphore = asyncio.Semaphore(max(1, llm.max_concurrency))
            async with self._semaphore:
                result = await self._request(stripped)
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            result = Translation(text=None, target_lang=target, error=f"{type(exc).__name__}: {exc}")

        if result.ok:
            self._store(key, result)
            if result.source_lang:
                self.languages[result.source_lang] += 1
            if not result.from_cache:
                self.characters += len(stripped)
        else:
            self._log(f"ошибка перевода: {result.error}")
        return result

    # ---------------------------------------------------------- статистика

    @property
    def tokens_total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def avg_latency_ms(self) -> int:
        if not self.latency_samples:
            return 0
        return int(self.total_latency / self.latency_samples * 1000)

    def usage_snapshot(self) -> dict:
        """Сколько потрачено на API за сессию."""
        return {
            "requests": self.requests_sent,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "cache_size": self.cache_size,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tokens": self.tokens_total,
            "characters": self.characters,
            "avg_latency_ms": self.avg_latency_ms,
            "languages": dict(self.languages),
        }

    def reset_usage(self) -> None:
        """Обнулить статистику (кэш переводов при этом остаётся)."""
        self.started_at = time.time()
        self.requests_sent = 0
        self.cache_hits = 0
        self.failures = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.characters = 0
        self.languages.clear()
        self.total_latency = 0.0
        self.latency_samples = 0

    # ------------------------------------------------------------ внутреннее

    def _log(self, text: str) -> None:
        if self.on_log:
            try:
                self.on_log(text)
            except Exception:  # noqa: BLE001
                pass

    def _store(self, key: tuple[str, str, str], value: Translation) -> None:
        limit = max(16, self.config.translation.cache_size)
        self._cache[key] = Translation(
            text=value.text,
            source_lang=value.source_lang,
            target_lang=value.target_lang,
        )
        self._cache.move_to_end(key)
        while len(self._cache) > limit:
            self._cache.popitem(last=False)

    async def _acquire_slot(self) -> None:
        """Не больше N запросов в минуту."""
        limit = max(1, self.config.llm.max_requests_per_minute)
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._request_times and now - self._request_times[0] > 60.0:
                    self._request_times.popleft()
                if len(self._request_times) < limit:
                    self._request_times.append(now)
                    return
                await asyncio.sleep(1.0)

    def _client_instance(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.config.llm.timeout),
                headers={"User-Agent": "Govorok/0.1 (+local)"},
            )
        return self._client

    def _payload(self, text: str, *, json_mode: bool) -> dict:
        llm = self.config.llm
        payload: dict = {
            "model": llm.model,
            "temperature": llm.temperature,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": self.config.system_prompt()},
                {"role": "user", "content": text},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return payload

    async def _request(self, text: str) -> Translation:
        llm = self.config.llm
        url = llm.chat_completions_url
        target = self.config.translation.target_lang
        if not url or not llm.api_key.strip():
            return Translation(text=None, target_lang=target, error="не задан base_url или api_key")

        headers = {
            "Authorization": f"Bearer {llm.api_key.strip()}",
            "Content-Type": "application/json",
        }
        client = self._client_instance()
        json_mode = llm.json_mode
        last_error = ""

        for attempt in range(3):
            started = time.perf_counter()
            try:
                response = await client.post(url, headers=headers, json=self._payload(text, json_mode=json_mode))
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(0.8 * (attempt + 1))
                continue

            if response.status_code in (400, 404, 422) and json_mode:
                # провайдер не поддерживает response_format — повторяем без него
                json_mode = False
                self._log("провайдер отклонил json_mode, повтор без response_format")
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue

            if response.status_code >= 400:
                detail = response.text[:200].replace("\n", " ")
                return Translation(text=None, target_lang=target, error=f"HTTP {response.status_code}: {detail}")

            try:
                data = response.json()
                content = data["choices"][0]["message"]["content"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                return Translation(text=None, target_lang=target, error=f"неожиданный ответ API: {exc}")

            self.requests_sent += 1
            self.total_latency += time.perf_counter() - started
            self.latency_samples += 1
            self._count_usage(data)
            return self._parse_content(str(content), original=text, target=target)

        self.failures += 1
        return Translation(text=None, target_lang=target, error=last_error or "не удалось выполнить запрос")

    def _count_usage(self, data: dict) -> None:
        """Собрать расход токенов из ответа (OpenAI, Ollama и совместимые)."""
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens", usage.get("prompt_eval_count", 0))
        completion = usage.get("completion_tokens", usage.get("eval_count", 0))
        try:
            self.prompt_tokens += int(prompt or 0)
            self.completion_tokens += int(completion or 0)
        except (TypeError, ValueError):
            pass

    @staticmethod
    def _parse_content(content: str, *, original: str, target: str) -> Translation:
        """Достать JSON из ответа модели (даже если он в ```-блоке)."""
        raw = content.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw).strip()

        payload: dict | None = None
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            match = JSON_BLOCK_RE.search(raw)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                    if isinstance(parsed, dict):
                        payload = parsed
                except json.JSONDecodeError:
                    payload = None

        if payload is None:
            # модель ответила просто текстом — считаем его переводом
            text = WHITESPACE_RE.sub(" ", raw).strip().strip('"')
            if not text or text.lower() == original.lower():
                return Translation(text=None, target_lang=target, source_lang="")
            return Translation(text=text, target_lang=target, source_lang="")

        lang = str(payload.get("lang") or "").strip().lower()[:8]
        value = payload.get("translation")
        if value is None:
            return Translation(text=None, target_lang=target, source_lang=lang)
        text = WHITESPACE_RE.sub(" ", str(value)).strip().strip('"')
        if not text or text.lower() == original.lower():
            return Translation(text=None, target_lang=target, source_lang=lang)
        if lang and lang.startswith(target[:2]):
            return Translation(text=None, target_lang=target, source_lang=lang)
        return Translation(text=text, target_lang=target, source_lang=lang)
