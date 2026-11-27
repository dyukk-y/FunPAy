# -*- coding: utf-8 -*-
"""Короткие уведомления об оплате, экранирование уведомлений, ответ после покупки."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PAID = ("Покупатель Vasya123 оплатил заказ #ABCD1234. Telegram, Реклама, Пост в канале на 24 ч. "
        "Vasya123, не забудьте потом нажать кнопку «Подтвердить выполнение заказа».")


def test_parse_paid():
    from utils.fp_system import parse_paid
    assert parse_paid(PAID) == ("Telegram, Реклама, Пост в канале на 24 ч", "ABCD1234", "Vasya123")
    assert parse_paid("The buyer Bob has paid for order #ZX12CV34. Steam, Keys. Bob, do not forget "
                      "to press the «Confirm order fulfilment» button once you finish.")[0] == "Steam, Keys"
    assert parse_paid("Покупатель Bob подтвердил успешное выполнение заказа #ZX12CV34") is None
    assert parse_paid("обычное сообщение") is None


@pytest.fixture
def notif(monkeypatch):
    import modules.notifications as n
    from config.settings import config_manager
    from core import funpay_client

    sent = []

    async def fake(uid, text, photo, markup=None):
        sent.append(text)

    client = NS(own_id=1, acc_cfg=None, index=0, is_recent_outgoing=lambda *a: False)
    monkeypatch.setattr(n, "_send_with_photo", fake)
    monkeypatch.setattr(n, "_all_recipients", lambda: [111])
    monkeypatch.setattr(funpay_client.accounts_manager, "client_for", lambda m: client)
    monkeypatch.setattr(funpay_client.accounts_manager, "tag", lambda *a, **k: "")
    config_manager.settings.notifications.new_messages = True
    old_ad = config_manager.settings.ad_flow.enabled
    config_manager.settings.ad_flow.enabled = False   # без карточки заказа рекламы
    yield n, sent
    config_manager.settings.ad_flow.enabled = old_ad


def _msg(author_id, text, author="FunPay"):
    return NS(author_id=author_id, author=author, text=text, chat_id=5, chat_name="Vasya123",
              by_bot=False, image_link=None)


def test_paid_system_message_is_short(notif):
    n, sent = notif
    asyncio.run(n.notify_message(_msg(0, PAID)))
    assert sent == ["🛒 <b>Куплено:</b> Telegram, Реклама, Пост в канале на 24 ч"]


def test_buyer_html_is_escaped(notif):
    n, sent = notif
    asyncio.run(n.notify_message(_msg(7, "люблю <3 и <b>жирный", author="Vasya<3")))
    assert "&lt;3" in sent[-1] and "<b>жирный" not in sent[-1]


@pytest.fixture
def paid(tmp_path, monkeypatch):
    import modules.order_reactions as orx
    from config.settings import OrderPaidReplySettings, config_manager
    from core import funpay_client

    monkeypatch.setattr(orx, "_PAID_DONE_PATH", tmp_path / "paid.json")
    saved = config_manager.settings.order_paid_reply
    config_manager.settings.order_paid_reply = OrderPaidReplySettings(delay_sec=0)
    out = []
    async def resolve(order):
        return order.chat_id

    client = NS(account=object(), send_message=lambda cid, text, attempts=3: out.append((cid, text)) or True,
                order_chat_id_async=resolve)
    monkeypatch.setattr(funpay_client.accounts_manager, "client_for", lambda o: client)
    yield orx, out, config_manager.settings.order_paid_reply
    config_manager.settings.order_paid_reply = saved


def test_paid_reply_once_per_order(paid):
    orx, out, cfg = paid
    order = NS(id="ABCD1234", chat_id=55, buyer_username="Vasya", price=100, description="Пост")
    asyncio.run(orx.handle_order_paid(order))
    asyncio.run(orx.handle_order_paid(order))         # дубль события / перезапуск
    assert out == [(55, "Пришлите пост на @theaugustine")]
    asyncio.run(orx.handle_order_paid(NS(id="ZZZZ0000", chat_id=56, buyer_username="Bob")))
    assert len(out) == 2


def test_paid_reply_disabled(paid):
    orx, out, cfg = paid
    cfg.enabled = False
    asyncio.run(orx.handle_order_paid(NS(id="ABCD1234", chat_id=55, buyer_username="Vasya")))
    assert out == []


# ─── Системные сообщения, пришедшие «от покупателя» (резервный режим) ─────────

def test_fallback_system_message_is_short(notif):
    n, sent = notif
    # в резервном режиме автор — покупатель, ID — ID чата
    asyncio.run(n.notify_message(_msg(5, PAID, author="Vasya123")))
    assert sent == ["🛒 <b>Куплено:</b> Telegram, Реклама, Пост в канале на 24 ч"]


def test_other_system_messages_not_forwarded(notif):
    n, sent = notif
    asyncio.run(n.notify_message(_msg(0, "Покупатель Vasya123 подтвердил успешное выполнение заказа #ABCD1234 "
                                          "и отправил деньги продавцу Me.")))
    asyncio.run(n.notify_message(_msg(5, "Покупатель Vasya123 написал отзыв к заказу #ABCD1234.", author="Vasya123")))
    assert sent == []
    asyncio.run(n.notify_message(_msg(5, "Здравствуйте, когда будет пост?", author="Vasya123")))
    assert len(sent) == 1 and "когда будет пост" in sent[0]


def test_paid_reply_via_system_message_once(paid):
    orx, out, cfg = paid
    msg = NS(author_id=0, chat_id=777, text=PAID)
    asyncio.run(orx.paid_from_system_message(msg))
    asyncio.run(orx.handle_order_paid(NS(id="ABCD1234", chat_id=777, buyer_username="Vasya123")))  # основной путь
    assert out == [(777, "Пришлите пост на @theaugustine")]


def test_old_default_text_migrated(tmp_path):
    import json
    from config.settings import ConfigManager
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"order_paid_reply": {"text": "Присылайте текст рекламы на @theaugustine"}}),
                    encoding="utf-8")
    assert ConfigManager(path).settings.order_paid_reply.text == "Пришлите пост на @theaugustine"


def test_old_mode_system_message_gets_system_author(monkeypatch):
    from core import funpay_client
    from core.event_bus import event_bus
    got = []

    async def fake_emit(name, msg):
        got.append(msg)
    monkeypatch.setattr(event_bus, "emit", fake_emit)
    cl = funpay_client.FunPayClient()
    asyncio.run(cl._emit_message_from_chat(NS(id=777, name="Vasya123", last_message_text=PAID, last_by_bot=False)))
    asyncio.run(cl._emit_message_from_chat(NS(id=778, name="Bob", last_message_text="привет", last_by_bot=False)))
    assert got[0].author_id == 0 and got[0].type.name == "ORDER_PURCHASED"
    assert got[1].author_id == 778


def test_paid_message_replaced_by_ad_card(notif):
    n, sent = notif
    from config.settings import config_manager
    config_manager.settings.ad_flow.enabled = True
    asyncio.run(n.notify_message(_msg(0, PAID)))
    assert sent == []          # вместо «Куплено» придёт карточка «📢 Куплена реклама»
