"""
📢 Заказы рекламы: оплата → ждём от вас ссылку на пост → бот пишет покупателю.

1. Покупатель оплатил → бот находит срок размещения (в названии, параметрах или
   описании лота: «24 часа», «3 дня», «в истории» = 24 ч, «навсегда») и присылает
   вам в Telegram карточку заказа.
2. Пост вышел → вы просто присылаете боту ссылку (или жмёте «🔗 Прислать ссылку»).
3. Бот пишет покупателю (шаблон меняется в меню):
       https://t.me/… — реклама в канале.
       Подтвердить лот можете сейчас или по истечении 24 часов.
   Если срок не нашёлся — бот сначала спросит его у вас (кнопками).
4. Покупатель подтвердил заказ → заказ убирается из списка (а благодарность и
   просьбу об отзыве шлёт modules/order_reactions.py).

Состояние — в data/ad_orders.json, переживает перезапуски.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config.settings import config_manager
from utils import ad_duration
from utils.logger import logger

STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "ad_orders.json"
KEEP_DAYS = 14
URL_RX = re.compile(r"(?:https?://|t\.me/|telegram\.me/)\S+", re.I)

_lock = threading.RLock()
_tasks: set = set()

WAITING, NEED_DURATION, SENT = "waiting", "need_duration", "sent"


def cfg():
    return config_manager.settings.ad_flow


# ──────────────────────────────────────────────────────────────────────────────
# Хранилище
# ──────────────────────────────────────────────────────────────────────────────

def _load() -> Dict[str, dict]:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _keep(v: dict, cutoff: float) -> bool:
    if v.get("ts", 0) > cutoff:
        return True
    p = v.get("post") or {}
    # пост ещё нужно открепить/удалить по сроку — запись не выбрасываем
    return bool((p.get("delete_at") and not p.get("deleted")) or p.get("unpin_at"))


def _save(d: Dict[str, dict]) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    d = {k: v for k, v in d.items() if _keep(v, cutoff)}
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        logger.warning(f"ad_orders: не удалось сохранить: {e}")


def get(order_id: str) -> Optional[dict]:
    with _lock:
        return _load().get(str(order_id))


def update(order_id: str, **fields) -> Optional[dict]:
    with _lock:
        d = _load()
        e = d.get(str(order_id))
        if e is None:
            return None
        e.update(fields)
        _save(d)
        return e


CONFIRMED = "confirmed"


def mark_confirmed(order_id: str) -> None:
    """Покупатель подтвердил заказ: ссылку больше не ждём, но пост по сроку ещё удалим/открепим."""
    with _lock:
        d = _load()
        e = d.get(str(order_id))
        if e is None:
            return
        p = e.get("post") or {}
        if (p.get("delete_at") and not p.get("deleted")) or p.get("unpin_at"):
            e["status"] = CONFIRMED
            e["confirmed_ts"] = time.time()
        else:
            d.pop(str(order_id), None)
        _save(d)


def forget_order(order_id: str) -> None:
    with _lock:
        d = _load()
        if d.pop(str(order_id), None) is not None:
            _save(d)


def waiting_orders() -> List[dict]:
    """Заказы, по которым ещё не отправлена ссылка (старые — первыми)."""
    with _lock:
        items = [e for e in _load().values() if e.get("status") in (WAITING, NEED_DURATION)]
    return sorted(items, key=lambda e: e.get("ts", 0))


_AUTOPOST_STAGES = ("pending", "collect", "confirm", "ready", "approval", "queued", "publishing", "published")


def manual_waiting() -> List[dict]:
    """Заказы, по которым ссылку на пост пришлёте вы (не идёт автопубликация)."""
    return [e for e in waiting_orders() if (e.get("post") or {}).get("stage") not in _AUTOPOST_STAGES]


def extract_link(text: str) -> Optional[str]:
    m = URL_RX.search(text or "")
    if not m:
        return None
    link = m.group(0).rstrip(".,);»\"'")
    if not link.lower().startswith("http"):
        link = "https://" + link
    return link


# ──────────────────────────────────────────────────────────────────────────────
# Текст покупателю
# ──────────────────────────────────────────────────────────────────────────────

def _duration_of(e: dict) -> Optional[ad_duration.Duration]:
    if e.get("hours") is None:
        return None
    return ad_duration.Duration(int(e["hours"]), e.get("where") or "в канале", e.get("dur_source", ""), e.get("unit", ""))


def compose(e: dict, link: str) -> Optional[str]:
    d = _duration_of(e)
    if d is None:
        return None
    text = cfg().template or "{link} — реклама {where}.\nПодтвердить лот можете {until}."
    for k, v in (("{link}", link), ("{where}", d.where), ("{until}", ad_duration.until_phrase(d)),
                 ("{duration}", ad_duration.human(d.hours, d.unit))):
        text = text.replace(k, v)
    return text


def card_text(e: dict) -> str:
    d = _duration_of(e)
    if d:
        dur = f"<b>{ad_duration.human(d.hours, d.unit)}</b> {html.escape(d.where)} <i>({html.escape(d.source)})</i>"
    else:
        dur = "⚠️ <b>не нашёл в лоте</b> — укажите кнопкой «⏱ Срок»"
    lines = [
        "📢 <b>Куплена реклама</b>",
        f"🛒 {html.escape(e.get('lot') or 'лот')}",
        f"👤 {html.escape(e.get('buyer') or '?')} · заказ #{html.escape(e['order_id'])}"
        + (f" · 💰 {html.escape(str(e['price']))} ₽" if e.get("price") else ""),
        f"⏱ Срок: {dur}",
    ]
    stage = (e.get("post") or {}).get("stage")
    if stage and stage not in ("manual", "failed"):
        from modules.ad_autopost import stage_line
        lines.append(f"\n🤖 {html.escape(stage_line(e))}")
        if stage in ("pending", "collect", "confirm"):
            lines.append("Бот сам соберёт пост у покупателя в чате FunPay, опубликует в канал и пришлёт ссылку. "
                         "Вам ничего делать не нужно.")
    elif e.get("status") == SENT:
        lines.append(f"\n✅ Ссылка отправлена покупателю: {html.escape(e.get('link', ''))}")
    elif e.get("status") == NEED_DURATION:
        lines.append(f"\n🔗 Ссылка получена: {html.escape(e.get('link', ''))}\n⏱ Укажите срок — и я сразу отправлю покупателю.")
    else:
        preview = compose(e, "<ссылка>")
        lines.append("\n👉 Когда пост выйдет — <b>просто пришлите мне ссылку на него</b>.")
        if preview:
            lines.append(f"Покупатель получит:\n<code>{html.escape(preview)}</code>")
    return "\n".join(lines)


def card_kb(e: dict):
    from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as M
    oid = e["order_id"]
    rows = []
    stage = (e.get("post") or {}).get("stage")
    if stage and stage not in ("manual", "failed", "published"):
        rows.append([B(text="📣 Опубликовать сейчас", callback_data=f"ap:go:{oid}"),
                     B(text="⏱ Срок", callback_data=f"ad:dur:{oid}")])
        rows.append([B(text="📨 Написать покупателю", callback_data=f"qr:reply:{e.get('chat_id')}@{e.get('acc', 0)}"),
                     B(text="✍️ Опубликую сам", callback_data=f"ap:manual:{oid}")])
        return M(inline_keyboard=rows)
    if e.get("status") != SENT:
        rows.append([B(text="🔗 Прислать ссылку", callback_data=f"ad:link:{oid}"),
                     B(text="⏱ Срок", callback_data=f"ad:dur:{oid}")])
        rows.append([B(text="📨 Написать покупателю", callback_data=f"qr:reply:{e.get('chat_id')}@{e.get('acc', 0)}"),
                     B(text="🚫 Не ждать ссылку", callback_data=f"ad:skip:{oid}")])
    else:
        rows.append([B(text="🔁 Отправить другую ссылку", callback_data=f"ad:link:{oid}")])
    return M(inline_keyboard=rows)


# ──────────────────────────────────────────────────────────────────────────────
# Оплата → карточка заказа
# ──────────────────────────────────────────────────────────────────────────────

def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def on_paid(order) -> None:
    """Вызывается при оплате (оба пути — событие заказа и системное сообщение). Повторы игнорирует."""
    try:
        if not cfg().enabled:
            return
        order_id = str(getattr(order, "id", "") or "")
        if not order_id:
            return
        with _lock:
            d = _load()
            if order_id in d:
                return
            d[order_id] = {"order_id": order_id, "ts": time.time(), "status": WAITING}  # бронь от дублей
            _save(d)

        from core.funpay_client import accounts_manager
        client = accounts_manager.client_for(order)
        buyer = getattr(order, "buyer_username", "") or ""
        title = str(getattr(order, "description", "") or "")
        desc = params = ""
        loop = asyncio.get_running_loop()
        chat_id = None
        if client.account is not None:
            try:
                chat_id = await client.order_chat_id_async(order)
            except Exception as e:
                logger.debug(f"ad_orders chat: {e}")
            try:
                full = await loop.run_in_executor(None, client.account.get_order, order_id)
                desc = getattr(full, "full_description", "") or ""
                params = getattr(full, "lot_params_text", "") or ""
                title = title or (getattr(full, "short_description", "") or "")
                buyer = buyer or (getattr(full, "buyer_username", "") or "")
            except Exception as e:
                logger.debug(f"ad_orders: детали заказа {order_id}: {e}")
        dur = ad_duration.detect(title, desc, params, cfg().default_hours or None)
        e = update(order_id, acc=getattr(client, "index", 0), chat_id=chat_id or getattr(order, "chat_id", None),
                   buyer=buyer, lot=title, price=str(getattr(order, "price", "") or ""),
                   hours=dur.hours if dur else None, unit=dur.unit if dur else "",
                   top_hours=dur.top_hours if dur else 0, pin=bool(dur and dur.pin),
                   pin_hours=dur.pin_hours if dur else 0,
                   where=dur.where if dur else ("в истории" if "истори" in title.lower() else "в канале"),
                   dur_source=dur.source if dur else "")
        if e is None:
            return
        logger.info(f"ad_orders: заказ #{order_id} ({buyer}) ждёт ссылку на пост, срок: "
                    f"{ad_duration.human(dur.hours, dur.unit) if dur else 'не найден'}")
        # 📣 Автопубликация: бот сам соберёт пост в чате FunPay и опубликует в канал
        try:
            from modules import ad_autopost
            if ad_autopost.active():
                await ad_autopost.begin(order_id)
                e = get(order_id) or e
        except Exception as ex:
            logger.warning(f"ad_orders: автопубликация не запустилась: {ex}")
        from modules.notifications import _send_to_all
        await _send_to_all(card_text(e), card_kb(e))
    except Exception as ex:
        logger.exception(f"ad_orders.on_paid: {ex}")


# ──────────────────────────────────────────────────────────────────────────────
# Ссылка / срок → сообщение покупателю
# ──────────────────────────────────────────────────────────────────────────────

async def attach_link(order_id: str, link: str) -> Tuple[str, str]:
    """
    → ("sent", текст) | ("need_duration", "") | ("error", причина).
    """
    e = get(order_id)
    if not e:
        return "error", "заказ не найден (уже подтверждён или удалён)"
    link = extract_link(link) or link.strip()
    text = compose(e, link)
    if text is None:
        update(order_id, link=link, status=NEED_DURATION)
        return "need_duration", ""
    ok = await _send_to_buyer(e, text)
    if not ok:
        update(order_id, link=link)
        return "error", "FunPay не принял сообщение — попробуйте ещё раз через минуту"
    update(order_id, link=link, status=SENT, sent_ts=time.time())
    try:
        from modules.sales_brain import note_manual  # это ваш ответ, не автоответ
        note_manual(e.get("acc", 0), e.get("chat_id"), text)
    except Exception:
        pass
    return "sent", text


async def set_duration(order_id: str, hours: int, unit: str = "") -> Tuple[str, str]:
    e = get(order_id)
    if not e:
        return "error", "заказ не найден"
    if not unit:
        unit = "day" if hours > 0 and hours % 24 == 0 and hours >= 24 * 7 else "hour"
    update(order_id, hours=int(hours), unit=unit, dur_source="указали вы")
    if e.get("status") == NEED_DURATION and e.get("link"):
        return await attach_link(order_id, e["link"])
    return "ok", ""


async def _send_to_buyer(e: dict, text: str) -> bool:
    from core.funpay_client import accounts_manager
    from utils.variables import send_with_vars
    client = accounts_manager.get(e.get("acc", 0)) or accounts_manager.active()
    if client.account is None or not e.get("chat_id"):
        return False
    try:
        return bool(await send_with_vars(text, client.send_message, e["chat_id"], account=client.account,
                                         username=e.get("buyer", ""), chat_name=e.get("buyer", ""),
                                         order_id=e.get("order_id", ""), order_desc=e.get("lot", "")))
    except Exception as ex:
        logger.warning(f"ad_orders send: {ex}")
        return False


def chats_waiting_post() -> set:
    """Чаты, где покупатель, скорее всего, присылает текст поста (автоответы и подсказки там не нужны)."""
    chats = {str(e.get("chat_id")) for e in waiting_orders()}
    try:
        from modules.ad_autopost import chats_collecting
        chats |= chats_collecting()
    except Exception:
        pass
    return chats
