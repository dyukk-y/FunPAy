"""
📣 Автопубликация рекламы в Telegram-канал прямо из чата FunPay.

Весь путь заказа — без вашего участия:

  оплата ──► бот в чате FunPay: «Пришлите текст поста (и фото) … напишите «готово»»
         ──► покупатель присылает текст / фото (можно несколькими сообщениями)
         ──► «готово» ──► бот показывает, как будет выглядеть пост: «Всё верно? «да» / «заново»»
         ──► «да» ──► публикация в канал (очередь, если другой рекламный пост ещё в топе;
                      «тихие часы»; закреп, если в лоте есть «закреп»)
         ──► бот отправляет покупателю ссылку и срок, вам — карточку «📣 Опубликовано»
         ──► по истечении срока: открепляет / удаляет пост и сообщает вам
         ──► покупатель подтвердил заказ ──► «Был рад посотрудничать…» + просьба об отзыве

Путаницы нет:
  • в одном чате несколько заказов — посты собираются по очереди;
  • служебные слова («готово», «да», «заново») в пост не попадают;
  • после «готово» можно дослать текст/фото — предпросмотр обновится;
  • каждое сообщение учитывается один раз (даже если FunPay прислал его дважды);
  • бот перезапустился — всё продолжается с того же места (data/ad_orders.json);
  • Telegram недоступен / нет прав — повторы, затем понятное сообщение вам
    с кнопками «🔁 Повторить» и «✍️ Опубликую сам».

Посты отправляются как простой текст (без HTML-разбора), поэтому символы
<, >, & и т.п. из текста покупателя публикуются как есть.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from config.settings import config_manager
from core.event_bus import Event, event_bus
from modules import ad_orders as ad
from utils import ad_duration
from utils.logger import logger

MSK = timezone(timedelta(hours=3))

# Этапы поста
PENDING = "pending"        # в этом чате сначала публикуется предыдущий заказ
COLLECT = "collect"        # ждём текст/фото
CONFIRM = "confirm"        # показали предпросмотр, ждём «да»
READY = "ready"            # покупатель сказал «да» — ставим в очередь / публикуем
APPROVAL = "approval"      # ждём вашего «✅ Опубликовать» (если включена проверка)
QUEUED = "queued"          # ждёт своего времени (топ другого поста / тихие часы)
PUBLISHING = "publishing"
PUBLISHED = "published"
FAILED = "failed"
MANUAL = "manual"          # вы публикуете сами и присылаете ссылку
ACTIVE_BUYER_STAGES = (COLLECT, CONFIRM)

MAX_IMAGES = 10
MAX_TEXT = 4096
MAX_CAPTION = 1024
RETRY_FAILED_SEC = 600
MAX_AUTO_RETRIES = 3

_DONE = {"готово", "готов", "готова", "все", "всё", "это все", "это всё", "done", "конец", "отправил",
         "отправила", "всё отправил", "все отправил", "всё отправила", "все отправила", "го", "finish"}
_YES = {"да", "верно", "все верно", "всё верно", "ок", "ok", "окей", "подтверждаю", "публикуйте", "публикуй",
        "+", "давай", "да публикуйте", "да все верно", "да всё верно", "отлично", "супер", "yes", "y", "ага", "угу",
        "да верно", "все ок", "всё ок", "да ок"}
_RESET = {"заново", "сначала", "нет", "сброс", "отмена", "исправить", "не то", "по новой", "нет заново",
          "начать заново", "переделать", "no"}

_ABOVE = re.compile(r"(выше|уже отправ|я же отправ|отправлял|отправляла|скинул|скинула|прислал выше|присылал|"
                    r"в чате выше|сообщением выше|above)", re.I)

_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_tasks: set = set()
_channel_cache = {"ts": 0.0, "ok": False, "msg": "", "bot_id": None}


def cfg():
    return config_manager.settings.ad_post


def active() -> bool:
    return cfg().active()


def _bot():
    from modules import notifications
    return notifications._bot_ref


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _now_msk() -> datetime:
    return datetime.now(MSK)


def _fmt_ts(ts: float) -> str:
    d = datetime.fromtimestamp(ts, MSK)
    today = _now_msk().date()
    if d.date() == today:
        return f"сегодня в {d:%H:%M}"
    if d.date() == today + timedelta(days=1):
        return f"завтра в {d:%H:%M}"
    return d.strftime("%d.%m в %H:%M")


def _cmd(text: str) -> str:
    """Короткая команда покупателя: «Готово!» → «готово». Длинные сообщения — не команды."""
    t = re.sub(r"[^\w\s+]", " ", (text or "").lower().replace("ё", "е")).strip()
    t = re.sub(r"\s+", " ", t)
    return t if 0 < len(t.split()) <= 3 else ""


def _is(word_set: set, text: str) -> bool:
    c = _cmd(text)
    return bool(c) and (c in {w.replace("ё", "е") for w in word_set})


# ──────────────────────────────────────────────────────────────────────────────
# Состояние поста (внутри записи заказа в ad_orders)
# ──────────────────────────────────────────────────────────────────────────────

def _post(e: dict) -> dict:
    p = e.get("post") or {}
    p.setdefault("texts", [])
    p.setdefault("images", [])
    p.setdefault("seen", [])
    return p


def _save_post(order_id: str, p: dict, **extra) -> Optional[dict]:
    return ad.update(order_id, post=p, **extra)


def post_text(p: dict) -> str:
    return "\n".join(t.strip("\n") for t in p.get("texts", []) if t and t.strip()).strip()


def _final_text(p: dict) -> str:
    text = post_text(p)
    footer = (cfg().footer or "").strip()
    if footer:
        text = f"{text}\n\n{footer}" if text else footer
    return text


def _orders_in_chat(chat_id) -> List[dict]:
    with ad._lock:
        items = [e for e in ad._load().values() if str(e.get("chat_id")) == str(chat_id) and e.get("post")]
    return sorted(items, key=lambda e: e.get("ts", 0))


def buyer_order_for_chat(chat_id) -> Optional[dict]:
    """Заказ, по которому покупатель сейчас присылает пост (самый ранний)."""
    for e in _orders_in_chat(chat_id):
        if _post(e).get("stage") in ACTIVE_BUYER_STAGES:
            return e
    return None


def chats_collecting() -> set:
    with ad._lock:
        return {str(e.get("chat_id")) for e in ad._load().values()
                if _post(e).get("stage") in ACTIVE_BUYER_STAGES + (PENDING,)}


def _duration(e: dict) -> Optional[ad_duration.Duration]:
    d = ad._duration_of(e)
    if d is not None:
        d.top_hours = int(e.get("top_hours") or 0)
        d.pin = bool(e.get("pin"))
        d.pin_hours = int(e.get("pin_hours") or 0)
    return d


# ──────────────────────────────────────────────────────────────────────────────
# Канал
# ──────────────────────────────────────────────────────────────────────────────

def link_for(msg_id: int) -> str:
    c = cfg()
    if c.channel_username:
        return f"https://t.me/{c.channel_username}/{msg_id}"
    cid = str(abs(int(c.channel_id)))
    if cid.startswith("100") and len(cid) > 10:
        cid = cid[3:]
    return f"https://t.me/c/{cid}/{msg_id}"


async def channel_status(force: bool = False) -> Tuple[bool, str]:
    """Может ли бот публиковать в канал. Результат кешируется на 10 минут."""
    if not force and time.time() - _channel_cache["ts"] < 600 and _channel_cache["msg"]:
        return _channel_cache["ok"], _channel_cache["msg"]
    bot = _bot()
    c = cfg()
    ok, msg = False, ""
    if bot is None:
        return False, "Telegram-бот ещё не запущен"
    try:
        chat = await bot.get_chat(c.channel_id)
        me = await bot.get_me()
        member = await bot.get_chat_member(c.channel_id, me.id)
        uname = getattr(chat, "username", None) or ""
        title = getattr(chat, "title", None) or ""
        if (uname, title) != (c.channel_username, c.channel_title):
            c.channel_username, c.channel_title = uname, title
            config_manager.save()
        status = getattr(member, "status", "")
        status = getattr(status, "value", status)
        if status == "creator":
            ok, msg = True, f"✅ Канал «{title}»: бот — владелец"
        elif status == "administrator":
            missing = [name for attr, name in (("can_post_messages", "публикация сообщений"),
                                               ("can_edit_messages", "редактирование (нужно для закрепа)"),
                                               ("can_delete_messages", "удаление сообщений"))
                       if not getattr(member, attr, False)]
            if "публикация сообщений" in missing:
                ok, msg = False, f"⚠️ Канал «{title}»: у бота нет права «публикация сообщений»"
            else:
                ok = True
                msg = f"✅ Канал «{title}» подключён" + (f" (нет прав: {', '.join(missing)})" if missing else "")
        else:
            msg = f"⚠️ Бот не админ канала «{title or c.channel_id}» — добавьте его администратором"
    except Exception as e:
        msg = (f"⚠️ Нет доступа к каналу {c.channel_id}: добавьте бота в канал администратором "
               f"с правом публикации ({type(e).__name__})")
    _channel_cache.update(ts=time.time(), ok=ok, msg=msg)
    return ok, msg


# ──────────────────────────────────────────────────────────────────────────────
# Оплата → начало сбора поста
# ──────────────────────────────────────────────────────────────────────────────

async def begin(order_id: str) -> None:
    """Вызывается после оплаты (из ad_orders.on_paid), когда автопубликация включена."""
    try:
        async with _locks[order_id]:
            e = ad.get(order_id)
            if not e or e.get("post"):
                return
            busy = [o for o in _orders_in_chat(e.get("chat_id"))
                    if o["order_id"] != order_id and _post(o).get("stage") in ACTIVE_BUYER_STAGES + (PENDING,)]
            p = _post(e)
            p["stage_ts"] = time.time()
            if busy:
                p["stage"] = PENDING
                _save_post(order_id, p)
                await ad._send_to_buyer(e, "Спасибо за покупку! 🙌 Сначала опубликуем ваш предыдущий пост, "
                                           "затем я напишу, что прислать для этого заказа.")
                return
            p["stage"] = COLLECT
            _save_post(order_id, p)
        # Покупатель часто присылает текст ДО оплаты («вот пост, сейчас оплачу») — ищем его в истории
        found = await _pull_from_history(e)
        async with _locks[order_id]:
            e = ad.get(order_id)
            p = _post(e)
            if found and (found[0] or found[1]) and p.get("stage") == COLLECT and not (post_text(p) or p["images"]):
                p.update(texts=found[0], images=found[1], prefilled=True)
                _save_post(order_id, p)
                await ad._send_to_buyer(e, "Спасибо за покупку! 🙌 Вижу, вы уже прислали текст поста выше.")
                await _show_preview(e, p, prefilled=True)
                logger.info(f"ad_autopost: заказ #{order_id} — нашёл пост в чате до оплаты, жду «да»")
                return
            await ad._send_to_buyer(e, cfg().prompt)
            logger.info(f"ad_autopost: заказ #{order_id} — жду пост от покупателя в чате")
    except Exception as ex:
        logger.exception(f"ad_autopost.begin: {ex}")


async def _pull_from_history(e: dict) -> Optional[Tuple[List[str], List[str]]]:
    """
    Сообщения покупателя, присланные перед оплатой этого заказа (после последнего
    сообщения продавца/бота). → (тексты, фото) или None.
    """
    from core.funpay_client import accounts_manager
    client = accounts_manager.get(e.get("acc", 0)) or accounts_manager.active()
    chat_id = e.get("chat_id")
    if client.account is None or not chat_id:
        return None
    try:
        hist = await asyncio.get_running_loop().run_in_executor(
            None, lambda: client.account.get_chat_history(chat_id, interlocutor_username=e.get("buyer") or None))
    except Exception as ex:
        logger.debug(f"ad_autopost: история чата {chat_id}: {ex}")
        return None
    from utils.fp_system import is_system_text
    msgs = sorted([m for m in (hist or []) if getattr(m, "id", 0)], key=lambda m: m.id)
    own = getattr(client, "own_id", None) or getattr(client.account, "id", None)
    # конец — системное «оплатил заказ #ID» (если есть), иначе конец истории
    end = len(msgs)
    for i, m in enumerate(msgs):
        if getattr(m, "author_id", None) == 0 and f"#{e['order_id']}" in (getattr(m, "text", "") or ""):
            end = i
            break
    texts, images = [], []
    for m in reversed(msgs[:end]):
        aid = getattr(m, "author_id", None)
        t = (getattr(m, "text", "") or "").strip()
        if aid == own or aid == 0 or is_system_text(t):
            break
        if getattr(m, "image_link", None):
            images.insert(0, _abs_url(m.image_link))
        elif t and not _is_question(t) and not _is(_DONE | _YES | _RESET, t):
            texts.insert(0, t)
    if len(images) > MAX_IMAGES:
        images = images[-MAX_IMAGES:]
    # Похоже на пост: есть фото или достаточно текста (не пара слов «здравствуйте»)
    if images or sum(len(t) for t in texts) >= 60:
        return texts, images
    return None


def _abs_url(url: Optional[str]) -> Optional[str]:
    """Ссылка на фото из чата FunPay → полный адрес."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://funpay.com" + url
    return url


def _is_question(text: str) -> bool:
    """Короткий вопрос («а когда выйдет пост?») — не часть рекламного текста."""
    t = (text or "").strip()
    return len(t) <= 80 and t.endswith("?") and "\n" not in t


async def _start_next_in_chat(chat_id) -> None:
    for e in _orders_in_chat(chat_id):
        if _post(e).get("stage") == PENDING:
            p = _post(e)
            p.update(stage=COLLECT, stage_ts=time.time(), reminded=False)
            _save_post(e["order_id"], p)
            await ad._send_to_buyer(e, f"Теперь пришлите пост для следующего заказа (#{e['order_id']}): "
                                       "текст и, если нужно, фото. Когда всё отправите — напишите «готово».")
            return


# ──────────────────────────────────────────────────────────────────────────────
# Сообщения покупателя в чате FunPay
# ──────────────────────────────────────────────────────────────────────────────

def _msg_key(msg) -> str:
    mid = getattr(msg, "id", 0) or 0
    if mid:
        return str(mid)
    raw = f"{getattr(msg, 'text', '')}|{getattr(msg, 'image_link', '')}"
    return "h" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


@event_bus.on(Event.NEW_MESSAGE)
async def on_new_message(message) -> None:
    try:
        if not active():
            return
        chat_id = getattr(message, "chat_id", None)
        if not chat_id:
            return
        from core.funpay_client import accounts_manager
        from utils.fp_system import is_system_text
        client = accounts_manager.client_for(message)
        author_id = getattr(message, "author_id", None)
        text = getattr(message, "text", "") or ""
        if author_id in (0, None) or author_id == client.own_id or getattr(message, "by_bot", False):
            return
        if is_system_text(text):
            return
        e = buyer_order_for_chat(chat_id)
        if not e:
            # ID чата заказа не определился («users-…») — ищем заказ по нику покупателя
            name = (getattr(message, "author", "") or getattr(message, "chat_name", "") or "").strip()
            with ad._lock:
                cand = [x for x in ad._load().values()
                        if _post(x).get("stage") in ACTIVE_BUYER_STAGES and name
                        and (x.get("buyer") or "").lower() == name.lower() and not str(x.get("chat_id", "")).isdigit()]
            if not cand:
                return
            e = sorted(cand, key=lambda x: x.get("ts", 0))[0]
            ad.update(e["order_id"], chat_id=chat_id)
            e = ad.get(e["order_id"])
        async with _locks[e["order_id"]]:
            e = ad.get(e["order_id"])
            if e:
                await _handle_buyer(e, message)
    except Exception as ex:
        logger.exception(f"ad_autopost.on_new_message: {ex}")


async def _handle_buyer(e: dict, message) -> None:
    oid = e["order_id"]
    p = _post(e)
    key = _msg_key(message)
    if key in p["seen"]:
        return
    p["seen"] = (p["seen"] + [key])[-300:]
    text = (getattr(message, "text", "") or "").strip()
    image = _abs_url(getattr(message, "image_link", None))
    stage = p.get("stage")
    has_content = bool(post_text(p) or p["images"])

    # «Я уже отправил выше» — берём текст из истории чата
    if text and not image and not has_content and stage == COLLECT and _ABOVE.search(text) and len(text) <= 80:
        found = await _pull_from_history(e)
        if found:
            p.update(texts=found[0], images=found[1], prefilled=True)
            await _show_preview(e, p)
        else:
            _save_post(oid, p)
            await ad._send_to_buyer(e, "Не нашёл текст поста выше 🙈 Пришлите его, пожалуйста, сюда ещё раз "
                                       "(и фото, если нужно), затем напишите «готово».")
        return

    # Короткий вопрос — не часть поста (вы увидите его в уведомлении и ответите сами)
    if text and not image and stage == COLLECT and _is_question(text):
        _save_post(oid, p)
        if not p.get("question_hint"):
            p["question_hint"] = True
            _save_post(oid, p)
            await ad._send_to_buyer(e, "Пришлите, пожалуйста, сам текст поста (и фото, если нужно), затем "
                                       "напишите «готово» 🙂 На вопрос ответит продавец здесь же.")
        return

    # «…текст поста\nготово» — служебное слово в конце сообщения
    tail_done = False
    if text and "\n" in text and _is(_DONE, text.rsplit("\n", 1)[1]) and stage == COLLECT:
        text = text.rsplit("\n", 1)[0].strip()
        tail_done = True

    # Команды (короткие сообщения)
    if text and not image:
        if _is(_RESET, text):
            p.update(texts=[], images=[], stage=COLLECT, stage_ts=time.time(), acked=False, reminded=False)
            _save_post(oid, p)
            await ad._send_to_buyer(e, "Хорошо, начнём заново 🙂 Пришлите текст поста (и фото, если нужно), "
                                       "затем напишите «готово».")
            return
        if stage == COLLECT and _is(_DONE, text):
            _save_post(oid, p)
            if not has_content:
                await ad._send_to_buyer(e, "Я пока не получил текст поста 🙂 Пришлите его сюда (и фото, если нужно), "
                                           "а потом напишите «готово».")
                return
            await _show_preview(e, p)
            return
        if stage == CONFIRM and (_is(_YES, text) or _is(_DONE, text)):
            p.update(stage=APPROVAL if cfg().require_approval else READY, stage_ts=time.time())
            _save_post(oid, p)
            _spawn(request_publish(oid))
            return

    # Содержимое поста
    added = False
    if image:
        if len(p["images"]) >= MAX_IMAGES:
            _save_post(oid, p)
            await ad._send_to_buyer(e, f"К посту можно прикрепить не больше {MAX_IMAGES} фото — это фото не добавил.")
            return
        p["images"].append(image)
        added = True
    if text:
        p["texts"].append(text)
        added = True
    if not added:
        _save_post(oid, p)
        return

    if stage == CONFIRM:
        if p.pop("prefilled", False):
            # покупатель прислал новый текст вместо найденного в истории — заменяем, а не дописываем
            p["texts"] = [text] if text else []
            p["images"] = [image] if image else []
        _save_post(oid, p)
        await _show_preview(e, p, updated=True)
        return
    if tail_done:
        _save_post(oid, p)
        await _show_preview(e, p)
        return
    first_ack = not p.get("acked")
    p["acked"] = True
    p["last_content_ts"] = time.time()
    _save_post(oid, p)
    if first_ack:
        await ad._send_to_buyer(e, "Принял ✅ Можете прислать ещё текст или фото. Когда всё — напишите «готово».")


def _limits_problem(p: dict) -> Optional[str]:
    text = _final_text(p)
    if len(text) > MAX_TEXT:
        return (f"Текст получился слишком длинным для Telegram: {len(text)} символов (максимум {MAX_TEXT}). "
                "Сократите его и пришлите заново — напишите «заново».")
    return None


def _preview_text(e: dict, p: dict, updated: bool = False, prefilled: bool = False) -> str:
    text = post_text(p)
    d = _duration(e)
    head = "Обновил пост 👇" if updated else "Проверьте, пожалуйста, пост 👇"
    lines = [head, "", "———"]
    lines.append(text or "(без текста — только фото)")
    lines.append("———")
    if p["images"]:
        lines.append(f"📷 Фото: {len(p['images'])} шт.")
    if d:
        place = f"{'реклама в истории' if d.where == 'в истории' else 'в канале'}: {ad_duration.human(d.hours, d.unit)}"
        lines.append(f"⏱ Размещение {place}" + (f", топ {d.top_hours} ч" if d.top_hours else "")
                     + (", с закрепом" if d.pin else ""))
    lines += ["", "Всё верно? Напишите «да» — и я опубликую пост."]
    lines.append("Если пост другой — просто пришлите новый текст (и фото)." if prefilled
                 else "Чтобы начать заново — напишите «заново».")
    return "\n".join(lines)


async def _show_preview(e: dict, p: dict, updated: bool = False, prefilled: bool = False) -> None:
    problem = _limits_problem(p)
    if problem:
        p["stage"] = COLLECT
        _save_post(e["order_id"], p)
        await ad._send_to_buyer(e, problem)
        return
    p.update(stage=CONFIRM, stage_ts=time.time(), reminded=False)
    _save_post(e["order_id"], p)
    await ad._send_to_buyer(e, _preview_text(e, p, updated, prefilled or bool(p.get("prefilled"))))


# ──────────────────────────────────────────────────────────────────────────────
# Публикация
# ──────────────────────────────────────────────────────────────────────────────

def _in_quiet(dt: datetime) -> bool:
    a, b = cfg().quiet_from, cfg().quiet_to
    if a < 0 or b < 0 or a == b:
        return False
    h = dt.hour
    return (a <= h < b) if a < b else (h >= a or h < b)


def _after_quiet(dt: datetime) -> datetime:
    b = cfg().quiet_to
    target = dt.replace(hour=b, minute=0, second=0, microsecond=0)
    if target <= dt:
        target += timedelta(days=1)
    return target


def planned_time(order_id: str) -> Tuple[float, str]:
    """
    Когда можно публиковать: (время, причина ожидания или '').
    Рекламный пост с «топом» (1/24) занимает канал на время топа — следующий
    рекламный пост встаёт сразу после. Плюс «тихие часы».
    """
    now = time.time()
    with ad._lock:
        others = [x for x in ad._load().values() if x.get("order_id") != order_id and x.get("post")]
    windows = []
    for x in others:
        px = _post(x)
        if px.get("stage") == PUBLISHED and not px.get("deleted") and px.get("top_until", 0) > now:
            windows.append((px.get("published_ts", now), px["top_until"], "сейчас в топе канала другой рекламный пост"))
        elif px.get("stage") == QUEUED and px.get("publish_at"):
            top = int(x.get("top_hours") or 0) * 3600
            windows.append((px["publish_at"], px["publish_at"] + max(top, 60), "перед вами в очереди другой рекламный пост"))
    when, reason = now, ""
    for _ in range(50):
        moved = False
        for a, b, why in windows:
            if a <= when < b:
                when, reason, moved = b, why, True
        dt = datetime.fromtimestamp(when, MSK)
        if _in_quiet(dt):
            when, reason, moved = _after_quiet(dt).timestamp(), "ночью посты не публикуются", True
        if not moved:
            break
    return when, reason


async def request_publish(order_id: str, approved: bool = False, now: bool = False) -> None:
    """
    Покупатель сказал «да» → публикуем (или ставим в очередь / на вашу проверку).
    approved — вы одобрили (если включена проверка); now — «📣 Опубликовать сейчас» (без очереди).
    """
    try:
        async with _locks[order_id]:
            e = ad.get(order_id)
            if not e:
                return
            p = _post(e)
            if p.get("stage") in (PUBLISHING, PUBLISHED):
                return
            if not (post_text(p) or p["images"]):
                return
            if cfg().require_approval and not approved:
                p.update(stage=APPROVAL, stage_ts=time.time())
                _save_post(order_id, p)
                await ad._send_to_buyer(e, "Спасибо! Пост передан на проверку — опубликую в ближайшее время 🙌")
                await _notify_admin(_approval_card(e, p), _kb_approval(order_id))
                return
            if not now:
                when, reason = planned_time(order_id)
                if when - time.time() > 30:
                    already = p.get("stage") == QUEUED and abs(p.get("publish_at", 0) - when) < 60
                    p.update(stage=QUEUED, publish_at=when, stage_ts=time.time())
                    _save_post(order_id, p)
                    if not already:
                        await ad._send_to_buyer(e, f"Спасибо! Ваш пост будет опубликован {_fmt_ts(when)} по МСК — "
                                                   f"{reason}. Я пришлю ссылку сразу после публикации.")
                        await _notify_admin(f"🕒 Пост по заказу #{order_id} ({html.escape(e.get('buyer') or '?')}) "
                                            f"в очереди: {_fmt_ts(when)} МСК — {html.escape(reason)}.",
                                            _kb_post(order_id, queued=True))
                    return
            p.update(stage=PUBLISHING, stage_ts=time.time())
            _save_post(order_id, p)
        await _do_publish(order_id)
    except Exception as ex:
        logger.exception(f"ad_autopost.request_publish: {ex}")


async def _download(url: str) -> Optional[bytes]:
    from core.funpay_client import accounts_manager
    client = accounts_manager.active()

    def get() -> Optional[bytes]:
        import requests
        sess = getattr(client.account, "session", None) or requests
        proxies = getattr(client.account, "proxy", None) or None
        r = sess.get(url, timeout=25, proxies=proxies)
        if r.status_code != 200 or len(r.content) > 10 * 1024 * 1024 or len(r.content) < 100:
            return None
        return r.content

    try:
        return await asyncio.get_running_loop().run_in_executor(None, get)
    except Exception as ex:
        logger.warning(f"ad_autopost: не скачал фото {url}: {ex}")
        return None


async def _send_post(text: str, images: List[Tuple[str, bytes]]) -> List[int]:
    """Публикует пост в канал. → id сообщений (первое — ссылка на пост)."""
    from aiogram.types import BufferedInputFile, InputMediaPhoto, LinkPreviewOptions
    bot, ch = _bot(), cfg().channel_id
    plain = {"parse_mode": None}
    if not images:
        m = await bot.send_message(ch, text, **plain)
        return [m.message_id]
    files = [BufferedInputFile(data, filename=f"photo{i}.jpg") for i, (_, data) in enumerate(images)]
    if len(images) == 1:
        if len(text) <= MAX_CAPTION:
            m = await bot.send_photo(ch, files[0], caption=text or None, **plain)
            return [m.message_id]
        # Длинный текст + одно фото: одним постом — картинка над текстом (превью ссылки на фото)
        try:
            m = await bot.send_message(ch, text, link_preview_options=LinkPreviewOptions(
                url=images[0][0], prefer_large_media=True, show_above_text=True), **plain)
            return [m.message_id]
        except Exception as ex:
            logger.info(f"ad_autopost: превью-фото не сработало ({ex}) — фото и текст отдельно")
            m1 = await bot.send_photo(ch, files[0], **plain)
            m2 = await bot.send_message(ch, text, **plain)
            return [m1.message_id, m2.message_id]
    caption_fits = len(text) <= MAX_CAPTION
    media = [InputMediaPhoto(media=f, caption=(text or None) if (i == 0 and caption_fits) else None, parse_mode=None)
             for i, f in enumerate(files)]
    msgs = await bot.send_media_group(ch, media)
    ids = [m.message_id for m in msgs]
    if text and not caption_fits:
        m = await bot.send_message(ch, text, **plain)
        ids.append(m.message_id)
    return ids


async def _do_publish(order_id: str) -> None:
    e = ad.get(order_id)
    if not e:
        return
    p = _post(e)
    ok, ch_msg = await channel_status(force=True)
    if not ok:
        await _fail(order_id, ch_msg)
        return
    images, lost = [], 0
    for url in p["images"]:
        data = await _download(url)
        if data:
            images.append((url, data))
        else:
            lost += 1
    text = _final_text(p)
    if not text and not images:
        await _fail(order_id, "не удалось скачать фото из чата FunPay, а текста нет")
        return
    from aiogram.exceptions import TelegramRetryAfter
    ids, last_err = None, ""
    for attempt in range(3):
        try:
            ids = await _send_post(text, images)
            break
        except TelegramRetryAfter as ex:
            await asyncio.sleep(min(int(getattr(ex, "retry_after", 5)) + 1, 60))
            last_err = "Telegram попросил подождать"
        except Exception as ex:
            last_err = f"{type(ex).__name__}: {str(ex)[:200]}"
            logger.warning(f"ad_autopost: публикация #{order_id}, попытка {attempt + 1}: {last_err}")
            await asyncio.sleep(3 * (attempt + 1))
    if not ids:
        await _fail(order_id, last_err or "Telegram не принял пост")
        return

    now = time.time()
    d = _duration(e)
    link = link_for(ids[0])
    p.update(stage=PUBLISHED, message_ids=ids, link=link, published_ts=now, stage_ts=now, retries=0,
             top_until=now + (d.top_hours * 3600 if d and d.top_hours else 0))
    if d and not d.forever and d.hours > 0 and cfg().delete_after_expiry:
        p["delete_at"] = now + d.hours * 3600
    pin_note = ""
    if d and d.pin:
        try:
            await _bot().pin_chat_message(cfg().channel_id, ids[0], disable_notification=True)
            p["pinned"] = True
            hours = d.pin_hours or (d.hours if not d.forever else 0)
            if hours > 0:
                p["unpin_at"] = now + hours * 3600
            pin_note = "\n📌 Закреплён" + (f" до {_fmt_ts(p['unpin_at'])}" if p.get("unpin_at") else "")
        except Exception as ex:
            pin_note = f"\n⚠️ Не смог закрепить ({type(ex).__name__}) — закрепите вручную"
    _save_post(order_id, p)
    logger.info(f"📣 Пост по заказу #{order_id} опубликован: {link}")

    status, info = await ad.attach_link(order_id, link)
    buyer_note = {"sent": "Покупателю отправлены ссылка и срок ✅",
                  "need_duration": "⏱ Не нашёл срок размещения — укажите его, и я напишу покупателю"}.get(
        status, f"⚠️ Ссылку покупателю отправить не удалось: {html.escape(info)}")
    extra = []
    if lost:
        extra.append(f"⚠️ Не удалось скачать фото: {lost} шт.")
    if p.get("delete_at"):
        extra.append(f"🗑 Удалю пост {_fmt_ts(p['delete_at'])} МСК")
    if p.get("top_until") and p["top_until"] > now:
        extra.append(f"🔝 В топе до {_fmt_ts(p['top_until'])} — новую рекламу поставлю после")
    card = (f"📣 <b>Опубликовано</b>: {html.escape(link)}\n"
            f"🛒 {html.escape(e.get('lot') or '')}\n👤 {html.escape(e.get('buyer') or '?')} · заказ #{order_id}"
            f"{pin_note}\n" + ("\n".join(extra) + "\n" if extra else "") + f"\n{buyer_note}")
    kb = _kb_post(order_id)
    if status == "need_duration":
        from bot.handlers.ad_orders import _dur_kb
        kb = _dur_kb(order_id)
    await _notify_admin(card, kb)
    await _start_next_in_chat(e.get("chat_id"))


async def _fail(order_id: str, reason: str) -> None:
    e = ad.get(order_id)
    if not e:
        return
    p = _post(e)
    p.update(stage=FAILED, fail_reason=reason, stage_ts=time.time(), retries=int(p.get("retries", 0)) + 1)
    _save_post(order_id, p)
    logger.error(f"ad_autopost: пост #{order_id} не опубликован: {reason}")
    if p["retries"] == 1:
        await ad._send_to_buyer(e, "Публикация немного задерживается — опубликую в ближайшее время и сразу "
                                   "пришлю ссылку 🙏")
    await _notify_admin(f"❌ <b>Пост по заказу #{order_id} не опубликован</b>\n{html.escape(reason)}\n\n"
                        f"{'Попробую ещё раз через 10 минут.' if p['retries'] < MAX_AUTO_RETRIES else 'Автоповторы закончились.'}",
                        _kb_failed(order_id))


# ──────────────────────────────────────────────────────────────────────────────
# Управление опубликованным постом
# ──────────────────────────────────────────────────────────────────────────────

def on_duration_set(order_id: str) -> None:
    """Срок указали после публикации — досчитываем, когда удалить пост."""
    e = ad.get(order_id)
    if not e:
        return
    p = _post(e)
    d = _duration(e)
    if p.get("stage") != PUBLISHED or p.get("deleted") or not d:
        return
    if d.forever or d.hours <= 0:
        p.pop("delete_at", None)
    elif cfg().delete_after_expiry:
        p["delete_at"] = p.get("published_ts", time.time()) + d.hours * 3600
    _save_post(order_id, p)


async def delete_post(order_id: str) -> Tuple[bool, str]:
    e = ad.get(order_id)
    if not e:
        return False, "заказ не найден"
    p = _post(e)
    ids = p.get("message_ids") or []
    if not ids:
        return False, "пост ещё не опубликован"
    bot = _bot()
    failed = 0
    try:
        await bot.delete_messages(cfg().channel_id, ids)
    except Exception:
        for mid in ids:
            try:
                await bot.delete_message(cfg().channel_id, mid)
            except Exception:
                failed += 1
    if failed == len(ids):
        return False, f"Telegram не дал удалить пост — удалите вручную: {p.get('link', '')}"
    p.update(deleted=True, deleted_ts=time.time())
    p.pop("delete_at", None)
    p.pop("unpin_at", None)
    _save_post(order_id, p)
    return True, ""


async def set_pin(order_id: str, pin: bool) -> Tuple[bool, str]:
    e = ad.get(order_id)
    p = _post(e) if e else {}
    ids = p.get("message_ids") or []
    if not ids:
        return False, "пост ещё не опубликован"
    try:
        if pin:
            await _bot().pin_chat_message(cfg().channel_id, ids[0], disable_notification=True)
        else:
            await _bot().unpin_chat_message(cfg().channel_id, message_id=ids[0])
    except Exception as ex:
        return False, f"{type(ex).__name__}: {str(ex)[:150]}"
    p["pinned"] = pin
    if not pin:
        p.pop("unpin_at", None)
    _save_post(order_id, p)
    return True, ""


async def reset(order_id: str, tell_buyer: bool = True) -> None:
    e = ad.get(order_id)
    if not e:
        return
    p = _post(e)
    p.update(texts=[], images=[], stage=COLLECT, stage_ts=time.time(), acked=False, reminded=False)
    _save_post(order_id, p)
    if tell_buyer:
        await ad._send_to_buyer(e, "Давайте соберём пост заново 🙂 Пришлите текст (и фото, если нужно), "
                                   "затем напишите «готово».")


def to_manual(order_id: str) -> None:
    e = ad.get(order_id)
    if not e:
        return
    p = _post(e)
    p.update(stage=MANUAL, stage_ts=time.time())
    _save_post(order_id, p)


# ──────────────────────────────────────────────────────────────────────────────
# Планировщик: очередь, открепление, удаление, напоминания, повторы
# ──────────────────────────────────────────────────────────────────────────────

_sched_task: Optional[asyncio.Task] = None


async def start() -> None:
    global _sched_task
    if _sched_task is None or _sched_task.done():
        _sched_task = asyncio.create_task(_loop(), name="ad-autopost")
        logger.info("📣 Автопубликация рекламы: планировщик запущен")


async def check_channel_on_start() -> None:
    if not active():
        return
    await asyncio.sleep(5)
    ok, msg = await channel_status(force=True)
    logger.info(f"📣 {msg}")
    if not ok:
        await _notify_admin(
            f"{html.escape(msg)}\n\nЧтобы бот сам публиковал рекламу:\n"
            "1. Откройте канал → Администраторы → Добавить администратора.\n"
            "2. Найдите этого бота и включите права: «Публикация сообщений», «Редактирование» и «Удаление».\n"
            "3. Нажмите /ads → «🔄 Проверить канал».\n\n"
            "Пока доступа нет, бот будет присылать вам карточку заказа, а ссылку на пост вы пришлёте сами.",
            None)


async def _loop() -> None:
    await asyncio.sleep(20)
    while True:
        try:
            await _tick()
        except Exception as ex:
            logger.exception(f"ad_autopost tick: {ex}")
        await asyncio.sleep(20)


async def _tick() -> None:
    now = time.time()
    with ad._lock:
        entries = [dict(e) for e in ad._load().values() if e.get("post")]
    remind_sec = max(10, cfg().remind_after_min) * 60
    for e in entries:
        oid = e["order_id"]
        p = _post(e)
        st = p.get("stage")
        try:
            if st == QUEUED and p.get("publish_at", 0) <= now:
                _spawn(request_publish(oid, approved=True))
            elif st == READY and now - p.get("stage_ts", now) > 60:
                _spawn(request_publish(oid))
            elif st == FAILED and p.get("retries", 0) < MAX_AUTO_RETRIES and now - p.get("stage_ts", now) > RETRY_FAILED_SEC:
                p.update(stage=PUBLISHING, stage_ts=now)
                _save_post(oid, p)
                _spawn(_do_publish(oid))
            elif st == PUBLISHING and now - p.get("stage_ts", now) > 600:
                p.update(stage=FAILED, stage_ts=now, fail_reason="публикация зависла (перезапуск бота?)")
                _save_post(oid, p)
            elif st == PUBLISHED:
                if p.get("unpin_at") and p["unpin_at"] <= now and p.get("pinned"):
                    ok, err = await set_pin(oid, False)
                    if not ok:
                        p = _post(ad.get(oid) or e)
                        p.pop("unpin_at", None)
                        _save_post(oid, p)
                        await _notify_admin(f"⚠️ Не смог открепить пост #{oid}: {html.escape(err)}", None)
                if p.get("delete_at") and p["delete_at"] <= now and not p.get("deleted"):
                    ok, err = await delete_post(oid)
                    msg = (f"🗑 Срок размещения по заказу #{oid} ({html.escape(e.get('buyer') or '?')}) закончился — "
                           f"пост удалён из канала." if ok else f"⚠️ {html.escape(err)}")
                    if not ok:
                        p = _post(ad.get(oid) or e)
                        p.pop("delete_at", None)
                        _save_post(oid, p)
                    await _notify_admin(msg, None)
            elif st in (COLLECT, CONFIRM) and not p.get("reminded") and now - p.get("stage_ts", now) > remind_sec:
                if st == COLLECT and (post_text(p) or p["images"]):
                    if now - p.get("last_content_ts", now) < remind_sec:
                        continue
                    text = "Вы прислали часть поста 🙂 Если это всё — напишите «готово», и я покажу предпросмотр."
                elif st == COLLECT:
                    text = "Напоминаю: пришлите, пожалуйста, текст рекламного поста (и фото, если нужно) — " \
                           "затем напишите «готово» 🙌"
                else:
                    text = "Пост готов к публикации 🙂 Если всё верно — напишите «да», если нужно исправить — «заново»."
                p["reminded"] = True
                _save_post(oid, p)
                await ad._send_to_buyer(e, text)
        except Exception as ex:
            logger.warning(f"ad_autopost tick #{oid}: {ex}")


# ──────────────────────────────────────────────────────────────────────────────
# Сообщения вам
# ──────────────────────────────────────────────────────────────────────────────

async def _notify_admin(text: str, kb) -> None:
    from modules.notifications import _send_to_all
    await _send_to_all(text, kb)


def _btn(t, cb):
    from aiogram.types import InlineKeyboardButton
    return InlineKeyboardButton(text=t, callback_data=cb)


def _kb(rows):
    from aiogram.types import InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _kb_post(order_id: str, queued: bool = False):
    if queued:
        return _kb([[_btn("📣 Опубликовать сейчас", f"ap:go:{order_id}")]])
    e = ad.get(order_id) or {}
    pinned = _post(e).get("pinned")
    return _kb([[_btn("📍 Открепить" if pinned else "📌 Закрепить", f"ap:{'unpin' if pinned else 'pin'}:{order_id}"),
                 _btn("🗑 Удалить пост", f"ap:del:{order_id}")]])


def _kb_failed(order_id: str):
    return _kb([[_btn("🔁 Повторить", f"ap:go:{order_id}"), _btn("✍️ Опубликую сам", f"ap:manual:{order_id}")]])


def _kb_approval(order_id: str):
    return _kb([[_btn("✅ Опубликовать", f"ap:ok:{order_id}"), _btn("🔄 Попросить переделать", f"ap:reset:{order_id}")],
                [_btn("✍️ Опубликую сам", f"ap:manual:{order_id}")]])


def _approval_card(e: dict, p: dict) -> str:
    imgs = "\n".join(f"📷 {html.escape(u)}" for u in p["images"])
    return (f"🔎 <b>Пост на проверку</b> — заказ #{e['order_id']} ({html.escape(e.get('buyer') or '?')})\n"
            f"🛒 {html.escape(e.get('lot') or '')}\n\n<code>{html.escape(_final_text(p)[:3000])}</code>\n{imgs}")


def stage_line(e: dict) -> str:
    """Строка для карточки заказа: на каком шаге пост."""
    p = _post(e)
    st = p.get("stage")
    return {
        PENDING: "⏳ Ждёт, пока опубликуем предыдущий пост этого покупателя",
        COLLECT: "📝 Покупатель присылает пост в чат FunPay" + (" (часть уже получена)" if post_text(p) or p["images"] else ""),
        CONFIRM: "👀 Покупатель проверяет предпросмотр — ждём «да»",
        READY: "✅ Покупатель подтвердил пост — публикую",
        APPROVAL: "🔎 Ждёт вашей проверки",
        QUEUED: f"🕒 В очереди: {_fmt_ts(p.get('publish_at', time.time()))} МСК",
        PUBLISHING: "📤 Публикую…",
        PUBLISHED: f"📣 Опубликован: {p.get('link', '')}" + (" (удалён)" if p.get("deleted") else ""),
        FAILED: f"❌ Не опубликован: {p.get('fail_reason', '')}",
        MANUAL: "✍️ Публикуете вы — пришлите ссылку на пост",
    }.get(st, "")
