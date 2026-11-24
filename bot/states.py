"""FSM-состояния для онбординга и редактирования настроек."""
from aiogram.fsm.state import State, StatesGroup


class AuthStates(StatesGroup):
    """Авторизация в TG-боте."""
    waiting_password = State()


class SetupStates(StatesGroup):
    """Первоначальная настройка FunPay-подключения после авторизации."""
    waiting_golden_key = State()


class AccountStates(StatesGroup):
    """Добавление нового FunPay-аккаунта (мультиаккаунт)."""
    waiting_golden_key = State()
    waiting_alias = State()


class EditStates(StatesGroup):
    """Редактирование настраиваемых полей через меню."""
    editing_text = State()
    editing_int = State()
    editing_template_keyword = State()
    editing_template_response = State()
    editing_blacklist_nick = State()
    editing_ai_instruction = State()
    editing_ai_key = State()


class GoldenKeyStates(StatesGroup):
    """Быстрая смена golden_key с телефона."""
    waiting_key = State()
    confirm = State()


class LotTextStates(StatesGroup):
    """Тексты и цены лотов."""
    count = State()
    inc = State()
    template = State()
    time = State()
    extra_ids = State()
    exclude_ids = State()
    rep_find = State()
    rep_to = State()
    price_ids = State()
    price_word = State()
    price_op = State()
    confirm = State()
