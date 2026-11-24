"""
Быстрая смена golden_key прямо из Telegram (удобно с телефона).

Способы:
  • /menu → 🔑 Обновить golden_key → прислать ключ;
  • /golden_key → прислать ключ;
  • просто прислать боту 32-символьный ключ в любой момент — бот сам предложит его поставить.

Перед сохранением ключ проверяется входом на FunPay (покажет ник аккаунта).
После сохранения бот сам перезапускается и подключается с новым ключом.
Сообщение с ключом бот удаляет из чата, чтобы ключ не висел в переписке.
"""
from __future__ import annotations

import asyncio
import html
import re

from aiogram import F, Router, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.handlers.auth import is_admin
from bot.states import GoldenKeyStates
from config.settings import FunPayAccountSettings, config_manager
from utils.logger import logger

router = Router(name="golden_key")

KEY_RX = re.compile(r"^\s*([a-zA-Z0-9]{32})\s*$")

HOWTO = (
    "🔑 <b>Новый golden_key</b>\n\n"
    "Пришлите ключ одним сообщением (32 буквы/цифры).\n\n"
    "<b>Где взять:</b>\n"
    "• <b>ПК:</b> funpay.com → F12 → Application → Cookies → <code>golden_key</code>\n"
    "• <b>Android:</b> Firefox или Kiwi Browser + расширение «Cookie-Editor» → "
    "зайти на funpay.com → открыть Cookie-Editor → <code>golden_key</code> → скопировать\n"
    "• <b>iPhone:</b> проще всего взять с ПК\n\n"
    "⚠️ Берите ключ с того же IP/прокси, через который работает бот, иначе FunPay может разлогинить.\n"
    "🗑 Сообщение с ключом я удалю из чата сразу после проверки."
)


def _kb(*rows) -> types.InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for text, cb in rows:
        b.button(text=text, callback_data=cb)
    b.adjust(1)
    return b.as_markup()


def _check_key_sync(key: str):
    """Пробует войти на FunPay с ключом. → (ok, текст)."""
    from core.funpay_client import FUNPAY_AVAILABLE, Account, FunPayClient
    if not FUNPAY_AVAILABLE or Account is None:
        return None, "FunPayAPI не загружена — проверить ключ нельзя."
    fp = config_manager.settings.funpay
    proxy = FunPayClient._build_proxy_dict(fp.proxy) if fp.proxy else None
    try:
        acc = Account(key, user_agent=fp.user_agent, proxy=proxy).get()
        return True, f"аккаунт <b>{html.escape(str(acc.username))}</b> (ID {acc.id})"
    except Exception as e:
        name = type(e).__name__
        if "Unauthorized" in name or "Unauthorized" in str(e):
            return False, "FunPay не принял ключ (неверный, устарел или взят с другого IP)."
        return None, f"не удалось проверить ({name}: {str(e)[:120]})"


def _save_key(key: str) -> None:
    fp = config_manager.settings.funpay
    fp.golden_key = key
    fp.account_id = None
    fp.username = None
    if not fp.accounts:
        fp.accounts = [FunPayAccountSettings(golden_key=key, proxy=fp.proxy, user_agent=fp.user_agent)]
        fp.active_index = 0
    config_manager.save()


async def _apply_and_restart(target: types.Message, key: str, checked_text: str = "") -> None:
    _save_key(key)
    logger.info("golden_key обновлён из Telegram — перезапуск")
    await target.answer(
        "✅ golden_key сохранён" + (f": {checked_text}" if checked_text else "") +
        "\n♻️ Перезапускаюсь — через ~10 секунд всё заработает с новым ключом."
    )
    from utils.restart import restart_soon
    restart_soon(2.0)


async def _process_key(msg: types.Message, state: FSMContext, key: str) -> None:
    try:
        await msg.delete()  # не оставляем ключ в переписке
    except Exception:
        pass
    status = await msg.answer("⏳ Проверяю ключ на FunPay…")
    ok, text = await asyncio.get_running_loop().run_in_executor(None, _check_key_sync, key)
    if ok:
        await state.clear()
        try:
            await status.delete()
        except Exception:
            pass
        await _apply_and_restart(msg, key, text)
        return
    await state.set_state(GoldenKeyStates.confirm)
    await state.update_data(gk=key)
    await status.edit_text(
        f"⚠️ Ключ …{key[-4:]}: {html.escape(text) if ok is None else text}\n\nВсё равно сохранить?",
        reply_markup=_kb(("💾 Сохранить всё равно", "gk:force"),
                         ("🔁 Прислать другой", "gk:ask"),
                         ("❌ Отмена", "gk:cancel")),
    )


# ─── Точки входа ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "gk:ask")
async def gk_ask(c: types.CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        await c.answer("⛔ Только для админов", show_alert=True); return
    await state.set_state(GoldenKeyStates.waiting_key)
    cur = config_manager.settings.funpay.golden_key
    tail = f"\n\nСейчас стоит: <code>…{cur[-4:]}</code>" if cur else "\n\nСейчас ключ не задан."
    await c.message.answer(HOWTO + tail, reply_markup=_kb(("❌ Отмена", "gk:cancel")))
    await c.answer()


@router.message(Command("golden_key"))
async def cmd_golden_key(msg: types.Message, state: FSMContext) -> None:
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔ Только для админов."); return
    parts = (msg.text or "").split(maxsplit=1)
    if len(parts) == 2 and KEY_RX.match(parts[1]):
        await _process_key(msg, state, KEY_RX.match(parts[1]).group(1)); return
    await state.set_state(GoldenKeyStates.waiting_key)
    await msg.answer(HOWTO, reply_markup=_kb(("❌ Отмена", "gk:cancel")))


@router.message(GoldenKeyStates.waiting_key, F.text)
async def gk_receive(msg: types.Message, state: FSMContext) -> None:
    m = KEY_RX.match(msg.text or "")
    if not m:
        await msg.answer("❌ Это не похоже на golden_key: нужно ровно 32 латинские буквы/цифры. "
                         "Пришлите ещё раз или нажмите «Отмена».",
                         reply_markup=_kb(("❌ Отмена", "gk:cancel")))
        return
    await _process_key(msg, state, m.group(1))


@router.message(StateFilter(None), F.text.regexp(KEY_RX),
                lambda m: bool(m.from_user) and is_admin(m.from_user.id))
async def gk_detect(msg: types.Message, state: FSMContext) -> None:
    """Админ просто прислал 32-символьный ключ — предлагаем поставить его."""
    key = KEY_RX.match(msg.text).group(1)
    try:
        await msg.delete()
    except Exception:
        pass
    await state.set_state(GoldenKeyStates.confirm)
    await state.update_data(gk=key)
    await msg.answer(
        f"🔑 Похоже на golden_key (…{key[-4:]}). Поставить его?",
        reply_markup=_kb(("✅ Да, проверить и поставить", "gk:check"), ("❌ Нет", "gk:cancel")),
    )


@router.callback_query(F.data == "gk:check")
async def gk_check(c: types.CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        await c.answer("⛔", show_alert=True); return
    key = (await state.get_data()).get("gk")
    await c.answer()
    if not key:
        await c.message.edit_text("Ключ потерялся — пришлите его ещё раз."); return
    await c.message.edit_text("⏳ Проверяю ключ на FunPay…")
    ok, text = await asyncio.get_running_loop().run_in_executor(None, _check_key_sync, key)
    if ok:
        await state.clear()
        await _apply_and_restart(c.message, key, text)
        return
    await c.message.edit_text(
        f"⚠️ Ключ …{key[-4:]}: {text}\n\nВсё равно сохранить?",
        reply_markup=_kb(("💾 Сохранить всё равно", "gk:force"), ("❌ Отмена", "gk:cancel")),
    )


@router.callback_query(F.data == "gk:force")
async def gk_force(c: types.CallbackQuery, state: FSMContext) -> None:
    if not is_admin(c.from_user.id):
        await c.answer("⛔", show_alert=True); return
    key = (await state.get_data()).get("gk")
    await state.clear()
    await c.answer()
    if not key:
        await c.message.edit_text("Ключ потерялся — пришлите его ещё раз."); return
    await c.message.edit_reply_markup(reply_markup=None)
    await _apply_and_restart(c.message, key)


@router.callback_query(F.data == "gk:cancel")
async def gk_cancel(c: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await c.message.edit_text("❌ Отменено. Ключ не менялся. /menu")
    except Exception:
        pass
    await c.answer()


async def notify_funpay_down(reason: str = "") -> None:
    """Сообщение админам с кнопкой смены ключа (если FunPay не подключился)."""
    from modules.notifications import _send_to_all
    text = "⚠️ <b>FunPay не подключён.</b>"
    if reason:
        text += f"\n\n{html.escape(reason[:500])}"
    text += "\n\nСкорее всего, нужен новый golden_key — нажмите кнопку и пришлите его."
    await _send_to_all(text, _kb(("🔑 Обновить golden_key", "gk:ask")))
