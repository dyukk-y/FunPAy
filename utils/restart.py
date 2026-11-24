"""
Безопасный перезапуск бота изнутри (после смены golden_key и т.п.).

Процесс заменяется самим собой (os.execv) — так же, как команда /restart.
Под сторожем из main.py это работает прозрачно; на хостинге тоже.
"""
from __future__ import annotations

import asyncio
import os
import sys

from utils.logger import logger


def _do_restart() -> None:
    try:
        from core.funpay_client import accounts_manager
        accounts_manager.stop_all()
    except Exception:
        pass
    logger.info("Перезапуск процесса бота...")
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        # execv не сработал — выходим с ошибкой, сторож поднимет бота заново
        logger.error(f"execv не удался ({e}) — выхожу, сторож перезапустит")
        os._exit(1)


def restart_soon(delay: float = 1.5) -> None:
    """Перезапустить бота через delay секунд (чтобы успели уйти сообщения в TG)."""
    try:
        asyncio.get_running_loop().call_later(delay, _do_restart)
    except RuntimeError:
        _do_restart()
