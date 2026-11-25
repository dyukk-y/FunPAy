# -*- coding: utf-8 -*-
"""Тесты «Анализ переписки и автопродажи» (modules/sales_brain.py) — без сети."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ME = 1
_ids = iter(range(1000, 100000))


def M(author_id, text, chat_id=10, by_bot=False, type_="NON_SYSTEM", author="buyer"):
    return SimpleNamespace(id=next(_ids), author_id=author_id, text=text, chat_id=chat_id, by_bot=by_bot,
                           type=SimpleNamespace(name=type_), author=author if author_id != ME else "me",
                           chat_name=author, is_employee=False, is_autoreply=False)


@pytest.fixture
def sb(tmp_path, monkeypatch):
    import modules.sales_brain as sb
    from config.settings import SalesBrainSettings, config_manager

    monkeypatch.setattr(sb, "DB_PATH", tmp_path / "sb.db")
    sb.reset_connection()
    saved = config_manager.settings.sales_brain
    config_manager.settings.sales_brain = SalesBrainSettings(min_cluster=2, auto_delay_min=0, auto_delay_max=0)
    monkeypatch.setattr(config_manager, "save", lambda: None)
    sb.pending.clear()
    yield sb
    config_manager.settings.sales_brain = saved
    sb.reset_connection()


def chat(sb, chat_id, buyer, msgs):
    for m in msgs:
        m.chat_id = chat_id
    return sb.save_history(0, chat_id, buyer, msgs, ME)


# ─── Текст ───────────────────────────────────────────────────────────────────

def test_similarity_basics(sb):
    assert sb.similarity("Есть в наличии?", "а в наличии есть ещё?") > 0.6
    assert sb.similarity("Сколько стоит реклама", "какая цена на рекламу") < 0.5
    assert sb.similarity("", "что-то") == 0


def test_secret_detection(sb):
    assert sb.looks_secret("Ваш ключ: ABCD-1234-EFGH-5678")
    assert sb.looks_secret("логин: vasya пароль: 123")
    assert sb.looks_secret("почта test@mail.ru")
    assert not sb.looks_secret("Здравствуйте! Да, в наличии, оплачивайте 🙂")


def test_generalize_username(sb):
    assert sb.generalize_answer("Привет, Petya! Да, есть", "Petya") == "Привет, $username! Да, есть"


# ─── Кто начал чат ───────────────────────────────────────────────────────────

def test_only_buyer_started_chats_count(sb):
    assert chat(sb, 1, "petya", [M(2, "Есть в наличии?"), M(ME, "Да, есть!")]) == "buyer"
    assert chat(sb, 2, "vasya", [M(ME, "Привет, есть предложение"), M(3, "Есть в наличии?"), M(ME, "Да")]) == "me"
    # системное «оплатил заказ» не считается тем, кто начал
    assert chat(sb, 3, "kolya", [M(0, "Покупатель оплатил", type_="ORDER_PURCHASED"), M(4, "Есть в наличии?"),
                                 M(ME, "Да, есть!")]) == "buyer"
    pairs = sb.all_pairs()
    assert {p.chat_id for p in pairs} == {"1", "3"}          # чат №2 (начал я) не учитывается


def test_full_history_page_is_unknown(sb):
    msgs = [M(2, "Привет")] + [M(ME if i % 2 else 2, f"сообщение {i}") for i in range(99)]
    assert chat(sb, 5, "x", msgs) == "unknown"               # видим только 100 последних — начало неизвестно


def test_bot_replies_are_not_mine(sb):
    chat(sb, 1, "petya", [M(2, "Есть в наличии?"), M(ME, "Автоответ: скоро отвечу", by_bot=True), M(ME, "Да, есть!")])
    pairs = sb.all_pairs()
    assert len(pairs) == 1 and pairs[0].answer == "Да, есть!"


def test_manual_telegram_reply_counts_as_mine(sb):
    sb.note_manual(0, 7, "Да, конечно есть")
    chat(sb, 7, "petya", [M(2, "Есть в наличии?"), M(ME, "Да, конечно есть", by_bot=True)])
    assert [p.answer for p in sb.all_pairs()] == ["Да, конечно есть"]


# ─── Анализ и база ───────────────────────────────────────────────────────────

def _seed(sb):
    chat(sb, 1, "petya", [M(2, "Здравствуйте, есть в наличии?"), M(ME, "Здравствуйте, Petya! Да, есть"),
                          M(0, "оплатил", type_="ORDER_PURCHASED")])
    chat(sb, 2, "vasya", [M(3, "в наличии есть?"), M(ME, "Здравствуйте! Да, есть")])
    chat(sb, 3, "kolya", [M(4, "Есть в наличии сейчас?"), M(ME, "Да, есть, оплачивайте")])
    chat(sb, 4, "misha", [M(5, "Как долго ждать выдачу?"), M(ME, "Выдача сразу после оплаты")])
    chat(sb, 5, "sasha", [M(6, "Какие сроки выдачи? долго ждать?"), M(ME, "Сразу после оплаты, автоматически")])
    chat(sb, 6, "dima", [M(7, "Привет, ау")])                 # без ответа
    chat(sb, 7, "olga", [M(ME, "Привет, купите"), M(8, "Есть в наличии?"), M(ME, "СЕКРЕТ")])  # начал я


def test_analyze_and_report(sb):
    _seed(sb)
    a = sb.analyze()
    assert a["chats"] == 6 and a["bought"] == 1 and len(a["silent"]) == 1 and a["ignored_me"] == 1
    assert a["clusters"][0].count == 3
    html = sb.report_html(a)
    assert "Купили после переписки: <b>1</b>" in html
    assert "СЕКРЕТ" not in html                               # чат, начатый мной, не попал в анализ


def test_rebuild_keeps_my_edits(sb):
    _seed(sb)
    added, updated = sb.rebuild_answers()
    assert added == 2 and updated == 0
    ans = sb.list_answers()
    stock = next(a for a in ans if a.uses == 3)
    assert "Да, есть" in stock.answer and "Petya" not in stock.answer
    sb.update_answer(stock.id, answer="Да, в наличии! Оплачивайте, выдам сразу", edited=True)
    added, updated = sb.rebuild_answers()
    assert added == 0 and updated == 2
    assert sb.get_answer(stock.id).answer == "Да, в наличии! Оплачивайте, выдам сразу"
    a, score = sb.best_answer("а наличие есть?")
    assert a.id == stock.id and score >= 0.45


# ─── Живые сообщения ─────────────────────────────────────────────────────────

class FakeClient:
    index = 0
    own_id = ME
    alias = "t"

    def __init__(self, history):
        self._recent_outgoing = {}
        self.history = history
        self.sent = []
        self.account = SimpleNamespace(id=ME, get_chat_history=lambda cid, interlocutor_username=None: list(self.history))


@pytest.fixture
def live(sb, monkeypatch):
    from core import funpay_client
    import modules.notifications as notif

    notes = []

    async def fake_send_all(text, markup=None, photo_url=None):
        notes.append(text)

    async def fake_send(acc, chat_id, text, buyer="", chat_name=""):
        holder["client"].sent.append((chat_id, text))
        return True

    holder = {}
    monkeypatch.setattr(notif, "_send_to_all", fake_send_all)
    monkeypatch.setattr(sb, "send", fake_send)
    monkeypatch.setattr(funpay_client.accounts_manager, "client_for", lambda m: holder["client"])

    def run(msg, history=None):
        holder.setdefault("client", FakeClient([]))
        if history is not None:
            holder["client"].history = history
        asyncio.run(_run(msg))
        return holder["client"], notes

    async def _run(msg):
        await sb.on_new_message(msg)
        await asyncio.gather(*list(sb._tasks))
    return run


def test_live_suggest(sb, live):
    _seed(sb)
    sb.rebuild_answers()
    sb.cfg().mode = "suggest"
    m = M(9, "Добрый день, есть в наличии?", chat_id=50, author="newbie")
    client, notes = live(m, history=[m])
    assert client.sent == [] and len(sb.pending) == 1
    assert "Подсказка" in notes[-1]


def test_live_auto_only_when_allowed(sb, live):
    _seed(sb)
    sb.rebuild_answers()
    stock = next(a for a in sb.list_answers() if a.uses == 3)
    sb.cfg().mode = "auto"
    m = M(9, "есть в наличии?", chat_id=51, author="newbie")
    client, _ = live(m, history=[m])
    assert client.sent == []                                  # «авто» у ответа не включено → только подсказка
    sb.update_answer(stock.id, auto=True)
    m2 = M(10, "а в наличии есть?", chat_id=52, author="newbie2")
    client, notes = live(m2, history=[m2])
    assert client.sent and client.sent[-1][0] == 52
    assert "Автопродавец ответил" in notes[-1]


def test_live_ignores_chats_started_by_me(sb, live):
    _seed(sb)
    sb.rebuild_answers()
    for a in sb.list_answers():
        sb.update_answer(a.id, auto=True)
    sb.cfg().mode = "auto"
    opener = M(ME, "Привет! Есть предложение", chat_id=60)
    m = M(9, "есть в наличии?", chat_id=60, author="x")
    client, notes = live(m, history=[opener, m])
    assert client.sent == [] and not sb.pending


def test_live_never_auto_sends_secrets(sb, live):
    aid = sb.add_answer(["есть в наличии?"], "Держи ключ ABCD-1234-EFGH-5678")
    sb.update_answer(aid, auto=True)
    sb.cfg().mode = "auto"
    m = M(9, "есть в наличии?", chat_id=70, author="x")
    client, _ = live(m, history=[m])
    assert client.sent == [] and len(sb.pending) == 1         # только подсказка


def test_live_silent_if_already_answered(sb, live, monkeypatch):
    aid = sb.add_answer(["есть в наличии?"], "Да, есть!")
    sb.update_answer(aid, auto=True)
    sb.cfg().mode = "auto"
    monkeypatch.setattr(sb, "_answered_since", lambda *a: True)
    m = M(9, "есть в наличии?", chat_id=80, author="x")
    client, _ = live(m, history=[m])
    assert client.sent == []


def test_live_handler_never_raises(sb, live, monkeypatch):
    monkeypatch.setattr(sb, "save_live", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    sb.cfg().mode = "auto"
    m = M(9, "есть?", chat_id=90)
    live(m, history=[m])                                      # не упало
