"""
Здоровье бота: сторож зависаний, отчёт о сбоях, контроль памяти.

1. Сторож зависаний. Основной asyncio-цикл раз в 5 сек отмечается «я жив».
   Отдельный поток проверяет отметку: если цикл молчит дольше HANG_SEC —
   бот завис (раньше так выглядели «падения»: процесс жив, но ничего не
   отвечает). Тогда пишем, ГДЕ он застрял (стек основного потока), и
   выходим с кодом 3 — сторож в main.py поднимает бота заново.

2. Отчёт о сбое. Причина аварийного выхода сохраняется в data/last_crash.json
   (пишет сам бот или сторож main.py). При следующем запуске бот присылает
   админам «⚠️ Бот перезапустился после сбоя» с причиной.
   Если процесс убили без шансов что-то записать (например, хостинг убил
   контейнер за нехватку памяти) — это видно по data/heartbeat.json без
   отметки о штатной остановке.

3. Память. Раз в минуту heartbeat пишет потребление памяти и лимит
   контейнера (cgroup), чтобы было видно, не упирается ли бот в тариф.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

DATA = Path(__file__).resolve().parent.parent / "data"
CRASH_PATH = DATA / "last_crash.json"
HEARTBEAT_PATH = DATA / "heartbeat.json"

HANG_SEC = 180          # столько основной цикл может молчать, прежде чем считаем бота зависшим
BEAT_SEC = 5
EXIT_HANG = 3

_last_beat = time.monotonic()
_main_thread_id: Optional[int] = None
_started = False


# ──────────────────────────────────────────────────────────────────────────────
# Память
# ──────────────────────────────────────────────────────────────────────────────

def rss_mb() -> Optional[float]:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        pass
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return None


def container_memory() -> tuple:
    """(занято МБ, лимит МБ) по cgroup — память всего контейнера. None, если не узнать."""
    used = limit = None
    for usage_f, limit_f in (("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max"),
                             ("/sys/fs/cgroup/memory/memory.usage_in_bytes",
                              "/sys/fs/cgroup/memory/memory.limit_in_bytes")):
        try:
            used = int(Path(usage_f).read_text().strip()) / 1024 / 1024
            raw = Path(limit_f).read_text().strip()
            if raw != "max":
                lim = int(raw) / 1024 / 1024
                limit = lim if lim < 1024 * 1024 else None  # «без лимита» = огромное число
            break
        except Exception:
            continue
    return used, limit


def memory_line() -> str:
    me = rss_mb()
    used, limit = container_memory()
    parts = []
    if me is not None:
        parts.append(f"бот {me:.0f} МБ")
    if used is not None:
        parts.append(f"контейнер {used:.0f}" + (f" из {limit:.0f} МБ" if limit else " МБ"))
    return ", ".join(parts) or "нет данных"


# ──────────────────────────────────────────────────────────────────────────────
# Отчёт о сбое
# ──────────────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass


def record_crash(reason: str, details: str = "") -> None:
    _atomic_write(CRASH_PATH, {"ts": time.time(), "reason": reason, "details": details[-3000:],
                               "memory": memory_line()})


def mark_clean_shutdown() -> None:
    _atomic_write(HEARTBEAT_PATH, {"ts": time.time(), "clean": True, "memory": memory_line()})


def _tail_errors(n: int = 6) -> str:
    """Последние ошибки из лога — подсказка, что было перед сбоем."""
    log = DATA / "logs" / "bot.log"
    try:
        with open(log, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 200_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        errs = [l for l in lines if "| ERROR" in l or "| CRITICAL" in l]
        return "\n".join(errs[-n:])
    except Exception:
        return ""


def describe_exit(code: int) -> str:
    """Понятное описание кода выхода дочернего процесса (для сторожа main.py)."""
    if code == EXIT_HANG:
        return "бот завис и был перезапущен сторожем зависаний"
    if code in (-9, 137):
        return "процесс убит системой (SIGKILL) — чаще всего из-за нехватки памяти на тарифе"
    if code in (-15, 143):
        return "процесс остановлен сигналом SIGTERM (перезапуск/остановка хостингом)"
    if code < 0:
        return f"процесс убит сигналом {-code}"
    return f"процесс завершился с ошибкой (код {code})"


def record_child_exit(code: int) -> None:
    """Вызывается сторожем main.py, когда дочерний процесс упал."""
    try:
        prev = json.loads(CRASH_PATH.read_text(encoding="utf-8")) if CRASH_PATH.exists() else None
    except Exception:
        prev = None
    if prev and time.time() - float(prev.get("ts", 0)) < 30:
        prev["exit"] = describe_exit(code)   # бот уже сам записал причину — дополняем
        _atomic_write(CRASH_PATH, prev)
        return
    record_crash(describe_exit(code), _tail_errors())


def pop_crash_report() -> Optional[str]:
    """Текст отчёта о прошлом сбое (и удаляет его). None — если запуск штатный."""
    import html
    report = None
    try:
        if CRASH_PATH.exists():
            d = json.loads(CRASH_PATH.read_text(encoding="utf-8"))
            CRASH_PATH.unlink()
            when = time.strftime("%d.%m %H:%M", time.localtime(d.get("ts", time.time())))
            report = (f"⚠️ <b>Бот перезапустился после сбоя</b> ({when})\n\n"
                      f"Причина: <b>{html.escape(d.get('reason', '?'))}</b>")
            if d.get("exit"):
                report += f"\n{html.escape(d['exit'])}"
            if d.get("memory"):
                report += f"\nПамять в момент сбоя: {html.escape(d['memory'])}"
            if d.get("details"):
                report += f"\n\n<code>{html.escape(d['details'][-1500:])}</code>"
        elif HEARTBEAT_PATH.exists():
            d = json.loads(HEARTBEAT_PATH.read_text(encoding="utf-8"))
            if not d.get("clean") and time.time() - float(d.get("ts", 0)) < 6 * 3600:
                when = time.strftime("%d.%m %H:%M", time.localtime(d.get("ts", time.time())))
                report = ("⚠️ <b>Бот был остановлен аварийно</b>\n\n"
                          f"Последний признак жизни: {when}, память: {html.escape(d.get('memory', '?'))}.\n"
                          "Процесс убили без возможности что-то записать — обычно это хостинг "
                          "(нехватка памяти на тарифе или перезапуск контейнера).")
    except Exception:
        return None
    return report


# ──────────────────────────────────────────────────────────────────────────────
# Сторож зависаний + heartbeat
# ──────────────────────────────────────────────────────────────────────────────

def _main_stack() -> str:
    frame = sys._current_frames().get(_main_thread_id) if _main_thread_id else None
    return "".join(traceback.format_stack(frame)[-12:]) if frame else "(стек недоступен)"


def _watchdog_thread() -> None:
    last_hb = 0.0
    while True:
        time.sleep(BEAT_SEC)
        now = time.monotonic()
        silent = now - _last_beat
        if silent > HANG_SEC:
            stack = _main_stack()
            try:
                from utils.logger import logger
                logger.critical(f"Бот завис: основной цикл не отвечает {int(silent)} сек. Где застрял:\n{stack}")
            except Exception:
                pass
            record_crash(f"бот завис — не отвечал {int(silent)} сек", f"Где застрял:\n{stack}")
            os._exit(EXIT_HANG)
        if now - last_hb >= 60:
            last_hb = now
            _atomic_write(HEARTBEAT_PATH, {"ts": time.time(), "clean": False, "memory": memory_line()})


async def _beat() -> None:
    global _last_beat
    while True:
        _last_beat = time.monotonic()
        await asyncio.sleep(BEAT_SEC)


def _thread_excepthook(args) -> None:
    try:
        from utils.logger import logger
        logger.opt(exception=(args.exc_type, args.exc_value, args.exc_traceback)).error(
            f"Необработанная ошибка в потоке {getattr(args.thread, 'name', '?')}: {args.exc_value}")
    except Exception:
        pass


def _loop_exception_handler(loop, context) -> None:
    try:
        from utils.logger import logger
        exc = context.get("exception")
        msg = context.get("message", "")
        if exc is not None:
            logger.opt(exception=exc).error(f"Ошибка в фоновой задаче: {msg} {exc}")
        else:
            logger.error(f"Ошибка в фоновой задаче: {msg}")
    except Exception:
        pass


_beat_task: Optional[asyncio.Task] = None


def start() -> None:
    """Запускать из основного asyncio-цикла после старта бота."""
    global _started, _main_thread_id, _beat_task, _last_beat
    if _started:
        return
    _started = True
    _main_thread_id = threading.get_ident()
    _last_beat = time.monotonic()
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    threading.excepthook = _thread_excepthook
    _beat_task = loop.create_task(_beat(), name="health-beat")
    threading.Thread(target=_watchdog_thread, name="HangWatchdog", daemon=True).start()
