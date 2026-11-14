"""A delayed confirmation cannot overwrite a later dialog."""

import asyncio
from unittest.mock import MagicMock

import pytest
from aiogram.methods import AnswerCallbackQuery
from aiogram.methods import SendMessage

from app.bot.keyboards import CB_ADD_CONFIRM
from app.bot.states import AddProduct
from app.bot.states import EditAlert
from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.exceptions import TargetEqualsCurrentPriceError
from app.models.alert import Alert
from app.models.enums import AlertDirection
from tests.unit.handlers.add_dialog_support import _CARD_ID
from tests.unit.handlers.add_dialog_support import _CONFIRMING_DATA
from tests.unit.handlers.add_dialog_support import _Dialog


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        None,
        DuplicateAlertError(),
        ProductLimitExceededError(50),
        MarketplaceNotSupportedError("wb"),
        TargetEqualsCurrentPriceError(),
    ],
    ids=["success", "duplicate", "limit", "unsupported", "equal"],
)
@pytest.mark.parametrize(
    "new_dialog", ["add", "confirm", "edit", "cleared", "price_retry"]
)
async def test_delayed_confirmation_preserves_new_dialog(
    card_dialog: _Dialog,
    card_service: MagicMock,
    error: Exception | None,
    new_dialog: str,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed(*args: object, **kwargs: object) -> Alert:
        entered.set()
        await release.wait()
        if error is not None:
            raise error
        return Alert(
            id=1,
            user_id=1,
            product_id=1,
            target_price=150_000,
            direction=AlertDirection.below,
        )

    card_service.add_tracking.side_effect = delayed
    await card_dialog.set_state(AddProduct.confirming.state, _CONFIRMING_DATA.copy())
    task = asyncio.create_task(card_dialog.press(CB_ADD_CONFIRM, message_id=_CARD_ID))
    try:
        async with asyncio.timeout(5):
            await entered.wait()
            if new_dialog == "confirm":
                await card_dialog.set_state(
                    AddProduct.confirming.state,
                    {
                        **_CONFIRMING_DATA,
                        "confirm_message_id": _CARD_ID + 1,
                        "product_name": "Новый товар",
                    },
                )
            elif new_dialog == "add":
                await card_dialog.command("add")
                assert await card_dialog.state() == AddProduct.waiting_link.state
            elif new_dialog == "edit":
                await card_dialog.set_state(
                    EditAlert.waiting_target_price.state,
                    {"product_id": 47, "alert_id": 48},
                )
            elif new_dialog == "price_retry":
                await card_dialog.set_state(
                    AddProduct.waiting_target_price.state, _CONFIRMING_DATA.copy()
                )
            else:
                await card_dialog.set_state(None)
            expected_state = await card_dialog.state()
            expected_data = await card_dialog.data()
            base = len(card_dialog.requests)
            release.set()
            await task
        assert await card_dialog.state() == expected_state
        assert await card_dialog.data() == expected_data
        requests = card_dialog.requests[base:]
        answers = [r for r in requests if isinstance(r, AnswerCallbackQuery)]
        assert len(answers) == 1
        sent = [r for r in requests if isinstance(r, SendMessage)]
        if error is None:
            assert len(sent) == 1
            assert sent[0].text == (
                "Отслеживание добавлено: Кроссовки <b>x*.\n"
                "Сообщу, когда цена станет ниже 1\xa0500\xa0₽.\n"
                "Цена без учёта персональных скидок.\n\n"
                "Чтобы не пропустить сигнал о цене, включите уведомления от этого чата."
            )
            assert sent[0].reply_markup is None
            assert len(requests) == 2
        else:
            assert requests == answers
    finally:
        cancelled_by_cleanup = not task.done()
        if cancelled_by_cleanup:
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if not cancelled_by_cleanup or (
                current is not None and current.cancelling()
            ):
                raise
