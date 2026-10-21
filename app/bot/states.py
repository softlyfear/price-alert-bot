"""FSM state groups of the Telegram bot."""

from aiogram.fsm.state import State
from aiogram.fsm.state import StatesGroup


class AddProduct(StatesGroup):
    """Steps of the /add dialog."""

    waiting_link = State()
    waiting_target_price = State()
    confirming = State()


class EditAlert(StatesGroup):
    """The single step of changing a threshold from the product card."""

    waiting_target_price = State()
