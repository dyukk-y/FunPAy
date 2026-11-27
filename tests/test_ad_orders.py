# -*- coding: utf-8 -*-
"""Заказы рекламы: оплата → ссылка на пост → сообщение покупателю → подтверждение."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ─── Срок размещения ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("title,human,until", [
    ("Telegram, Реклама, Пост в канале на 24 часа", "24 часа", "сейчас или по истечении 24 часов"),
    ("Пост 48ч", "48 часов", "сейчас или по истечении 48 часов"),
    ("Закреп на 3 дня", "3 дня", "сейчас или по истечении 3 дней"),
    ("Реклама 1 неделя", "1 неделя", "сейчас или по истечении 1 недели"),
    ("Пост 1 час", "1 час", "сейчас или по истечении 1 часа"),
    ("Пост на 21 час", "21 час", "сейчас или по истечении 21 часа"),
    ("Пост навсегда", "навсегда", "уже сейчас"),
    ("Пост на сутки", "24 часа", "сейчас или по истечении 24 часов"),
])
def test_duration_from_title(title, human, until):
    from utils import ad_duration as d
    dur = d.detect(title)
    assert d.human(dur.hours, dur.unit) == human and d.until_phrase(dur) == until


def test_duration_story_and_description():
    from utils import ad_duration as d
    s = d.detect("Реклама в истории")
    assert s.where == "в истории" and s.hours == 24
    x = d.detect("Пост в канале", description="Пост висит 72 часа, потом удаляется")
    assert x.hours == 72 and x.source == "описание лота"
    assert d.detect("Пост в канале") is None
    assert d.detect("Пост в канале", default_hours=48).hours == 48


# ─── Сценарий ────────────────────────────────────────────────────────────────

class FakeAccount:
    def __init__(self):
        self.sent = []

    def send_message(self, cid, text, update_last_saved_message=False):
        self.sent.append((cid, text))

    def get_chat_by_name(self, name, make_request=False):
        return NS(id=777)

    def get_order(self, order_id):
        return NS(full_description="Ваш пост будет в канале 48 часов", lot_params_text="",
                  short_description="Пост в канале", buyer_username="Vasya")


@pytest.fixture
def flow(tmp_path, monkeypatch):
    import modules.ad_orders as ad
    import modules.notifications as notif
    import modules.order_reactions as orx
    from config.settings import AdFlowSettings, AskReviewSettings, OrderConfirmReplySettings, config_manager
    from core import funpay_client

    monkeypatch.setattr(ad, "STATE_PATH", tmp_path / "ad.json")
    monkeypatch.setattr(orx, "_PAID_DONE_PATH", tmp_path / "done.json")
    monkeypatch.setattr(config_manager, "save", lambda: None)
    from config.settings import AdPostSettings
    s = config_manager.settings
    saved = (s.ad_flow, s.order_confirm_reply, s.ask_review, s.ad_post)
    s.ad_flow, s.order_confirm_reply, s.ask_review = AdFlowSettings(), OrderConfirmReplySettings(), AskReviewSettings()
    s.ad_post = AdPostSettings(enabled=False)        # здесь проверяем ручной режим (ссылку присылаете вы)

    acc = FakeAccount()
    cl = funpay_client.FunPayClient()
    cl.account = acc
    monkeypatch.setattr(funpay_client.accounts_manager, "client_for", lambda o: cl)
    monkeypatch.setattr(funpay_client.accounts_manager, "get", lambda i: cl)
    cards = []

    async def fake_all(text, markup=None, photo_url=None):
        cards.append(text)
    monkeypatch.setattr(notif, "_send_to_all", fake_all)
    yield ad, orx, acc, cards
    s.ad_flow, s.order_confirm_reply, s.ask_review, s.ad_post = saved


def test_full_ad_flow(flow):
    ad, orx, acc, cards = flow
    order = NS(id="ABCD1234", chat_id="users-1-2", buyer_username="Vasya", price=500,
               description="Telegram, Реклама, Пост в канале на 24 часа")
    asyncio.run(ad.on_paid(order))
    asyncio.run(ad.on_paid(order))                         # дубль события — второй карточки нет
    assert len(cards) == 1 and "Куплена реклама" in cards[0] and "24 часа" in cards[0]
    assert [e["order_id"] for e in ad.waiting_orders()] == ["ABCD1234"]

    status, text = asyncio.run(ad.attach_link("ABCD1234", "Вот: t.me/mychannel/55"))
    assert status == "sent"
    assert acc.sent[-1] == (777, "https://t.me/mychannel/55 — реклама в канале.\n"
                                 "Подтвердить лот можете сейчас или по истечении 24 часов.")
    assert ad.waiting_orders() == []

    # покупатель подтвердил → благодарность, затем просьба об отзыве
    acc.sent.clear()
    asyncio.run(orx.handle_order_confirmed(NS(id="ABCD1234", chat_id="users-1-2", buyer_username="Vasya")))
    assert [t for _, t in acc.sent] == [
        "Был рад посотрудничать, будем ждать вас вновь! ❤️",
        "Если не тяжело, напишите отзыв пожалуйста о рекламе, нам важно ваше мнение 😉",
    ]
    assert ad.get("ABCD1234") is None


def test_duration_from_order_description(flow):
    ad, orx, acc, cards = flow
    asyncio.run(ad.on_paid(NS(id="QQQQ1111", chat_id=777, buyer_username="Vasya", description="Пост в канале")))
    e = ad.get("QQQQ1111")
    assert e["hours"] == 48 and e["dur_source"] == "описание лота"


def test_unknown_duration_asks_then_sends(flow, monkeypatch):
    ad, orx, acc, cards = flow
    monkeypatch.setattr(FakeAccount, "get_order", lambda self, oid: NS(full_description="", lot_params_text="",
                                                                       short_description="", buyer_username=""))
    asyncio.run(ad.on_paid(NS(id="ZZZZ2222", chat_id=777, buyer_username="Bob", description="Пост в канале")))
    assert "не нашёл" in cards[-1]
    assert asyncio.run(ad.attach_link("ZZZZ2222", "https://t.me/c/1")) == ("need_duration", "")
    assert acc.sent == []
    status, text = asyncio.run(ad.set_duration("ZZZZ2222", 12))
    assert status == "sent" and "по истечении 12 часов" in acc.sent[-1][1]


def test_forever_and_story(flow, monkeypatch):
    ad, orx, acc, cards = flow
    monkeypatch.setattr(FakeAccount, "get_order", lambda self, oid: NS(full_description="", lot_params_text="",
                                                                       short_description="", buyer_username=""))
    asyncio.run(ad.on_paid(NS(id="AAAA0001", chat_id=777, buyer_username="X", description="Пост навсегда")))
    asyncio.run(ad.attach_link("AAAA0001", "https://t.me/c/2"))
    assert acc.sent[-1][1] == "https://t.me/c/2 — реклама в канале.\nПодтвердить лот можете уже сейчас."
    asyncio.run(ad.on_paid(NS(id="AAAA0002", chat_id=777, buyer_username="X", description="Реклама в истории")))
    asyncio.run(ad.attach_link("AAAA0002", "https://t.me/c/3"))
    assert "реклама в истории" in acc.sent[-1][1] and "24 часов" in acc.sent[-1][1]


def test_extract_link():
    from modules.ad_orders import extract_link
    assert extract_link("пост: https://t.me/abc/12.") == "https://t.me/abc/12"
    assert extract_link("t.me/abc/12") == "https://t.me/abc/12"
    assert extract_link("просто текст") is None


def test_migration_sets_confirm_texts(tmp_path):
    import json
    from config.settings import ConfigManager
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"order_confirm_reply": {"enabled": False, "text": "старый"},
                             "ask_review": {"enabled": False, "text": "старый", "delay_minutes": 10},
                             "sales_brain": {"mode": "off"}}), encoding="utf-8")
    s = ConfigManager(p).settings
    assert s.order_confirm_reply.enabled and s.order_confirm_reply.text.startswith("Был рад посотрудничать")
    assert s.ask_review.enabled and s.ask_review.delay_minutes == 0 and "отзыв" in s.ask_review.text
    assert s.sales_brain.mode == "auto"             # v2: уверен — отвечает сам, не уверен — подсказка
    assert s.auto_lift.enabled and s.ad_post.active() and s.ad_post.channel_id == -1002120185316
    # повторная загрузка не перетирает ваши правки
    data = json.loads(p.read_text(encoding="utf-8"))
    data["order_confirm_reply"]["text"] = "мой текст"
    p.write_text(json.dumps(data), encoding="utf-8")
    assert ConfigManager(p).settings.order_confirm_reply.text == "мой текст"
