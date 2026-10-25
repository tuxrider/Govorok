"""Статистика сессии для окна бота и док-панели OBS.

Считаем то, что интересно стримеру: сколько сообщений прошло, сколько
переведено, какие языки в чате и во что обходится API.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any, Iterable

LANGUAGE_NAMES = {
    "ru": "русский",
    "en": "английский",
    "es": "испанский",
    "pt": "португальский",
    "de": "немецкий",
    "fr": "французский",
    "it": "итальянский",
    "pl": "польский",
    "tr": "турецкий",
    "uk": "украинский",
    "ja": "японский",
    "ko": "корейский",
    "zh": "китайский",
    "ar": "арабский",
    "hi": "хинди",
    "nl": "нидерландский",
    "sv": "шведский",
    "fi": "финский",
    "cs": "чешский",
    "ro": "румынский",
    "th": "тайский",
    "vi": "вьетнамский",
    "id": "индонезийский",
    "??": "не определено",
}


def language_title(code: str) -> str:
    """«en» → «английский (en)»."""
    code = (code or "").strip().lower()
    if not code:
        return "не определено"
    name = LANGUAGE_NAMES.get(code)
    return f"{name} ({code})" if name else code.upper()


def top_languages(counter: Counter | dict, limit: int = 6) -> list[dict[str, Any]]:
    """Топ языков с долями в процентах."""
    items = counter.items() if isinstance(counter, Counter) else (counter or {}).items()
    pairs = [(str(lang or "??"), int(count or 0)) for lang, count in items if count]
    pairs.sort(key=lambda item: (-item[1], item[0]))
    total = sum(count for _, count in pairs)
    return [
        {"code": code, "title": language_title(code), "count": count,
         "share": round(count / total * 100, 1) if total else 0.0}
        for code, count in pairs[:limit]
    ]


def estimate_cost(tokens_in: int, tokens_out: int, price_in: float, price_out: float) -> float | None:
    """Оценка расхода: None, если цены не заданы."""
    if price_in <= 0 and price_out <= 0:
        return None
    cost = (tokens_in or 0) / 1_000_000 * max(0.0, price_in)
    cost += (tokens_out or 0) / 1_000_000 * max(0.0, price_out)
    return round(cost, 4)


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes:02d} мин"
    if minutes:
        return f"{minutes} мин {secs:02d} с"
    return f"{secs} с"


def stats_payload(
    *,
    started_at: float,
    messages: int,
    translated: int,
    skipped: int,
    errors: int,
    translator,
    config,
    channels: Iterable[str] = (),
    clients: int = 0,
    can_send: bool = False,
    sent: int = 0,
    commands_fired: int = 0,
    timers_fired: int = 0,
    emotes=None,
) -> dict[str, Any]:
    """Собрать снимок статистики для страницы дока."""
    usage = translator.usage_snapshot()
    llm = config.llm
    cost = estimate_cost(usage["prompt_tokens"], usage["completion_tokens"],
                         llm.price_in_per_mtok, llm.price_out_per_mtok)
    uptime = max(0.0, time.time() - started_at)
    rate = (translated / messages * 100) if messages else 0.0

    return {
        "type": "stats",
        "uptime": round(uptime, 1),
        "uptime_text": format_duration(uptime),
        "started_at": started_at,
        "messages": messages,
        "translated": translated,
        "skipped": skipped,
        "errors": errors,
        "sent": sent,
        "commands_fired": commands_fired,
        "timers_fired": timers_fired,
        "translated_share": round(rate, 1),
        "requests": usage["requests"],
        "cache_hits": usage["cache_hits"],
        "cache_size": usage["cache_size"],
        "failures": usage["failures"],
        "prompt_tokens": usage["prompt_tokens"],
        "completion_tokens": usage["completion_tokens"],
        "tokens": usage["tokens"],
        "characters": usage["characters"],
        "avg_latency_ms": usage["avg_latency_ms"],
        "languages": top_languages(usage["languages"]),
        "cost": cost,
        "currency": llm.currency or "USD",
        "model": llm.model,
        "provider": llm.base_url,
        "channels": list(channels),
        "clients": clients,
        "can_send": can_send,
        "emotes": emotes or {},
    }
