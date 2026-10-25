"""Разбор сообщения чата на сегменты: текст и смайлы.

Один источник правды для двух интерфейсов — окна на Flet и док-панели OBS.
Модуль ничего не знает про размеры: потребитель сам решает, каким кеглем
рисовать текст и во сколько пикселей превращать `em` у смайлов.
"""

from __future__ import annotations

from dataclasses import dataclass

from .emotes import EmoteRegistry
from .irc import ChatMessage, split_by_emotes

#: родные смайлы Twitch
TWITCH_EMOTE_URL = "https://static-cdn.jtvnw.net/emoticons/v2/{emote_id}/default/dark/1.0"


@dataclass(slots=True)
class Segment:
    """Кусок сообщения: либо текст, либо смайл."""

    kind: str                 # "text" | "emote"
    text: str = ""            # для текста — сам текст, для смайла — код
    url: str = ""             # ссылка на картинку смайла
    provider: str = ""        # twitch | bttv | ffz | 7tv
    width: int = 0            # исходные габариты смайла, если известны
    height: int = 0

    @property
    def aspect(self) -> float | None:
        """Соотношение сторон смайла (FFZ и 7TV его отдают)."""
        if self.width > 0 and self.height > 0:
            return self.width / self.height
        return None

    def as_dict(self) -> dict:
        if self.kind == "emote":
            return {"t": "emote", "v": self.text, "u": self.url, "p": self.provider}
        return {"t": "text", "v": self.text}


def build_segments(message: ChatMessage, emotes: EmoteRegistry | None = None) -> list[Segment]:
    """Нарезать сообщение на сегменты.

    Смайлы Twitch приходят с точными позициями (тег ``emotes``), сторонние
    ищутся по словам через реестр смайлов.
    """
    segments: list[Segment] = []

    def push_text(value: str) -> None:
        if not value:
            return
        if segments and segments[-1].kind == "text":
            segments[-1].text += value
        else:
            segments.append(Segment(kind="text", text=value))

    for kind, value in split_by_emotes(message.text, message.emotes):
        if kind == "emote":
            segments.append(Segment(kind="emote", text="", url=TWITCH_EMOTE_URL.format(emote_id=value),
                                    provider="twitch"))
            continue
        parts = emotes.resolve(value, message.room_id) if emotes else [("text", value)]
        for part_kind, part in parts:
            if part_kind == "emote" and getattr(part, "url", ""):
                segments.append(Segment(kind="emote", text=part.code, url=part.url,
                                        provider=part.provider,
                                        width=int(getattr(part, "width", 0) or 0),
                                        height=int(getattr(part, "height", 0) or 0)))
            else:
                push_text(str(part))

    if not segments:
        push_text(message.text)
    return segments
