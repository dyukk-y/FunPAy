# -*- coding: utf-8 -*-
"""
Сообщения по заказам: у заказа FunPay chat_id — строка «users-<id>-<id>».
Раньше send_message её отбрасывал — не работали автовыдача, ответ на
подтверждение, просьба об отзыве.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeAccount:
    def __init__(self, chats=None):
        self.sent = []
        self.chats = chats or {}

    def send_message(self, cid, text, update_last_saved_message=False):
        self.sent.append((cid, text))

    def get_chat_by_name(self, name, make_request=False):
        cid = self.chats.get(name)
        return NS(id=cid) if cid else None


def make_client(account):
    from core.funpay_client import FunPayClient
    cl = FunPayClient()
    cl.account = account
    return cl


def test_send_message_accepts_private_node_name():
    acc = FakeAccount()
    cl = make_client(acc)
    assert cl.send_message("users-100-200", "привет")
    assert acc.sent == [("users-100-200", "привет")]
    assert cl.send_message(12345, "ок") and acc.sent[-1] == (12345, "ок")
    assert not cl.send_message("мусор", "x")


def test_order_chat_id_resolves_by_buyer():
    acc = FakeAccount(chats={"Vasya": 777})
    cl = make_client(acc)
    assert cl.order_chat_id(NS(chat_id="users-1-2", buyer_username="Vasya")) == 777
    assert cl.order_chat_id(NS(chat_id="users-1-2", buyer_username="Unknown")) == "users-1-2"   # запасной вариант
    assert cl.order_chat_id(NS(chat_id=555, buyer_username="Vasya")) == 555


@pytest.fixture
def confirm(tmp_path, monkeypatch):
    import modules.order_reactions as orx
    from config.settings import config_manager
    from core import funpay_client

    monkeypatch.setattr(orx, "_PAID_DONE_PATH", tmp_path / "done.json")
    s = config_manager.settings
    old = (s.order_confirm_reply.enabled, s.order_confirm_reply.text, s.ask_review.enabled)
    s.order_confirm_reply.enabled, s.order_confirm_reply.text = True, "Спасибо, $username!"
    s.ask_review.enabled = False
    acc = FakeAccount(chats={"Vasya": 777})
    cl = make_client(acc)
    monkeypatch.setattr(funpay_client.accounts_manager, "client_for", lambda o: cl)
    yield orx, acc
    s.order_confirm_reply.enabled, s.order_confirm_reply.text, s.ask_review.enabled = old


def test_confirm_reply_is_sent(confirm):
    orx, acc = confirm
    asyncio.run(orx.handle_order_confirmed(NS(id="ABCD1234", chat_id="users-1-2", buyer_username="Vasya",
                                              price=100, description="Пост")))
    assert acc.sent == [(777, "Спасибо, Vasya!")]


def test_confirm_via_system_message_and_status_only_once(confirm):
    orx, acc = confirm
    sysmsg = NS(author_id=0, chat_id=777,
                text="Покупатель Vasya подтвердил успешное выполнение заказа #ABCD1234 и отправил деньги продавцу Me.")
    asyncio.run(orx.confirmed_from_system_message(sysmsg))
    asyncio.run(orx.handle_order_confirmed(NS(id="ABCD1234", chat_id="users-1-2", buyer_username="Vasya")))
    assert acc.sent == [(777, "Спасибо, Vasya!")]


def test_confirm_disabled_does_not_mark(confirm):
    orx, acc = confirm
    from config.settings import config_manager
    config_manager.settings.order_confirm_reply.enabled = False
    order = NS(id="QQQQ1111", chat_id="users-1-2", buyer_username="Vasya")
    asyncio.run(orx.handle_order_confirmed(order))
    assert acc.sent == []
    config_manager.settings.order_confirm_reply.enabled = True       # включили позже — ответ всё ещё возможен
    asyncio.run(orx.handle_order_confirmed(order))
    assert acc.sent == [(777, "Спасибо, Vasya!")]


def test_parse_confirmed():
    from utils.fp_system import parse_confirmed
    assert parse_confirmed("Покупатель Vasya подтвердил успешное выполнение заказа #ABCD1234 и отправил "
                           "деньги продавцу Me.") == ("ABCD1234", "Vasya")
    assert parse_confirmed("The buyer Bob has confirmed that order #ZX12CV34 has been fulfilled") == ("ZX12CV34", "Bob")
    assert parse_confirmed("обычный текст #ABCD1234") is None
