# -*- coding: utf-8 -*-
"""
Автопубликация рекламы из чата FunPay в Telegram-канал — сквозные сценарии
на фейковых FunPay и Telegram (без сети).
"""
import asyncio
import itertools
import sys
import time as _time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHANNEL = -1002120185316
BUYER_ID = 555
ME = 1
_ids = itertools.count(1000)


class FakeBot:
    def __init__(self, admin=True):
        self.calls = []
        self.admin = admin
        self._mid = itertools.count(10)

    async def get_chat(self, chat_id):
        return NS(id=chat_id, username=None, title="Мой канал")

    async def get_me(self):
        return NS(id=42, username="help_bot")

    async def get_chat_member(self, chat_id, user_id):
        if not self.admin:
            return NS(status="member")
        return NS(status="administrator", can_post_messages=True, can_edit_messages=True, can_delete_messages=True)

    def _msg(self):
        return NS(message_id=next(self._mid))

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send_message", chat_id, text, kw))
        return self._msg()

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.calls.append(("send_photo", chat_id, caption, kw))
        return self._msg()

    async def send_media_group(self, chat_id, media, **kw):
        self.calls.append(("send_media_group", chat_id, [(m.caption) for m in media], kw))
        return [self._msg() for _ in media]

    async def pin_chat_message(self, chat_id, message_id, **kw):
        self.calls.append(("pin", chat_id, message_id))

    async def unpin_chat_message(self, chat_id, message_id=None, **kw):
        self.calls.append(("unpin", chat_id, message_id))

    async def delete_messages(self, chat_id, ids):
        self.calls.append(("delete", chat_id, list(ids)))

    async def delete_message(self, chat_id, mid):
        self.calls.append(("delete1", chat_id, mid))

    def channel_posts(self):
        return [c for c in self.calls if c[0] in ("send_message", "send_photo", "send_media_group") and c[1] == CHANNEL]


class FakeAccount:
    id = ME

    def __init__(self):
        self.sent = []
        self.proxy = None
        self.session = NS(get=lambda url, timeout=0, proxies=None: NS(status_code=200, content=b"\xff\xd8" + b"x" * 500))

    def send_message(self, cid, text, update_last_saved_message=False):
        self.sent.append((cid, text))

    def get_chat_by_name(self, name, make_request=False):
        return NS(id=777)

    def get_order(self, order_id):
        return NS(full_description="", lot_params_text="", short_description="", buyer_username="")

    history = []

    def get_chat_history(self, chat_id, interlocutor_username=None):
        return list(self.history)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import modules.ad_autopost as ap
    import modules.ad_orders as ad
    import modules.notifications as notif
    import modules.order_reactions as orx
    from config.settings import (AdFlowSettings, AdPostSettings, AskReviewSettings, OrderConfirmReplySettings,
                                 config_manager)
    from core import funpay_client

    monkeypatch.setattr(ad, "STATE_PATH", tmp_path / "ad.json")
    monkeypatch.setattr(orx, "_PAID_DONE_PATH", tmp_path / "done.json")
    monkeypatch.setattr(config_manager, "save", lambda: None)
    s = config_manager.settings
    saved = (s.ad_flow, s.ad_post, s.order_confirm_reply, s.ask_review)
    s.ad_flow, s.ad_post = AdFlowSettings(), AdPostSettings()
    s.order_confirm_reply, s.ask_review = OrderConfirmReplySettings(), AskReviewSettings()
    ap._channel_cache.update(ts=0, ok=False, msg="")
    ap._tasks.clear()

    acc = FakeAccount()
    cl = funpay_client.FunPayClient()
    cl.account = acc
    cl.acc_cfg = NS(account_id=ME, display_name=lambda: "me", enabled=True)
    for name in ("client_for", "get"):
        monkeypatch.setattr(funpay_client.accounts_manager, name, lambda *_a: cl)
    monkeypatch.setattr(funpay_client.accounts_manager, "active", lambda: cl)
    bot = FakeBot()
    monkeypatch.setattr(notif, "_bot_ref", bot)
    admin = []

    async def fake_all(text, markup=None, photo_url=None):
        admin.append(text)
    monkeypatch.setattr(notif, "_send_to_all", fake_all)

    clock = {"t": _time.time()}
    monkeypatch.setattr(ap.time, "time", lambda: clock["t"])

    async def settle():
        for _ in range(20):
            await asyncio.sleep(0)
            if ap._tasks:
                await asyncio.gather(*list(ap._tasks), return_exceptions=True)

    def run(coro):
        async def go():
            await coro
            await settle()
        asyncio.run(go())

    def pay(oid, title, chat=777, buyer="Vasya"):
        run(ad.on_paid(NS(id=oid, chat_id=chat, buyer_username=buyer, price=500, description=title)))

    def buyer(text=None, image=None, chat=777, mid=None, author="Vasya"):
        msg = NS(id=mid or next(_ids), chat_id=chat, author_id=BUYER_ID, author=author, chat_name=author,
                 text=text, image_link=image, by_bot=False, type=None)
        run(ap.on_new_message(msg))

    yield NS(ap=ap, ad=ad, orx=orx, acc=acc, bot=bot, admin=admin, clock=clock, run=run, pay=pay, buyer=buyer,
             cfg=s.ad_post)
    s.ad_flow, s.ad_post, s.order_confirm_reply, s.ask_review = saved


def fp_texts(env):
    return [t for _, t in env.acc.sent]


# ─── Сценарии ────────────────────────────────────────────────────────────────

def test_happy_path_with_pin_and_top(env):
    env.pay("ORD00001", "Реклама 1/24 с закрепом")
    assert "Пришлите сюда, в этот чат, текст рекламного поста" in fp_texts(env)[-1]
    assert "Куплена реклама" in env.admin[-1] and "Бот сам соберёт пост" in env.admin[-1]

    env.buyer("Лучший канал про котиков! Подписывайтесь 👉 @cats")
    assert "Принял" in fp_texts(env)[-1]
    env.buyer(image="https://sfunpay.com/s/chat/a.jpg")
    env.buyer("Готово!")
    preview = fp_texts(env)[-1]
    assert "Лучший канал про котиков" in preview and "Фото: 1" in preview and "«да»" in preview
    assert "топ 1 ч" in preview and "с закрепом" in preview
    assert env.bot.channel_posts() == []                       # до «да» ничего не публикуем

    env.buyer("да")
    posts = env.bot.channel_posts()
    assert len(posts) == 1 and posts[0][0] == "send_photo"
    assert posts[0][2] == "Лучший канал про котиков! Подписывайтесь 👉 @cats"   # без «готово»/«да»
    assert posts[0][3]["parse_mode"] is None
    assert ("pin", CHANNEL, 10) in env.bot.calls
    assert fp_texts(env)[-1] == ("https://t.me/c/2120185316/10 — реклама в канале.\n"
                                 "Подтвердить лот можете сейчас или по истечении 24 часов.")
    assert "Опубликовано" in env.admin[-1]
    e = env.ad.get("ORD00001")
    assert e["post"]["stage"] == "published" and e["post"]["top_until"] > env.clock["t"]
    assert abs(e["post"]["delete_at"] - env.clock["t"] - 24 * 3600) < 5


def test_queue_respects_top_of_previous_post(env):
    env.pay("ORD00001", "Реклама 1/24")
    env.buyer("Пост номер один"); env.buyer("готово"); env.buyer("да")
    env.pay("ORD00002", "Реклама 1/24", chat=888, buyer="Bob")
    env.buyer("Пост номер два", chat=888, author="Bob"); env.buyer("готово", chat=888, author="Bob")
    env.buyer("да", chat=888, author="Bob")
    assert len(env.bot.channel_posts()) == 1                   # первый ещё в топе
    assert "будет опубликован" in env.acc.sent[-1][1]
    e2 = env.ad.get("ORD00002")
    assert e2["post"]["stage"] == "queued"
    env.clock["t"] = e2["post"]["publish_at"] + 1                 # прошёл час
    env.run(env.ap._tick())
    posts = env.bot.channel_posts()
    assert len(posts) == 2 and posts[1][2] == "Пост номер два"


def test_long_text_with_one_photo_is_one_post(env):
    env.pay("ORD00003", "Пост 48 часов")
    long = "Очень длинный рекламный текст. " * 50
    env.buyer(long); env.buyer(image="https://sfunpay.com/s/chat/b.jpg"); env.buyer("готово"); env.buyer("да")
    post = env.bot.channel_posts()[0]
    assert post[0] == "send_message" and post[3]["link_preview_options"].url == "https://sfunpay.com/s/chat/b.jpg"
    assert post[3]["link_preview_options"].show_above_text


def test_album(env):
    env.pay("ORD00004", "Пост 24ч")
    for i in range(3):
        env.buyer(image=f"https://sfunpay.com/s/chat/{i}.jpg")
    env.buyer("Подпись к альбому"); env.buyer("готово"); env.buyer("да")
    post = env.bot.channel_posts()[0]
    assert post[0] == "send_media_group" and post[2] == ["Подпись к альбому", None, None]


def test_reset_empty_done_dupes_and_edit_after_preview(env):
    env.pay("ORD00005", "Пост 24ч")
    env.buyer("готово")
    assert "пока не получил текст" in fp_texts(env)[-1]
    env.buyer("Черновик", mid=9001)
    env.buyer("Черновик", mid=9001)                            # FunPay прислал дважды
    env.buyer("заново")
    env.buyer("Финальный текст"); env.buyer("готово")
    assert "Финальный текст" in fp_texts(env)[-1] and "Черновик" not in fp_texts(env)[-1]
    env.buyer("Добавьте: скидка 10%")                          # дописал после предпросмотра
    assert "Обновил пост" in fp_texts(env)[-1]
    env.buyer("всё верно")
    assert env.bot.channel_posts()[0][2] == "Финальный текст\nДобавьте: скидка 10%"


def test_two_orders_in_same_chat_one_by_one(env):
    env.pay("ORD00006", "Пост 24ч")
    env.pay("ORD00007", "Пост 24ч")
    assert "Сначала опубликуем ваш предыдущий пост" in fp_texts(env)[-1]
    env.buyer("Первый пост"); env.buyer("готово"); env.buyer("да")
    assert any("Теперь пришлите пост для следующего заказа" in t for t in fp_texts(env))
    env.buyer("Второй пост"); env.buyer("готово"); env.buyer("да")
    assert [p[2] for p in env.bot.channel_posts()] == ["Первый пост", "Второй пост"]
    assert env.ad.get("ORD00007")["post"]["stage"] == "published"


def test_no_channel_rights_then_retry(env):
    env.bot.admin = False
    env.pay("ORD00008", "Пост 24ч")
    env.buyer("Текст"); env.buyer("готово"); env.buyer("да")
    assert env.bot.channel_posts() == []
    assert "не опубликован" in env.admin[-1] and "задерживается" in fp_texts(env)[-1]
    env.bot.admin = True
    env.clock["t"] += 11 * 60
    env.run(env.ap._tick())
    assert len(env.bot.channel_posts()) == 1


def test_delete_and_unpin_after_expiry_even_if_confirmed(env):
    env.pay("ORD00009", "Пост на 24 часа + закреп на 12 часов")
    env.buyer("Текст"); env.buyer("готово"); env.buyer("да")
    env.run(env.orx.handle_order_confirmed(NS(id="ORD00009", chat_id=777, buyer_username="Vasya")))
    assert "Был рад посотрудничать" in fp_texts(env)[-2] and "отзыв" in fp_texts(env)[-1]
    assert env.ad.get("ORD00009") is not None                  # запись живёт до удаления поста
    env.clock["t"] += 12 * 3600 + 30
    env.run(env.ap._tick())
    assert ("unpin", CHANNEL, 10) in env.bot.calls
    env.clock["t"] += 12 * 3600
    env.run(env.ap._tick())
    assert ("delete", CHANNEL, [10]) in env.bot.calls and "удалён" in env.admin[-1]


def test_approval_mode(env):
    env.cfg.require_approval = True
    env.pay("ORD00010", "Пост 24ч")
    env.buyer("Текст на проверку"); env.buyer("готово"); env.buyer("да")
    assert env.bot.channel_posts() == [] and "Пост на проверку" in env.admin[-1]
    assert "передан на проверку" in fp_texts(env)[-1]
    env.run(env.ap.request_publish("ORD00010", approved=True))
    assert env.bot.channel_posts()[0][2] == "Текст на проверку"


def test_quiet_hours(env):
    from datetime import datetime
    night = datetime(2026, 11, 28, 2, 0, tzinfo=env.ap.MSK).timestamp()
    env.clock["t"] = night
    env.cfg.quiet_from, env.cfg.quiet_to = 0, 8
    env.pay("ORD00011", "Пост 24ч")
    env.buyer("Ночной пост"); env.buyer("готово"); env.buyer("да")
    assert env.bot.channel_posts() == [] and "ночью посты не публикуются" in fp_texts(env)[-1]
    env.clock["t"] = datetime(2026, 11, 28, 8, 0, 30, tzinfo=env.ap.MSK).timestamp()
    env.run(env.ap._tick())
    assert len(env.bot.channel_posts()) == 1


def test_html_like_text_published_as_is_and_footer(env):
    env.cfg.footer = "#реклама"
    env.pay("ORD00012", "Пост 24ч")
    env.buyer("Скидки <50%> & подарки <b>"); env.buyer("готово"); env.buyer("да")
    post = env.bot.channel_posts()[0]
    assert post[2] == "Скидки <50%> & подарки <b>\n\n#реклама" and post[3]["parse_mode"] is None


def test_system_and_own_messages_ignored(env):
    env.pay("ORD00013", "Пост 24ч")
    from types import SimpleNamespace
    env.run(env.ap.on_new_message(SimpleNamespace(id=1, chat_id=777, author_id=0, text="Покупатель Vasya оплатил "
                                                  "заказ #ORD00013. Пост. Vasya, не забудьте…", image_link=None)))
    env.run(env.ap.on_new_message(SimpleNamespace(id=2, chat_id=777, author_id=ME, text="мой ответ",
                                                  image_link=None, by_bot=False)))
    env.buyer("готово")
    assert "пока не получил текст" in fp_texts(env)[-1]


def test_chat_matched_by_buyer_name_when_chat_id_unknown(env, monkeypatch):
    monkeypatch.setattr(FakeAccount, "get_chat_by_name", lambda self, n, m=False: None)
    env.pay("ORD00014", "Пост 24ч", chat="users-1-555")
    env.buyer("Текст", chat=999); env.buyer("готово", chat=999); env.buyer("да", chat=999)
    assert env.ad.get("ORD00014")["chat_id"] == 999 and len(env.bot.channel_posts()) == 1


def test_too_long_text_rejected(env):
    env.pay("ORD00015", "Пост 24ч")
    env.buyer("а" * 5000); env.buyer("готово")
    assert "слишком длинным" in fp_texts(env)[-1]
    assert env.ad.get("ORD00015")["post"]["stage"] == "collect"


def H(mid, author_id, text=None, image=None):
    return NS(id=mid, author_id=author_id, text=text, image_link=image)


def test_post_sent_before_payment_is_found(env):
    env.acc.history = [
        H(1, ME, "Здравствуйте! Пришлите текст, как будете готовы"),
        H(2, BUYER_ID, "Привет! Вот мой пост: Лучший магазин аксессуаров, скидки до 50% всю неделю 🔥"),
        H(3, BUYER_ID, image="https://sfunpay.com/s/chat/pre.jpg"),
        H(4, 0, "Покупатель Vasya оплатил заказ #PRE00001. Пост 24ч. Vasya, не забудьте потом нажать кнопку "
                "«Подтвердить выполнение заказа»."),
    ]
    env.pay("PRE00001", "Пост 24ч")
    texts = fp_texts(env)
    assert "уже прислали текст поста выше" in texts[-2] and "Лучший магазин аксессуаров" in texts[-1]
    assert "Фото: 1" in texts[-1] and "пришлите новый текст" in texts[-1]
    env.buyer("да")
    assert env.bot.channel_posts()[0][0] == "send_photo"


def test_prefilled_replaced_by_new_text(env):
    env.acc.history = [H(1, BUYER_ID, "Старый вариант поста, который покупатель передумал публиковать вообще")]
    env.pay("PRE00002", "Пост 24ч")
    env.buyer("Новый текст поста")
    assert "Новый текст поста" in fp_texts(env)[-1] and "Старый вариант" not in fp_texts(env)[-1]
    env.buyer("да")
    assert env.bot.channel_posts()[0][2] == "Новый текст поста"


def test_sent_above_phrase(env):
    env.pay("PRE00003", "Пост 24ч")                 # в истории пусто — бот просит прислать пост
    env.acc.history = [H(5, BUYER_ID, "Рекламный текст, отправленный до того, как бот попросил, достаточно длинный")]
    env.buyer("я же отправил выше")
    assert "Рекламный текст, отправленный" in fp_texts(env)[-1]


def test_question_not_added_and_trailing_done(env):
    env.pay("Q0000001", "Пост 24ч")
    env.buyer("А когда выйдет пост?")
    assert "На вопрос ответит продавец" in fp_texts(env)[-1]
    env.buyer("Текст рекламы\nвторая строка\nготово")
    preview = fp_texts(env)[-1]
    assert "Текст рекламы\nвторая строка" in preview and "когда выйдет" not in preview and "«да»" in preview
    env.buyer("да")
    assert env.bot.channel_posts()[0][2] == "Текст рекламы\nвторая строка"


def test_duration_set_after_publish_schedules_delete(env):
    env.pay("DUR00001", "Пост в канале")               # срока в лоте нет
    env.buyer("Текст"); env.buyer("готово"); env.buyer("да")
    assert "укажите его" in env.admin[-1]
    e = env.ad.get("DUR00001")
    assert e["post"]["stage"] == "published" and "delete_at" not in e["post"]
    status, text = asyncio.run(env.ad.set_duration("DUR00001", 48))
    assert status == "sent" and "по истечении 48 часов" in fp_texts(env)[-1]
    e = env.ad.get("DUR00001")
    assert abs(e["post"]["delete_at"] - e["post"]["published_ts"] - 48 * 3600) < 2


def test_non_ad_order_goes_usual_way(env):
    from config.settings import OrderPaidReplySettings, config_manager
    saved = config_manager.settings.order_paid_reply
    config_manager.settings.order_paid_reply = OrderPaidReplySettings(delay_sec=0, text="Спасибо за покупку!")
    try:
        order = NS(id="KEY00001", chat_id=777, buyer_username="Vasya", price=100, description="Steam ключ Cyberpunk")
        env.run(env.orx.handle_order_paid(order))
        assert env.ad.get("KEY00001") is None                       # не реклама — пост не собираем
        assert fp_texts(env) == ["Спасибо за покупку!"]             # обычный ответ после оплаты
        assert not any("Куплена реклама" in t for t in env.admin)
    finally:
        config_manager.settings.order_paid_reply = saved


def test_polite_replies_and_early_yes(env):
    env.pay("ACK00001", "Пост 24ч")
    n = len(fp_texts(env))
    env.buyer("Хорошо, сейчас пришлю")
    env.buyer("да")                                  # «да» без текста — не пост
    assert len(fp_texts(env)) == n                   # бот молча ждёт сам пост
    env.buyer("Реклама нашего магазина: скидки на всё")
    env.buyer("да")                                  # прислал текст и сразу «да» = «готово»
    assert "Проверьте, пожалуйста, пост" in fp_texts(env)[-1]
    env.buyer("да")
    assert env.bot.channel_posts()[0][2] == "Реклама нашего магазина: скидки на всё"


def test_split_long_funpay_message():
    from core.funpay_client import FunPayClient
    chunks = FunPayClient._split_message("слово " * 1000)
    assert len(chunks) >= 3 and all(len(c) <= 1800 for c in chunks)
    assert FunPayClient._split_message("a\nb") == ["a\nb"]
    assert len(FunPayClient._split_message("\n".join(["x"] * 45))) == 3


def test_confirm_stage_ignores_thanks_and_questions(env):
    env.pay("CNF00001", "Пост 24ч")
    env.buyer("Наш пост"); env.buyer("готово")
    env.buyer("Спасибо")
    env.buyer("А во сколько выйдет?")
    assert "напишите «да»" in fp_texts(env)[-1]
    env.buyer("Хорошо")                                    # «хорошо» = согласие
    assert env.bot.channel_posts()[0][2] == "Наш пост"


def test_reset_while_queued(env):
    env.pay("QQ000001", "Реклама 1/24")
    env.buyer("Первый"); env.buyer("готово"); env.buyer("да")
    env.pay("QQ000002", "Реклама 1/24", chat=888, buyer="Bob")
    env.buyer("Второй", chat=888, author="Bob"); env.buyer("готово", chat=888, author="Bob")
    env.buyer("да", chat=888, author="Bob")
    assert env.ad.get("QQ000002")["post"]["stage"] == "queued"
    env.buyer("заново", chat=888, author="Bob")
    assert env.ad.get("QQ000002")["post"]["stage"] == "collect" and "отменил" in env.acc.sent[-1][1]
    env.buyer("Второй исправленный", chat=888, author="Bob"); env.buyer("готово", chat=888, author="Bob")
    env.buyer("да", chat=888, author="Bob")
    e = env.ad.get("QQ000002")
    env.clock["t"] = e["post"]["publish_at"] + 1
    env.run(env.ap._tick())
    assert env.bot.channel_posts()[-1][2] == "Второй исправленный"
