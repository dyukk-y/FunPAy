"""
Help Funpay by yasha — точка входа (основано на PaltoFunPayBot © PaltoProjects).

Если что-то падает на старте — пишет полный traceback в data/logs/startup_error.log
и не закрывает консоль, а ждёт Enter, чтобы можно было прочитать ошибку.
"""
import os
import sys
import traceback
from pathlib import Path
from datetime import datetime

# Windows: при перенаправленном выводе (pipe, > файл) stdout использует
# cp1251/cp866 и падает на эмодзи. Принудительно UTF-8 с заменой символов.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _show_error_and_wait(err_text: str) -> None:
    """Показывает ошибку и держит консоль открытой."""
    log_dir = Path(__file__).resolve().parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    err_file = log_dir / "startup_error.log"
    try:
        with open(err_file, "a", encoding="utf-8") as f:
            f.write(f"\n{'=' * 70}\n")
            f.write(f"СТАРТ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"{'=' * 70}\n")
            f.write(err_text)
            f.write("\n")
    except Exception:
        pass

    print()
    print("=" * 70)
    print("❌ ОШИБКА ПРИ ЗАПУСКЕ БОТА")
    print("=" * 70)
    print(err_text)
    print("=" * 70)
    print(f"📄 Полный лог сохранён: {err_file}")
    print("=" * 70)
    print()
    # На хостинге (нет консоли) не ждём Enter — просто выходим, сторож перезапустит
    if sys.stdin is not None and sys.stdin.isatty() and not os.environ.get("FPB_CHILD"):
        try:
            input("Нажмите Enter, чтобы закрыть окно...")
        except Exception:
            pass


def _main_safe() -> None:
    """Обёртка — ловит все ошибки старта и пишет их."""
    try:
        # Импорты внутри try — чтобы поймать даже импорт-ошибки
        import asyncio
        import getpass
        import os
        import secrets
        import sys

        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.types import BotCommand

        from utils.logger import logger, setup_logger
        setup_logger()

        # Фирменный баннер
        BANNER = r"""
  _   _      _         _____                                 _
 | | | | ___| |_ __   |  ___|   _ _ __  _ __   __ _ _   _   | |__  _   _
 | |_| |/ _ \ | '_ \  | |_ | | | | '_ \| '_ \ / _` | | | |  | '_ \| | | |
 |  _  |  __/ | |_) | |  _|| |_| | | | | |_) | (_| | |_| |  | |_) | |_| |
 |_| |_|\___|_| .__/  |_|   \__,_|_| |_| .__/ \__,_|\__, |  |_.__/ \__, |
              |_|                      |_|          |___/          |___/
                                   y a s h a
"""
        print("\033[36m" + BANNER + "\033[0m")  # cyan
        print("\033[35m" + "  Help Funpay by yasha — помощник продавца FunPay (основано на PaltoFunPayBot)" + "\033[0m\n")

        # ─── Обработка CLI-аргументов ────────────────────────────────────
        # python main.py --setup-proxy   — заново настроить прокси без перезапуска визарда целиком
        if "--setup-proxy" in sys.argv or "--proxy" in sys.argv:
            from config.settings import config_manager

            def _ask_proxy_only() -> None:
                f = config_manager.settings.funpay
                print("\n" + "=" * 64)
                print(" 🌐 Настройка прокси")
                print("=" * 64 + "\n")

                if f.proxy:
                    print(f"Текущий прокси: {f.proxy.split('@')[-1]}")
                    print("Введите новый, '-' чтобы удалить, или Enter чтобы оставить.")
                else:
                    print("Прокси сейчас не задан.")

                print()
                print("Поддерживаемые форматы:")
                print("  • login:pass@ip:port   (с авторизацией)")
                print("  • ip:port              (без авторизации)")
                print("  • login:pass:ip:port   (альтернативный)")
                print()
                while True:
                    proxy = input("Прокси: ").strip()
                    if not proxy:
                        print("⏭ Без изменений.")
                        return
                    if proxy in ("-", "нет", "none", "off"):
                        f.proxy = ""
                        config_manager.save()
                        print("🗑 Прокси удалён.")
                        return
                    import re
                    if re.search(r"\d+\.\d+\.\d+\.\d+:\d+|@[^:]+:\d+", proxy):
                        f.proxy = proxy
                        config_manager.save()
                        host_port = proxy.split("@")[-1]
                        has_auth = "@" in proxy
                        print(f"✅ Прокси сохранён: {host_port} (авторизация: {'да' if has_auth else 'нет'})")
                        return
                    print("❌ Неверный формат. Пример: login:pass@1.2.3.4:8080")

            _ask_proxy_only()
            print("\nЗапустите бота: python main.py")
            return

        # python main.py --setup-ua   — заново настроить User-Agent
        if "--setup-ua" in sys.argv or "--ua" in sys.argv or "--user-agent" in sys.argv:
            from config.settings import config_manager

            DEFAULT_UA = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36"
            )

            def _ask_ua_only() -> None:
                f = config_manager.settings.funpay
                print("\n" + "=" * 64)
                print(" 🪪 Настройка User-Agent для FunPay")
                print("=" * 64 + "\n")
                print("⚠️ КРИТИЧЕСКИ ВАЖНО! Без браузерного User-Agent FunPay не отдаёт")
                print("   чаты и сообщения.\n")

                if f.user_agent:
                    short = f.user_agent[:80] + ("..." if len(f.user_agent) > 80 else "")
                    print(f"Текущий: {short}\n")

                print("Чтобы получить ваш РЕАЛЬНЫЙ User-Agent (рекомендуется):")
                print("  1. Откройте https://www.whatsmyua.info/ в браузере")
                print("  2. Скопируйте 'Your User Agent String'")
                print("  3. Вставьте сюда\n")
                print("Или нажмите Enter — поставится стандартный (Chrome 109).\n")

                ua = input("User-Agent: ").strip()
                if not ua:
                    f.user_agent = DEFAULT_UA
                    print(f"✅ Установлен стандартный: {DEFAULT_UA[:60]}...")
                elif "python-requests" in ua:
                    print("❌ Это python-requests — именно тот UA что не работает!")
                    print("   Попробуйте ещё раз.")
                    return _ask_ua_only()
                elif len(ua) < 30:
                    print("❌ User-Agent слишком короткий.")
                    return _ask_ua_only()
                else:
                    f.user_agent = ua
                    print(f"✅ User-Agent сохранён.")
                config_manager.save()

            _ask_ua_only()
            print("\nЗапустите бота: python main.py")
            return

        from config.settings import config_manager
        from core.event_bus import Event, event_bus
        from core.funpay_client import FUNPAY_AVAILABLE, FUNPAY_IMPORT_ERROR, funpay_client

        # Предупреждение если FunPayAPI не загрузилась
        if not FUNPAY_AVAILABLE:
            print()
            print("⚠️ " + "=" * 66)
            print(" ВНИМАНИЕ: библиотека FunPayAPI не загрузилась.")
            print("=" * 70)
            if "operand" in str(FUNPAY_IMPORT_ERROR) or "unsupported" in str(FUNPAY_IMPORT_ERROR):
                py_ver = ".".join(map(str, sys.version_info[:3]))
                print(f" Причина: ваша версия Python ({py_ver}) не поддерживает")
                print(" современный синтаксис типов, который использует FunPayAPI.")
                print()
                print(" 🔧 РЕШЕНИЕ: установите Python 3.10 или новее с python.org")
                print(" После установки — переустановите зависимости в новом venv:")
                print("   python -m venv venv")
                print("   venv\\Scripts\\activate         (Windows)")
                print("   pip install -r requirements.txt")
            else:
                print(f" Ошибка: {FUNPAY_IMPORT_ERROR}")
                print(" Установите: pip install FunPayAPI")
            print()
            print(" Бот запустится, но БЕЗ подключения к FunPay (только Telegram-меню).")
            print("=" * 70)
            print()

        # ─── CLI-визард ──────────────────────────────────────────────────────

        def _ask(prompt: str, secret: bool = False) -> str:
            if secret:
                try:
                    return getpass.getpass(prompt).strip()
                except Exception:
                    return input(prompt).strip()
            return input(prompt).strip()

        def cli_initial_setup() -> None:
            s = config_manager.settings.telegram
            f = config_manager.settings.funpay

            DEFAULT_UA = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36"
            )

            print("\n" + "=" * 64)
            print(" 🤖 Help Funpay by yasha — первичная настройка")
            print("=" * 64 + "\n")

            if not s.token:
                print("Шаг 1/4. Создайте бота через @BotFather и получите токен.")
                print("Формат: 1234567890:AAH...30+ символов")
                while True:
                    token = _ask("Токен Telegram-бота: ")
                    if ":" in token and len(token) >= 35:
                        s.token = token
                        break
                    print("❌ Токен похоже некорректный.")
                print("✅ Токен сохранён.\n")

            if not s.admin_password:
                print("Шаг 2/4. Придумайте пароль администратора.")
                print("Этот пароль вы введёте при первом /start в TG-боте.")
                print("Оставьте пустым, чтобы сгенерировать случайный.")
                pwd = _ask("Пароль админа: ", secret=True)
                if not pwd:
                    pwd = secrets.token_urlsafe(12)
                    print(f"📋 Сгенерирован: {pwd}")
                    print("   ❗ Сохраните его!")
                # В конфиг пишем bcrypt-хэш, а не плейнтекст
                from utils.passwords import hash_password
                s.admin_password = hash_password(pwd)
                print("✅ Пароль сохранён.\n")

            if not f.proxy:
                print("Шаг 3/4. Прокси (опционально, но РЕКОМЕНДУЕТСЯ).")
                print("Бот сам ходит через прокси с авторизацией — Proxifier не нужен.")
                print("Один и тот же прокси используется для FunPay и Telegram.")
                print()
                print("Поддерживаемые форматы:")
                print("  • login:pass@ip:port   (с авторизацией — рекомендуется)")
                print("  • ip:port              (без авторизации)")
                print("  • login:pass:ip:port   (альтернативный)")
                print()
                print("Оставьте пустым, чтобы пропустить (бот пойдёт напрямую).")
                while True:
                    proxy = _ask("Прокси: ").strip()
                    if not proxy:
                        print("⏭ Шаг пропущен. Прокси можно настроить позже:")
                        print("   /menu → 🔧 Система → 🌐 Прокси")
                        break
                    # Минимальная валидация — должно быть ip:port или с @
                    import re
                    if re.search(r"\d+\.\d+\.\d+\.\d+:\d+|@[^:]+:\d+", proxy):
                        f.proxy = proxy
                        host_port = proxy.split("@")[-1]
                        has_auth = "@" in proxy
                        print(f"✅ Прокси сохранён: {host_port} (авторизация: {'да' if has_auth else 'нет'})")
                        break
                    print("❌ Неверный формат. Пример: login:pass@1.2.3.4:8080")
                print()

            # User-Agent — критически важно! Без него FunPay видит python-requests
            # и не отдаёт чаты/сообщения. По умолчанию — Chrome на Win10 (как Cardinal).
            need_ua_setup = (not f.user_agent) or "python-requests" in f.user_agent
            if need_ua_setup:
                print("Шаг 4/4. User-Agent для FunPay.")
                print("⚠️ КРИТИЧЕСКИ ВАЖНО! Без браузерного User-Agent FunPay не отдаёт")
                print("   чаты и сообщения (видит что вы — бот).")
                print()
                print(f"По умолчанию: {DEFAULT_UA[:60]}...")
                print()
                print("Чтобы получить ваш РЕАЛЬНЫЙ User-Agent (рекомендуется):")
                print("  1. Откройте https://www.whatsmyua.info/ в браузере")
                print("  2. Скопируйте строку 'Your User Agent String'")
                print("  3. Вставьте сюда")
                print()
                print("Можно нажать Enter — будет использован стандартный (Chrome 109).")
                ua = _ask("User-Agent (Enter — по умолчанию): ").strip()
                f.user_agent = ua if ua else DEFAULT_UA
                print(f"✅ User-Agent сохранён.\n")

            config_manager.save()
            print("=" * 64)
            print(" ✅ Базовая настройка завершена.")
            print()
            print(" Откройте TG-бота → /start → введите пароль админа.")
            print(" Дальше — введёте golden_key прямо в боте.")
            print("=" * 64 + "\n")

        # ─── Слэш-команды в меню Telegram ───────────────────────────────────

        # В меню Telegram — только главное (остальные команды тоже работают: /about, /logs, /sys, …)
        BOT_COMMANDS = [
            BotCommand(command="menu", description="главное меню"),
            BotCommand(command="ads", description="реклама: заказы и канал"),
            BotCommand(command="sales", description="автоответы и обучение"),
            BotCommand(command="lots", description="тексты и цены лотов"),
            BotCommand(command="golden_key", description="обновить golden_key"),
            BotCommand(command="balance", description="баланс FunPay"),
            BotCommand(command="profile", description="статистика аккаунта"),
            BotCommand(command="restart", description="перезапустить бота"),
            BotCommand(command="logs", description="скачать лог"),
        ]

        # ─── Запуск фоновых сервисов ─────────────────────────────────────────

        async def start_background_modules() -> None:
            from modules.online_keeper import online_keeper
            from modules.auto_lift import auto_lift
            from modules.ai_assistant import ai_assistant
            from modules.auto_deactivation import auto_restore
            from modules.auto_withdraw import auto_withdraw
            from modules.anti_dumping import anti_dumping

            import modules.auto_greeting       # noqa: F401
            import modules.auto_response       # noqa: F401
            import modules.auto_delivery       # noqa: F401
            import modules.auto_review_reply   # noqa: F401
            import modules.order_reactions     # noqa: F401
            import modules.notifications       # noqa: F401
            import modules.analytics           # noqa: F401
            import modules.sales_brain         # noqa: F401  — анализ переписки и автопродажи

            from modules.lot_texts import lot_texts_scheduler
            await lot_texts_scheduler.start()
            from modules.sales_brain import start_learning
            await start_learning()
            from modules import ad_autopost
            await ad_autopost.start()
            await online_keeper.start()
            await auto_lift.start()
            await ai_assistant.start()
            await auto_restore.start()
            await auto_withdraw.start()
            await anti_dumping.start()
            logger.info("Фоновые модули запущены.")

        async def start_funpay_if_configured() -> None:
            fp = config_manager.settings.funpay
            if not fp.accounts and not fp.golden_key:
                logger.info("FunPay не настроен — введите golden_key в Telegram-боте.")
                try:
                    from bot.handlers.golden_key import notify_funpay_down
                    await notify_funpay_down("golden_key ещё не задан.")
                except Exception as e:
                    logger.debug(f"notify_funpay_down: {e}")
                return
            if not FUNPAY_AVAILABLE:
                logger.warning("FunPayAPI не установлена. pip install FunPayAPI")
                return

            # Мультиаккаунт: подключаем и поллим ВСЕ включённые аккаунты
            from core.funpay_client import accounts_manager
            accounts_manager.load_from_config()
            results = await accounts_manager.start_all()
            ok_count = sum(1 for _, ok, _ in results if ok)
            total = len(results)
            if total > 1:
                logger.info(f"Мультиаккаунт: подключено {ok_count} из {total} аккаунтов.")
            if not ok_count:
                logger.error("Ни один FunPay-аккаунт не подключился.")
                try:
                    from bot.handlers.golden_key import notify_funpay_down
                    await notify_funpay_down(results[0][2] if results else "")
                except Exception as e:
                    logger.debug(f"notify_funpay_down: {e}")
                return
            loop = asyncio.get_event_loop()

            # Дозаполняем названия лотов для сопоставления автовыдачи
            # (старые конфиги без match_text). В фоне, чтобы не тормозить старт.
            try:
                from modules.auto_delivery import backfill_match_texts
                asyncio.create_task(loop.run_in_executor(None, backfill_match_texts))
            except Exception as e:
                logger.debug(f"backfill_match_texts не запущен: {e}")

            # Keep-alive чтобы прокси не закрывал idle-соединение
            try:
                from utils.proxy_keepalive import proxy_keepalive
                await proxy_keepalive.start()
            except Exception as e:
                logger.debug(f"ProxyKeepAlive не запустился: {e}")

        # ─── Главная корутина ────────────────────────────────────────────────

        async def main() -> None:
            # Переменные окружения (BOT_TOKEN, ADMIN_PASSWORD, GOLDEN_KEY, ...) —
            # для хостингов без консоли. См. utils/env_config.py и HOSTING.md
            from utils.env_config import apply_env_overrides
            apply_env_overrides(logger)

            cfg = config_manager.settings.telegram
            fcfg = config_manager.settings.funpay
            ua_broken = (not fcfg.user_agent) or "python-requests" in fcfg.user_agent
            if not cfg.token or not (cfg.admin_password or cfg.admin_ids) or ua_broken:
                interactive = sys.stdin is not None and sys.stdin.isatty()
                if not interactive:
                    missing = []
                    if not cfg.token:
                        missing.append("BOT_TOKEN")
                    if not (cfg.admin_password or cfg.admin_ids):
                        missing.append("ADMIN_PASSWORD (или ADMIN_IDS)")
                    print("\n" + "=" * 64)
                    print(" ❌ Бот не настроен, а консоли для ввода нет (хостинг).")
                    print(" Задайте переменные окружения в панели хостинга:")
                    for m in missing:
                        print(f"   • {m}")
                    print(" Подробно — в файле HOSTING.md")
                    print("=" * 64 + "\n")
                    sys.exit(2)
                cli_initial_setup()

            # Если задан прокси — Telegram-бот тоже идёт через него.
            # Это решает блокировку Telegram в РФ + работает с авторизованными прокси.
            from aiogram.client.session.aiohttp import AiohttpSession
            session = None
            proxy_str = config_manager.settings.funpay.proxy
            if proxy_str:
                # Нормализация: login:pass:ip:port → login:pass@ip:port
                if "@" not in proxy_str and proxy_str.count(":") == 3:
                    login, password, ip, port = proxy_str.split(":")
                    proxy_str = f"{login}:{password}@{ip}:{port}"
                proxy_url = f"http://{proxy_str}"
                # Проверяем доступна ли aiohttp-socks (нужна aiogram для прокси с авторизацией)
                try:
                    import aiohttp_socks  # noqa: F401
                except ImportError:
                    logger.error(
                        "❌ Прокси задан, но не установлена aiohttp-socks.\n"
                        "Установите её для поддержки прокси в Telegram-боте:\n"
                        "    pip install aiohttp-socks"
                    )
                    print("\n" + "=" * 64)
                    print(" ⚠️ ВНИМАНИЕ — нужна доп. библиотека для прокси")
                    print("=" * 64)
                    print(" Запустите команду:")
                    print("    pip install aiohttp-socks")
                    print(" И снова запустите бота.")
                    print("=" * 64 + "\n")
                    return
                try:
                    session = AiohttpSession(proxy=proxy_url)
                    logger.info(f"Telegram через прокси: {proxy_str.split('@')[-1]}")
                except Exception as e:
                    logger.warning(f"Не удалось настроить прокси для Telegram: {e}")

            bot = Bot(
                token=config_manager.settings.telegram.token,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
                session=session,
            )
            dp = Dispatcher(storage=MemoryStorage())

            from modules.notifications import set_bot
            set_bot(bot)

            from bot.handlers.setup import router as setup_router
            from bot.handlers.commands import router as commands_router
            from bot.handlers.menu import router as menu_router
            from bot.handlers.auth import router as auth_router
            from bot.handlers.quick_reply import router as qr_router
            from bot.handlers.plugins import router as plugins_router
            from bot.handlers.twofa import router as twofa_router
            from bot.middleware import AuditMiddleware, TwoFAMiddleware, ErrorMiddleware

            # Middleware для всех событий (audit + error + 2FA)
            # ErrorMiddleware первая — она ловит все исключения
            dp.message.middleware(ErrorMiddleware())
            dp.callback_query.middleware(ErrorMiddleware())
            dp.message.middleware(AuditMiddleware())
            dp.callback_query.middleware(AuditMiddleware())
            dp.message.middleware(TwoFAMiddleware())
            dp.callback_query.middleware(TwoFAMiddleware())

            from bot.handlers.golden_key import router as golden_key_router
            from bot.handlers.lot_texts import router as lot_texts_router
            from bot.handlers.sales_brain import router as sales_brain_router
            from bot.handlers.ad_orders import router as ad_orders_router
            from bot.handlers.ad_autopost import router as ad_autopost_router

            dp.include_router(setup_router)
            dp.include_router(twofa_router)      # /login, /2fa_setup и т.д. — до auth
            dp.include_router(golden_key_router) # 🔑 смена golden_key с телефона
            dp.include_router(ad_autopost_router)  # 📣 кнопки автопубликации
            dp.include_router(ad_orders_router)  # 📢 заказы рекламы (ссылка на пост)
            dp.include_router(lot_texts_router)  # 📝 тексты и цены лотов
            dp.include_router(sales_brain_router)  # 🧠 анализ переписки и автопродажи
            dp.include_router(commands_router)
            dp.include_router(qr_router)         # быстрый ответ из TG — до menu/auth
            dp.include_router(menu_router)
            dp.include_router(plugins_router)    # /plugins + диспатчер команд плагинов
            dp.include_router(auth_router)       # catch-all для неавторизованных — последним

            try:
                await bot.set_my_commands(BOT_COMMANDS)
            except Exception as e:
                logger.warning(f"set_my_commands: {e}")

            from plugins.manager import plugin_manager
            plugin_manager.reload_all()

            # Сторож зависаний + отчёты о сбоях (utils/health.py)
            from utils import health
            health.start()

            await start_background_modules()
            await start_funpay_if_configured()

            # Telegram может быть недоступен при старте (сбой сети у хостинга) — ждём связь,
            # а не падаем: FunPay-часть уже работает
            me = None
            for _attempt, _pause in enumerate((5, 10, 20, 30, 60, 60, 60, 60), 1):
                try:
                    me = await bot.get_me()
                    break
                except Exception as e:
                    logger.warning(f"Telegram недоступен ({type(e).__name__}) — повтор через {_pause} с "
                                   f"(попытка {_attempt})")
                    await asyncio.sleep(_pause)
            if me is None:
                me = await bot.get_me()   # последняя попытка: не вышло — сторож перезапустит бота
            logger.info(f"Telegram-бот запущен: @{me.username}")
            logger.info(f"FunPayAPI: {'установлен' if FUNPAY_AVAILABLE else 'НЕ установлен'}")

            # Шлём BOT_STARTED — Telegram-бот точно подключился, можно уведомлять
            # Если FunPay подключён, передаём account, иначе None
            try:
                fp_account = funpay_client.account if funpay_client.account else None
                await event_bus.emit(Event.BOT_STARTED, fp_account)
            except Exception as e:
                logger.warning(f"Не удалось отправить BOT_STARTED: {e}")
            # Брендинг и проверка канала для автопубликации (в фоне, не задерживают старт)
            try:
                from utils import branding
                from modules import ad_autopost as _ap
                from utils import tg_backup
                _bg = [asyncio.create_task(branding.apply(bot)), asyncio.create_task(_ap.check_channel_on_start()),
                       asyncio.create_task(tg_backup.start())]
            except Exception as e:
                logger.debug(f"branding/channel: {e}")
            # Если прошлый запуск закончился сбоем — сообщаем админам причину
            try:
                report = health.pop_crash_report()
                if report:
                    logger.warning(report.replace("<b>", "").replace("</b>", ""))
                    from modules.notifications import _send_to_all
                    await _send_to_all(report)
            except Exception as e:
                logger.warning(f"Не удалось отправить отчёт о сбое: {e}")

            try:
                await dp.start_polling(bot, handle_signals=False)
            finally:
                # Сначала шлём уведомление в TG, ПОКА сессия живая
                try:
                    await event_bus.emit(Event.BOT_STOPPED)
                except Exception as e:
                    logger.warning(f"Не удалось отправить BOT_STOPPED: {e}")
                # Потом закрываем всё (все аккаунты мультиаккаунта)
                from core.funpay_client import accounts_manager
                accounts_manager.stop_all()
                try:
                    await bot.session.close()
                except Exception:
                    pass
                health.mark_clean_shutdown()
                logger.info("Бот остановлен.")

        # ─── Запуск ──────────────────────────────────────────────────────────

        # SIGTERM (хостинг останавливает/передеплоит) — это штатная остановка,
        # а не сбой: отмечаем, чтобы при следующем старте не было ложной тревоги
        try:
            import signal as _signal

            def _on_term(signum, frame):
                try:
                    from utils.health import mark_clean_shutdown
                    mark_clean_shutdown()
                finally:
                    os._exit(0)
            _signal.signal(_signal.SIGTERM, _on_term)
        except Exception:
            pass

        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("\nОстановка по Ctrl+C.")
        except SystemExit:
            # Если кто-то позвал /power_off → sys.exit; код выхода пробрасываем
            # наружу — по нему сторож решает, перезапускать ли бота
            raise

    except Exception:
        # Полный traceback с указанием файла и строки
        err = traceback.format_exc()
        try:
            from utils.health import record_crash
            record_crash("ошибка в работе бота", err)
        except Exception:
            pass
        _show_error_and_wait(err)
        sys.exit(1)


# ──────────────────────────────────────────────────────────────────────────────
# Сторож (supervisor): держит бота живым
# ──────────────────────────────────────────────────────────────────────────────
# python main.py запускает НЕ самого бота, а сторожа, который запускает бота
# дочерним процессом и перезапускает его, если тот упал (ошибка, обрыв сети,
# умер поток FunPay и т.п.). Коды выхода бота:
#   0 — штатная остановка (/power_off, Ctrl+C) → сторож тоже выходит
#   2 — бот не настроен (нет токена/пароля) → перезапуск бесполезен, выходим
#   остальное — падение → перезапуск с паузой (5с, 10с, 20с … до 2 мин)
# Отключить сторожа: переменная NO_SUPERVISOR=1 или флаг --no-supervisor.

EXIT_OK = 0
EXIT_NOT_CONFIGURED = 2


def _supervise() -> None:
    import signal
    import subprocess
    import time

    from utils.env_config import prepare_data_dir
    prepare_data_dir()

    script = str(Path(__file__).resolve())
    args = [sys.executable, "-u", script] + [a for a in sys.argv[1:] if a != "--no-supervisor"]
    env = dict(os.environ, FPB_CHILD="1", PYTHONUNBUFFERED="1")

    child = {"p": None}
    stopping = {"v": False}

    def _forward(signum, _frame):
        stopping["v"] = True
        p = child["p"]
        if p is not None and p.poll() is None:
            try:
                p.send_signal(signum)
            except Exception:
                pass

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is not None:
            try:
                signal.signal(sig, _forward)
            except Exception:
                pass

    delay = 5
    while True:
        started = time.time()
        print(f"[supervisor] Запуск бота ({time.strftime('%Y-%m-%d %H:%M:%S')})", flush=True)
        try:
            child["p"] = subprocess.Popen(args, env=env, cwd=str(Path(__file__).resolve().parent))
            code = child["p"].wait()
        except KeyboardInterrupt:
            stopping["v"] = True
            try:
                code = child["p"].wait(timeout=15) if child["p"] else 0
            except Exception:
                code = 0
        except Exception as e:
            print(f"[supervisor] Не удалось запустить бота: {e}", flush=True)
            code = 1

        if stopping["v"]:
            print("[supervisor] Остановлено.", flush=True)
            return
        if code == EXIT_OK:
            print("[supervisor] Бот остановлен штатно (код 0).", flush=True)
            return
        if code == EXIT_NOT_CONFIGURED:
            print("[supervisor] Бот не настроен — перезапуск не поможет. См. HOSTING.md", flush=True)
            sys.exit(EXIT_NOT_CONFIGURED)

        # Бот проработал долго — значит это разовый сбой, начинаем паузы заново
        if time.time() - started > 300:
            delay = 5
        try:
            from utils.health import describe_exit, record_child_exit
            record_child_exit(code)
            why = describe_exit(code)
        except Exception:
            why = f"код {code}"
        print(f"[supervisor] ⚠️ Бот упал: {why}. Перезапуск через {delay} с...", flush=True)
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            return
        delay = min(delay * 2, 120)


if __name__ == "__main__":
    _interactive_cli = any(a in sys.argv for a in (
        "--setup-proxy", "--proxy", "--setup-ua", "--ua", "--user-agent",
    ))
    if (os.environ.get("FPB_CHILD") or os.environ.get("NO_SUPERVISOR")
            or "--no-supervisor" in sys.argv or _interactive_cli):
        from utils.env_config import prepare_data_dir
        prepare_data_dir()
        _main_safe()
    else:
        _supervise()
