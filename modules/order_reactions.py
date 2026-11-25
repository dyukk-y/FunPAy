"""
Ответ на подтверждение заказа и просьба об отзыве.

Контекстные переменные: $order_id, $order_price, $order_desc (доп. к базовым)
"""
from __future__ import annotations

import asyncio

from config.settings import config_manager
from core.event_bus import Event, event_bus
from core.funpay_client import accounts_manager
from utils.logger import logger
from utils.variables import send_with_vars

_bg_tasks: set = set()


@event_bus.on(Event.ORDER_CONFIRMED)
async def handle_order_confirmed(order) -> None:
    try:
        await _handle_order_confirmed(order)
    except Exception as e:
        logger.exception(f"order_confirm_reply: {e}")


async def _handle_order_confirmed(order) -> None:
    cfg_confirm = config_manager.settings.order_confirm_reply
    cfg_review  = config_manager.settings.ask_review
    if not ((cfg_confirm.enabled and cfg_confirm.text) or (cfg_review.enabled and cfg_review.text)):
        logger.info(f"order_confirm_reply: заказ #{getattr(order, 'id', '?')} подтверждён, "
                    f"но ответ на подтверждение и просьба об отзыве выключены")
        return
    # Один раз на заказ: подтверждение ловим двумя путями (смена статуса заказа
    # и системное сообщение в чате) — ответить нужно только один раз
    if not await _first_time("confirmed", str(getattr(order, "id", "") or "")):
        return
    # chat_id у заказа — «users-<id>-<id>»; находим настоящий ID чата
    chat_id = await accounts_manager.client_for(order).order_chat_id_async(order)
    if not chat_id:
        logger.warning(f"order_confirm_reply: нет чата для заказа #{getattr(order, 'id', '?')}")
        return

    buyer      = getattr(order, "buyer_username", "") or ""
    order_id   = str(getattr(order, "id", "") or "")
    order_price = str(getattr(order, "price", "") or "")
    order_desc  = str(getattr(order, "description", "") or "")
    chat_name   = buyer

    # Отвечаем через аккаунт, на который пришёл заказ (мультиаккаунт)
    client = accounts_manager.client_for(order)

    sub_kw = dict(
        username=buyer,
        chat_name=chat_name,
        order_id=order_id,
        order_price=order_price,
        order_desc=order_desc,
        account=client.account,
    )

    # 1) Сразу отвечаем
    if cfg_confirm.enabled and cfg_confirm.text:
        ok = await send_with_vars(cfg_confirm.text, client.send_message, chat_id, **sub_kw)
        logger.info(f"order_confirm_reply → заказ {order_id}, покупатель {buyer}: "
                    f"{'ok' if ok else 'НЕ отправлено'}")

    # 2) Через delay просим отзыв
    if cfg_review.enabled and cfg_review.text:
        delay = max(0, cfg_review.delay_minutes) * 60
        t = asyncio.create_task(_ask_review_later(
            client, chat_id, cfg_review.text, delay, order_id=order_id, sub_kw=sub_kw
        ))
        _bg_tasks.add(t)  # без ссылки Python может удалить задачу до срабатывания
        t.add_done_callback(_bg_tasks.discard)


async def _ask_review_later(client, chat_id: int, text: str, delay: int,
                            order_id: str, sub_kw: dict) -> None:
    try:
        await asyncio.sleep(delay)
        ok = await send_with_vars(text, client.send_message, chat_id, **sub_kw)
        logger.info(f"ask_review → заказ {order_id} (delay {delay}s): {'ok' if ok else 'НЕ отправлено'}")
    except Exception as e:
        logger.exception(f"ask_review: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# Автоответ сразу после оплаты (форк): «Присылайте текст рекламы на …»
# ──────────────────────────────────────────────────────────────────────────────

import json as _json
import time as _time
from pathlib import Path as _Path

_PAID_DONE_PATH = _Path(__file__).resolve().parent.parent / "data" / "paid_replied.json"
_paid_lock = asyncio.Lock()


def _load_paid_done() -> dict:
    try:
        return _json.loads(_PAID_DONE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_paid_done(d: dict) -> None:
    try:
        # храним последние 3000 заказов
        if len(d) > 3000:
            d = dict(sorted(d.items(), key=lambda kv: kv[1])[-3000:])
        _PAID_DONE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _PAID_DONE_PATH.with_suffix(".tmp")
        tmp.write_text(_json.dumps(d), encoding="utf-8")
        tmp.replace(_PAID_DONE_PATH)
    except Exception as e:
        logger.debug(f"paid_reply: не удалось сохранить: {e}")


async def _first_time(kind: str, order_id: str) -> bool:
    """True, если по этому заказу действие kind ещё не выполнялось (и отмечает его)."""
    if not order_id:
        return True
    key = order_id if kind == "paid" else f"{kind}:{order_id}"
    async with _paid_lock:
        done = _load_paid_done()
        if key in done:
            return False
        done[key] = _time.time()
        _save_paid_done(done)
        return True


@event_bus.on(Event.NEW_MESSAGE)
async def confirmed_from_system_message(message) -> None:
    """
    Запасной путь: системное «Покупатель X подтвердил успешное выполнение заказа #ID».
    Приходит сразу и уже с правильным ID чата — не зависит от разбора списка продаж.
    """
    try:
        if getattr(message, "author_id", None) != 0:
            return
        from utils.fp_system import parse_confirmed
        parsed = parse_confirmed(getattr(message, "text", "") or "")
        if not parsed:
            return
        order_id, buyer = parsed
        from types import SimpleNamespace
        order = SimpleNamespace(id=order_id, buyer_username=buyer, chat_id=getattr(message, "chat_id", None),
                                price="", description="")
        acc = getattr(message, "_palto_acc", None)
        if acc is not None:
            order._palto_acc = acc
        await handle_order_confirmed(order)
    except Exception as e:
        logger.exception(f"confirmed_from_system_message: {e}")


@event_bus.on(Event.ORDER_PAID)
async def handle_order_paid(order) -> None:
    try:
        cfg = config_manager.settings.order_paid_reply
        if not cfg.enabled or not (cfg.text or "").strip():
            return
        order_id = str(getattr(order, "id", "") or "")
        if not order_id:
            return
        # Один ответ на заказ — даже если событие пришло дважды или бот перезапускался
        if not await _first_time("paid", order_id):
            return

        buyer = getattr(order, "buyer_username", "") or ""
        bl = config_manager.settings.blacklist
        if bl.enabled and buyer in bl.nicknames:
            return
        client = accounts_manager.client_for(order)
        if client.account is None:
            return
        chat_id = await client.order_chat_id_async(order)
        if not chat_id:
            return
        await asyncio.sleep(max(0, cfg.delay_sec))
        ok = await send_with_vars(
            cfg.text, client.send_message, chat_id,
            username=buyer, chat_name=buyer, order_id=order_id,
            order_price=str(getattr(order, "price", "") or ""),
            order_desc=str(getattr(order, "description", "") or ""),
            account=client.account,
        )
        logger.info(f"paid_reply → заказ {order_id}, {buyer}: {'ok' if ok else 'НЕ отправлено'}")
    except Exception as e:
        logger.exception(f"paid_reply: {e}")
