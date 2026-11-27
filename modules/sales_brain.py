"""
🧠 Анализ переписки и автопродажи.

Что делает:
  • Запоминает переписку ТОЛЬКО в чатах, где первым написал покупатель
    (чаты, которые начал продавец, не учитываются вообще).
  • Отделяет сообщения покупателя, ваши ручные ответы (с сайта или через
    быстрый ответ в Telegram), ответы бота и системные сообщения FunPay.
  • Анализ: частые вопросы, ваш типичный ответ на каждый, конверсия
    «написал → купил», скорость ответа, часы активности, неотвеченные чаты.
  • База ответов: строится из ваших реальных ответов; можно править вручную.
  • Автопродавец: на новое сообщение покупателя ищет похожий вопрос в базе и
      - режим «Подсказки»: присылает вам готовый ответ с кнопкой «Отправить»;
      - режим «Авто»: отвечает сам (только ответами, где вы включили «авто»),
        выждав паузу — если вы успели ответить сами, бот промолчит.
  • ИИ (если настроен в «ИИ-помощнике»): подсказка в вашем стиле, когда
    в базе ответа нет, и советы по продажам на основе анализа.

Всё хранится локально в data/sales_brain.db. Никаких ошибок наружу:
каждый обработчик ловит исключения сам и пишет их в лог.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import sqlite3
import statistics
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config.settings import config_manager
from core.event_bus import Event, event_bus
from utils.logger import logger

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "sales_brain.db"

ROLE_BUYER, ROLE_ME, ROLE_BOT, ROLE_SYSTEM, ROLE_ORDER = "buyer", "me", "bot", "system", "order"
STARTED_BUYER, STARTED_ME, STARTED_UNKNOWN = "buyer", "me", "unknown"

HISTORY_PAGE = 100  # FunPay отдаёт до 100 последних сообщений чата

_db_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None


def cfg():
    return config_manager.settings.sales_brain


# ──────────────────────────────────────────────────────────────────────────────
# База данных
# ──────────────────────────────────────────────────────────────────────────────

def db() -> sqlite3.Connection:
    global _conn
    with _db_lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
            _conn.row_factory = sqlite3.Row
            _conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS chats (
                    acc INTEGER NOT NULL, chat_id TEXT NOT NULL,
                    buyer TEXT, started_by TEXT DEFAULT 'unknown',
                    first_ts REAL, last_ts REAL,
                    PRIMARY KEY (acc, chat_id)
                );
                CREATE TABLE IF NOT EXISTS messages (
                    acc INTEGER NOT NULL, chat_id TEXT NOT NULL, msg_id INTEGER NOT NULL,
                    ts REAL, role TEXT NOT NULL, text TEXT,
                    PRIMARY KEY (acc, chat_id, msg_id)
                );
                CREATE TABLE IF NOT EXISTS manual_sent (
                    acc INTEGER NOT NULL, chat_id TEXT NOT NULL, h TEXT NOT NULL, ts REAL,
                    PRIMARY KEY (acc, chat_id, h)
                );
                CREATE TABLE IF NOT EXISTS answers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    examples TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    uses INTEGER DEFAULT 0,
                    auto INTEGER DEFAULT 0,
                    edited INTEGER DEFAULT 0,
                    sent INTEGER DEFAULT 0,
                    created_ts REAL
                );
                CREATE TABLE IF NOT EXISTS auto_log (
                    acc INTEGER, chat_id TEXT, ts REAL, answer_id INTEGER
                );
            """)
        return _conn


def _exec(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _db_lock:
        c = db()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def _query(sql: str, args: tuple = ()) -> List[sqlite3.Row]:
    with _db_lock:
        return db().execute(sql, args).fetchall()


def reset_connection() -> None:
    """Для тестов: переоткрыть БД по новому DB_PATH."""
    global _conn
    with _db_lock:
        if _conn is not None:
            try:
                _conn.close()
            except Exception:
                pass
        _conn = None


# ──────────────────────────────────────────────────────────────────────────────
# Текст: нормализация и похожесть (без внешних библиотек)
# ──────────────────────────────────────────────────────────────────────────────

_STOP = set("""
и в во на а но я ты вы он она мы они у к ко с со о об от до по за из для при же ли бы
то это этот эта эти тот там тут здесь вот ну да нет не ни уже еще ещё как так что чтобы
мне меня мой моя мои вам вас ваш ваша ваши тебе тебя твой его ее её их нам нас наш
есть быть был была были будет было можно надо нужно очень просто только или либо
пожалуйста плиз pls please the a an is are to of and or in on for it you i
здравствуйте здравствуй здрасте привет приветствую добрый доброе доброй день вечер утро ночи
хай hello hi hey салам ку
сейчас щас подскажите скажите подскажи скажи интересует интересно хочу хотел хотела
вообще также тоже типа просто вопрос
""".split())
# Шум для сравнения вопросов — режем по тем же 5 буквам, что и стемминг
_STOP |= {w[:5] for w in list(_STOP) if len(w) > 5}


def tokens(text: str) -> frozenset:
    words = re.findall(r"[a-zа-я0-9]+", (text or "").lower().replace("ё", "е"))
    out = set()
    for w in words:
        if w in _STOP or (len(w) < 2 and not w.isdigit()):
            continue
        out.add(w[:5] if len(w) > 5 and not w.isdigit() else w)  # грубый «стемминг» для русского
    return frozenset(out)


def similarity(a, b) -> float:
    ta = a if isinstance(a, frozenset) else tokens(a)
    tb = b if isinstance(b, frozenset) else tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    if not inter:
        return 0.0
    jac = inter / len(ta | tb)
    cover = inter / min(len(ta), len(tb))
    return round(0.5 * jac + 0.5 * cover, 4)


def _h(text: str) -> str:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


_SECRET_RX = [
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),                         # e-mail
    re.compile(r"\b(?=[A-Za-z0-9-]*\d)(?=[A-Za-z0-9-]*[A-Za-z])[A-Za-z0-9]{4,}(?:-[A-Za-z0-9]{4,}){2,}\b"),  # ключи XXXX-XXXX-XXXX
    re.compile(r"\b(?=\w*\d)(?=\w*[A-Za-z])[A-Za-z0-9]{14,}\b"),        # длинные токены
    re.compile(r"(?i)(логин|login|пароль|password|pass)\s*[:=]"),       # логин: … / пароль: …
    re.compile(r"\b\S+:\S+@\S+\b"),                                     # user:pass@host
]


def looks_secret(text: str) -> bool:
    """Ответ похож на выдачу товара/ключа — такой нельзя слать другим автоматически."""
    return any(rx.search(text or "") for rx in _SECRET_RX)


def generalize_answer(answer: str, buyer: str) -> str:
    """Имя конкретного покупателя → $username (подставится имя нового покупателя)."""
    if buyer and len(buyer) >= 3:
        answer = re.sub(re.escape(buyer), "$username", answer, flags=re.IGNORECASE)
    return answer.strip()


# ──────────────────────────────────────────────────────────────────────────────
# Классификация сообщений
# ──────────────────────────────────────────────────────────────────────────────

def classify(msg, own_id: Optional[int], acc: int = 0) -> str:
    author_id = getattr(msg, "author_id", None)
    mtype = getattr(getattr(msg, "type", None), "name", "NON_SYSTEM")
    if (not mtype or mtype == "NON_SYSTEM") and author_id != own_id:
        from utils.fp_system import is_system_text, message_type_name, parse_paid
        _t = getattr(msg, "text", "") or ""
        if is_system_text(_t):
            # системное сообщение FunPay, пришедшее «от покупателя» (резервный режим чтения)
            mtype = "ORDER_PURCHASED" if parse_paid(_t) else (message_type_name(_t) if message_type_name(_t) != "NON_SYSTEM" else "SYSTEM")
            author_id = 0
    if author_id == 0 or (mtype and mtype != "NON_SYSTEM"):
        return ROLE_ORDER if mtype == "ORDER_PURCHASED" else ROLE_SYSTEM
    if getattr(msg, "is_employee", False) or getattr(msg, "is_support", False):
        return ROLE_SYSTEM
    if own_id and author_id == own_id:
        if getattr(msg, "is_autoreply", False):
            return ROLE_BOT
        if getattr(msg, "by_bot", False) or getattr(msg, "by_vertex", False):
            # Через бота уходят и ручные ответы из Telegram — их мы запомнили
            if _is_manual(acc, getattr(msg, "chat_id", ""), getattr(msg, "text", "") or ""):
                return ROLE_ME
            return ROLE_BOT
        return ROLE_ME
    return ROLE_BUYER


def note_manual(acc: int, chat_id, text: str) -> None:
    """Вызывается, когда продавец сам отправил сообщение из Telegram."""
    try:
        _exec("INSERT OR REPLACE INTO manual_sent VALUES (?,?,?,?)", (acc, str(chat_id), _h(text), time.time()))
    except Exception as e:
        logger.debug(f"sales_brain.note_manual: {e}")


def _is_manual(acc: int, chat_id, text: str) -> bool:
    try:
        return bool(_query("SELECT 1 FROM manual_sent WHERE acc=? AND chat_id=? AND h=?",
                           (acc, str(chat_id), _h(text))))
    except Exception:
        return False


def _starter(roles: List[str]) -> str:
    for r in roles:
        if r == ROLE_BUYER:
            return STARTED_BUYER
        if r in (ROLE_ME, ROLE_BOT):
            return STARTED_ME
    return STARTED_UNKNOWN


def save_history(acc: int, chat_id, buyer: str, messages: list, own_id: Optional[int]) -> str:
    """Сохраняет историю чата и определяет, кто начал. → started_by."""
    rows = []
    for m in messages:
        mid = getattr(m, "id", None) or 0
        if not mid:
            continue
        rows.append((acc, str(chat_id), int(mid), None, classify(m, own_id, acc), (getattr(m, "text", "") or "")[:2000]))
    rows.sort(key=lambda r: r[2])
    started = _starter([r[4] for r in rows])
    if len(rows) >= HISTORY_PAGE and started == STARTED_BUYER:
        # Видим только последние 100 сообщений — начало чата могло быть раньше
        started = STARTED_UNKNOWN
    with _db_lock:
        c = db()
        c.executemany("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)", rows)
        c.execute("""INSERT INTO chats (acc, chat_id, buyer, started_by, first_ts, last_ts) VALUES (?,?,?,?,?,?)
                     ON CONFLICT(acc, chat_id) DO UPDATE SET buyer=excluded.buyer,
                       started_by=CASE WHEN chats.started_by='unknown' THEN excluded.started_by ELSE chats.started_by END""",
                  (acc, str(chat_id), buyer, started, None, None))
        c.commit()
    return _query("SELECT started_by FROM chats WHERE acc=? AND chat_id=?", (acc, str(chat_id)))[0]["started_by"]


def chat_started_by(acc: int, chat_id) -> Optional[str]:
    r = _query("SELECT started_by FROM chats WHERE acc=? AND chat_id=?", (acc, str(chat_id)))
    return r[0]["started_by"] if r else None


def save_live(acc: int, msg, own_id: Optional[int]) -> Tuple[str, int]:
    """Сохраняет одно живое сообщение. → (роль, msg_id)."""
    role = classify(msg, own_id, acc)
    now = time.time()
    mid = int(getattr(msg, "id", 0) or 0) or -int(now * 1000)
    chat_id = str(getattr(msg, "chat_id", ""))
    with _db_lock:
        c = db()
        # Если сообщение уже есть (пришло вместе с историей чата) — дописываем время
        c.execute("""INSERT INTO messages VALUES (?,?,?,?,?,?)
                     ON CONFLICT(acc, chat_id, msg_id) DO UPDATE SET ts=COALESCE(messages.ts, excluded.ts)""",
                  (acc, chat_id, mid, now, role, (getattr(msg, "text", "") or "")[:2000]))
        c.execute("UPDATE chats SET last_ts=?, first_ts=COALESCE(first_ts, ?) WHERE acc=? AND chat_id=?",
                  (now, now, acc, chat_id))
        c.commit()
    return role, mid


def last_outgoing_ts(acc: int, chat_id, roles=(ROLE_ME, ROLE_BOT)) -> float:
    q = f"SELECT MAX(ts) AS t FROM messages WHERE acc=? AND chat_id=? AND role IN ({','.join('?' * len(roles))})"
    r = _query(q, (acc, str(chat_id), *roles))
    return float(r[0]["t"] or 0) if r else 0.0


# ──────────────────────────────────────────────────────────────────────────────
# Анализ
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Pair:
    question: str
    answer: str
    buyer: str
    chat_id: str
    acc: int
    response_sec: Optional[float] = None


def _chat_messages(acc: int, chat_id: str) -> List[sqlite3.Row]:
    return _query("""SELECT * FROM messages WHERE acc=? AND chat_id=?
                     ORDER BY CASE WHEN msg_id > 0 THEN msg_id ELSE 9e18 END, ts""", (acc, chat_id))


def extract_pairs(rows, buyer: str = "", acc: int = 0, chat_id: str = "") -> List[Pair]:
    """Вопрос покупателя (последние до 3 сообщений подряд) → ваш ответ."""
    pairs: List[Pair] = []
    q: List[Tuple[str, Optional[float]]] = []
    for r in rows:
        role, text, ts = r["role"], (r["text"] or "").strip(), r["ts"]
        if role == ROLE_BUYER and text:
            q.append((text, ts))
        elif role == ROLE_ME and text:
            if q:
                question = " ".join(t for t, _ in q[-3:])
                rs = (ts - q[0][1]) if (ts and q[0][1]) else None
                pairs.append(Pair(question, text, buyer, chat_id, acc, rs))
                q = []
            elif pairs and pairs[-1].chat_id == chat_id and pairs[-1].answer and len(pairs[-1].answer) < 1500:
                pairs[-1].answer += "\n" + text  # ответ в несколько сообщений подряд
        elif role == ROLE_BOT and q:
            continue  # бот ответил — ваш ручной ответ может прийти позже, вопрос ещё «открыт»
    return pairs


def buyer_chats() -> List[sqlite3.Row]:
    return _query("SELECT * FROM chats WHERE started_by=?", (STARTED_BUYER,))


def all_pairs() -> List[Pair]:
    out = []
    for ch in buyer_chats():
        out.extend(extract_pairs(_chat_messages(ch["acc"], ch["chat_id"]), ch["buyer"] or "", ch["acc"], ch["chat_id"]))
    return out


@dataclass
class Cluster:
    tok: frozenset
    questions: List[str] = field(default_factory=list)
    answers: List[Tuple[str, str]] = field(default_factory=list)  # (ответ, покупатель)

    @property
    def count(self) -> int:
        return len(self.questions)

    def typical_answer(self) -> Tuple[str, str]:
        """Медоида: ответ, больше всего похожий на остальные (ваш «типичный»)."""
        if len(self.answers) <= 2:
            return self.answers[-1]
        best, best_score = self.answers[-1], -1.0
        toks = [tokens(a) for a, _ in self.answers]
        for i, (a, b) in enumerate(self.answers):
            sc = sum(similarity(toks[i], t) for j, t in enumerate(toks) if j != i)
            if sc > best_score or (sc == best_score and i > self.answers.index(best)):
                best, best_score = (a, b), sc
        return best


def cluster_pairs(pairs: List[Pair], threshold: float = 0.5) -> List[Cluster]:
    clusters: List[Cluster] = []
    for p in pairs:
        t = tokens(p.question)
        if not t:
            continue
        best, best_s = None, 0.0
        for c in clusters:
            s = similarity(t, c.tok)
            if s > best_s:
                best, best_s = c, s
        if best is not None and best_s >= threshold:
            best.questions.append(p.question)
            best.answers.append((p.answer, p.buyer))
            if len(best.questions) <= 5:
                best.tok = best.tok | t if len(best.tok) < 12 else best.tok
        else:
            clusters.append(Cluster(t, [p.question], [(p.answer, p.buyer)]))
    clusters.sort(key=lambda c: c.count, reverse=True)
    return clusters


def _ordered_chats() -> set:
    return {(r["acc"], r["chat_id"]) for r in _query("SELECT DISTINCT acc, chat_id FROM messages WHERE role=?", (ROLE_ORDER,))}


def _fmt_dur(sec: float) -> str:
    if sec < 90:
        return f"{int(sec)} сек"
    if sec < 5400:
        return f"{int(sec // 60)} мин"
    return f"{sec / 3600:.1f} ч"


def analyze() -> Dict:
    chats = buyer_chats()
    ordered = _ordered_chats()
    pairs = all_pairs()
    answered_chats = {(p.acc, p.chat_id) for p in pairs}
    n = len(chats)
    keys = {(c["acc"], c["chat_id"]) for c in chats}
    bought = len(keys & ordered)
    resp = [p.response_sec for p in pairs if p.response_sec is not None and p.response_sec >= 0]
    hours = Counter()
    for r in _query("""SELECT m.ts FROM messages m JOIN chats c ON c.acc=m.acc AND c.chat_id=m.chat_id
                       WHERE c.started_by=? AND m.role=? AND m.ts IS NOT NULL""", (STARTED_BUYER, ROLE_BUYER)):
        hours[time.localtime(r["ts"]).tm_hour] += 1
    silent = []
    for c in chats:
        rows = _chat_messages(c["acc"], c["chat_id"])
        roles = [r["role"] for r in rows]
        if ROLE_BUYER in roles and ROLE_ME not in roles and ROLE_BOT not in roles:
            silent.append(c["buyer"] or c["chat_id"])
    clusters = cluster_pairs(pairs)
    return {
        "chats": n, "answered": len(answered_chats & keys), "bought": bought,
        "pairs": len(pairs), "resp_median": statistics.median(resp) if resp else None,
        "resp_n": len(resp), "hours": hours.most_common(3), "silent": silent,
        "clusters": clusters, "ignored_me": len(_query("SELECT 1 FROM chats WHERE started_by=?", (STARTED_ME,))),
        "unknown": len(_query("SELECT 1 FROM chats WHERE started_by=?", (STARTED_UNKNOWN,))),
    }


def report_html(a: Optional[Dict] = None) -> str:
    import html as H
    a = a or analyze()
    if not a["chats"]:
        return ("📊 <b>Анализ переписки</b>\n\nПока нет данных. Нажмите «📥 Изучить переписку» — "
                "бот прочитает последние чаты. Новые чаты он запоминает сам.")
    conv = a["bought"] / a["chats"] * 100
    lines = [
        "📊 <b>Анализ переписки</b> (только чаты, где покупатель написал первым)",
        "",
        f"💬 Чатов: <b>{a['chats']}</b> · вы ответили в <b>{a['answered']}</b>",
        f"🛒 Купили после переписки: <b>{a['bought']}</b> ({conv:.0f}%)",
    ]
    if a["resp_median"] is not None:
        lines.append(f"⏱ Ваш типичный ответ через: <b>{_fmt_dur(a['resp_median'])}</b> (по {a['resp_n']} ответам)")
    if a["hours"]:
        lines.append("🕐 Чаще пишут в: " + ", ".join(f"{h:02d}:00" for h, _ in a["hours"]))
    if a["silent"]:
        lines.append(f"🙈 Без ответа осталось чатов: <b>{len(a['silent'])}</b> — это потерянные продажи")
    lines.append(f"<i>Не учтено: начатых вами — {a['ignored_me']}, неясно кто начал — {a['unknown']}.</i>")
    top = [c for c in a["clusters"] if c.count >= 2][:8] or a["clusters"][:5]
    if top:
        lines += ["", "❓ <b>Частые вопросы и ваш типичный ответ:</b>"]
        for i, c in enumerate(top, 1):
            ans, buyer = c.typical_answer()
            ans = generalize_answer(ans, buyer)
            lines.append(f"\n{i}. «{H.escape(c.questions[-1][:90])}» — <b>×{c.count}</b>\n"
                         f"   ↳ {H.escape(ans[:160])}{'…' if len(ans) > 160 else ''}")
    text = "\n".join(lines)
    return text if len(text) < 4000 else text[:3990] + "…"


# ──────────────────────────────────────────────────────────────────────────────
# База ответов
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Answer:
    id: int
    examples: List[str]
    answer: str
    uses: int
    auto: bool
    edited: bool
    sent: int

    @property
    def risky(self) -> bool:
        return looks_secret(self.answer)

    def score(self, text_tok: frozenset) -> float:
        return max((similarity(text_tok, tokens(e)) for e in self.examples), default=0.0)


def _row_to_answer(r) -> Answer:
    return Answer(r["id"], json.loads(r["examples"]), r["answer"], r["uses"], bool(r["auto"]),
                  bool(r["edited"]), r["sent"])


def list_answers() -> List[Answer]:
    return [_row_to_answer(r) for r in _query("SELECT * FROM answers ORDER BY uses DESC, id")]


def get_answer(aid: int) -> Optional[Answer]:
    r = _query("SELECT * FROM answers WHERE id=?", (aid,))
    return _row_to_answer(r[0]) if r else None


def add_answer(examples: List[str], answer: str, uses: int = 0, edited: bool = True) -> int:
    cur = _exec("INSERT INTO answers (examples, answer, uses, auto, edited, sent, created_ts) VALUES (?,?,?,?,?,?,?)",
                (json.dumps(examples[:8], ensure_ascii=False), answer, uses, 0, int(edited), 0, time.time()))
    return int(cur.lastrowid)


def update_answer(aid: int, **kw) -> None:
    allowed = {"answer", "auto", "edited", "uses", "sent", "examples"}
    sets = []
    vals = []
    for k, v in kw.items():
        if k not in allowed:
            continue
        if k == "examples":
            v = json.dumps(v[:8], ensure_ascii=False)
        if isinstance(v, bool):
            v = int(v)
        sets.append(f"{k}=?")
        vals.append(v)
    if sets:
        _exec(f"UPDATE answers SET {', '.join(sets)} WHERE id=?", (*vals, aid))


def delete_answer(aid: int) -> None:
    _exec("DELETE FROM answers WHERE id=?", (aid,))


def rebuild_answers() -> Tuple[int, int]:
    """
    Обновляет базу из переписки. Ответы, которые вы правили вручную, не трогаются
    (только счётчик и примеры вопросов). → (добавлено, обновлено).
    """
    clusters = [c for c in cluster_pairs(all_pairs()) if c.count >= max(1, cfg().min_cluster)]
    existing = list_answers()
    added = updated = 0
    for c in clusters:
        ans, buyer = c.typical_answer()
        ans = generalize_answer(ans, buyer)
        examples = list(dict.fromkeys(reversed(c.questions)))[:8]
        match, best = None, 0.0
        for e in existing:
            s = max((similarity(c.tok, tokens(x)) for x in e.examples), default=0.0)
            if s > best:
                match, best = e, s
        if match is not None and best >= 0.5:
            fields = {"uses": max(match.uses, c.count),
                      "examples": list(dict.fromkeys(examples + match.examples))[:8]}
            if not match.edited:
                fields["answer"] = ans
            update_answer(match.id, **fields)
            updated += 1
        else:
            add_answer(examples, ans, uses=c.count, edited=False)
            added += 1
    return added, updated


def best_answer(text: str) -> Tuple[Optional[Answer], float]:
    t = tokens(text)
    if not t:
        return None, 0.0
    best, best_s = None, 0.0
    for a in list_answers():
        s = a.score(t)
        if s > best_s:
            best, best_s = a, s
    return best, best_s


# ──────────────────────────────────────────────────────────────────────────────
# Изучение истории
# ──────────────────────────────────────────────────────────────────────────────

_import_lock = asyncio.Lock()


def importing() -> bool:
    return _import_lock.locked()


async def import_history(progress=None) -> str:
    """Читает последние чаты с FunPay. Ошибки по отдельным чатам не прерывают импорт."""
    from core.funpay_client import accounts_manager
    if _import_lock.locked():
        return "⏳ Изучение уже идёт."
    async with _import_lock:
        clients = accounts_manager.connected_clients()
        if not clients:
            return "❌ FunPay не подключён. Обновите golden_key."
        loop = asyncio.get_running_loop()
        stats = Counter()
        limit = max(10, cfg().history_chats_limit)
        for cl in clients:
            try:
                chats = await loop.run_in_executor(None, cl.account.request_chats)
            except Exception as e:
                stats["errors"] += 1
                logger.warning(f"sales_brain: список чатов [{cl.alias}]: {e}")
                continue
            chats = list(chats)[:limit]
            fails_in_row = 0
            for i, ch in enumerate(chats, 1):
                if progress and i % 5 == 1:
                    await progress(f"📥 Читаю чаты [{cl.alias}]: {i}/{len(chats)}…")
                try:
                    hist = await loop.run_in_executor(
                        None, lambda: cl.account.get_chat_history(ch.id, interlocutor_username=ch.name))
                    started = save_history(cl.index, ch.id, ch.name or "", hist, cl.account.id)
                    stats[started] += 1
                    fails_in_row = 0
                except Exception as e:
                    stats["errors"] += 1
                    fails_in_row += 1
                    logger.debug(f"sales_brain: история {ch.id}: {e}")
                    if fails_in_row >= 5:
                        stats["aborted"] += 1
                        break
                await asyncio.sleep(random.uniform(1.0, 2.0))
        added, updated = rebuild_answers()
        cfg().last_import = time.strftime("%d.%m.%Y %H:%M")
        config_manager.save()
        res = (f"✅ Изучено чатов: покупатель писал первым — <b>{stats[STARTED_BUYER]}</b>, "
               f"начали вы — {stats[STARTED_ME]} (не учитываются), неясно — {stats[STARTED_UNKNOWN]}.\n"
               f"📚 База ответов: +{added} новых, обновлено {updated}.")
        if stats["errors"]:
            res += f"\n⚠️ Не прочитано чатов: {stats['errors']}" + (" (остановился: много ошибок подряд)" if stats["aborted"] else "")
        return res


# ──────────────────────────────────────────────────────────────────────────────
# Живые сообщения → подсказка / автоответ
# ──────────────────────────────────────────────────────────────────────────────

# Подсказки, ждущие нажатия «Отправить»: pid → данные (живут сутки)
pending: Dict[str, Dict] = {}
_history_sem = asyncio.Semaphore(1)
_tasks: set = set()


def _spawn(coro) -> None:
    t = asyncio.create_task(coro)
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


def _new_pid() -> str:
    now = time.time()
    for k in [k for k, v in pending.items() if now - v["ts"] > 86400]:
        pending.pop(k, None)
    return f"{int(now * 1000) % 10 ** 10:x}{random.randint(0, 255):02x}"


async def _ensure_chat_known(client, msg) -> Optional[str]:
    acc, chat_id = client.index, getattr(msg, "chat_id", None)
    started = chat_started_by(acc, chat_id)
    if started is not None:
        return started
    loop = asyncio.get_running_loop()
    async with _history_sem:
        started = chat_started_by(acc, chat_id)
        if started is not None:
            return started
        try:
            hist = await loop.run_in_executor(
                None, lambda: client.account.get_chat_history(chat_id, interlocutor_username=getattr(msg, "chat_name", None)))
        except Exception as e:
            logger.debug(f"sales_brain: история нового чата {chat_id}: {e}")
            hist = [msg]  # видим только это сообщение
        return save_history(acc, chat_id, getattr(msg, "chat_name", "") or getattr(msg, "author", "") or "",
                            hist, client.own_id)


def _answered_since(client, acc: int, chat_id, since: float) -> bool:
    """Кто-то (вы, бот, другой модуль) уже написал в чат после сообщения покупателя?"""
    if last_outgoing_ts(acc, chat_id) > since:
        return True
    try:
        bucket = getattr(client, "_recent_outgoing", {}).get(int(chat_id), [])
        return any(ts > since for _, ts in bucket)
    except Exception:
        return False


def _auto_count_today(acc: int, chat_id) -> int:
    r = _query("SELECT COUNT(*) AS n FROM auto_log WHERE acc=? AND chat_id=? AND ts>?",
               (acc, str(chat_id), time.time() - 86400))
    return int(r[0]["n"]) if r else 0


@event_bus.on(Event.NEW_MESSAGE)
async def on_new_message(message) -> None:
    try:
        await _on_new_message(message)
    except Exception as e:
        logger.exception(f"sales_brain: {e}")


async def _on_new_message(message) -> None:
    c = cfg()
    if not c.collect and c.mode == "off":
        return
    from core.funpay_client import accounts_manager
    client = accounts_manager.client_for(message)
    if client.account is None or not getattr(message, "chat_id", None):
        return
    started = await _ensure_chat_known(client, message)
    role, _ = save_live(client.index, message, client.own_id)
    if role != ROLE_BUYER or started != STARTED_BUYER or c.mode == "off":
        return
    text = (getattr(message, "text", "") or "").strip()
    if not text:
        return
    buyer = getattr(message, "author", "") or getattr(message, "chat_name", "") or ""
    bl = config_manager.settings.blacklist
    if bl.enabled and buyer in bl.nicknames:
        return
    _spawn(_react(client, message, text, buyer))


async def _react(client, message, text: str, buyer: str) -> None:
    try:
        c = cfg()
        acc, chat_id = client.index, getattr(message, "chat_id")
        chat_name = getattr(message, "chat_name", "") or buyer
        msg_ts = time.time()
        ans, score = best_answer(text)
        can_auto = (c.mode == "auto" and ans is not None and ans.auto and not ans.risky
                    and score >= c.auto_threshold
                    and _auto_count_today(acc, chat_id) < c.max_auto_per_chat
                    and time.time() - last_outgoing_ts(acc, chat_id, (ROLE_ME,)) > c.skip_if_active_min * 60)
        if can_auto:
            await asyncio.sleep(random.uniform(c.auto_delay_min, max(c.auto_delay_min, c.auto_delay_max)))
            if _answered_since(client, acc, chat_id, msg_ts):
                logger.info(f"sales_brain: в чате {chat_id} уже ответили — автоответ не нужен")
                return
            ok = await send(acc, chat_id, ans.answer, buyer, chat_name)
            if ok:
                _exec("INSERT INTO auto_log VALUES (?,?,?,?)", (acc, str(chat_id), time.time(), ans.id))
                update_answer(ans.id, sent=ans.sent + 1)
                await _notify(acc, chat_id, chat_name,
                              f"🤖 <b>Автопродавец ответил</b> {_esc(buyer)} ({int(score * 100)}%)\n"
                              f"❓ {_esc(text[:300])}\n💬 {_esc(ans.answer[:500])}", None)
            return

        if ans is not None and score >= c.suggest_threshold:
            await _suggest(acc, chat_id, chat_name, buyer, text, ans.answer, f"из базы, {int(score * 100)}%", ans.id)
            return
        if c.ai_suggest:
            draft = await ai_draft(text, buyer)
            if draft:
                if c.ai_auto and c.mode == "auto" and not looks_secret(draft) \
                        and _auto_count_today(acc, chat_id) < c.max_auto_per_chat:
                    await asyncio.sleep(random.uniform(c.auto_delay_min, max(c.auto_delay_min, c.auto_delay_max)))
                    if _answered_since(client, acc, chat_id, msg_ts):
                        return
                    if await send(acc, chat_id, draft, buyer, chat_name):
                        _exec("INSERT INTO auto_log VALUES (?,?,?,?)", (acc, str(chat_id), time.time(), 0))
                        await _notify(acc, chat_id, chat_name,
                                      f"🧠 <b>ИИ ответил</b> {_esc(buyer)}\n❓ {_esc(text[:300])}\n💬 {_esc(draft[:500])}", None)
                    return
                await _suggest(acc, chat_id, chat_name, buyer, text, draft, "ИИ в вашем стиле", 0)
    except Exception as e:
        logger.exception(f"sales_brain react: {e}")


def _esc(s: str) -> str:
    import html as H
    return H.escape(s or "")


async def _suggest(acc, chat_id, chat_name, buyer, question, answer, source, answer_id) -> None:
    pid = _new_pid()
    pending[pid] = {"acc": acc, "chat_id": str(chat_id), "chat_name": chat_name, "buyer": buyer,
                    "answer": answer, "answer_id": answer_id, "question": question, "ts": time.time()}
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data=f"sb:go:{pid}"),
         InlineKeyboardButton(text="✏️ Изменить", callback_data=f"sb:edit:{pid}")],
        [InlineKeyboardButton(text="📨 Ответить своим текстом", callback_data=f"qr:reply:{chat_id}@{acc}"),
         InlineKeyboardButton(text="🙈 Не надо", callback_data=f"sb:skip:{pid}")],
    ])
    await _notify(acc, chat_id, chat_name,
                  f"💡 <b>Подсказка</b> для {_esc(buyer)} ({source})\n"
                  f"❓ {_esc(question[:300])}\n\n💬 <code>{_esc(answer[:1500])}</code>", kb)


async def _notify(acc, chat_id, chat_name, text, kb) -> None:
    from modules.notifications import _send_to_all, kb_message_actions
    await _send_to_all(text, kb or kb_message_actions(chat_id, chat_name, acc_index=acc))


async def send(acc: int, chat_id, text: str, buyer: str = "", chat_name: str = "") -> bool:
    """Отправка в FunPay с подстановкой $username и т.п."""
    from core.funpay_client import accounts_manager
    from utils.variables import send_with_vars
    client = accounts_manager.get(acc) or accounts_manager.active()
    if client.account is None:
        return False
    try:
        return bool(await send_with_vars(text, client.send_message, chat_id, account=client.account,
                                         username=buyer, chat_name=chat_name or buyer))
    except Exception as e:
        logger.warning(f"sales_brain send: {e}")
        return False


# ──────────────────────────────────────────────────────────────────────────────
# ИИ
# ──────────────────────────────────────────────────────────────────────────────

def _similar_pairs(text: str, k: int = 8) -> List[Pair]:
    t = tokens(text)
    pairs = [p for p in all_pairs() if not looks_secret(p.answer)]
    pairs.sort(key=lambda p: similarity(t, tokens(p.question)), reverse=True)
    best = pairs[:k]
    if len(best) < k:
        best += [p for p in pairs[k:k * 2] if p not in best][: k - len(best)]
    return best


async def ai_draft(text: str, buyer: str) -> Optional[str]:
    from modules.ai_assistant import call_llm
    ai = config_manager.settings.ai
    if not ai.enabled or not ai.api_key:
        return None
    examples = _similar_pairs(text)
    shots = "\n\n".join(f"Покупатель: {p.question[:300]}\nЯ: {generalize_answer(p.answer, p.buyer)[:400]}"
                        for p in examples)
    system = (
        "Ты отвечаешь покупателю на FunPay от имени продавца, в ЕГО стиле (длина, тон, эмодзи, обращение — как в примерах). "
        "Цель — помочь и довести до покупки. Правила: не выдумывай факты, цены, сроки и наличие, которых нет в примерах; "
        "не выдавай товар, ключи, логины и пароли; не обещай скидок; не уводи с FunPay. "
        "Если не уверен — коротко скажи, что уточнишь и ответишь. Ответь ОДНИМ сообщением без пояснений."
    )
    user = (f"Примеры моих прошлых ответов:\n\n{shots or '(примеров пока нет)'}\n\n"
            f"Новое сообщение от покупателя {buyer}:\n{text[:800]}\n\nМой ответ:")
    try:
        res = await call_llm(system, user)
    except Exception as e:
        logger.warning(f"sales_brain ai_draft: {e}")
        return None
    res = (res or "").strip().strip('"').strip()
    return res[:1500] or None


async def ai_advice() -> str:
    from modules.ai_assistant import call_llm
    ai = config_manager.settings.ai
    if not ai.enabled or not ai.api_key:
        return "❌ ИИ не настроен: /menu → 🤖 Автоматизация → 🧠 ИИ-помощник (ключ + включить)."
    a = analyze()
    if not a["chats"]:
        return "Пока нет данных — сначала «📥 Изучить переписку»."
    import html as H
    clusters = "\n".join(
        f"- ×{c.count} «{c.questions[-1][:120]}» → мой типичный ответ: «{generalize_answer(*c.typical_answer())[:200]}»"
        for c in a["clusters"][:15])
    data = (f"Чатов, где покупатель написал первым: {a['chats']}; я ответил в {a['answered']}; "
            f"купили после переписки: {a['bought']}; без ответа: {len(a['silent'])}; "
            f"медианное время моего ответа: {_fmt_dur(a['resp_median']) if a['resp_median'] else 'нет данных'}; "
            f"пик сообщений: {', '.join(f'{h}:00' for h, _ in a['hours']) or 'нет данных'}.\n"
            f"Частые вопросы:\n{clusters}")
    system = ("Ты — опытный консультант по продажам на маркетплейсе FunPay. Дай продавцу конкретные советы, "
              "как продавать больше через переписку: что улучшить в ответах (с примерами готовых формулировок "
              "в его стиле), какие вопросы закрыть заранее в описании лота, что автоматизировать. "
              "Кратко, по пунктам, на русском, до 15 пунктов. Без воды.")
    res = await call_llm(system, data)
    return f"🧠 <b>Советы ИИ по продажам</b>\n\n{H.escape(res.strip())}" if res else "❌ ИИ не ответил — проверьте ключ."


def stats_line() -> str:
    try:
        n = _query("SELECT COUNT(*) AS n FROM chats WHERE started_by=?", (STARTED_BUYER,))[0]["n"]
        m = _query("""SELECT COUNT(*) AS n FROM messages m JOIN chats c ON c.acc=m.acc AND c.chat_id=m.chat_id
                      WHERE c.started_by=?""", (STARTED_BUYER,))[0]["n"]
        return f"{n} чатов, {m} сообщений"
    except Exception:
        return "нет данных"
