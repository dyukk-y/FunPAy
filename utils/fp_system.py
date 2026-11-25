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


def parse_paid(text: str) -> Optional[Tuple[str, str, str]]:
    """→ (лот, номер заказа, покупатель) или None, если это не сообщение об оплате."""
    m = _PAID_RX.search(text or "")
    if not m:
        return None
    lot = m.group("lot").strip().rstrip(".").strip()
    return lot, m.group("order"), m.group("buyer")
