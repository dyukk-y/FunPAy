# -*- coding: utf-8 -*-
"""
Тесты модуля «Тексты и цены лотов» (modules/lot_texts.py) — без сети,
FunPay подменяется фейковым аккаунтом.
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LOT_TEXT = (
    "Реклама в канале.\r\n\r\n"
    "📅 Актуально на 06.04.2026, 04:00 МСК:\r\n"
    "Мы уже успешно продали 532 рекламных размещений! 🚀\r\n"
    "*(Данные обновляются каждую неделю для вашей уверенности)*"
)


# ─── Фейковый FunPay ─────────────────────────────────────────────────────────

class FakeLotFields:
    def __init__(self, lot_id, desc, price=100.0):
        self.lot_id = lot_id
        self.title_ru = f"Лот {lot_id}"
        self.title_en = ""
        self.description_ru = desc
        self.description_en = ""
        self.payment_msg_ru = ""
        self.payment_msg_en = ""
        self.price = price


class FakeLot:
    def __init__(self, lot_id, title):
        self.id = lot_id
        self.description = title


class FakeSubcat:
    id = 1

    class type:
        name = "COMMON"


class FakeProfile:
    def __init__(self, ids):
        self.ids = ids

    def get_sorted_lots(self, mode):
        return {FakeSubcat(): {i: FakeLot(i, f"Лот {i}") for i in self.ids}}


class FakeAccount:
    id = 1

    def __init__(self, lots, fail_save=()):
        self.lots = lots              # lot_id → (desc, price)
        self.saved = {}
        self.fail_save = set(fail_save)

    def get_user(self, _id):
        return FakeProfile(list(self.lots))

    def get_lot_fields(self, lot_id):
        desc, price = self.lots[lot_id]
        return FakeLotFields(lot_id, desc, price)

    def save_lot(self, lf):
        if lf.lot_id in self.fail_save:
            raise RuntimeError("FunPay сказал нет")
        self.saved[lf.lot_id] = lf
        self.lots[lf.lot_id] = (lf.description_ru, lf.price)


class FakeClient:
    alias = "test"

    def __init__(self, account):
        self.account = account


@pytest.fixture
def env(monkeypatch):
    from config.settings import LotTextsSettings, config_manager
    from core import funpay_client
    import modules.lot_texts as lt

    saved = config_manager.settings.lot_texts
    config_manager.settings.lot_texts = LotTextsSettings(delay_min_sec=0, delay_max_sec=0)
    monkeypatch.setattr(lt, "READ_DELAY", (0, 0))
    monkeypatch.setattr(config_manager, "save", lambda: None)

    holder = {}

    def connected():
        return [holder["client"]]

    monkeypatch.setattr(funpay_client.accounts_manager, "connected_clients", connected)
    monkeypatch.setattr(funpay_client.accounts_manager, "all", connected)

    def make(lots, **kw):
        acc = FakeAccount(lots, **kw)
        holder["client"] = FakeClient(acc)
        return acc

    yield lt, config_manager.settings.lot_texts, make
    config_manager.settings.lot_texts = saved


def run(coro):
    return asyncio.run(coro)


# ─── Шаблон ──────────────────────────────────────────────────────────────────

def test_template_matches_user_text():
    import modules.lot_texts as lt
    from config.settings import DEFAULT_COUNTER_TEMPLATE

    rx = lt.compile_template(DEFAULT_COUNTER_TEMPLATE)
    assert lt.find_counters(LOT_TEXT, rx) == [("06.04.2026", 532)]
    new, n = lt.apply_counter(LOT_TEXT, rx, {"date": "24.11.2026", "time": "04:00", "count": "539"})
    assert n == 1
    assert "Актуально на 24.11.2026, 04:00 МСК" in new
    assert "продали 539 рекламных" in new
    # всё остальное не тронуто
    assert new.replace("24.11.2026", "06.04.2026").replace("539", "532") == LOT_TEXT


def test_template_tolerant_to_emoji_and_newlines():
    import modules.lot_texts as lt
    from config.settings import DEFAULT_COUNTER_TEMPLATE

    rx = lt.compile_template(DEFAULT_COUNTER_TEMPLATE)
    t = "Актуально на 6.4.2026 04:00 МСК — Мы уже успешно продали 1000 рекламных размещений"
    assert lt.find_counters(t, rx) == [("6.4.2026", 1000)]
    assert lt.find_counters("Мы продали 532 штуки", rx) == []


def test_template_validation():
    import modules.lot_texts as lt

    with pytest.raises(lt.TemplateError):
        lt.compile_template("просто текст")
    with pytest.raises(lt.TemplateError):
        lt.compile_template("{count} и ещё {count}")
    lt.compile_template("Продано: {count}")


def test_price_ops():
    import modules.lot_texts as lt

    assert lt.calc_price(100, lt.parse_price_op("150")) == 150
    assert lt.calc_price(100, lt.parse_price_op("+20")) == 120
    assert lt.calc_price(100, lt.parse_price_op("-10%")) == 90
    assert lt.calc_price(100, lt.parse_price_op("+5,5%")) == 105.5
    assert lt.calc_price(10, lt.parse_price_op("-50")) == 0
    assert lt.parse_price_op("=10%") is None
    assert lt.parse_price_op("abc") is None


def test_schedule_slots():
    from datetime import datetime
    import modules.lot_texts as lt
    from config.settings import config_manager

    c = config_manager.settings.lot_texts
    old = (c.weekday, c.hour, c.minute)
    try:
        c.weekday, c.hour, c.minute = 0, 4, 0     # понедельник 04:00
        ts = datetime(2026, 11, 25, 12, 0, tzinfo=lt.MSK)  # среда
        assert lt.last_slot(ts) == datetime(2026, 11, 23, 4, 0, tzinfo=lt.MSK)
        assert lt.next_slot(ts) == datetime(2026, 11, 30, 4, 0, tzinfo=lt.MSK)
        ts = datetime(2026, 11, 23, 3, 59, tzinfo=lt.MSK)  # пн, ещё не наступило
        assert lt.last_slot(ts) == datetime(2026, 11, 16, 4, 0, tzinfo=lt.MSK)
    finally:
        c.weekday, c.hour, c.minute = old


# ─── Прогон на фейковом FunPay ───────────────────────────────────────────────

def test_weekly_run_same_number_everywhere(env):
    lt, cfg, make = env
    acc = make({1: (LOT_TEXT, 100.0), 2: (LOT_TEXT.replace("532", "530"), 50.0), 3: ("Без счётчика", 10.0)})
    rep = run(lt.run_job(lt.weekly_job()))
    assert rep.ok, rep.error
    assert set(acc.saved) == {1, 2}            # лот без шаблона не трогали
    rx = lt.compile_template(cfg.counter.template)
    vals = {lt.find_counters(acc.lots[i][0], rx)[0] for i in (1, 2)}
    assert len(vals) == 1                      # одна дата и одно число во всех лотах
    date, count = vals.pop()
    assert date == lt.now_msk().strftime("%d.%m.%Y")
    assert 535 <= count <= 542                 # от максимума 532 + 3…10
    assert cfg.counter.count == count


def test_auto_run_is_idempotent(env):
    lt, cfg, make = env
    acc = make({1: (LOT_TEXT, 100.0), 2: (LOT_TEXT, 100.0)})
    run(lt.run_job(lt.weekly_job()))
    first = dict(acc.lots)
    acc.saved.clear()
    rep = run(lt.run_job(lt.weekly_job()))     # повтор (например, после перезапуска)
    assert rep.ok
    assert acc.saved == {}                     # ничего не пересохранили
    assert acc.lots == first


def test_catch_up_lot_that_failed_before(env):
    lt, cfg, make = env
    acc = make({1: (LOT_TEXT, 100.0), 2: (LOT_TEXT, 100.0)}, fail_save=[2])
    rep = run(lt.run_job(lt.weekly_job()))
    assert rep.ok and len(rep.failed) == 1 and 1 in acc.saved
    rx = lt.compile_template(cfg.counter.template)
    target = lt.find_counters(acc.lots[1][0], rx)[0][1]
    # FunPay «починился» — второй запуск в тот же день догоняет лот 2 с ТЕМ ЖЕ числом
    acc.fail_save.clear()
    acc.saved.clear()
    run(lt.run_job(lt.weekly_job()))
    assert set(acc.saved) == {2}
    assert lt.find_counters(acc.lots[2][0], rx)[0][1] == target


def test_manual_count_and_force(env):
    lt, cfg, make = env
    acc = make({1: (LOT_TEXT, 100.0)})
    cfg.counter.count, cfg.counter.count_manual = 100, True
    cfg.counter.inc_min = cfg.counter.inc_max = 5
    run(lt.run_job(lt.weekly_job(force=True)))
    rx = lt.compile_template(cfg.counter.template)
    assert lt.find_counters(acc.lots[1][0], rx)[0][1] == 105
    run(lt.run_job(lt.weekly_job(force=True)))  # «Обновить сейчас» ещё раз — прибавляет
    assert lt.find_counters(acc.lots[1][0], rx)[0][1] == 110


def test_dry_run_changes_nothing(env):
    lt, cfg, make = env
    acc = make({1: (LOT_TEXT, 100.0)})
    rep = run(lt.run_job(lt.Job(counter=True, price_op=("+", 10, True), dry_run=True)))
    assert rep.ok and len(rep.changed) == 1
    assert acc.saved == {} and acc.lots[1] == (LOT_TEXT, 100.0)


def test_replace_and_price_filters(env):
    lt, cfg, make = env
    from config.settings import LotReplaceRule
    acc = make({1: ("Скидка 10%", 100.0), 2: ("Скидка 10%", 200.0)})
    run(lt.run_job(lt.Job(replaces=[LotReplaceRule(find="10%", replace="20%")], lot_ids=[2])))
    assert acc.lots == {1: ("Скидка 10%", 100.0), 2: ("Скидка 20%", 200.0)}
    run(lt.run_job(lt.Job(price_op=("-", 10, True))))
    assert acc.lots[1][1] == 90 and acc.lots[2][1] == 180


def test_errors_do_not_crash(env):
    lt, cfg, make = env
    acc = make({i: (LOT_TEXT, 100.0) for i in range(1, 9)}, fail_save=range(1, 9))
    rep = run(lt.run_job(lt.weekly_job()))
    assert rep.ok and rep.aborted                # остановился после 5 ошибок подряд
    assert len(rep.failed) == lt.MAX_CONSECUTIVE_ERRORS
    assert "Остановлено" in rep.to_html("t")


def test_not_connected(env, monkeypatch):
    lt, cfg, make = env
    from core import funpay_client
    monkeypatch.setattr(funpay_client.accounts_manager, "connected_clients", lambda: [])
    rep = run(lt.run_job(lt.weekly_job()))
    assert not rep.ok and "golden_key" in rep.error


# ─── ENV не затирает ключ, присланный с телефона ─────────────────────────────

def test_env_does_not_override_phone_key(monkeypatch):
    from config.settings import Settings, config_manager
    from utils.env_config import apply_env_overrides

    saved = config_manager.settings
    config_manager.settings = Settings()
    monkeypatch.setattr(config_manager, "save", lambda: None)
    try:
        monkeypatch.setenv("GOLDEN_KEY", "a" * 32)
        monkeypatch.setenv("BOT_TOKEN", "1:" + "x" * 40)
        apply_env_overrides()
        fp = config_manager.settings.funpay
        assert fp.golden_key == "a" * 32 and fp.accounts
        fp.golden_key = "b" * 32                 # прислали новый ключ в Telegram
        apply_env_overrides()                    # перезапуск, в панели старое значение
        assert fp.golden_key == "b" * 32
        monkeypatch.setenv("GOLDEN_KEY", "c" * 32)  # поменяли в панели — применяется
        apply_env_overrides()
        assert fp.golden_key == "c" * 32
    finally:
        config_manager.settings = saved
