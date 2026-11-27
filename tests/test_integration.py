# -*- coding: utf-8 -*-
"""
Сквозной тест: заказ рекламы через НАСТОЯЩУЮ шину событий, со всеми модулями бота сразу
(уведомления, ответ после оплаты, карточка заказа, автопубликация, автоответчик,
анализ продаж, ИИ). Проверяем, что модули не мешают друг другу: покупатель не получает
лишних сообщений, вы — дублей.
"""
import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_ad_autopost import FakeAccount, FakeBot, CHANNEL, BUYER_ID, ME  # noqa: E402

PAID_TEXT = ("Покупатель Vasya оплатил заказ #INT00001. Telegram, Реклама, Пост 1/24. Vasya, не забудьте потом "
             "нажать кнопку «Подтвердить выполнение заказа».")
CONFIRM_TEXT = "Покупатель Vasya подтвердил успешное выполнение заказа #INT00001 и отправил деньги продавцу Me."


def test_full_pipeline(tmp_path, monkeypatch):
    # все модули, которые подписываются на события (как main.py)
    import modules.notifications as notif
    import modules.order_reactions as orx
    import modules.ad_orders as ad
    import modules.ad_autopost as ap
    import modules.sales_brain as sb
    import modules.auto_response  # noqa: F401
    import modules.ai_assistant as ai
    import modules.auto_greeting  # noqa: F401
    import modules.analytics  # noqa: F401
    from config.settings import (AdFlowSettings, AdPostSettings, AskReviewSettings, OrderConfirmReplySettings,
                                 OrderPaidReplySettings, SalesBrainSettings, config_manager)
    from core import funpay_client
    from core.event_bus import Event, event_bus

    monkeypatch.setattr(ad, "STATE_PATH", tmp_path / "ad.json")
    monkeypatch.setattr(orx, "_PAID_DONE_PATH", tmp_path / "done.json")
    monkeypatch.setattr(sb, "DB_PATH", tmp_path / "sb.db")
    monkeypatch.setattr(ai, "PENDING_PATH", tmp_path / "ai.json")
    sb.reset_connection()
    monkeypatch.setattr(config_manager, "save", lambda: None)
    s = config_manager.settings
    saved = {k: getattr(s, k) for k in ("ad_flow", "ad_post", "order_confirm_reply", "ask_review",
                                        "order_paid_reply", "sales_brain")}
    s.ad_flow, s.ad_post = AdFlowSettings(), AdPostSettings()
    s.order_confirm_reply, s.ask_review = OrderConfirmReplySettings(), AskReviewSettings()
    s.order_paid_reply, s.sales_brain = OrderPaidReplySettings(delay_sec=0), SalesBrainSettings(mode="auto")
    s.auto_response.enabled = True
    s.auto_response.templates = {"пост": "АВТООТВЕТ ШАБЛОНОМ"}      # не должен сработать на текст рекламы
    s.notifications.new_messages = True
    old_admins = list(s.telegram.admin_ids)
    s.telegram.admin_ids = [111]
    ap._channel_cache.update(ts=0, ok=False, msg="")

    acc = FakeAccount()
    acc.history = [NS(id=1, author_id=0, text=PAID_TEXT, image_link=None)]
    cl = funpay_client.FunPayClient()
    cl.account = acc
    cl.acc_cfg = NS(account_id=ME, username="me", display_name=lambda: "me", enabled=True)
    for name in ("client_for", "get"):
        monkeypatch.setattr(funpay_client.accounts_manager, name, lambda *_a: cl)
    for name in ("active",):
        monkeypatch.setattr(funpay_client.accounts_manager, name, lambda: cl)
    monkeypatch.setattr(funpay_client.accounts_manager, "connected_clients", lambda: [cl])
    monkeypatch.setattr(funpay_client.accounts_manager, "all", lambda: [cl])
    bot = FakeBot()
    monkeypatch.setattr(notif, "_bot_ref", bot)

    ids = iter(range(5000, 6000))

    def msg(author_id, text=None, image=None, author="Vasya"):
        return NS(id=next(ids), chat_id=777, chat_name="Vasya", author=author, author_id=author_id, text=text,
                  image_link=image, by_bot=False, type=None, badge=None, is_employee=False, is_autoreply=False)

    async def settle():
        for _ in range(30):
            await asyncio.sleep(0.01)
            pend = [t for t in list(ap._tasks) + list(sb._tasks) + list(orx._bg_tasks) + list(ad._tasks) if not t.done()]
            if pend:
                await asyncio.gather(*pend, return_exceptions=True)

    async def scenario():
        order = NS(id="INT00001", chat_id="users-1-555", buyer_username="Vasya", buyer_id=BUYER_ID, price=700,
                   description="Telegram, Реклама, Пост 1/24", status=None)
        await event_bus.emit(Event.NEW_ORDER, order)
        await event_bus.emit(Event.ORDER_PAID, order)
        await event_bus.emit(Event.NEW_MESSAGE, msg(0, PAID_TEXT, author="FunPay"))   # системное «оплатил»
        await settle()
        await event_bus.emit(Event.NEW_MESSAGE, msg(BUYER_ID, "Канал про путешествия: лучшие туры по Азии ✈️ "
                                                              "Подписывайтесь, будет интересно! Пост для всех."))
        await settle()
        await event_bus.emit(Event.NEW_MESSAGE, msg(BUYER_ID, image="https://sfunpay.com/s/chat/x.jpg"))
        await event_bus.emit(Event.NEW_MESSAGE, msg(BUYER_ID, "готово"))
        await settle()
        await event_bus.emit(Event.NEW_MESSAGE, msg(BUYER_ID, "да"))
        await settle()
        await event_bus.emit(Event.NEW_MESSAGE, msg(0, CONFIRM_TEXT, author="FunPay"))  # покупатель подтвердил
        await settle()

    try:
        asyncio.run(scenario())
        to_buyer = [t for _, t in acc.sent]
        # 1) инструкция ровно один раз и без «Пришлите пост на @theaugustine»
        assert sum("Пришлите сюда, в этот чат, текст рекламного поста" in t for t in to_buyer) == 1
        assert not any("@theaugustine" in t for t in to_buyer)
        # 2) автоответчик/ИИ/подсказки не ответили на текст рекламы
        assert not any("АВТООТВЕТ ШАБЛОНОМ" in t for t in to_buyer)
        # 3) предпросмотр → публикация → ссылка → благодарность → отзыв
        assert any("Проверьте, пожалуйста, пост" in t for t in to_buyer)
        posts = bot.channel_posts()
        assert len(posts) == 1 and posts[0][0] == "send_photo" and posts[0][1] == CHANNEL
        assert "Канал про путешествия" in posts[0][2] and "готово" not in posts[0][2]
        link_msgs = [t for t in to_buyer if t.startswith("https://t.me/c/2120185316/")]
        assert len(link_msgs) == 1 and "по истечении 24 часов" in link_msgs[0]
        assert to_buyer[-2] == "Был рад посотрудничать, будем ждать вас вновь! ❤️"
        assert to_buyer[-1].startswith("Если не тяжело, напишите отзыв")
        # 4) вам: одна карточка о покупке (без «Новый заказ»/«Заказ оплачен»/«Куплено»), «Опубликовано»
        admin = [c[2] for c in bot.calls if c[0] == "send_message" and c[1] == 111]
        assert sum("Куплена реклама" in t for t in admin) == 1
        assert not any("Новый заказ" in t or "Заказ оплачен" in t or "Куплено:" in t for t in admin)
        assert sum("Опубликовано" in t for t in admin) == 1
        assert not any("оплатил заказ" in t or "подтвердил успешное" in t for t in admin)  # системные не пересылаются
        # 5) пост удалится по сроку, хотя заказ уже подтверждён
        e = ad.get("INT00001")
        assert e and e["post"]["delete_at"] > time.time()
    finally:
        for k, v in saved.items():
            setattr(s, k, v)
        s.telegram.admin_ids = old_admins
        s.auto_response.templates = {}
        sb.reset_connection()
