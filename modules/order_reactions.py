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


@event_bus.on(Event.ORDER_CONFIRMED)
async def handle_order_confirmed(order) -> None:
    chat_id = getattr(order, "chat_id", None) or getattr(order, "buyer_chat_id", None)
    if not chat_id:
        return

    cfg_confirm = config_manager.settings.order_confirm_reply
    cfg_review  = config_manager.settings.ask_review

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
        await send_with_vars(cfg_confirm.text, client.send_message, chat_id, **sub_kw)
        logger.info(f"order_confirm_reply → заказ {order_id}, покупатель {buyer}")

    # 2) Через delay просим отзыв
    if cfg_review.enabled and cfg_review.text:
        delay = max(0, cfg_review.delay_minutes) * 60
        asyncio.create_task(_ask_review_later(
            client, chat_id, cfg_review.text, delay, order_id=order_id, sub_kw=sub_kw
        ))


async def _ask_review_later(client, chat_id: int, text: str, delay: int,
                            order_id: str, sub_kw: dict) -> None:
    await asyncio.sleep(delay)
    await send_with_vars(text, client.send_message, chat_id, **sub_kw)
    logger.info(f"ask_review → заказ {order_id} (delay {delay}s)")


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


@event_bus.on(Event.ORDER_PAID)
async def handle_order_paid(order) -> None:
    try:
        cfg = config_manager.settings.order_paid_reply
        if not cfg.enabled or not (cfg.text or "").strip():
            return
        chat_id = getattr(order, "chat_id", None) or getattr(order, "buyer_chat_id", None)
        order_id = str(getattr(order, "id", "") or "")
        if not chat_id or not order_id:
            return
        # Один ответ на заказ — даже если событие пришло дважды или бот перезапускался
        async with _paid_lock:
            done = _load_paid_done()
            if order_id in done:
                return
            done[order_id] = _time.time()
            _save_paid_done(done)

        buyer = getattr(order, "buyer_username", "") or ""
        bl = config_manager.settings.blacklist
        if bl.enabled and buyer in bl.nicknames:
            return
        client = accounts_manager.client_for(order)
        if client.account is None:
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
