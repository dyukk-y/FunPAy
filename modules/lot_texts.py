"""
Тексты и цены лотов: автообновление по расписанию + массовые правки с телефона.

Что умеет:
  1. «Живой» счётчик. В тексте лота есть блок вида
         📅 Актуально на 06.04.2026, 04:00 МСК:
         Мы уже успешно продали 532 рекламных размещений! 🚀
     Раз в неделю (день/время МСК задаются в меню) бот ставит свежую дату/время
     и прибавляет к числу случайное значение от inc_min до inc_max (3…10).
     Число ОДИНАКОВОЕ во всех лотах.
  2. Замена текста «найти → заменить» во всех лотах (разово или каждую неделю).
  3. Массовое изменение цен: поставить цену / +N / −N / +N% / −N%.
  4. Предпросмотр: показывает, что поменяется, ничего не сохраняя.

Надёжность:
  • ошибка на одном лоте не останавливает остальные и не роняет бота;
  • автозапуск идемпотентен: если в лотах уже стоит сегодняшняя дата, число
    второй раз не прибавляется (даже если бот перезапустился или конфиг потерялся);
  • между сохранениями — случайная пауза (антибан);
  • два обновления одновременно не запускаются.

Шаблон счётчика ищется «мягко»: важны только слова и места {date}/{time}/{count},
а эмодзи, знаки препинания, звёздочки и переносы строк могут отличаться.
"""
from __future__ import annotations

import asyncio
import html
import random
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from config.settings import LotReplaceRule, config_manager
from utils.logger import logger

# Москва без перехода на летнее время с 2014 г. — фиксированный UTC+3,
# чтобы не зависеть от tzdata на хостинге.
MSK = timezone(timedelta(hours=3), name="MSK")

WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

TEXT_FIELDS = (
    ("title_ru", "название"),
    ("title_en", "название EN"),
    ("description_ru", "описание"),
    ("description_en", "описание EN"),
    ("payment_msg_ru", "сообщение после оплаты"),
    ("payment_msg_en", "сообщение после оплаты EN"),
)

PLACEHOLDERS = {
    "date": r"(?P<date>\d{1,2}\.\d{1,2}\.\d{2,4})",
    "time": r"(?P<time>\d{1,2}:\d{2})",
    "count": r"(?P<count>\d+)",
}

# Сколько подряд ошибок на лотах терпим, прежде чем решить, что FunPay
# недоступен / разлогинил, и остановить прогон
MAX_CONSECUTIVE_ERRORS = 5

# Пауза между ЧТЕНИЯМИ лотов (сек). Сохранения — по настройкам delay_*_sec.
READ_DELAY = (0.6, 1.4)


def now_msk() -> datetime:
    return datetime.now(MSK)


# ──────────────────────────────────────────────────────────────────────────────
# Шаблон счётчика → регулярка
# ──────────────────────────────────────────────────────────────────────────────

class TemplateError(ValueError):
    pass


def compile_template(template: str) -> "re.Pattern[str]":
    """
    '📅 Актуально на {date}, {time} МСК:\\nМы уже успешно продали {count} …'
    → регулярка, где слова обязательны, а всё «не-слово» между ними гибкое.
    """
    parts = re.split(r"\{(date|time|count)\}", template)
    found = parts[1::2]
    if "count" not in found and "date" not in found:
        raise TemplateError("В шаблоне должен быть хотя бы {count} или {date}.")
    if len(found) != len(set(found)):
        raise TemplateError("Каждый из {date}, {time}, {count} можно указать только один раз.")

    out: List[str] = []
    for i, chunk in enumerate(parts):
        if i % 2 == 1:
            out.append(PLACEHOLDERS[chunk])
            continue
        words = re.findall(r"\w+", chunk)
        if not words:
            # между плейсхолдерами — только знаки (например ", ")
            if 0 < i < len(parts) - 1:
                out.append(r"\W*?")
            continue
        body = r"\W+".join(re.escape(w) for w in words)
        if i == 0:
            out.append(body + r"\W*")
        elif i == len(parts) - 1:
            out.append(r"\W*" + body)
        else:
            out.append(r"\W*" + body + r"\W*")
    return re.compile("".join(out), re.IGNORECASE)


def find_counters(text: str, rx: "re.Pattern[str]") -> List[Tuple[Optional[str], Optional[int]]]:
    """Все вхождения шаблона в тексте: [(дата, число), ...]."""
    res = []
    for m in rx.finditer(text or ""):
        gd = m.groupdict()
        cnt = gd.get("count")
        res.append((gd.get("date"), int(cnt) if cnt is not None else None))
    return res


def apply_counter(text: str, rx: "re.Pattern[str]", values: Dict[str, str]) -> Tuple[str, int]:
    """Подставляет values в группы date/time/count всех вхождений. → (текст, кол-во вхождений)."""
    if not text:
        return text, 0
    matches = list(rx.finditer(text))
    if not matches:
        return text, 0
    pieces = []
    for m in matches:
        for g in ("date", "time", "count"):
            if g in m.groupdict() and m.group(g) is not None and g in values:
                pieces.append((m.start(g), m.end(g), values[g]))
    pieces.sort(key=lambda x: x[0], reverse=True)
    for start, end, val in pieces:
        text = text[:start] + val + text[end:]
    return text, len(matches)


def apply_replaces(text: str, rules: List[LotReplaceRule]) -> Tuple[str, int]:
    n = 0
    for r in rules:
        if r.find and text and r.find in text:
            n += text.count(r.find)
            text = text.replace(r.find, r.replace)
    return text, n


# ──────────────────────────────────────────────────────────────────────────────
# Цены
# ──────────────────────────────────────────────────────────────────────────────

PRICE_OP_RX = re.compile(r"^\s*([=+\-]?)\s*(\d+(?:[.,]\d+)?)\s*(%?)\s*$")


def parse_price_op(raw: str) -> Optional[Tuple[str, float, bool]]:
    """
    '150' или '=150' → поставить 150; '+20' → +20; '-10%' → −10 %.
    → (знак, значение, проценты?) или None.
    """
    m = PRICE_OP_RX.match(raw or "")
    if not m:
        return None
    sign, val, pct = m.group(1) or "=", float(m.group(2).replace(",", ".")), bool(m.group(3))
    if sign == "=" and pct:
        return None
    return sign, val, pct


def calc_price(old: float, op: Tuple[str, float, bool]) -> float:
    sign, val, pct = op
    if sign == "=":
        new = val
    else:
        delta = old * val / 100 if pct else val
        new = old + delta if sign == "+" else old - delta
    return round(max(new, 0.0), 2)


def describe_price_op(op: Tuple[str, float, bool]) -> str:
    sign, val, pct = op
    v = f"{val:g}{'%' if pct else '₽'}"
    return {"=": f"поставить {v}", "+": f"поднять на {v}", "-": f"снизить на {v}"}[sign]


# ──────────────────────────────────────────────────────────────────────────────
# Задание и отчёт
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Job:
    counter: bool = False                         # обновить счётчик/дату
    force_counter: bool = False                   # прибавить, даже если сегодня уже обновляли
    replaces: List[LotReplaceRule] = field(default_factory=list)
    price_op: Optional[Tuple[str, float, bool]] = None
    lot_ids: Optional[List[int]] = None           # только эти лоты (None — все)
    title_filter: str = ""                        # только лоты, где в названии есть эта строка
    dry_run: bool = False


@dataclass
class Report:
    ok: bool = True
    error: str = ""
    dry_run: bool = False
    total_lots: int = 0
    changed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    counter_info: str = ""
    aborted: bool = False

    def to_html(self, title: str) -> str:
        lines = [f"<b>{title}</b>"]
        if not self.ok:
            lines.append(f"❌ {html.escape(self.error)}")
            return "\n".join(lines)
        if self.counter_info:
            lines.append(self.counter_info)
        verb = "будет изменено" if self.dry_run else "изменено"
        lines.append(f"Лотов проверено: <b>{self.total_lots}</b>, {verb}: <b>{len(self.changed)}</b>"
                     + (f", ошибок: <b>{len(self.failed)}</b>" if self.failed else ""))
        if self.aborted:
            lines.append("⚠️ Остановлено: много ошибок подряд (FunPay недоступен или разлогинил).")
        body = self.changed[:25]
        if body:
            lines.append("")
            lines.extend(body)
            if len(self.changed) > 25:
                lines.append(f"… и ещё {len(self.changed) - 25}")
        if self.failed:
            lines.append("\n<b>Ошибки:</b>")
            lines.extend(self.failed[:10])
            if len(self.failed) > 10:
                lines.append(f"… и ещё {len(self.failed) - 10}")
        if self.dry_run:
            lines.append("\n<i>Это предпросмотр — на FunPay ничего не менялось.</i>")
        text = "\n".join(lines)
        return text if len(text) < 4000 else text[:3990] + "…"


ProgressCb = Optional[Callable[[str], Awaitable[None]]]


# ──────────────────────────────────────────────────────────────────────────────
# Работа с FunPay
# ──────────────────────────────────────────────────────────────────────────────

_lock = asyncio.Lock()


def is_running() -> bool:
    return _lock.locked()


async def _collect_lots(client) -> List[Tuple[int, str]]:
    """[(lot_id, название)] лотов аккаунта: из профиля + доп. ID, минус исключённые."""
    cfg = config_manager.settings.lot_texts
    loop = asyncio.get_running_loop()
    result: Dict[int, str] = {}
    try:
        profile = await loop.run_in_executor(None, client.account.get_user, client.account.id)
        for subcat, lots in (profile.get_sorted_lots(2) or {}).items():
            st = getattr(getattr(subcat, "type", None), "name", "COMMON")
            if st == "CURRENCY":
                continue  # «валюта»/чипы редактируются иначе — не трогаем
            for lot_id, lot in lots.items():
                try:
                    result[int(lot_id)] = (getattr(lot, "description", None) or "").strip()
                except (TypeError, ValueError):
                    continue
    except Exception as e:
        logger.warning(f"LotTexts [{client.alias}]: не удалось получить лоты профиля: {e}")
    for lid in cfg.extra_lot_ids:
        result.setdefault(int(lid), "")
    for lid in cfg.exclude_lot_ids:
        result.pop(int(lid), None)
    return list(result.items())


def _short(title: str, lot_id: int) -> str:
    t = (title or "").replace("\n", " ").strip()
    if len(t) > 40:
        t = t[:40] + "…"
    return html.escape(t) if t else f"#{lot_id}"


async def run_job(job: Job, progress: ProgressCb = None) -> Report:
    """Выполняет задание. Никогда не бросает исключений — всё в Report."""
    if _lock.locked():
        return Report(ok=False, error="Обновление уже идёт — дождитесь окончания.")
    async with _lock:
        try:
            return await _run_job(job, progress)
        except Exception as e:
            logger.exception(f"LotTexts: неожиданная ошибка: {e}")
            return Report(ok=False, error=f"Неожиданная ошибка: {e}")


async def _run_job(job: Job, progress: ProgressCb) -> Report:
    from core.funpay_client import accounts_manager

    cfg = config_manager.settings.lot_texts
    rep = Report(dry_run=job.dry_run)
    clients = accounts_manager.connected_clients()
    if not clients:
        return Report(ok=False, error="FunPay не подключён. Обновите golden_key: /menu → 🔑")

    rx = None
    if job.counter:
        try:
            rx = compile_template(cfg.counter.template)
        except TemplateError as e:
            return Report(ok=False, error=f"Шаблон счётчика: {e}")

    loop = asyncio.get_running_loop()
    multi = len(accounts_manager.all()) > 1
    title_filter = job.title_filter.strip().lower()

    # ── Проход 1: загружаем поля всех нужных лотов ────────────────────────
    loaded = []   # (client, lot_id, title, lot_fields)
    for cl in clients:
        lots = await _collect_lots(cl)
        if job.lot_ids is not None:
            wanted = set(job.lot_ids)
            lots = [(i, t) for i, t in lots if i in wanted]
            for i in wanted - {i for i, _ in lots}:
                if i not in cfg.exclude_lot_ids and len(clients) == 1:
                    lots.append((i, ""))
        if title_filter:
            lots = [(i, t) for i, t in lots if title_filter in (t or "").lower()]
        errors_in_row = 0
        for n, (lot_id, title) in enumerate(lots, 1):
            if progress and n % 5 == 1:
                await progress(f"⏳ Читаю лоты [{cl.alias}]: {n}/{len(lots)}…")
            try:
                lf = await loop.run_in_executor(None, cl.account.get_lot_fields, lot_id)
                title = title or (lf.title_ru or "")
                loaded.append((cl, lot_id, title, lf))
                errors_in_row = 0
            except Exception as e:
                errors_in_row += 1
                rep.failed.append(f"• {_short(title, lot_id)}: не открылся ({type(e).__name__})")
                if errors_in_row >= MAX_CONSECUTIVE_ERRORS:
                    rep.aborted = True
                    break
            await asyncio.sleep(random.uniform(*READ_DELAY))
    rep.total_lots = len(loaded) + len(rep.failed)

    # ── Счётчик: одно число и одна дата на все лоты ───────────────────────
    values: Dict[str, str] = {}
    if rx is not None and loaded:
        ts = now_msk()
        today = ts.strftime("%d.%m.%Y")
        found: List[Tuple[Optional[str], Optional[int]]] = []
        for _, _, _, lf in loaded:
            for attr, _ in TEXT_FIELDS:
                found.extend(find_counters(getattr(lf, attr, "") or "", rx))
        if not found:
            rep.counter_info = "⚠️ Текст счётчика не найден ни в одном лоте — проверьте шаблон."
        else:
            counts = [c for _, c in found if c is not None]
            today_counts = [c for d, c in found if d == today and c is not None]
            rc = cfg.counter
            if today_counts and not job.force_counter:
                target = max(today_counts)       # сегодня уже обновляли — не прибавляем второй раз
            else:
                if rc.count_manual and rc.count is not None:
                    base = rc.count
                else:
                    base = max(counts + ([rc.count] if rc.count is not None else []), default=0)
                lo, hi = sorted((max(0, rc.inc_min), max(0, rc.inc_max)))
                target = base + random.randint(lo, hi)
            values = {"date": today, "time": ts.strftime("%H:%M"), "count": str(target)}
            old = max(counts, default=None)
            if job.dry_run and not (today_counts and not job.force_counter):
                shown = f"<b>{base + lo}…{base + hi}</b> (случайно)"
            else:
                shown = f"<b>{target}</b>"
            rep.counter_info = (f"🔢 Счётчик: {old if old is not None else '—'} → {shown}, "
                                f"дата: <b>{today}, {values['time']} МСК</b>")
            if today_counts and not job.force_counter:
                values.pop("time")               # уже обновлённые лоты не трогаем вовсе
                rep.counter_info += " (сегодня уже обновляли — число не прибавляется)"

    # ── Проход 2: меняем и сохраняем ───────────────────────────────────────
    errors_in_row = 0
    saved_any_counter = False
    for n, (cl, lot_id, title, lf) in enumerate(loaded, 1):
        if rep.aborted:
            break
        notes = []
        try:
            for attr, label in TEXT_FIELDS:
                old_text = getattr(lf, attr, None)
                if not old_text:
                    continue
                new_text = old_text
                if values:
                    v = values
                    if "time" not in v:
                        # Лот уже с сегодняшней датой? Тогда оставляем как есть.
                        if any(d == v["date"] for d, _ in find_counters(new_text, rx)):
                            v = {}
                        else:
                            v = dict(values, time=now_msk().strftime("%H:%M"))
                    if v:
                        new_text, k = apply_counter(new_text, rx, v)
                        if k and new_text != old_text:
                            notes.append("счётчик")
                if job.replaces:
                    new_text, k = apply_replaces(new_text, job.replaces)
                    if k:
                        notes.append(f"замена ×{k} ({label})")
                if new_text != old_text:
                    setattr(lf, attr, new_text)

            if job.price_op is not None and lf.price is not None:
                new_price = calc_price(float(lf.price), job.price_op)
                if new_price <= 0:
                    notes.append("⚠️ цена ≤ 0 — пропущено")
                elif abs(new_price - float(lf.price)) >= 0.01:
                    notes.append(f"цена {float(lf.price):g} → <b>{new_price:g}</b>")
                    lf.price = new_price

            real_changes = [x for x in notes if not x.startswith("⚠️")]
            if not real_changes:
                continue

            name = _short(title, lot_id)
            if multi:
                name = f"[{html.escape(cl.alias)}] {name}"
            line = f"• {name}: {', '.join(dict.fromkeys(notes))}"

            if not job.dry_run:
                if progress and (n % 3 == 1 or n == len(loaded)):
                    await progress(f"💾 Сохраняю: {n}/{len(loaded)}…")
                await loop.run_in_executor(None, cl.account.save_lot, lf)
                await asyncio.sleep(random.uniform(cfg.delay_min_sec, max(cfg.delay_min_sec, cfg.delay_max_sec)))
                if "счётчик" in notes:
                    saved_any_counter = True
            rep.changed.append(line)
            errors_in_row = 0
        except Exception as e:
            errors_in_row += 1
            msg = str(e).strip().splitlines()[0][:120] if str(e).strip() else type(e).__name__
            rep.failed.append(f"• {_short(title, lot_id)}: {html.escape(msg)}")
            logger.warning(f"LotTexts: лот {lot_id} не сохранён: {e}")
            if errors_in_row >= MAX_CONSECUTIVE_ERRORS:
                rep.aborted = True

    # Запоминаем число (чтобы следующий раз считать от него)
    if values and not job.dry_run and (saved_any_counter or "time" not in values):
        cfg.counter.count = int(values["count"])
        cfg.counter.count_manual = False
        config_manager.save()
    return rep


# ──────────────────────────────────────────────────────────────────────────────
# Расписание
# ──────────────────────────────────────────────────────────────────────────────

def last_slot(ts: Optional[datetime] = None) -> datetime:
    """Последний момент запуска по расписанию, который уже наступил (МСК)."""
    cfg = config_manager.settings.lot_texts
    ts = ts or now_msk()
    slot = ts.replace(hour=cfg.hour % 24, minute=cfg.minute % 60, second=0, microsecond=0)
    slot -= timedelta(days=(slot.weekday() - cfg.weekday % 7) % 7)
    if slot > ts:
        slot -= timedelta(days=7)
    return slot


def next_slot(ts: Optional[datetime] = None) -> datetime:
    return last_slot(ts) + timedelta(days=7)


def weekly_job(dry_run: bool = False, force: bool = False) -> Job:
    cfg = config_manager.settings.lot_texts
    return Job(
        counter=cfg.counter.enabled,
        force_counter=force,
        replaces=[r for r in cfg.replaces if r.weekly],
        dry_run=dry_run,
    )


class LotTextsScheduler:
    # Если бот был выключен в момент запуска — догоняем в течение этого окна
    CATCH_UP = timedelta(hours=24)

    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._warned_slot: str = ""

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name="LotTextsScheduler")
            logger.info("Автообновление текстов лотов: планировщик запущен")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(30)  # даём FunPay подключиться
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"LotTexts scheduler: {e}")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        cfg = config_manager.settings.lot_texts
        if not cfg.enabled or is_running():
            return
        ts = now_msk()
        slot = last_slot(ts)
        if ts - slot > self.CATCH_UP:
            return
        try:
            last = datetime.fromisoformat(cfg.last_auto_run) if cfg.last_auto_run else None
        except ValueError:
            last = None
        if last is not None and last.tzinfo is None:
            last = last.replace(tzinfo=MSK)
        if last is not None and last >= slot:
            return

        from core.funpay_client import accounts_manager
        from modules.notifications import _send_to_all
        if not accounts_manager.connected_clients():
            if self._warned_slot != slot.isoformat():
                self._warned_slot = slot.isoformat()
                await _send_to_all(
                    "⚠️ <b>Автообновление лотов</b> не выполнено: FunPay не подключён.\n"
                    "Обновите golden_key: /menu → 🔑 — бот сам повторит попытку."
                )
            return

        logger.info("LotTexts: еженедельное автообновление лотов…")
        rep = await run_job(weekly_job())
        if not rep.ok and "уже идёт" in rep.error:
            return
        cfg.last_auto_run = ts.isoformat(timespec="seconds")
        cfg.last_report = (f"{ts.strftime('%d.%m %H:%M')}: изменено {len(rep.changed)}, "
                           f"ошибок {len(rep.failed)}" if rep.ok else f"{ts.strftime('%d.%m %H:%M')}: {rep.error}")
        config_manager.save()
        await _send_to_all(rep.to_html("🗓 Еженедельное обновление лотов"))


lot_texts_scheduler = LotTextsScheduler()
