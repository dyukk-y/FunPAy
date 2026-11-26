# -*- coding: utf-8 -*-
"""Сторож зависаний и отчёты о сбоях (utils/health.py)."""
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_hang_watchdog_restarts_and_reports(tmp_path):
    script = tmp_path / "hang.py"
    script.write_text(textwrap.dedent(f"""
        import asyncio, sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from pathlib import Path
        import utils.health as h
        h.DATA = Path({str(tmp_path)!r}); h.CRASH_PATH = h.DATA / "last_crash.json"
        h.HEARTBEAT_PATH = h.DATA / "heartbeat.json"
        h.HANG_SEC = 2; h.BEAT_SEC = 0.3

        def frozen_network_call():
            time.sleep(30)      # как зависший запрос к FunPay в основном потоке

        async def main():
            h.start()
            await asyncio.sleep(0.5)
            frozen_network_call()
        asyncio.run(main())
    """), encoding="utf-8")
    t0 = time.time()
    r = subprocess.run([sys.executable, str(script)], capture_output=True, timeout=25)
    assert r.returncode == 3 and time.time() - t0 < 15
    crash = json.loads((tmp_path / "last_crash.json").read_text(encoding="utf-8"))
    assert "завис" in crash["reason"] and "frozen_network_call" in crash["details"]


def test_crash_report_texts(tmp_path, monkeypatch):
    import utils.health as h
    monkeypatch.setattr(h, "CRASH_PATH", tmp_path / "c.json")
    monkeypatch.setattr(h, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(h, "DATA", tmp_path)

    assert h.pop_crash_report() is None                      # первый запуск — тишина
    h.record_child_exit(-9)
    rep = h.pop_crash_report()
    assert "нехватки памяти" in rep and not (tmp_path / "c.json").exists()

    h.record_crash("бот завис", "where <x>")
    h.record_child_exit(3)                                   # сторож дополняет запись бота
    rep = h.pop_crash_report()
    assert "бот завис" in rep and "сторожем зависаний" in rep and "&lt;x&gt;" in rep

    (tmp_path / "hb.json").write_text(json.dumps({"ts": time.time(), "clean": False, "memory": "бот 200 МБ"}))
    assert "аварийно" in h.pop_crash_report()
    h.mark_clean_shutdown()
    assert h.pop_crash_report() is None                      # штатная остановка — без тревоги


def test_send_with_vars_does_not_block_loop():
    """Отправка в FunPay идёт в отдельном потоке: основной цикл продолжает работать."""
    import asyncio
    from utils.variables import send_with_vars

    def slow_send(chat_id, text):
        time.sleep(1.0)
        return True

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(8):
                await asyncio.sleep(0.1)
                ticks += 1

        res = await asyncio.gather(send_with_vars("привет", slow_send, "users-1-2"), ticker())
        return res[0], ticks

    ok, ticks = asyncio.run(main())
    assert ok and ticks == 8
