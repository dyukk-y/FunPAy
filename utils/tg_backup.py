"""
💾 Резервная копия заказов рекламы в Telegram (на случай, если хостинг стирает data/).

На BotHost при пересборке (обновлении бота) папка data/ может создаваться заново —
бот бы «забыл» посты в очереди, покупателей, которые прямо сейчас присылают пост,
и когда удалять опубликованные посты. Поэтому состояние (data/ad_orders.json, это
несколько килобайт) хранится ещё и в вашем чате с ботом: одно закреплённое сообщение-
файл, которое бот обновляет на месте (без новых сообщений и уведомлений).

При запуске, если локального файла нет, бот берёт закреплённую копию и восстанавливает
всё как было — и сообщает вам об этом.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

from utils.logger import logger

FILE_NAME = "ad_orders_backup.json"
CAPTION = "💾 Резервная копия заказов рекламы — бот обновляет её сам. Не удаляйте и не открепляйте."
DEBOUNCE_SEC = 30

_dirty = False
_task: Optional[asyncio.Task] = None
_msg_id: Optional[int] = None


def mark_dirty() -> None:
    global _dirty
    _dirty = True


def _admin_id() -> Optional[int]:
    from config.settings import config_manager
    ids = config_manager.settings.telegram.admin_ids
    return int(ids[0]) if ids else None


def _bot():
    from modules import notifications
    return notifications._bot_ref


async def _pinned_backup(bot, chat_id):
    """Закреплённое сообщение с копией (или None)."""
    try:
        chat = await bot.get_chat(chat_id)
        pm = getattr(chat, "pinned_message", None)
        doc = getattr(pm, "document", None) if pm else None
        if doc is not None and getattr(doc, "file_name", "") == FILE_NAME:
            return pm
    except Exception as e:
        logger.debug(f"tg_backup: get_chat: {e}")
    return None


async def upload() -> bool:
    """Обновить копию в Telegram (одно закреплённое сообщение, правится на месте)."""
    global _msg_id
    from aiogram.types import BufferedInputFile, InputMediaDocument
    from modules import ad_orders as ad
    bot, admin = _bot(), _admin_id()
    if bot is None or admin is None:
        return False
    with ad._lock:
        data = ad._load()
    payload = json.dumps({"ts": time.time(), "orders": data}, ensure_ascii=False).encode("utf-8")
    file = BufferedInputFile(payload, filename=FILE_NAME)
    if _msg_id is None:
        pm = await _pinned_backup(bot, admin)
        _msg_id = getattr(pm, "message_id", None) if pm else None
    if _msg_id is not None:
        try:
            await bot.edit_message_media(chat_id=admin, message_id=_msg_id,
                                         media=InputMediaDocument(media=file, caption=CAPTION))
            return True
        except Exception as e:
            if "not modified" in str(e).lower():
                return True
            logger.debug(f"tg_backup: правка копии не удалась ({e}) — создаю новую")
            _msg_id = None
    try:
        m = await bot.send_document(admin, file, caption=CAPTION, disable_notification=True)
        _msg_id = m.message_id
        try:
            await bot.pin_chat_message(admin, _msg_id, disable_notification=True)
        except Exception as e:
            logger.debug(f"tg_backup: pin: {e}")
        return True
    except Exception as e:
        logger.warning(f"tg_backup: не удалось сохранить копию в Telegram: {e}")
        return False


async def restore_if_needed() -> int:
    """Восстановить из Telegram заказы, которых нет локально (data/ стёрли). → сколько восстановлено."""
    from modules import ad_orders as ad
    bot, admin = _bot(), _admin_id()
    if bot is None or admin is None:
        return 0
    pm = await _pinned_backup(bot, admin)
    if pm is None:
        return 0
    try:
        f = await bot.get_file(pm.document.file_id)
        buf = await bot.download_file(f.file_path)
        raw = buf.read() if hasattr(buf, "read") else bytes(buf)
        orders = json.loads(raw.decode("utf-8")).get("orders") or {}
    except Exception as e:
        logger.warning(f"tg_backup: не удалось прочитать копию: {e}")
        return 0
    if not orders:
        return 0
    with ad._lock:
        cur = ad._load()
        missing = {k: v for k, v in orders.items() if k not in cur}   # дополняем, ничего не затираем
        if not missing:
            return 0
        cur.update(missing)
        ad._save(cur)
    orders = missing
    global _msg_id
    _msg_id = pm.message_id
    logger.info(f"💾 Восстановлено заказов рекламы из резервной копии: {len(orders)}")
    return len(orders)


async def _loop() -> None:
    global _dirty
    while True:
        await asyncio.sleep(DEBOUNCE_SEC)
        if _dirty:
            _dirty = False
            ok = await upload()
            if not ok:
                _dirty = True


async def start() -> None:
    """Вызывать после запуска Telegram-бота: восстановление + фоновое обновление копии."""
    global _task
    try:
        n = await restore_if_needed()
        if n:
            from modules.notifications import _send_to_all
            await _send_to_all(f"♻️ После перезапуска восстановил из резервной копии заказы рекламы: {n}. "
                               "Очередь, сбор постов и удаление по сроку продолжаются.")
    except Exception as e:
        logger.warning(f"tg_backup restore: {e}")
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop(), name="tg-backup")
