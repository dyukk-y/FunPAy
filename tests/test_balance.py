# -*- coding: utf-8 -*-
"""Account.get_balance() без lot_id — раньше падал с TypeError (баг оригинала)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PAGE = ('<div class="user-link-name">me</div><select name="method" '
        'data-balance-total-rub="150.5" data-balance-rub="100" data-balance-total-usd="1" '
        'data-balance-usd="1" data-balance-total-eur="0" data-balance-eur="0"></select>')
PAGE_NO_FORM = '<div class="user-link-name">me</div>'


class _Resp:
    def __init__(self, text):
        self.content = text.encode()


class _Seller:
    def __init__(self, id_):
        self.id = id_


class _Lot:
    def __init__(self, id_, seller):
        self.id = id_
        self.seller = _Seller(seller)


@pytest.fixture
def acc(monkeypatch):
    from FunPayAPI.account import Account
    from FunPayAPI.common import enums

    a = Account("x" * 32)
    a._Account__initiated = True
    a.id = 1
    pages = {}
    calls = []

    def method(_m, url, *args, **kw):
        calls.append(url)
        lot_id = int(url.split("=")[1])
        return _Resp(pages.get(lot_id, PAGE))

    monkeypatch.setattr(a, "method", method)
    monkeypatch.setattr(a, "_Account__update_csrf_token", lambda p: None)
    monkeypatch.setattr(a, "get_sorted_subcategories", lambda: {enums.SubCategoryTypes.COMMON: {10: None}})
    monkeypatch.setattr(a, "get_subcategory_public_lots",
                        lambda t, sid: [_Lot(555, seller=1), _Lot(777, seller=2)])
    return a, pages, calls


def test_balance_without_lot_id(acc):
    a, pages, calls = acc
    b = a.get_balance()
    assert b.total_rub == 150.5
    assert calls == ["lots/offer?id=777"]      # свой лот (555) не берём
    a.get_balance()
    assert calls[-1] == "lots/offer?id=777"    # лот запомнен


def test_balance_lot_without_form_is_skipped(acc):
    a, pages, calls = acc
    a.get_balance()
    pages[777] = PAGE_NO_FORM                  # лот сняли — формы нет
    with pytest.raises(Exception):
        a.get_balance()                        # единственный чужой лот без формы → честная ошибка
    pages.pop(777)
    assert a.get_balance().total_rub == 150.5  # восстановился сам
