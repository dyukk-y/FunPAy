"""
Разбор системных сообщений FunPay.

«Покупатель Vasya оплатил заказ #ABCD1234. Telegram, Реклама, Пост в канале.
 Vasya, не забудьте потом нажать кнопку «Подтвердить выполнение заказа».»
→ лот «Telegram, Реклама, Пост в канале», заказ ABCD1234, покупатель Vasya.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

_PAID_RX = re.compile(
    r"(?:Покупатель|The buyer|Покупець)\s+(?P<buyer>\S+)\s+"
    r"(?:оплатил заказ|has paid for order|сплатив замовлення)\s+#(?P<order>[A-Z0-9]{8})\.\s*"
    r"(?P<lot>.*?)\s*"
    r"(?:(?P=buyer),\s*(?:не забудьте|do not forget|не забудьте|не забудьте)|\Z)",
    re.S,
)


_PAID_HEAD_RX = re.compile(
    r"(?:Покупатель|The buyer|Покупець)\s+(?P<buyer>\S+)\s+"
    r"(?:оплатил заказ|has paid for order|сплатив замовлення)\s+#(?P<order>[A-Za-z0-9]{8})\.?\s*",
    re.I,
)
_PAID_TAIL_RX = re.compile(r"\s*\S+,\s*(?:не забудьте|do not forget|не забудьте|не забудьте)\b.*\Z", re.S | re.I)


def parse_paid(text: str) -> Optional[Tuple[str, str, str]]:
    """→ (лот, номер заказа, покупатель) или None, если это не сообщение об оплате."""
    text = (text or "").replace("\xa0", " ")
    m = _PAID_RX.search(text)
    if m:
        lot = _PAID_TAIL_RX.sub("", m.group("lot")).strip().rstrip(".").strip()
        return lot, m.group("order"), m.group("buyer")
    # Мягкий разбор: имя в концовке может отличаться/отсутствовать
    h = _PAID_HEAD_RX.search(text)
    if not h:
        return None
    rest = _PAID_TAIL_RX.sub("", text[h.end():]).strip().rstrip(".").strip()
    return rest, h.group("order").upper(), h.group("buyer")


def message_type_name(text: str) -> str:
    """Тип сообщения FunPay по тексту (как FunPayAPI): NON_SYSTEM, ORDER_PURCHASED, NEW_FEEDBACK…"""
    try:
        from types import SimpleNamespace
        from FunPayAPI.types import Message
        return Message.get_message_type(SimpleNamespace(text=text)).name
    except Exception:
        return "NON_SYSTEM"


def is_system_text(text: str) -> bool:
    """Текст — системное сообщение FunPay (оплата, подтверждение, отзыв, возврат…)."""
    if not text:
        return False
    if parse_paid(text) or parse_confirmed(text):
        return True
    return message_type_name(text) not in ("NON_SYSTEM", "DISCORD", "DEAR_VENDORS")


_CONFIRMED_RX = re.compile(
    r"(?:Покупатель|The buyer|Покупець|Администратор|The administrator)\s+(?P<buyer>\S+)\s+"
    r"(?:подтвердил успешное выполнение заказа|has confirmed that order|підтвердив успішне виконання замовлення)\s+"
    r"#(?P<order>[A-Z0-9]{8})"
)


def parse_confirmed(text: str) -> Optional[Tuple[str, str]]:
    """«Покупатель X подтвердил успешное выполнение заказа #ID …» → (номер заказа, покупатель)."""
    m = _CONFIRMED_RX.search(text or "")
    if not m:
        return None
    return m.group("order"), m.group("buyer")
