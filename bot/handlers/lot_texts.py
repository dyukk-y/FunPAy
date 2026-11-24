"""
Меню «📝 Тексты и цены лотов» — всё управляется кнопками с телефона.

/lots — открыть меню; /price <ID> <цена|+N|-N%> — быстро поменять цену одного лота.
Логика — в modules/lot_texts.py.
"""
from __future__ import annotations

import asyncio
import html
import re
import time
from typing import List, Optional

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.handlers.auth import is_admin
from bot.states import LotTextStates
from config.settings import DEFAULT_COUNTER_TEMPLATE, LotReplaceRule, config_manager
from modules import lot_texts as lt
from utils.logger import logger

router = Router(name="lot_texts")

# Фоновые задачи держим здесь, чтобы их не собрал сборщик мусора
_tasks: set = set()


def cfg():
    return config_manager.settings.lot_texts


def _kb(rows: List[List[tuple]]) -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    sizes = []
    for row in rows:
        for text, cb in row:
            b.button(text=text, callback_data=cb)
        sizes.append(len(row))
    b.adjust(*sizes)
    return b.as_markup()


CANCEL = [("❌ Отмена", "lt:main")]
BACK = [("⬅️ Назад", "lt:main")]


def _parse_ids(text: str) -> List[int]:
    return [int(x) for x in re.findall(r"\d{3,}", text or "")]


async def _deny(ev) -> bool:
    uid = ev.from_user.id if ev.from_user else 0
    if is_admin(uid):
        return False
    if isinstance(ev, types.CallbackQuery):
        await ev.answer("⛔ Только для админов", show_alert=True)
    else:
        await ev.answer("⛔ Только для админов.")
    return True


# ═════════════════════════════════════════════════════════════════════════════
# Главный экран
# ═════════════════════════════════════════════════════════════════════════════

def main_text() -> str:
    c = cfg()
    rc = c.counter
    nxt = lt.next_slot() if c.enabled else None
    auto = (f"🟢 включено — каждый <b>{lt.WEEKDAYS[c.weekday % 7].lower()}</b> "
            f"в <b>{c.hour:02d}:{c.minute:02d}</b> МСК") if c.enabled else "🔴 выключено"
    weekly_reps = sum(1 for r in c.replaces if r.weekly)
    lines = [
        "📝 <b>Тексты и цены лотов</b>",
        "",
        f"🗓 Автообновление: {auto}",
    ]
    if nxt:
        lines.append(f"⏭ Следующее: {nxt.strftime('%d.%m.%Y %H:%M')} МСК")
    lines += [
        f"🔢 Счётчик: <b>{rc.count if rc.count is not None else 'возьму из лотов'}</b>, "
        f"прибавка {rc.inc_min}–{rc.inc_max} в неделю" + ("" if rc.enabled else " (выкл)"),
        f"🔁 Замены текста: {len(c.replaces)} (еженедельных: {weekly_reps})",
    ]
    if c.extra_lot_ids or c.exclude_lot_ids:
        lines.append(f"📦 Доп. лотов: {len(c.extra_lot_ids)}, исключено: {len(c.exclude_lot_ids)}")
    if c.last_report:
        lines.append(f"📋 Последний автозапуск: {html.escape(c.last_report)}")
    if lt.is_running():
        lines.append("\n⏳ <i>Сейчас идёт обновление…</i>")
    lines.append("\n<i>Шаблон счётчика: «✏️ Шаблон». Всё можно сначала проверить через «👀 Предпросмотр».</i>")
    return "\n".join(lines)


def main_kb() -> types.InlineKeyboardMarkup:
    c = cfg()
    return _kb([
        [(f"{'🟢' if c.enabled else '🔴'} Автообновление раз в неделю", "lt:toggle")],
        [(f"📅 {lt.WEEKDAYS_SHORT[c.weekday % 7]} {c.hour:02d}:{c.minute:02d} — изменить", "lt:sched")],
        [(f"🔢 Число: {c.counter.count if c.counter.count is not None else 'авто'}", "lt:count"),
         (f"➕ Прибавка {c.counter.inc_min}–{c.counter.inc_max}", "lt:inc")],
        [("✏️ Шаблон счётчика", "lt:tpl"),
         (f"{'🟢' if c.counter.enabled else '🔴'} Счётчик", "lt:ctoggle")],
        [("👀 Предпросмотр", "lt:preview"), ("🚀 Обновить сейчас", "lt:runask")],
        [("🔁 Замена текста", "lt:rep"), ("💰 Изменить цены", "lt:price")],
        [("📦 Какие лоты", "lt:lots"), ("ℹ️ Помощь", "lt:help")],
        [("⬅️ В главное меню", "menu:main")],
    ])


async def _show_main(c: types.CallbackQuery) -> None:
    try:
        await c.message.edit_text(main_text(), reply_markup=main_kb())
    except Exception:
        await c.message.answer(main_text(), reply_markup=main_kb())


@router.message(Command("lots"))
async def cmd_lots(msg: types.Message, state: FSMContext) -> None:
    if await _deny(msg):
        return
    await state.clear()
    await msg.answer(main_text(), reply_markup=main_kb())


@router.callback_query(F.data == "lt:main")
async def lt_main(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.clear()
    await _show_main(c)
    await c.answer()


@router.callback_query(F.data == "lt:toggle")
async def lt_toggle(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    x = cfg()
    x.enabled = not x.enabled
    if x.enabled and not x.last_auto_run:
        # Чтобы при включении не сработал сразу «догоняющий» запуск за прошедшую неделю
        x.last_auto_run = lt.now_msk().isoformat(timespec="seconds")
    config_manager.save()
    await _show_main(c)
    await c.answer("Включено" if x.enabled else "Выключено")


@router.callback_query(F.data == "lt:ctoggle")
async def lt_ctoggle(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    cfg().counter.enabled = not cfg().counter.enabled
    config_manager.save()
    await _show_main(c)
    await c.answer()


@router.callback_query(F.data == "lt:help")
async def lt_help(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    text = (
        "ℹ️ <b>Как это работает</b>\n\n"
        "<b>Счётчик.</b> В описании лота должен быть текст по шаблону, например:\n"
        "<code>📅 Актуально на 06.04.2026, 04:00 МСК:\n"
        "Мы уже успешно продали 532 рекламных размещений! 🚀</code>\n\n"
        "Раз в неделю бот ставит туда сегодняшнюю дату и время, а к числу прибавляет "
        "случайное от 3 до 10. Число одинаковое во всех лотах. Лоты без этого текста не трогаются.\n\n"
        "Эмодзи, звёздочки и переносы строк могут немного отличаться — важны слова.\n\n"
        "<b>Если бот перезапустился</b> — он не прибавит число дважды: лоты с сегодняшней "
        "датой пропускаются.\n\n"
        "<b>Замена текста</b> — «найти → заменить» во всех лотах: разово или каждую неделю.\n\n"
        "<b>Цены</b> — для всех лотов, по ID или по слову в названии: "
        "<code>150</code> (поставить), <code>+20</code>, <code>-10</code>, <code>+5%</code>, <code>-10%</code>.\n"
        "Быстро для одного лота: <code>/price 12345678 150</code>\n\n"
        "<b>Всегда</b> перед изменением показывается предпросмотр. "
        "Между сохранениями лотов — пауза 2–5 сек, чтобы FunPay не ругался.\n\n"
        "Бот видит лоты из вашего профиля (включённые). Выключенные можно добавить в «📦 Какие лоты»."
    )
    await c.message.edit_text(text, reply_markup=_kb([BACK]))
    await c.answer()


# ═════════════════════════════════════════════════════════════════════════════
# Расписание
# ═════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "lt:sched")
async def lt_sched(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    rows = [[(d, f"lt:wd:{i}") for i, d in enumerate(lt.WEEKDAYS_SHORT[:4])],
            [(d, f"lt:wd:{i + 4}") for i, d in enumerate(lt.WEEKDAYS_SHORT[4:])],
            BACK]
    await c.message.edit_text("📅 В какой день недели обновлять?", reply_markup=_kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("lt:wd:"))
async def lt_wd(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    cfg().weekday = int(c.data.split(":")[2]) % 7
    config_manager.save()
    hours = list(range(24))
    rows = [[(f"{h:02d}:00", f"lt:hr:{h}") for h in hours[i:i + 6]] for i in range(0, 24, 6)]
    rows.append([("⌨️ Ввести время (например 04:30)", "lt:time")])
    rows.append(BACK)
    await c.message.edit_text(
        f"🕓 {lt.WEEKDAYS[cfg().weekday]}. Во сколько (МСК)?", reply_markup=_kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("lt:hr:"))
async def lt_hr(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    cfg().hour = int(c.data.split(":")[2]) % 24
    cfg().minute = 0
    config_manager.save()
    await _show_main(c)
    await c.answer("Сохранено")


@router.callback_query(F.data == "lt:time")
async def lt_time(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.set_state(LotTextStates.time)
    await c.message.edit_text("⌨️ Пришлите время по МСК, например <code>04:00</code> или <code>21:30</code>",
                              reply_markup=_kb([CANCEL]))
    await c.answer()


@router.message(LotTextStates.time, F.text)
async def lt_time_in(msg: types.Message, state: FSMContext) -> None:
    m = re.match(r"^\s*(\d{1,2})[:.\s](\d{2})\s*$", msg.text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        await msg.answer("❌ Формат: ЧЧ:ММ, например 04:00", reply_markup=_kb([CANCEL])); return
    cfg().hour, cfg().minute = int(m.group(1)), int(m.group(2))
    config_manager.save()
    await state.clear()
    await msg.answer(main_text(), reply_markup=main_kb())


# ═════════════════════════════════════════════════════════════════════════════
# Счётчик: число, прибавка, шаблон
# ═════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "lt:count")
async def lt_count(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.set_state(LotTextStates.count)
    cur = cfg().counter.count
    await c.message.edit_text(
        f"🔢 Текущее число: <b>{cur if cur is not None else 'не задано (возьму из лотов)'}</b>\n\n"
        "Пришлите новое число. При следующем обновлении к нему прибавится 3–10.\n"
        "Или <code>авто</code> — брать самое большое число из лотов.",
        reply_markup=_kb([CANCEL]))
    await c.answer()


@router.message(LotTextStates.count, F.text)
async def lt_count_in(msg: types.Message, state: FSMContext) -> None:
    t = (msg.text or "").strip().lower()
    rc = cfg().counter
    if t in ("авто", "auto", "-"):
        rc.count, rc.count_manual = None, False
    elif t.isdigit():
        rc.count, rc.count_manual = int(t), True
    else:
        await msg.answer("❌ Нужно число, например 532, или «авто».", reply_markup=_kb([CANCEL])); return
    config_manager.save()
    await state.clear()
    await msg.answer("✅ Сохранено.\n\n" + main_text(), reply_markup=main_kb())


@router.callback_query(F.data == "lt:inc")
async def lt_inc(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.set_state(LotTextStates.inc)
    await c.message.edit_text(
        "➕ Сколько прибавлять каждую неделю? Пришлите диапазон, например <code>3-10</code>, "
        "или одно число (<code>5</code>).", reply_markup=_kb([CANCEL]))
    await c.answer()


@router.message(LotTextStates.inc, F.text)
async def lt_inc_in(msg: types.Message, state: FSMContext) -> None:
    nums = [int(x) for x in re.findall(r"\d+", msg.text or "")]
    if not nums or len(nums) > 2 or max(nums) > 100000:
        await msg.answer("❌ Пример: 3-10", reply_markup=_kb([CANCEL])); return
    lo, hi = (nums[0], nums[0]) if len(nums) == 1 else sorted(nums)
    cfg().counter.inc_min, cfg().counter.inc_max = lo, hi
    config_manager.save()
    await state.clear()
    await msg.answer("✅ Сохранено.\n\n" + main_text(), reply_markup=main_kb())


@router.callback_query(F.data == "lt:tpl")
async def lt_tpl(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.set_state(LotTextStates.template)
    await c.message.edit_text(
        "✏️ <b>Шаблон счётчика</b>\n\nСейчас:\n"
        f"<code>{html.escape(cfg().counter.template)}</code>\n\n"
        "Пришлите новый текст, где вместо даты — <code>{date}</code>, времени — <code>{time}</code>, "
        "числа — <code>{count}</code>. Можно просто скопировать кусок из своего лота и заменить "
        "в нём дату/время/число на эти метки.\n\n"
        "Отправьте <code>default</code>, чтобы вернуть стандартный.",
        reply_markup=_kb([CANCEL]))
    await c.answer()


@router.message(LotTextStates.template, F.text)
async def lt_tpl_in(msg: types.Message, state: FSMContext) -> None:
    t = (msg.text or "").strip()
    if t.lower() in ("default", "по умолчанию", "стандарт"):
        t = DEFAULT_COUNTER_TEMPLATE
    try:
        lt.compile_template(t)
    except lt.TemplateError as e:
        await msg.answer(f"❌ {e}", reply_markup=_kb([CANCEL])); return
    cfg().counter.template = t
    config_manager.save()
    await state.clear()
    await msg.answer("✅ Шаблон сохранён. Проверьте его кнопкой «👀 Предпросмотр».\n\n" + main_text(),
                     reply_markup=main_kb())


# ═════════════════════════════════════════════════════════════════════════════
# Запуск: предпросмотр / сейчас
# ═════════════════════════════════════════════════════════════════════════════

def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _run_with_progress(chat_msg: types.Message, job: lt.Job, title: str,
                             after_kb: Optional[types.InlineKeyboardMarkup] = None) -> lt.Report:
    """Запускает задание, обновляя одно сообщение с прогрессом. Ошибки не выпускает."""
    status = await chat_msg.answer("⏳ Начинаю…")
    last = {"t": 0.0}

    async def progress(text: str) -> None:
        if time.monotonic() - last["t"] < 3:
            return
        last["t"] = time.monotonic()
        try:
            await status.edit_text(text)
        except Exception:
            pass

    rep = await lt.run_job(job, progress)
    try:
        await status.edit_text(rep.to_html(title), reply_markup=after_kb or _kb([BACK]))
    except Exception:
        try:
            await chat_msg.answer(rep.to_html(title), reply_markup=after_kb or _kb([BACK]))
        except Exception as e:
            logger.warning(f"LotTexts: не удалось отправить отчёт: {e}")
    return rep


@router.callback_query(F.data == "lt:preview")
async def lt_preview(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    if lt.is_running():
        await c.answer("Уже идёт обновление, подождите", show_alert=True); return
    await c.answer("Проверяю лоты…")
    _spawn(_run_with_progress(
        c.message, lt.weekly_job(dry_run=True, force=True), "👀 Предпросмотр еженедельного обновления",
        _kb([[("🚀 Применить сейчас", "lt:runask")], BACK])))


@router.callback_query(F.data == "lt:runask")
async def lt_runask(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    rc = cfg().counter
    reps = [r for r in cfg().replaces if r.weekly]
    what = []
    if rc.enabled:
        what.append(f"• поставит сегодняшнюю дату/время и прибавит {rc.inc_min}–{rc.inc_max} к числу")
    if reps:
        what.append(f"• выполнит еженедельные замены текста ({len(reps)})")
    if not what:
        await c.answer("Счётчик выключен и еженедельных замен нет — нечего делать", show_alert=True); return
    await c.message.edit_text(
        "🚀 <b>Обновить лоты сейчас?</b>\n\nБот:\n" + "\n".join(what) +
        "\n\nЕсли сегодня уже обновляли — число прибавится ещё раз.",
        reply_markup=_kb([[("✅ Да, обновить", "lt:run")], [("👀 Сначала предпросмотр", "lt:preview")], BACK]))
    await c.answer()


@router.callback_query(F.data == "lt:run")
async def lt_run(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    if lt.is_running():
        await c.answer("Уже идёт обновление, подождите", show_alert=True); return
    await c.answer("Запускаю…")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _spawn(_run_with_progress(c.message, lt.weekly_job(force=True), "🚀 Обновление лотов"))


# ═════════════════════════════════════════════════════════════════════════════
# Замена текста
# ═════════════════════════════════════════════════════════════════════════════

def _short(s: str, n: int = 22) -> str:
    s = s.replace("\n", "⏎")
    return s if len(s) <= n else s[:n] + "…"


@router.callback_query(F.data == "lt:rep")
async def lt_rep(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.clear()
    reps = cfg().replaces
    lines = ["🔁 <b>Замена текста во всех лотах</b>", ""]
    if reps:
        for i, r in enumerate(reps, 1):
            lines.append(f"{i}. «{html.escape(_short(r.find, 60))}» → «{html.escape(_short(r.replace, 60)) or '(удалить)'}» "
                         f"{'🔁 каждую неделю' if r.weekly else '(была разовой)'}")
    else:
        lines.append("Пока нет сохранённых замен.")
    lines.append("\nНажмите на замену, чтобы удалить её.")
    rows = [[(f"🗑 {i}. {_short(r.find)}", f"lt:repdel:{i - 1}")] for i, r in enumerate(reps, 1)]
    rows.append([("➕ Новая замена", "lt:repadd")])
    rows.append(BACK)
    await c.message.edit_text("\n".join(lines), reply_markup=_kb(rows))
    await c.answer()


@router.callback_query(F.data.startswith("lt:repdel:"))
async def lt_repdel(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    idx = int(c.data.split(":")[2])
    if 0 <= idx < len(cfg().replaces):
        cfg().replaces.pop(idx)
        config_manager.save()
    await lt_rep(c, state)


@router.callback_query(F.data == "lt:repadd")
async def lt_repadd(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.set_state(LotTextStates.rep_find)
    await c.message.edit_text(
        "🔍 Пришлите текст, который нужно <b>найти</b> в лотах (точно как в лоте).",
        reply_markup=_kb([CANCEL]))
    await c.answer()


@router.message(LotTextStates.rep_find, F.text)
async def lt_rep_find(msg: types.Message, state: FSMContext) -> None:
    await state.update_data(find=msg.text)
    await state.set_state(LotTextStates.rep_to)
    await msg.answer("✏️ Теперь пришлите, <b>на что заменить</b>.\nОтправьте <code>-</code>, чтобы просто удалить найденный текст.",
                     reply_markup=_kb([CANCEL]))


@router.message(LotTextStates.rep_to, F.text)
async def lt_rep_to(msg: types.Message, state: FSMContext) -> None:
    to = "" if msg.text.strip() == "-" else msg.text
    data = await state.get_data()
    await state.update_data(to=to)
    await state.set_state(LotTextStates.confirm)
    rule = LotReplaceRule(find=data["find"], replace=to, weekly=False)
    _spawn(_run_with_progress(
        msg, lt.Job(replaces=[rule], dry_run=True), "👀 Предпросмотр замены",
        _kb([[("✅ Заменить один раз", "lt:repgo:1")],
             [("🔁 Заменить и повторять каждую неделю", "lt:repgo:w")],
             CANCEL])))


@router.callback_query(F.data.startswith("lt:repgo:"))
async def lt_repgo(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    data = await state.get_data()
    if "find" not in data:
        await c.answer("Данные потерялись — начните заново", show_alert=True); return
    if lt.is_running():
        await c.answer("Уже идёт обновление, подождите", show_alert=True); return
    weekly = c.data.endswith(":w")
    rule = LotReplaceRule(find=data["find"], replace=data.get("to", ""), weekly=weekly)
    if weekly:
        cfg().replaces.append(rule)
        config_manager.save()
    await state.clear()
    await c.answer("Запускаю…")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _spawn(_run_with_progress(c.message, lt.Job(replaces=[rule]), "🔁 Замена текста"))


# ═════════════════════════════════════════════════════════════════════════════
# Цены
# ═════════════════════════════════════════════════════════════════════════════

PRICE_HELP = (
    "Пришлите, что сделать с ценой:\n"
    "• <code>150</code> — поставить 150\n"
    "• <code>+20</code> / <code>-20</code> — поднять / снизить на 20\n"
    "• <code>+10%</code> / <code>-10%</code> — поднять / снизить на 10%"
)


@router.callback_query(F.data == "lt:price")
async def lt_price(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.clear()
    await c.message.edit_text(
        "💰 <b>Изменить цены</b>\n\nДля каких лотов?",
        reply_markup=_kb([[("📦 Все лоты", "lt:p:all")],
                          [("🔢 По ID лотов", "lt:p:ids")],
                          [("🔤 По слову в названии", "lt:p:word")],
                          BACK]))
    await c.answer()


@router.callback_query(F.data.startswith("lt:p:"))
async def lt_p_scope(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    scope = c.data.split(":")[2]
    if scope == "all":
        await state.update_data(p_ids=None, p_word="")
        await state.set_state(LotTextStates.price_op)
        await c.message.edit_text("💰 Все лоты.\n\n" + PRICE_HELP, reply_markup=_kb([CANCEL]))
    elif scope == "ids":
        await state.set_state(LotTextStates.price_ids)
        await c.message.edit_text(
            "🔢 Пришлите ID лотов через пробел или запятую (число из ссылки <code>offer?id=…</code>).",
            reply_markup=_kb([CANCEL]))
    else:
        await state.set_state(LotTextStates.price_word)
        await c.message.edit_text("🔤 Пришлите слово или часть названия лота.", reply_markup=_kb([CANCEL]))
    await c.answer()


@router.message(LotTextStates.price_ids, F.text)
async def lt_p_ids(msg: types.Message, state: FSMContext) -> None:
    ids = _parse_ids(msg.text)
    if not ids:
        await msg.answer("❌ Не нашёл ID. Пример: 12345678 23456789", reply_markup=_kb([CANCEL])); return
    await state.update_data(p_ids=ids, p_word="")
    await state.set_state(LotTextStates.price_op)
    await msg.answer(f"💰 Лотов: {len(ids)}.\n\n" + PRICE_HELP, reply_markup=_kb([CANCEL]))


@router.message(LotTextStates.price_word, F.text)
async def lt_p_word(msg: types.Message, state: FSMContext) -> None:
    await state.update_data(p_ids=None, p_word=msg.text.strip())
    await state.set_state(LotTextStates.price_op)
    await msg.answer(f"💰 Лоты, где в названии есть «{html.escape(msg.text.strip())}».\n\n" + PRICE_HELP,
                     reply_markup=_kb([CANCEL]))


@router.message(LotTextStates.price_op, F.text)
async def lt_p_op(msg: types.Message, state: FSMContext) -> None:
    op = lt.parse_price_op(msg.text)
    if op is None:
        await msg.answer("❌ Не понял.\n\n" + PRICE_HELP, reply_markup=_kb([CANCEL])); return
    data = await state.get_data()
    await state.update_data(p_op=list(op))
    await state.set_state(LotTextStates.confirm)
    job = lt.Job(price_op=op, lot_ids=data.get("p_ids"), title_filter=data.get("p_word", ""), dry_run=True)
    _spawn(_run_with_progress(
        msg, job, f"👀 Предпросмотр: {lt.describe_price_op(op)}",
        _kb([[("✅ Применить", "lt:pgo")], CANCEL])))


@router.callback_query(F.data == "lt:pgo")
async def lt_pgo(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    data = await state.get_data()
    if "p_op" not in data:
        await c.answer("Данные потерялись — начните заново", show_alert=True); return
    if lt.is_running():
        await c.answer("Уже идёт обновление, подождите", show_alert=True); return
    op = tuple(data["p_op"])
    await state.clear()
    await c.answer("Меняю цены…")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    _spawn(_run_with_progress(
        c.message, lt.Job(price_op=op, lot_ids=data.get("p_ids"), title_filter=data.get("p_word", "")),
        f"💰 Цены: {lt.describe_price_op(op)}"))


@router.message(Command("price"))
async def cmd_price(msg: types.Message, state: FSMContext) -> None:
    """/price <ID [ID…]> <цена|+N|-N|+N%|-N%> — с предпросмотром и кнопкой «Применить»."""
    if await _deny(msg):
        return
    parts = (msg.text or "").split()[1:]
    op = lt.parse_price_op(parts[-1]) if parts else None
    ids = _parse_ids(" ".join(parts[:-1])) if len(parts) >= 2 else []
    if not ids or op is None:
        await msg.answer("💰 Пример: <code>/price 12345678 150</code> или <code>/price 12345678 -10%</code>\n"
                         "Для всех лотов — /lots → 💰 Изменить цены"); return
    await state.set_state(LotTextStates.confirm)
    await state.update_data(p_ids=ids, p_word="", p_op=list(op))
    _spawn(_run_with_progress(
        msg, lt.Job(price_op=op, lot_ids=ids, dry_run=True), f"👀 Предпросмотр: {lt.describe_price_op(op)}",
        _kb([[("✅ Применить", "lt:pgo")], CANCEL])))


# ═════════════════════════════════════════════════════════════════════════════
# Какие лоты
# ═════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "lt:lots")
async def lt_lots(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.clear()
    x = cfg()
    text = (
        "📦 <b>Какие лоты обрабатываются</b>\n\n"
        "По умолчанию — все лоты из вашего профиля FunPay (включённые).\n\n"
        f"➕ Дополнительно (например, выключенные): "
        f"{', '.join(map(str, x.extra_lot_ids)) or 'нет'}\n"
        f"🚫 Никогда не трогать: {', '.join(map(str, x.exclude_lot_ids)) or 'нет'}"
    )
    await c.message.edit_text(text, reply_markup=_kb([
        [("➕ Доп. лоты", "lt:extra"), ("🚫 Исключить", "lt:excl")], BACK]))
    await c.answer()


@router.callback_query(F.data.in_({"lt:extra", "lt:excl"}))
async def lt_ids_ask(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    extra = c.data == "lt:extra"
    await state.set_state(LotTextStates.extra_ids if extra else LotTextStates.exclude_ids)
    await c.message.edit_text(
        ("➕ Пришлите ID лотов, которые нужно обрабатывать дополнительно"
         if extra else "🚫 Пришлите ID лотов, которые бот не должен трогать") +
        " (через пробел/запятую). Список заменится целиком.\nОтправьте <code>-</code>, чтобы очистить.",
        reply_markup=_kb([CANCEL]))
    await c.answer()


@router.message(LotTextStates.extra_ids, F.text)
@router.message(LotTextStates.exclude_ids, F.text)
async def lt_ids_in(msg: types.Message, state: FSMContext) -> None:
    st = await state.get_state()
    ids = [] if msg.text.strip() == "-" else _parse_ids(msg.text)
    if msg.text.strip() != "-" and not ids:
        await msg.answer("❌ Не нашёл ID. Пример: 12345678 23456789", reply_markup=_kb([CANCEL])); return
    if st == LotTextStates.extra_ids.state:
        cfg().extra_lot_ids = sorted(set(ids))
    else:
        cfg().exclude_lot_ids = sorted(set(ids))
    config_manager.save()
    await state.clear()
    await msg.answer("✅ Сохранено.\n\n" + main_text(), reply_markup=main_kb())
