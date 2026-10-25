"""Список шрифтов, установленных в системе — для настройки шрифта чата.

На Windows семейства читаются из реестра (HKLM + HKCU), в реестре каждая
гарнитура лежит отдельной записью вида «Arial Bold (TrueType)», поэтому
названия чистятся от начертаний и приводятся к семейству.

На других ОС возвращается небольшой список распространённых семейств, а если
установленного шрифта нет — Flutter просто возьмёт шрифт по умолчанию.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache

#: что показываем в начале списка (если шрифт установлен)
POPULAR: tuple[str, ...] = (
    "Segoe UI",
    "Consolas",
    "Cascadia Mono",
    "Cascadia Code",
    "JetBrains Mono",
    "Fira Code",
    "Verdana",
    "Tahoma",
    "Trebuchet MS",
    "Arial",
    "Calibri",
    "Cambria",
    "Georgia",
    "Times New Roman",
    "Courier New",
    "Lucida Console",
    "Comic Sans MS",
    "Impact",
    "Bahnschrift",
    "Segoe Print",
    "Segoe Script",
    "Franklin Gothic Medium",
    "Microsoft Sans Serif",
    "Arial Narrow",
    "MS Gothic",
)

#: начертания, которые надо отрезать от названия гарнитуры
STYLE_WORDS: tuple[str, ...] = (
    "Bold Italic", "Bold Oblique", "SemiBold Italic", "Semi Bold Italic",
    "Light Italic", "Extra Light Italic", "Ultra Light Italic",
    "Bold", "Italic", "Oblique", "Regular", "SemiBold", "Semi Bold", "DemiBold",
    "Demi Bold", "ExtraLight", "Extra Light", "UltraLight", "Ultra Light",
    "ExtraBold", "Extra Bold", "UltraBold", "Ultra Bold", "Black", "Heavy",
    "Medium", "Light", "Thin", "Book", "Condensed", "Semi Condensed", "Expanded",
)

_SUFFIX_RE = re.compile(
    r"\s*\((?:TrueType|OpenType|All res|VGA res|Plotter|Type\s*1|OTF|TTF)\)\s*$",
    re.IGNORECASE,
)
_STYLES_SORTED = tuple(sorted(STYLE_WORDS, key=len, reverse=True))
MAX_FONTS = 400


def normalize_family(name: str) -> str:
    """«Arial Bold (TrueType)» -> «Arial»."""
    family = _SUFFIX_RE.sub("", str(name or "")).strip()
    # отрезаем начертания, пока они есть в конце названия
    changed = True
    while changed and family:
        changed = False
        for style in _STYLES_SORTED:
            suffix = f" {style}"
            if family.lower().endswith(suffix.lower()) and len(family) > len(suffix):
                family = family[: -len(suffix)].strip()
                changed = True
                break
    return family


def _windows_families() -> list[str]:
    """Семейства из реестра Windows (машинные и пользовательские)."""
    import winreg  # доступен только на Windows

    names: list[str] = []
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    )
    for root, path in roots:
        try:
            with winreg.OpenKey(root, path) as key:
                count = winreg.QueryInfoKey(key)[1]
                for index in range(count):
                    try:
                        value = winreg.EnumValue(key, index)[0]
                    except OSError:
                        continue
                    names.append(value)
        except OSError:
            continue
    return names


def _fallback_families() -> list[str]:
    return list(POPULAR)


def collect_families(raw_names) -> list[str]:
    """Почистить, отфильтровать и отсортировать названия шрифтов."""
    seen: set[str] = set()
    families: list[str] = []
    for raw in raw_names:
        family = normalize_family(raw)
        if not family or family.startswith("@") or len(family) > 60:
            continue
        if any(ch in family for ch in "(),;"):
            continue
        key = family.casefold()
        if key in seen:
            continue
        seen.add(key)
        families.append(family)

    popular_order = {name.casefold(): index for index, name in enumerate(POPULAR)}
    pinned = sorted([f for f in families if f.casefold() in popular_order],
                    key=lambda name: popular_order[name.casefold()])
    rest = sorted([f for f in families if f.casefold() not in popular_order],
                  key=lambda name: name.casefold())
    return (pinned + rest)[:MAX_FONTS]


@lru_cache(maxsize=1)
def list_system_fonts() -> list[str]:
    """Список установленных семейств (кэшируется на время работы)."""
    raw: list[str] = []
    if sys.platform.startswith("win"):
        try:
            raw = _windows_families()
        except Exception:  # noqa: BLE001 - реестр может быть недоступен
            raw = []
    families = collect_families(raw) if raw else []
    return families or _fallback_families()


def resolve_family(name: str) -> str:
    """Привести введённое имя к реально установленному (если совпало)."""
    wanted = str(name or "").strip()
    if not wanted:
        return ""
    for family in list_system_fonts():
        if family.casefold() == wanted.casefold():
            return family
    return wanted
