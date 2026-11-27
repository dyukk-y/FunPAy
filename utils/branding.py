"""
Брендинг бота: «Help Funpay by yasha».

Основано на PaltoFunPayBot (© PaltoProjects, https://github.com/PaltoProjects/PaltoFunPayBot) —
по условиям лицензии оригинальный автор указывается в /about и README.
"""
from __future__ import annotations

BOT_NAME = "Help Funpay by yasha"
SHORT_DESCRIPTION = "Помощник продавца FunPay: реклама в канал, автоответы, автоподнятие, онлайн 24/7."
DESCRIPTION = (
    "Help Funpay by yasha — личный помощник продавца FunPay.\n\n"
    "• публикует оплаченную рекламу в канал прямо из чата FunPay\n"
    "• отвечает покупателям так, как отвечаете вы (учится на ваших ответах)\n"
    "• держит аккаунт онлайн и поднимает лоты\n"
    "• присылает заказы и сообщения в Telegram"
)
ORIGINAL_CREDIT = 'основано на <a href="https://github.com/PaltoProjects/PaltoFunPayBot">PaltoFunPayBot</a> © PaltoProjects'


async def apply(bot) -> None:
    """Имя и описание бота в Telegram (меняет только если отличаются — у Telegram жёсткие лимиты)."""
    from utils.logger import logger
    try:
        cur = await bot.get_my_name()
        if getattr(cur, "name", "") != BOT_NAME:
            await bot.set_my_name(name=BOT_NAME)
    except Exception as e:
        logger.debug(f"branding name: {e}")
    try:
        cur = await bot.get_my_short_description()
        if getattr(cur, "short_description", "") != SHORT_DESCRIPTION:
            await bot.set_my_short_description(short_description=SHORT_DESCRIPTION)
    except Exception as e:
        logger.debug(f"branding short: {e}")
    try:
        cur = await bot.get_my_description()
        if getattr(cur, "description", "") != DESCRIPTION:
            await bot.set_my_description(description=DESCRIPTION)
    except Exception as e:
        logger.debug(f"branding desc: {e}")
