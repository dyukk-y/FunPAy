"""Кнопки управления автопубликацией (карточки заказов и /ads)."""
from __future__ import annotations

import html

from aiogram import F, Router, types
from aiogram.fsm.context import FSMContext

from bot.handlers.auth import is_admin
from modules import ad_autopost as ap
from modules import ad_orders as ad

router = Router(name="ad_autopost")


async def _ok(c: types.CallbackQuery) -> bool:
    if c.from_user and is_admin(c.from_user.id):
        return True
    await c.answer("⛔ Только для админов", show_alert=True)
    return False


def _oid(c: types.CallbackQuery) -> str:
    return c.data.split(":")[2]


async def _strip_kb(c: types.CallbackQuery) -> None:
    try:
        await c.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


@router.callback_query(F.data.startswith("ap:go:"))
async def ap_go(c: types.CallbackQuery) -> None:
    """Опубликовать сейчас — без очереди и проверки."""
    if not await _ok(c):
        return
    oid = _oid(c)
    e = ad.get(oid)
    if not e:
        await c.answer("Заказ не найден", show_alert=True); return
    p = ap._post(e)
    if not (ap.post_text(p) or p["images"]):
        await c.answer("Покупатель ещё не прислал текст поста — публиковать нечего", show_alert=True); return
    await c.answer("Публикую…")
    await _strip_kb(c)
    if p.get("stage") == ap.FAILED:
        p["retries"] = 0
        ap._save_post(oid, p)
    await ap.request_publish(oid, approved=True, now=True)


@router.callback_query(F.data.startswith("ap:ok:"))
async def ap_ok(c: types.CallbackQuery) -> None:
    """Одобрено вами (проверка перед публикацией) — с учётом очереди."""
    if not await _ok(c):
        return
    await c.answer("Одобрено")
    await _strip_kb(c)
    await ap.request_publish(_oid(c), approved=True)


@router.callback_query(F.data.startswith("ap:reset:"))
async def ap_reset(c: types.CallbackQuery) -> None:
    if not await _ok(c):
        return
    await ap.reset(_oid(c))
    await _strip_kb(c)
    await c.answer("Попросил покупателя прислать пост заново", show_alert=True)


@router.callback_query(F.data.startswith("ap:manual:"))
async def ap_manual(c: types.CallbackQuery) -> None:
    if not await _ok(c):
        return
    oid = _oid(c)
    ap.to_manual(oid)
    await _strip_kb(c)
    await c.message.answer(f"✍️ Ок, заказ #{oid} публикуете вы. Когда пост выйдет — просто пришлите мне ссылку, "
                           "я отправлю её покупателю со сроком.")
    await c.answer()


@router.callback_query(F.data.startswith("ap:del:"))
async def ap_del(c: types.CallbackQuery) -> None:
    if not await _ok(c):
        return
    ok, err = await ap.delete_post(_oid(c))
    if ok:
        await _strip_kb(c)
    await c.answer("🗑 Пост удалён из канала" if ok else f"Не получилось: {err}", show_alert=True)


@router.callback_query(F.data.startswith("ap:pin:") | F.data.startswith("ap:unpin:"))
async def ap_pin(c: types.CallbackQuery) -> None:
    if not await _ok(c):
        return
    pin = c.data.startswith("ap:pin:")
    oid = _oid(c)
    ok, err = await ap.set_pin(oid, pin)
    if ok:
        try:
            await c.message.edit_reply_markup(reply_markup=ap._kb_post(oid))
        except Exception:
            pass
    await c.answer(("📌 Закреплён" if pin else "📍 Откреплён") if ok else f"Не получилось: {err}", show_alert=not ok)


@router.callback_query(F.data == "ap:check")
async def ap_check(c: types.CallbackQuery, state: FSMContext) -> None:
    if not await _ok(c):
        return
    ok, msg = await ap.channel_status(force=True)
    await c.answer(msg[:190], show_alert=True)
    from bot.handlers.ad_orders import ad_menu
    await ad_menu(c, state)
