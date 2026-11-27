# -*- coding: utf-8 -*-
"""Онлайн 24/7: проверка входа каждые N сек, обновление сессии FunPay, сигнал о разлогине."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class UnauthorizedError(Exception):
    pass


def test_online_keeper(monkeypatch):
    import modules.online_keeper as ok
    from config.settings import config_manager
    from core import funpay_client
    import bot.handlers.golden_key as gk

    calls, alerts = [], []

    class Acc:
        fail = False

        def get(self, update_phpsessid=False):
            calls.append(update_phpsessid)
            if Acc.fail:
                raise UnauthorizedError("logout")

    cl = NS(alias="me", account=Acc())
    monkeypatch.setattr(funpay_client.accounts_manager, "connected_clients", lambda: [cl])
    monkeypatch.setattr(ok.accounts_manager, "connected_clients", lambda: [cl])

    async def fake_alert(reason=""):
        alerts.append(reason)
    monkeypatch.setattr(gk, "notify_funpay_down", fake_alert)
    monkeypatch.setattr(ok.OnlineKeeper, "CHECK_EVERY_SEC", 0.05)
    monkeypatch.setattr(ok.OnlineKeeper, "SESSION_EVERY_SEC", 0.2)
    monkeypatch.setattr(ok.OnlineKeeper, "ALERT_EVERY_SEC", 100)
    monkeypatch.setattr(config_manager.settings.online_keeper, "poll_interval_seconds", 0)
    monkeypatch.setattr(ok.OnlineKeeper, "MIN_SLEEP", 0.02)

    async def main():
        k = ok.OnlineKeeper()
        await k.start()
        await asyncio.sleep(0.5)
        Acc.fail = True
        await asyncio.sleep(0.3)
        await k.stop()

    asyncio.run(main())
    assert calls.count(False) >= 2 and True in calls      # регулярные проверки + обновление сессии
    assert len(alerts) == 1                               # о разлогине — один раз, без спама
