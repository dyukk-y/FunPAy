"""
Меню «🧠 Анализ и автопродажи» (/sales). Логика — modules/sales_brain.py.
"""
from __future__ import annotations

import asyncio
import html
import time
from typing import List

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.handlers.auth import is_admin
from bot.states import SalesStates
from config.settings import config_manager
from modules import sales_brain as sb
from utils.logger import logger

router = Router(name="sales_brain")
_tasks: set = set()

MODES = [("off", "⏸ Выключен"), ("suggest", "💡 Подсказки"), ("auto", "🤖 Авто")]
PAGE = 8


def cfg():
    return config_manager.settings.sales_brain


def _kb(rows: List[List[tuple]]) -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        for text, cb in row:
            b.button(text=text, callback_data=cb)
    b.adjust(*[len(r) for r in rows])
    return b.as_markup()


BACK = [("⬅️ Назад", "sb:main")]


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _deny(ev) -> bool:
    if ev.from_user and is_admin(ev.from_user.id):
        return False
    if isinstance(ev, types.CallbackQuery):
        await ev.answer("⛔ Только для админов", show_alert=True)
    else:
        await ev.answer("⛔ Только для админов.")
    return True


async def _edit(c: types.CallbackQuery, text: str, kb) -> None:
    try:
        await c.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await c.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


# ═════════════════════════════════════════════════════════════════════════════
# Главный экран
# ═════════════════════════════════════════════════════════════════════════════

def main_text() -> str:
    c = cfg()
    answers = sb.list_answers()
    mode = dict(MODES)[c.mode if c.mode in dict(MODES) else "off"]
    ai_ok = config_manager.settings.ai.enabled and bool(config_manager.settings.ai.api_key)
    lines = [
        "🧠 <b>Анализ переписки и автопродажи</b>",
        "",
        "Бот учится на чатах, где <b>покупатель написал первым</b>, — на ваших реальных ответах.",
        "",
        f"📦 Собрано: {sb.stats_line()}" + (f" · изучено {c.last_import}" if c.last_import else ""),
        f"📚 База ответов: <b>{len(answers)}</b> (отвечают сами: {sum(1 for a in answers if a.auto)})",
        f"⚙️ Режим: <b>{mode}</b>",
        f"🧠 ИИ: {'подсказки ✅' if c.ai_suggest else 'подсказки ❌'}, "
        f"{'сам отвечает ✅' if c.ai_auto else 'сам не отвечает'}"
        + ("" if ai_ok else " <i>(ИИ не настроен — /menu → Автоматизация → ИИ-помощник)</i>"),
    ]
    if sb.importing():
        lines.append("\n⏳ <i>Идёт изучение переписки…</i>")
    if c.mode == "off":
        lines.append("\n<i>С чего начать: «📥 Изучить переписку» → «📚 База ответов» → режим «💡 Подсказки».</i>")
    return "\n".join(lines)


def main_kb() -> types.InlineKeyboardMarkup:
    c = cfg()
    mode = dict(MODES).get(c.mode, "⏸ Выключен")
    return _kb([
        [("📥 Изучить переписку", "sb:import")],
        [("📊 Анализ", "sb:report"), ("🧠 Советы ИИ", "sb:advice")],
        [(f"📚 База ответов ({len(sb.list_answers())})", "sb:kb:0"), ("♻️ Обновить базу", "sb:build")],
        [(f"Режим: {mode} — сменить", "sb:mode")],
        [(f"{'✅' if c.ai_suggest else '❌'} ИИ-подсказки", "sb:aisug"),
         (f"{'✅' if c.ai_auto else '❌'} ИИ сам отвечает", "sb:aiauto")],
        [("⚙️ Настройки", "sb:set"), ("ℹ️ Как это работает", "sb:help")],
        [("⬅️ В главное меню", "menu:main")],
    ])


@router.message(Command("sales"))
async def cmd_sales(msg: types.Message, state: FSMContext) -> None:
    if await _deny(msg):
        return
    await state.clear()
    await msg.answer(main_text(), reply_markup=main_kb())


@router.callback_query(F.data == "sb:main")
async def sb_main(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.clear()
    await _edit(c, main_text(), main_kb())
    await c.answer()


@router.callback_query(F.data == "sb:mode")
async def sb_mode(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    keys = [k for k, _ in MODES]
    cur = cfg().mode if cfg().mode in keys else "off"
    cfg().mode = keys[(keys.index(cur) + 1) % len(keys)]
    config_manager.save()
    await _edit(c, main_text(), main_kb())
    note = {"off": "Выключено — бот только собирает статистику",
            "suggest": "Подсказки: бот присылает готовый ответ, вы жмёте «Отправить»",
            "auto": "Авто: бот отвечает сам ответами, где вы включили «авто». Остальное — подсказками"}[cfg().mode]
    await c.answer(note, show_alert=True)


@router.callback_query(F.data.in_({"sb:aisug", "sb:aiauto"}))
async def sb_ai_toggle(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    if c.data == "sb:aisug":
        cfg().ai_suggest = not cfg().ai_suggest
    else:
        cfg().ai_auto = not cfg().ai_auto
        if cfg().ai_auto:
            await c.answer("⚠️ ИИ будет отправлять ответы сам (в режиме «Авто»), когда в базе нет подходящего. "
                           "Он может ошибиться — следите за уведомлениями.", show_alert=True)
    config_manager.save()
    await _edit(c, main_text(), main_kb())
    if c.data == "sb:aisug" or not cfg().ai_auto:
        await c.answer()


@router.callback_query(F.data == "sb:help")
async def sb_help(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    text = (
        "ℹ️ <b>Как это работает</b>\n\n"
        "1️⃣ <b>Сбор.</b> Бот запоминает переписку только в чатах, где <b>первым написал покупатель</b>. "
        "Чаты, которые начали вы, не учитываются. Новые чаты он запоминает сам, старые — по кнопке "
        "«📥 Изучить переписку» (последние ~200 чатов).\n\n"
        "2️⃣ <b>Ваши ответы.</b> Бот отличает ваши ручные ответы (с сайта или «📨 Ответить» в Telegram) "
        "от автоответов бота и системных сообщений FunPay. Учится только на ваших.\n\n"
        "3️⃣ <b>База ответов.</b> Похожие вопросы покупателей группируются, к каждой группе берётся ваш "
        "типичный ответ. Имя покупателя заменяется на $username. Любой ответ можно исправить или удалить.\n\n"
        "4️⃣ <b>Режимы:</b>\n"
        "• 💡 <b>Подсказки</b> — на новый вопрос бот присылает готовый ответ, вы жмёте «✅ Отправить».\n"
        "• 🤖 <b>Авто</b> — бот отвечает сам, но <b>только</b> ответами, где вы включили «🤖 авто», "
        "и только если вопрос очень похож. Перед ответом ждёт 15–45 сек — если вы ответили сами, промолчит. "
        "Не больше 3 автоответов в чате за сутки; не отвечает, если вы сами переписывались в чате последние 10 минут.\n\n"
        "🔒 Ответы, похожие на выдачу товара (ключи, логины, пароли, почты), <b>никогда</b> не отправляются автоматически.\n\n"
        "🧠 <b>ИИ</b> (если подключён): подсказка в вашем стиле, когда в базе ничего не нашлось, "
        "и «Советы ИИ» — что улучшить, чтобы продавать больше.\n\n"
        "Все данные хранятся только у вас в боте (data/sales_brain.db)."
    )
    await _edit(c, text, _kb([BACK]))
    await c.answer()


# ═════════════════════════════════════════════════════════════════════════════
# Изучение, анализ, советы
# ═════════════════════════════════════════════════════════════════════════════

@router.callback_query(F.data == "sb:import")
async def sb_import(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    if sb.importing():
        await c.answer("Уже идёт, подождите", show_alert=True); return
    await c.answer("Начинаю — это займёт несколько минут")
    status = await c.message.answer("📥 Начинаю изучать переписку…")
    last = {"t": 0.0}

    async def progress(text: str) -> None:
        if time.monotonic() - last["t"] < 3:
            return
        last["t"] = time.monotonic()
        try:
            await status.edit_text(text)
        except Exception:
            pass

    async def job():
        try:
            res = await sb.import_history(progress)
        except Exception as e:
            logger.exception(f"sales import: {e}")
            res = f"❌ Ошибка: {html.escape(str(e))}"
        try:
            await status.edit_text(res, reply_markup=_kb([[("📊 Анализ", "sb:report"), ("📚 База ответов", "sb:kb:0")], BACK]))
        except Exception:
            pass
    _spawn(job())


@router.callback_query(F.data == "sb:report")
async def sb_report(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    await c.answer("Считаю…")
    try:
        text = await asyncio.get_running_loop().run_in_executor(None, sb.report_html)
    except Exception as e:
        logger.exception(f"sales report: {e}")
        text = f"❌ Не удалось посчитать: {html.escape(str(e))}"
    await c.message.answer(text, reply_markup=_kb([[("🧠 Советы ИИ", "sb:advice"), ("📚 База ответов", "sb:kb:0")], BACK]),
                           disable_web_page_preview=True)


@router.callback_query(F.data == "sb:advice")
async def sb_advice(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    await c.answer("Спрашиваю ИИ…")
    status = await c.message.answer("🧠 Анализирую переписку с ИИ…")

    async def job():
        try:
            text = await sb.ai_advice()
        except Exception as e:
            text = f"❌ Ошибка ИИ: {html.escape(str(e))}"
        try:
            await status.edit_text(text[:4000], reply_markup=_kb([BACK]))
        except Exception:
            pass
    _spawn(job())


@router.callback_query(F.data == "sb:build")
async def sb_build(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    try:
        added, updated = await asyncio.get_running_loop().run_in_executor(None, sb.rebuild_answers)
        await c.answer(f"База обновлена: +{added} новых, обновлено {updated}", show_alert=True)
    except Exception as e:
        logger.exception(f"sales build: {e}")
        await c.answer(f"Ошибка: {e}", show_alert=True)
    await _edit(c, main_text(), main_kb())


# ═════════════════════════════════════════════════════════════════════════════
# База ответов
# ═════════════════════════════════════════════════════════════════════════════

def _short(s: str, n: int = 28) -> str:
    s = (s or "").replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


@router.callback_query(F.data.startswith("sb:kb:"))
async def sb_kb(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.clear()
    page = int(c.data.split(":")[2])
    items = sb.list_answers()
    pages = max(1, (len(items) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE:(page + 1) * PAGE]
    text = ("📚 <b>База ответов</b>\n\n"
            "🤖 — бот отвечает сам (в режиме «Авто»), 💡 — только подсказка, 🔒 — похоже на выдачу товара.\n"
            "Нажмите на ответ, чтобы посмотреть, исправить или включить «авто».")
    if not items:
        text += "\n\nПока пусто. «📥 Изучить переписку» или «➕ Добавить вручную»."
    rows = [[(f"{'🔒' if a.risky else ('🤖' if a.auto else '💡')} ×{a.uses} {_short(a.examples[0] if a.examples else '')}",
              f"sb:e:{a.id}")] for a in chunk]
    nav = []
    if page > 0:
        nav.append(("◀️", f"sb:kb:{page - 1}"))
    if pages > 1:
        nav.append((f"{page + 1}/{pages}", f"sb:kb:{page}"))
    if page < pages - 1:
        nav.append(("▶️", f"sb:kb:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("➕ Добавить вручную", "sb:add"), ("🤖 Авто для частых", "sb:autoall")])
    rows.append(BACK)
    await _edit(c, text, _kb(rows))
    await c.answer()


def _entry_text(a: sb.Answer) -> str:
    ex = "\n".join(f"• {html.escape(_short(e, 120))}" for e in a.examples[:6])
    warn = "\n\n🔒 <b>Похоже на выдачу товара/данные</b> — автоматически не отправляется." if a.risky else ""
    return (f"📚 <b>Ответ #{a.id}</b> · встречался ×{a.uses} · отправлен ботом ×{a.sent}\n\n"
            f"<b>Вопросы покупателей:</b>\n{ex}\n\n<b>Ответ:</b>\n<code>{html.escape(a.answer[:2500])}</code>"
            f"\n\n{'🤖 Бот отвечает сам (режим «Авто»)' if a.auto else '💡 Только подсказка'}"
            f"{' · ✏️ исправлен вами' if a.edited else ''}{warn}")


def _entry_kb(a: sb.Answer) -> types.InlineKeyboardMarkup:
    return _kb([
        [(("🤖 Авто: вкл" if a.auto else "💡 Авто: выкл — включить"), f"sb:ea:{a.id}")],
        [("✏️ Исправить ответ", f"sb:ee:{a.id}"), ("🗑 Удалить", f"sb:ed:{a.id}")],
        [("⬅️ К списку", "sb:kb:0")],
    ])


@router.callback_query(F.data.startswith("sb:e:"))
async def sb_entry(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.clear()
    a = sb.get_answer(int(c.data.split(":")[2]))
    if not a:
        await c.answer("Ответ не найден", show_alert=True); return
    await _edit(c, _entry_text(a), _entry_kb(a))
    await c.answer()


@router.callback_query(F.data.startswith("sb:ea:"))
async def sb_entry_auto(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    a = sb.get_answer(int(c.data.split(":")[2]))
    if not a:
        await c.answer("Ответ не найден", show_alert=True); return
    if not a.auto and a.risky:
        await c.answer("🔒 Этот ответ похож на выдачу товара — включать «авто» нельзя. Исправьте текст.", show_alert=True)
        return
    sb.update_answer(a.id, auto=not a.auto)
    a = sb.get_answer(a.id)
    await _edit(c, _entry_text(a), _entry_kb(a))
    await c.answer("Бот будет отвечать сам" if a.auto else "Только подсказка")


@router.callback_query(F.data == "sb:autoall")
async def sb_autoall(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    n = 0
    for a in sb.list_answers():
        if not a.auto and not a.risky and a.uses >= 3:
            sb.update_answer(a.id, auto=True)
            n += 1
    await c.answer(f"Включено «авто» для {n} ответов, которые встречались 3+ раза (кроме 🔒).", show_alert=True)
    c.data = "sb:kb:0"
    await sb_kb(c, state)


@router.callback_query(F.data.startswith("sb:ed:"))
async def sb_entry_del(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    sb.delete_answer(int(c.data.split(":")[2]))
    await c.answer("Удалено")
    c.data = "sb:kb:0"
    await sb_kb(c, state)


@router.callback_query(F.data.startswith("sb:ee:"))
async def sb_entry_edit(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    aid = int(c.data.split(":")[2])
    await state.set_state(SalesStates.edit_answer)
    await state.update_data(aid=aid)
    await c.message.answer("✏️ Пришлите новый текст ответа. Можно использовать <code>$username</code> — имя покупателя.",
                           reply_markup=_kb([[("❌ Отмена", f"sb:e:{aid}")]]))
    await c.answer()


@router.message(SalesStates.edit_answer, F.text)
async def sb_entry_edit_in(msg: types.Message, state: FSMContext) -> None:
    aid = (await state.get_data()).get("aid")
    await state.clear()
    sb.update_answer(aid, answer=msg.text.strip(), edited=True)
    a = sb.get_answer(aid)
    if a and a.auto and a.risky:
        sb.update_answer(aid, auto=False)
        a = sb.get_answer(aid)
    await msg.answer("✅ Сохранено.\n\n" + _entry_text(a), reply_markup=_entry_kb(a))


@router.callback_query(F.data == "sb:add")
async def sb_add(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.set_state(SalesStates.add_question)
    await c.message.answer("➕ Пришлите вопрос покупателя (можно несколько вариантов — каждый с новой строки).",
                           reply_markup=_kb([[("❌ Отмена", "sb:kb:0")]]))
    await c.answer()


@router.message(SalesStates.add_question, F.text)
async def sb_add_q(msg: types.Message, state: FSMContext) -> None:
    qs = [x.strip() for x in msg.text.splitlines() if x.strip()]
    await state.update_data(qs=qs)
    await state.set_state(SalesStates.add_answer)
    await msg.answer("💬 Теперь пришлите ответ (можно с <code>$username</code>).",
                     reply_markup=_kb([[("❌ Отмена", "sb:kb:0")]]))


@router.message(SalesStates.add_answer, F.text)
async def sb_add_a(msg: types.Message, state: FSMContext) -> None:
    qs = (await state.get_data()).get("qs") or []
    await state.clear()
    aid = sb.add_answer(qs, msg.text.strip(), uses=0, edited=True)
    a = sb.get_answer(aid)
    await msg.answer("✅ Добавлено.\n\n" + _entry_text(a), reply_markup=_entry_kb(a))


# ═════════════════════════════════════════════════════════════════════════════
# Кнопки под подсказкой
# ═════════════════════════════════════════════════════════════════════════════

async def _send_pending(target: types.Message, pid: str, text: str) -> None:
    p = sb.pending.get(pid)
    if not p:
        await target.answer("⌛ Подсказка устарела — ответьте через «📨 Ответить»."); return
    ok = await sb.send(p["acc"], p["chat_id"], text, p["buyer"], p["chat_name"])
    if ok:
        sb.note_manual(p["acc"], p["chat_id"], text)
        if p.get("answer_id"):
            a = sb.get_answer(p["answer_id"])
            if a:
                sb.update_answer(a.id, sent=a.sent + 1)
        sb.pending.pop(pid, None)
        await target.answer(f"✅ Отправлено {html.escape(p['buyer'])}:\n<code>{html.escape(text[:500])}</code>")
    else:
        await target.answer("❌ Не удалось отправить — FunPay недоступен. Попробуйте ещё раз.")


@router.callback_query(F.data.startswith("sb:go:"))
async def sb_go(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    pid = c.data.split(":")[2]
    p = sb.pending.get(pid)
    await c.answer("Отправляю…")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send_pending(c.message, pid, p["answer"] if p else "")


@router.callback_query(F.data.startswith("sb:edit:"))
async def sb_edit_pending(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    pid = c.data.split(":")[2]
    if pid not in sb.pending:
        await c.answer("Подсказка устарела", show_alert=True); return
    await state.set_state(SalesStates.edit_pending)
    await state.update_data(pid=pid)
    await c.message.answer("✏️ Пришлите исправленный ответ — отправлю его покупателю.",
                           reply_markup=_kb([[("❌ Отмена", f"sb:skip:{pid}")]]))
    await c.answer()


@router.message(SalesStates.edit_pending, F.text)
async def sb_edit_pending_in(msg: types.Message, state: FSMContext) -> None:
    pid = (await state.get_data()).get("pid")
    await state.clear()
    await _send_pending(msg, pid, msg.text.strip())


@router.callback_query(F.data.startswith("sb:skip:"))
async def sb_skip(c: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    sb.pending.pop(c.data.split(":")[2], None)
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await c.answer("Ок, не отправляю")


# ═════════════════════════════════════════════════════════════════════════════
# Настройки
# ═════════════════════════════════════════════════════════════════════════════

AUTO_TH = [0.5, 0.6, 0.7, 0.8]
SUG_TH = [0.35, 0.45, 0.55]
DELAYS = [(5, 15), (15, 45), (30, 90), (60, 180)]
LIMITS = [1, 2, 3, 5, 10]
HIST = [50, 100, 200, 400]


def _next(seq, cur):
    try:
        return seq[(seq.index(cur) + 1) % len(seq)]
    except ValueError:
        return seq[0]


def _set_kb() -> types.InlineKeyboardMarkup:
    c = cfg()
    return _kb([
        [(f"🎯 Точность для авто: {int(c.auto_threshold * 100)}%", "sb:s:ath")],
        [(f"💡 Точность для подсказки: {int(c.suggest_threshold * 100)}%", "sb:s:sth")],
        [(f"⏱ Пауза перед автоответом: {c.auto_delay_min}–{c.auto_delay_max} сек", "sb:s:dl")],
        [(f"🔁 Автоответов в чате за сутки: {c.max_auto_per_chat}", "sb:s:lim")],
        [(f"📥 Изучать последних чатов: {c.history_chats_limit}", "sb:s:hist")],
        [(f"{'✅' if c.collect else '❌'} Собирать переписку", "sb:s:col")],
        [("🗑 Стереть собранную переписку", "sb:s:wipe")],
        BACK,
    ])


SET_TEXT = ("⚙️ <b>Настройки автопродаж</b>\n\nНажимайте на кнопку — значение меняется по кругу.\n"
            "• <b>Точность</b> — насколько вопрос должен быть похож на вопрос из базы.\n"
            "  Выше — реже, но точнее.\n"
            "• <b>Пауза</b> — сколько бот ждёт перед автоответом: успеете ответить сами — он промолчит.")


@router.callback_query(F.data == "sb:set")
async def sb_set(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    await _edit(c, SET_TEXT, _set_kb())
    await c.answer()


@router.callback_query(F.data.startswith("sb:s:"))
async def sb_set_change(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    key, x = c.data.split(":")[2], cfg()
    if key == "ath":
        x.auto_threshold = _next(AUTO_TH, x.auto_threshold)
    elif key == "sth":
        x.suggest_threshold = _next(SUG_TH, x.suggest_threshold)
    elif key == "dl":
        x.auto_delay_min, x.auto_delay_max = _next(DELAYS, (x.auto_delay_min, x.auto_delay_max))
    elif key == "lim":
        x.max_auto_per_chat = _next(LIMITS, x.max_auto_per_chat)
    elif key == "hist":
        x.history_chats_limit = _next(HIST, x.history_chats_limit)
    elif key == "col":
        x.collect = not x.collect
    elif key == "wipe":
        await _edit(c, "🗑 Стереть всю собранную переписку? База ответов останется.",
                    _kb([[("✅ Да, стереть", "sb:s:wipeok")], [("❌ Нет", "sb:set")]]))
        await c.answer(); return
    elif key == "wipeok":
        with sb._db_lock:
            conn = sb.db()
            conn.executescript("DELETE FROM messages; DELETE FROM chats; DELETE FROM manual_sent; DELETE FROM auto_log;")
            conn.commit()
        await c.answer("Переписка стёрта", show_alert=True)
    config_manager.save()
    await _edit(c, SET_TEXT, _set_kb())
    if key != "wipeok":
        await c.answer()
