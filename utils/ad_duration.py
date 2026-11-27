"""
Срок размещения рекламы из названия/описания лота.

«Пост в канале на 24 часа» → 24 ч; «Реклама в истории» → 24 ч (история в Telegram
живёт сутки); «Закреп на 3 дня» → 72 ч; «Навсегда / без удаления» → навсегда.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

FOREVER = -1


@dataclass
class Duration:
    hours: int                 # FOREVER (-1) — навсегда
    where: str                 # «в канале» / «в истории»
    source: str = ""           # откуда взяли: «название», «описание», «по умолчанию»…
    unit: str = ""             # в каких единицах было в лоте: hour/day/week/month
    top_hours: int = 0         # «1/24» — 1 час в топе (без других постов после рекламы)
    pin: bool = False          # в лоте есть «закреп»
    pin_hours: int = 0         # сколько держать закреп (0 — весь срок)

    @property
    def forever(self) -> bool:
        return self.hours == FOREVER


_NUM = r"(\d{1,4})"
_UNITS = [
    (re.compile(_NUM + r"\s*(?:-?\s*)(?:ч\b|час(?:а|ов)?\b|h\b|hours?\b|hrs?\b)", re.I), 1, "hour"),
    (re.compile(_NUM + r"\s*(?:-?\s*)(?:сут(?:ок|ки)?\b|д\b|дн\b|дн(?:я|ей)\b|день\b|days?\b|d\b)", re.I), 24, "day"),
    (re.compile(_NUM + r"\s*(?:-?\s*)(?:нед\b|недел(?:я|и|ь|ю)\b|weeks?\b|w\b)", re.I), 24 * 7, "week"),
    (re.compile(_NUM + r"\s*(?:-?\s*)(?:мес\b|месяц(?:а|ев)?\b|months?\b)", re.I), 24 * 30, "month"),
]
_WORDS = [
    (re.compile(r"\b(?:навсегда|навечно|без удаления|не удаля\w*|forever|permanent)\b", re.I), FOREVER, ""),
    (re.compile(r"\b(?:сутки|на сутки)\b", re.I), 24, "hour"),
    (re.compile(r"\b(?:неделю|неделя|на неделю|week)\b", re.I), 24 * 7, "week"),
    (re.compile(r"\b(?:месяц|на месяц|month)\b", re.I), 24 * 30, "month"),
]
_STORY = re.compile(r"(?:истори|сторис|stories|story|сторь)", re.I)


def _norm(text: str) -> str:
    return (text or "").replace("ё", "е").replace("Ё", "Е").replace("\xa0", " ")


def find_duration(text: str):
    """(часы или FOREVER, единица) из текста; None — срока в тексте нет."""
    t = _norm(text)
    best = None  # (позиция, часы, единица) — берём самое раннее упоминание
    for rx, mult, unit in _UNITS:
        for m in rx.finditer(t):
            n = int(m.group(1))
            if 0 < n <= 10000:
                if best is None or m.start() < best[0]:
                    best = (m.start(), n * mult, unit)
                break
    for rx, hours, unit in _WORDS:
        m = rx.search(t)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), hours, unit)
    return (best[1], best[2]) if best else None


# «1/24», «2/48», «1/0» — формат рекламы: часы в топе / часы в ленте (0 — без удаления)
_TOP_FEED = re.compile(r"(?<![\d.,/])(\d{1,2})\s*/\s*(\d{1,3})(?![\d.,/])")
_TOP_WORDS = re.compile(r"\bтоп\w*\s*(?:на\s*)?(\d{1,2})\s*(?:ч|час)", re.I)
_PIN = re.compile(r"закреп|закрепл|\bpin", re.I)
_PIN_HOURS = re.compile(r"закреп\w*\s*(?:на\s*)?(\d{1,4})\s*(ч\b|час\w*|сут\w*|д\b|дн\w*|день)", re.I)


def _top_feed(text: str):
    """«1/24» → (1, 24); «1/0» → (1, FOREVER); None — формата нет."""
    m = _TOP_FEED.search(_norm(text))
    if not m:
        return None
    top, feed = int(m.group(1)), int(m.group(2))
    # правая часть — реальный срок в ленте (0 — навсегда, 6/12/24/48/72…), иначе это не формат рекламы
    if top < 1 or top > 24 or (feed and (feed < 6 or feed % 6 or feed < top)):
        return None
    return top, (FOREVER if feed == 0 else feed)


def _extras(full: str, d: Duration) -> Duration:
    t = _norm(full)
    if not d.top_hours:
        m = _TOP_WORDS.search(t)
        if m:
            d.top_hours = int(m.group(1))
    if _PIN.search(t):
        d.pin = True
        m = _PIN_HOURS.search(t)
        if m:
            n = int(m.group(1))
            d.pin_hours = n * (24 if not m.group(2).lower().startswith("ч") else 1)
    return d


def detect(title: str, description: str = "", params: str = "", default_hours: Optional[int] = None) -> Optional[Duration]:
    """Ищет срок сначала в названии, потом в параметрах и описании лота."""
    full = " ".join([title or "", params or "", description or ""])
    where = "в истории" if _STORY.search(_norm(title or "") or _norm(full)) else "в канале"
    for src, text in (("название лота", title), ("параметры лота", params), ("описание лота", description)):
        tf = _top_feed(text)
        if tf:
            return _extras(full, Duration(tf[1], where, src, "hour", top_hours=tf[0]))
        found = find_duration(text)
        if found is not None:
            return _extras(full, Duration(found[0], where, src, found[1]))
    if where == "в истории":
        return _extras(full, Duration(24, where, "история в Telegram живёт 24 часа", "hour"))
    if default_hours:
        return _extras(full, Duration(default_hours, where, "срок по умолчанию"))
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Русские окончания
# ──────────────────────────────────────────────────────────────────────────────

def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    if n == 1:
        return one
    if 2 <= n <= 4:
        return few
    return many


def _split(hours: int, unit: str = ""):
    """Как писать срок: в единицах из лота, иначе — самыми крупными целыми."""
    sizes = {"hour": 1, "day": 24, "week": 24 * 7, "month": 24 * 30}
    if unit in sizes and hours % sizes[unit] == 0:
        return hours // sizes[unit], unit
    if hours % (24 * 30) == 0:
        return hours // (24 * 30), "month"
    if hours % (24 * 7) == 0:
        return hours // (24 * 7), "week"
    if hours % 24 == 0 and hours > 24:
        return hours // 24, "day"
    return hours, "hour"


_GEN = {  # родительный падеж: «по истечении 1 часа / 24 часов»
    "hour": ("часа", "часов"), "day": ("дня", "дней"),
    "week": ("недели", "недель"), "month": ("месяца", "месяцев"),
}
_NOM = {  # именительный: «24 часа»
    "hour": ("час", "часа", "часов"), "day": ("день", "дня", "дней"),
    "week": ("неделя", "недели", "недель"), "month": ("месяц", "месяца", "месяцев"),
}


def human(hours: int, unit: str = "") -> str:
    """24 → «24 часа», 72 → «3 дня», -1 → «навсегда»."""
    if hours == FOREVER:
        return "навсегда"
    n, unit = _split(hours, unit)
    return f"{n} {_plural(n, *_NOM[unit])}"


def genitive(hours: int, unit: str = "") -> str:
    """24 → «24 часов», 1 → «1 часа», 72 → «3 дней»."""
    n, unit = _split(hours, unit)
    one, other = _GEN[unit]
    return f"{n} {one if (n % 10 == 1 and n % 100 != 11) else other}"


def until_phrase(d: Duration) -> str:
    """«сейчас или по истечении 24 часов» / «уже сейчас» (если навсегда)."""
    if d.forever:
        return "уже сейчас"
    return f"сейчас или по истечении {genitive(d.hours, d.unit)}"
