"""A size notification uses its label or its numeric fallback."""

import pytest
from aiogram.methods import SendMessage

from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from app.services.notification import NotificationService
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import send_message_responder
from tests.unit.services.test_notification import _keyboard
from tests.unit.services.test_notification import _sent


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "expected"), [("54-188", "54-188"), (None, "42")])
async def test_notification_identifies_selected_size(
    name: str | None, expected: str
) -> None:
    bot = build_fake_bot(responder=send_message_responder())
    product = Product(
        id=7,
        user_id=3,
        marketplace=Marketplace.wb,
        article=123,
        product_name="Item",
        current_price=8500,
        wb_size_id=42,
        wb_size_name=name,
    )
    alert = Alert(
        id=11,
        user_id=3,
        product_id=7,
        target_price=9000,
        direction=AlertDirection.below,
    )
    await NotificationService(bot, alert_keyboard=_keyboard).send_alert(
        alert, product, User(id=3, tg_user_id=100)
    )
    messages: list[SendMessage] = _sent(bot)
    assert len(messages) == 1
    assert f"Размер: {expected}\n" in messages[0].text
    assert (
        "https://www.wildberries.ru/catalog/123/detail.aspx?size=42" in messages[0].text
    )
