"""
📢 Заказы рекламы в Telegram: ссылка на пост → сообщение покупателю.
Логика — modules/ad_orders.py. Команда /ads.
"""
from __future__ import annotations

import html

from aiogram import F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.handlers.auth import is_admin
from bot.states import AdStates
from config.settings import AdFlowSettings, config_manager
from modules import ad_orders as ad
from utils import ad_duration

router = Router(name="ad_orders")

DUR_PRESETS = [(6, "6 ч"), (12, "12 ч"), (24, "24 ч"), (48, "48 ч"), (72, "72 ч"),
               (24 * 7, "7 дней"), (ad_duration.FOREVER, "навсегда")]


def _kb(rows) -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        for text, cb in row:
            b.button(text=text, callback_data=cb)
    b.adjust(*[len(r) for r in rows])
    return b.as_markup()


async def _deny(ev) -> bool:
    if ev.from_user and is_admin(ev.from_user.id):
        return False
    if isinstance(ev, types.CallbackQuery):
        await ev.answer("⛔ Только для админов", show_alert=True)
    return True


def _admin_with_link(m: types.Message) -> bool:
    return bool(m.from_user and is_admin(m.from_user.id) and m.text and ad.extract_link(m.text)
                and ad.manual_waiting())


async def _report(target: types.Message, order_id: str, res: tuple) -> None:
    status, info = res
    e = ad.get(order_id)
    who = html.escape((e or {}).get("buyer") or "покупателю")
    if status == "sent":
        await target.answer(f"✅ Отправил {who}:\n<code>{html.escape(info)}</code>\n\n"
                            "Когда покупатель подтвердит заказ — поблагодарю и попрошу отзыв.")
    elif status == "need_duration":
        await target.answer(f"⏱ Ссылку принял. Не нашёл в лоте срок размещения — выберите, и я сразу отправлю {who}:",
                            reply_markup=_dur_kb(order_id))
    else:
        await target.answer(f"❌ Не отправлено: {html.escape(info)}")


def _dur_kb(order_id: str) -> types.InlineKeyboardMarkup:
    rows = [[(t, f"ad:h:{order_id}:{h}") for h, t in DUR_PRESETS[i:i + 4]] for i in range(0, len(DUR_PRESETS), 4)]
    rows.append([("✍️ Другой срок", f"ad:hc:{order_id}")])
    return _kb(rows)


# ─── Ссылка ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ad:link:"))
async def ad_link(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    oid = c.data.split(":")[2]
    e = ad.get(oid)
    if not e:
        await c.answer("Заказ уже закрыт", show_alert=True); return
    await state.set_state(AdStates.link)
    await state.update_data(order_id=oid)
    await c.message.answer(f"🔗 Пришлите ссылку на пост для {html.escape(e.get('buyer') or 'покупателя')} "
                           f"(заказ #{oid}).", reply_markup=_kb([[("❌ Отмена", "ad:cancel")]]))
    await c.answer()


@router.message(AdStates.link, F.text)
async def ad_link_in(msg: types.Message, state: FSMContext) -> None:
    link = ad.extract_link(msg.text)
    if not link:
        await msg.answer("❌ Это не похоже на ссылку. Пример: https://t.me/channel/123",
                         reply_markup=_kb([[("❌ Отмена", "ad:cancel")]]))
        return
    oid = (await state.get_data()).get("order_id")
    await state.clear()
    await _report(msg, oid, await ad.attach_link(oid, link))


@router.message(StateFilter(None), F.text, _admin_with_link)
async def ad_link_plain(msg: types.Message, state: FSMContext) -> None:
    """Админ просто прислал ссылку, а заказы ждут — подставляем в нужный."""
    link = ad.extract_link(msg.text)
    waiting = ad.manual_waiting()
    if len(waiting) == 1:
        await _report(msg, waiting[0]["order_id"], await ad.attach_link(waiting[0]["order_id"], link))
        return
    await state.set_state(AdStates.choose)
    await state.update_data(link=link)
    rows = [[(f"{e.get('buyer') or '?'} · {(e.get('lot') or '')[:25]}", f"ad:use:{e['order_id']}")] for e in waiting[:10]]
    rows.append([("❌ Это не ссылка на пост", "ad:cancel")])
    await msg.answer("🔗 Для какого заказа эта ссылка?", reply_markup=_kb(rows))


@router.callback_query(F.data.startswith("ad:use:"))
async def ad_use(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    link = (await state.get_data()).get("link")
    await state.clear()
    await c.answer()
    if not link:
        await c.message.answer("Ссылка потерялась — пришлите её ещё раз."); return
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    oid = c.data.split(":")[2]
    await _report(c.message, oid, await ad.attach_link(oid, link))


# ─── Срок ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("ad:dur:"))
async def ad_dur(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    oid = c.data.split(":")[2]
    await c.message.answer(f"⏱ Сколько висит реклама по заказу #{oid}?", reply_markup=_dur_kb(oid))
    await c.answer()


@router.callback_query(F.data.startswith("ad:h:"))
async def ad_h(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    _, _, oid, hours = c.data.split(":")
    await c.answer("Сохранено")
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    res = await ad.set_duration(oid, int(hours))
    if res[0] == "ok":
        e = ad.get(oid)
        await c.message.answer(ad.card_text(e), reply_markup=ad.card_kb(e))
    else:
        await _report(c.message, oid, res)


@router.callback_query(F.data.startswith("ad:hc:"))
async def ad_hc(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.set_state(AdStates.duration)
    await state.update_data(order_id=c.data.split(":")[2])
    await c.message.answer("✍️ Напишите срок, например: <code>36 часов</code>, <code>5 дней</code>, <code>2 недели</code>, "
                           "<code>навсегда</code>.", reply_markup=_kb([[("❌ Отмена", "ad:cancel")]]))
    await c.answer()


@router.message(AdStates.duration, F.text)
async def ad_hc_in(msg: types.Message, state: FSMContext) -> None:
    found = ad_duration.find_duration(msg.text)
    if not found:
        await msg.answer("❌ Не понял срок. Пример: 24 часа, 3 дня, навсегда", reply_markup=_kb([[("❌ Отмена", "ad:cancel")]]))
        return
    oid = (await state.get_data()).get("order_id")
    await state.clear()
    res = await ad.set_duration(oid, found[0], found[1])
    if res[0] == "ok":
        e = ad.get(oid)
        await msg.answer(ad.card_text(e), reply_markup=ad.card_kb(e))
    else:
        await _report(msg, oid, res)


@router.callback_query(F.data.startswith("ad:skip:"))
async def ad_skip(c: types.CallbackQuery) -> None:
    if await _deny(c):
        return
    ad.forget_order(c.data.split(":")[2])
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await c.answer("Ок, ссылку по этому заказу не жду")


@router.callback_query(F.data == "ad:cancel")
async def ad_cancel(c: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await c.answer("Отменено")


# ─── Меню /ads ──────────────────────────────────────────────────────────────

QUIET = [(-1, -1), (0, 8), (1, 9), (23, 8)]


def _quiet_label() -> str:
    a, b = config_manager.settings.ad_post.quiet_from, config_manager.settings.ad_post.quiet_to
    return "выкл" if a < 0 or b < 0 else f"{a:02d}:00–{b:02d}:00"


def menu_text() -> str:
    from modules import ad_autopost as ap
    x, pc = config_manager.settings.ad_flow, config_manager.settings.ad_post
    active = [e for e in ad._load().values() if e.get("status") in (ad.WAITING, ad.NEED_DURATION)]
    lines = ["📢 <b>Реклама</b>", ""]
    if pc.active():
        st = ap._channel_cache.get("msg") or "канал ещё не проверялся — нажмите «🔄 Проверить канал»"
        lines += [
            "🤖 <b>Автопубликация включена.</b> После оплаты бот сам попросит у покупателя текст и фото "
            "в чате FunPay, покажет предпросмотр, опубликует пост в канал, пришлёт покупателю ссылку и срок, "
            "а по окончании срока удалит пост.",
            f"📣 {html.escape(st)}",
            f"🔎 Проверка перед публикацией: <b>{'вы одобряете каждый пост' if pc.require_approval else 'нет — публикую сразу'}</b>",
            f"🗑 Удалять по окончании срока: <b>{'да' if pc.delete_after_expiry else 'нет'}</b>",
            f"🌙 Тихие часы (не публиковать): <b>{_quiet_label()}</b>",
            f"✍️ Подпись под постом: <b>{html.escape(pc.footer) if pc.footer else 'нет'}</b>",
        ]
    else:
        lines.append(f"{'🟢' if x.enabled else '🔴'} Ручной режим: после оплаты бот ждёт от вас ссылку на пост "
                     "и сам пишет покупателю.")
    lines += ["", "<b>Сообщение покупателю после публикации:</b>", f"<code>{html.escape(x.template)}</code>",
              f"Срок, если не найден в лоте: <b>{ad_duration.human(x.default_hours) if x.default_hours else 'спросить у вас'}</b>",
              "", f"📋 Активных заказов: <b>{len(active)}</b>"]
    for e in sorted(active, key=lambda e: e.get("ts", 0))[:10]:
        line = (ap.stage_line(e) if e.get("post") else "ждёт вашу ссылку")
        lines.append(f"• {html.escape(e.get('buyer') or '?')} #{e['order_id']} — {html.escape(line[:70])}")
    return "\n".join(lines)


def menu_kb() -> types.InlineKeyboardMarkup:
    x, pc = config_manager.settings.ad_flow, config_manager.settings.ad_post
    rows = [[(f"🔗 {e.get('buyer') or '?'} · #{e['order_id']}", f"ad:link:{e['order_id']}")] for e in ad.manual_waiting()[:6]]
    rows.append([(f"{'🟢' if pc.active() else '🔴'} Автопубликация в канал", "ad:m:auto"),
                 ("🔄 Проверить канал", "ap:check")])
    if pc.active():
        rows.append([(f"🔎 Проверка: {'вкл' if pc.require_approval else 'выкл'}", "ad:m:appr"),
                     (f"🗑 Удаление: {'вкл' if pc.delete_after_expiry else 'выкл'}", "ad:m:del")])
        rows.append([(f"🌙 Тихие часы: {_quiet_label()}", "ad:m:quiet"), ("✍️ Подпись", "ad:m:footer")])
        rows.append([("💬 Текст-инструкция покупателю", "ad:m:prompt")])
    rows.append([("✏️ Сообщение со ссылкой", "ad:m:tpl"),
                 (f"⏱ Срок по умолч.: {ad_duration.human(x.default_hours) if x.default_hours else 'спросить'}", "ad:m:def")])
    rows.append([("⬅️ В главное меню", "menu:main")])
    return _kb(rows)


@router.message(Command("ads"))
async def cmd_ads(msg: types.Message, state: FSMContext) -> None:
    if not (msg.from_user and is_admin(msg.from_user.id)):
        return
    await state.clear()
    await msg.answer(menu_text(), reply_markup=menu_kb())


@router.callback_query(F.data == "ad:menu")
async def ad_menu(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    await state.clear()
    try:
        await c.message.edit_text(menu_text(), reply_markup=menu_kb())
    except Exception:
        await c.message.answer(menu_text(), reply_markup=menu_kb())
    try:
        await c.answer()
    except Exception:
        pass


@router.callback_query(F.data.startswith("ad:m:"))
async def ad_menu_action(c: types.CallbackQuery, state: FSMContext) -> None:
    if await _deny(c):
        return
    x, pc = config_manager.settings.ad_flow, config_manager.settings.ad_post
    act = c.data.split(":")[2]
    if act == "toggle":
        x.enabled = not x.enabled
    elif act == "auto":
        pc.enabled = not pc.enabled
        x.enabled = True
    elif act == "appr":
        pc.require_approval = not pc.require_approval
    elif act == "del":
        pc.delete_after_expiry = not pc.delete_after_expiry
    elif act == "quiet":
        cur = (pc.quiet_from, pc.quiet_to)
        pc.quiet_from, pc.quiet_to = QUIET[(QUIET.index(cur) + 1) % len(QUIET)] if cur in QUIET else QUIET[0]
    elif act == "def":
        steps = [0, 12, 24, 48, 72, 24 * 7]
        x.default_hours = steps[(steps.index(x.default_hours) + 1) % len(steps)] if x.default_hours in steps else 0
    elif act in ("tpl", "footer", "prompt"):
        await state.set_state(AdStates.template)
        await state.update_data(what=act)
        prompts = {
            "tpl": ("✏️ Пришлите шаблон сообщения покупателю со ссылкой. Метки:\n"
                    "<code>{link}</code> — ссылка на пост\n<code>{where}</code> — «в канале» / «в истории»\n"
                    "<code>{until}</code> — «сейчас или по истечении 24 часов»\n<code>{duration}</code> — «24 часа»\n"
                    "<code>$username</code> — ник покупателя"),
            "footer": "✍️ Пришлите подпись, которую бот добавит в конец каждого поста (например <code>#реклама</code>).",
            "prompt": "💬 Пришлите текст, которым бот после оплаты попросит покупателя прислать пост.",
        }
        await c.message.answer(prompts[act] + "\n\nОтправьте <code>-</code>, чтобы убрать / <code>default</code> — "
                               "вернуть стандартный.", reply_markup=_kb([[("❌ Отмена", "ad:menu")]]))
        await c.answer(); return
    config_manager.save()
    await ad_menu(c, state)


@router.message(AdStates.template, F.text)
async def ad_tpl_in(msg: types.Message, state: FSMContext) -> None:
    from config.settings import AdPostSettings
    what = (await state.get_data()).get("what", "tpl")
    t = msg.text.strip()
    reset = t.lower() in ("default", "по умолчанию")
    if what == "tpl":
        if reset:
            t = AdFlowSettings().template
        if "{link}" not in t:
            await msg.answer("❌ В шаблоне должна быть метка {link} — туда подставится ссылка на пост.",
                             reply_markup=_kb([[("❌ Отмена", "ad:menu")]]))
            return
        config_manager.settings.ad_flow.template = t
    elif what == "footer":
        config_manager.settings.ad_post.footer = "" if (t == "-" or reset) else t
    else:
        config_manager.settings.ad_post.prompt = AdPostSettings().prompt if (reset or t == "-") else t
    config_manager.save()
    await state.clear()
    await msg.answer("✅ Сохранено.\n\n" + menu_text(), reply_markup=menu_kb())
