"""
Настройка бота через переменные окружения (для хостингов без консоли: BotHost и т.п.).

На хостинге нельзя ввести токен/пароль в консоль при первом запуске, поэтому
всё базовое можно задать переменными окружения в панели хостинга:

    BOT_TOKEN        — токен Telegram-бота от @BotFather            (обязательно)
    ADMIN_PASSWORD   — пароль для входа в бота (/start → пароль)     (обязательно)
    ADMIN_IDS        — твой Telegram ID (или несколько через запятую) — сразу админ, без пароля
    GOLDEN_KEY       — golden_key от FunPay (32 символа)
    FUNPAY_PROXY     — прокси login:pass@ip:port (необязательно)
    USER_AGENT       — User-Agent браузера (необязательно)
    DATA_DIR         — папка постоянного хранилища (если хостинг даёт диск, который
                       не стирается при передеплое; см. prepare_data_dir)
    UPDATE_REPO      — owner/repo для команды /update (по умолчанию твой форк)

Если переменная задана — она ПОБЕЖДАЕТ значение из data/config.json при каждом старте.
Если не задана — используется то, что сохранено в конфиге (например, через меню бота).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import List

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env(*names: str) -> str:
    for n in names:
        v = os.environ.get(n)
        if v is not None and v.strip():
            return v.strip()
    return ""


def prepare_data_dir() -> None:
    """
    Если задан DATA_DIR — делаем ./data симлинком на него.

    Весь код бота пишет в ./data (конфиг, ключи автовыдачи, аналитика, логи),
    поэтому проще всего перенаправить саму папку. Вызывать ДО импорта
    config.settings. Безопасно вызывать несколько раз.
    """
    target_raw = _env("DATA_DIR")
    if not target_raw:
        return
    target = Path(target_raw).expanduser().resolve()
    local = PROJECT_ROOT / "data"
    try:
        target.mkdir(parents=True, exist_ok=True)
        if local.is_symlink():
            if local.resolve() == target:
                return
            local.unlink()
        elif local.exists():
            if local.resolve() == target:
                return
            # Переносим то, чего ещё нет в постоянном хранилище (например .gitkeep
            # или конфиг, созданный до того, как задали DATA_DIR)
            for item in local.iterdir():
                dst = target / item.name
                if not dst.exists():
                    shutil.move(str(item), str(dst))
            shutil.rmtree(local, ignore_errors=True)
        local.symlink_to(target, target_is_directory=True)
        print(f"[env] data/ → {target}")
    except Exception as e:  # не валим бота из-за этого — просто работаем с ./data
        print(f"[env] Не удалось подключить DATA_DIR={target}: {e}")


def _parse_ids(raw: str) -> List[int]:
    ids: List[int] = []
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


def apply_env_overrides(logger=None) -> bool:
    """
    Переносит значения из переменных окружения в config_manager.settings.
    Возвращает True, если что-то поменялось (и конфиг сохранён).
    """
    from config.settings import DEFAULT_USER_AGENT, FunPayAccountSettings, config_manager
    from utils.passwords import hash_password, verify_password

    def log(msg: str) -> None:
        if logger is not None:
            logger.info(msg)
        else:
            print(msg)

    s = config_manager.settings
    tg = s.telegram
    fp = s.funpay
    changed = False

    token = _env("BOT_TOKEN", "TELEGRAM_TOKEN", "TOKEN")
    if token and token != tg.token:
        tg.token = token
        changed = True
        log("[env] Токен Telegram взят из BOT_TOKEN")

    pwd = _env("ADMIN_PASSWORD")
    if pwd and not verify_password(pwd, tg.admin_password):
        tg.admin_password = hash_password(pwd)
        changed = True
        log("[env] Пароль админа взят из ADMIN_PASSWORD")

    ids = _parse_ids(_env("ADMIN_IDS", "ADMIN_ID"))
    for i in ids:
        if i not in tg.admin_ids:
            tg.admin_ids.append(i)
            changed = True
            log(f"[env] Добавлен админ из ADMIN_IDS: {i}")

    proxy = _env("FUNPAY_PROXY", "PROXY")
    if proxy and proxy != fp.proxy:
        fp.proxy = proxy
        changed = True
        log("[env] Прокси взят из FUNPAY_PROXY")

    ua = _env("USER_AGENT")
    if ua and ua != fp.user_agent:
        fp.user_agent = ua
        changed = True
        log("[env] User-Agent взят из USER_AGENT")
    if not fp.user_agent or "python-requests" in fp.user_agent:
        fp.user_agent = DEFAULT_USER_AGENT
        changed = True

    gk = _env("GOLDEN_KEY", "FUNPAY_GOLDEN_KEY")
    if gk and gk != fp.golden_key:
        fp.golden_key = gk
        changed = True
        log("[env] golden_key взят из GOLDEN_KEY")
    if fp.golden_key and not fp.accounts:
        fp.accounts = [FunPayAccountSettings(
            golden_key=fp.golden_key,
            proxy=fp.proxy,
            user_agent=fp.user_agent,
        )]
        fp.active_index = 0
        changed = True

    repo = _env("UPDATE_REPO")
    if repo and repo != s.update.repo:
        s.update.repo = repo
        changed = True

    if changed:
        config_manager.save()
    return changed
