"""
Постоянный онлайн-статус.

FunPayAPI поддерживает онлайн автоматически, пока работает Runner.get_updates()
(он шлёт хартбит). Этот модуль просто следит, чтобы поллинг был активен,
и логирует статус.
"""
from __future__ import annotations

from typing import Optional

import asyncio

from config.settings import config_manager
from core.event_bus import Event, event_bus
from core.funpay_client import accounts_manager
from utils.logger import logger


class OnlineKeeper:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("OnlineKeeper запущен.")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    CHECK_EVERY_SEC = 300          # раз в 5 минут — заходим на FunPay (аккаунт «в сети» + проверка входа)
    SESSION_EVERY_SEC = 40 * 60    # раз в 40 минут — обновляем сессию FunPay (PHPSESSID), как FunPay Cardinal.
                                   # Без этого сессия со временем истекала и бот «отваливался» до перезапуска.
    ALERT_EVERY_SEC = 3600         # о разлогине напоминаем не чаще раза в час
    MIN_SLEEP = 30

    async def _loop(self) -> None:
        """
        Онлайн на FunPay держится, пока бот опрашивает FunPay (Runner). Дополнительно раз в
        5 минут открываем FunPay от имени аккаунта: это поддерживает статус «в сети» и сразу
        показывает, если FunPay разлогинил (golden_key устарел) — тогда шлём кнопку смены ключа.
        """
        loop = asyncio.get_running_loop()
        last_check = 0.0
        last_alert = 0.0
        last_session = loop.time()          # при старте сессия свежая (её получил connect)
        while self._running:
            cfg = config_manager.settings.online_keeper
            try:
                now = loop.time()
                refresh = now - last_session >= self.SESSION_EVERY_SEC
                if (cfg.enabled and now - last_check >= self.CHECK_EVERY_SEC) or refresh:
                    last_check = now
                    if refresh:
                        last_session = now
                    for cl in accounts_manager.connected_clients():
                        try:
                            await loop.run_in_executor(None, lambda c=cl, r=refresh: c.account.get(update_phpsessid=r))
                            if refresh:
                                logger.info(f"OnlineKeeper [{cl.alias}]: сессия FunPay обновлена")
                            logger.debug(f"OnlineKeeper [{cl.alias}]: ✅ онлайн")
                        except Exception as e:
                            name = type(e).__name__
                            logger.warning(f"OnlineKeeper [{cl.alias}]: FunPay не ответил ({name}: {str(e)[:120]})")
                            if "Unauthorized" in name and now - last_alert > self.ALERT_EVERY_SEC:
                                last_alert = now
                                from bot.handlers.golden_key import notify_funpay_down
                                await notify_funpay_down("FunPay разлогинил аккаунт — бот не в сети, "
                                                         "заказы и сообщения не обрабатываются.")
            except Exception as e:
                logger.debug(f"OnlineKeeper: {e}")
            await asyncio.sleep(max(self.MIN_SLEEP, cfg.poll_interval_seconds))


online_keeper = OnlineKeeper()
