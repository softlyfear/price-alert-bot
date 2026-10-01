"""Tests for app.services.notification (PAB-069 AC1-AC3, Р5, Р7).

The boundary is the Telegram Bot API: a real ``aiogram.Bot`` over the fake
session from ``tests/unit/conftest.py``. Expected message texts are literals
written out by hand (``\\xa0`` is the U+00A0 separator of ``format_price``).
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.methods import TelegramMethod
from loguru import logger

import app.services.notification as notification_module
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from app.services.notification import AlertDeliveryError
from app.services.notification import NotificationService
from tests.unit.conftest import FakeTelegramSession
from tests.unit.conftest import build_fake_bot
from tests.unit.conftest import send_message_responder

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_TG_USER_ID = 100500
_SECRET_MARKER = "bot123456:SECRET-TOKEN-MARKER"
_FIXED_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class _FrozenClock:
    """Stand-in for the ``datetime`` name in ``app.services.notification``."""

    def now(self, tz: object = None) -> datetime:
        return _FIXED_NOW


def _product(marketplace: Marketplace = Marketplace.wb) -> Product:
    return Product(
        id=7,
        user_id=3,
        marketplace=marketplace,
        article=860043555,
        product_name="Кроссовки Nike",
        current_price=142900,
    )


def _alert(direction: AlertDirection, target_price: int = 120000) -> Alert:
    return Alert(
        id=11,
        user_id=3,
        product_id=7,
        target_price=target_price,
        direction=direction,
        is_active=True,
        triggered_at=None,
    )


def _user() -> User:
    return User(id=3, tg_user_id=_TG_USER_ID, tg_username="user")


def _sent(bot: Any) -> list[SendMessage]:
    session: FakeTelegramSession = bot.session
    return [m for m in session.requests if isinstance(m, SendMessage)]


def _capture_logs() -> tuple[list[Record], int]:
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    return records, logger.add(_sink, level=0)


def _failing_bot(exc_factory: Any) -> Any:
    async def responder(method: TelegramMethod[Any]) -> Any:
        raise exc_factory(method)

    return build_fake_bot(responder=responder)


_DISCLAIMER = "Цена без учёта персональных скидок"
_WB_URL = "https://www.wildberries.ru/catalog/860043555/detail.aspx"
_OZON_URL = "https://www.ozon.ru/product/860043555/"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("direction", "marketplace", "expected_text"),
    [
        (
            AlertDirection.below,
            Marketplace.wb,
            "↘ Цена снизилась\n"
            "Товар: Кроссовки Nike\n"
            "Артикул: 860043555\n"
            "Текущая цена: 1\xa0429\xa0₽\n"
            "Порог: не выше 1\xa0200\xa0₽\n"
            f"{_DISCLAIMER}.\n"
            f"Ссылка: {_WB_URL}",
        ),
        (
            AlertDirection.below,
            Marketplace.ozon,
            "↘ Цена снизилась\n"
            "Товар: Кроссовки Nike\n"
            "Артикул: 860043555\n"
            "Текущая цена: 1\xa0429\xa0₽\n"
            "Порог: не выше 1\xa0200\xa0₽\n"
            f"{_DISCLAIMER}.\n"
            f"Ссылка: {_OZON_URL}",
        ),
        (
            AlertDirection.above,
            Marketplace.wb,
            "↗ Цена выросла\n"
            "Товар: Кроссовки Nike\n"
            "Артикул: 860043555\n"
            "Текущая цена: 1\xa0429\xa0₽\n"
            "Порог: не ниже 1\xa0200\xa0₽\n"
            f"{_DISCLAIMER}.\n"
            f"Ссылка: {_WB_URL}",
        ),
        (
            AlertDirection.above,
            Marketplace.ozon,
            "↗ Цена выросла\n"
            "Товар: Кроссовки Nike\n"
            "Артикул: 860043555\n"
            "Текущая цена: 1\xa0429\xa0₽\n"
            "Порог: не ниже 1\xa0200\xa0₽\n"
            f"{_DISCLAIMER}.\n"
            f"Ссылка: {_OZON_URL}",
        ),
    ],
)
async def test_send_alert_sends_exact_text_for_every_direction_and_marketplace(
    direction: AlertDirection, marketplace: Marketplace, expected_text: str
) -> None:
    """Mutations: swap current/target price, drop the disclaimer, build the URL
    for the wrong marketplace, swap the direction headline."""
    bot = build_fake_bot(responder=send_message_responder())

    await NotificationService(bot).send_alert(
        _alert(direction), _product(marketplace), _user()
    )

    sent = _sent(bot)
    assert len(sent) == 1
    assert sent[0].chat_id == _TG_USER_ID
    assert sent[0].text == expected_text


@pytest.mark.asyncio
async def test_send_alert_passes_parse_mode_none_explicitly() -> None:
    """Mutation: drop ``parse_mode=None`` -- the method then carries aiogram's
    ``Default("parse_mode")`` marker and a future Bot default would apply to
    a marketplace-supplied product name (Р7)."""
    bot = build_fake_bot(responder=send_message_responder())

    await NotificationService(bot).send_alert(
        _alert(AlertDirection.below), _product(), _user()
    )

    assert _sent(bot)[0].parse_mode is None


@pytest.mark.asyncio
async def test_send_alert_success_marks_triggered_at_utc_and_logs_one_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: do not set ``triggered_at``, or set it before the send."""
    monkeypatch.setattr(notification_module, "datetime", _FrozenClock())
    bot = build_fake_bot(responder=send_message_responder())
    alert = _alert(AlertDirection.below)
    records, sink_id = _capture_logs()

    try:
        await NotificationService(bot).send_alert(alert, _product(), _user())
    finally:
        logger.remove(sink_id)

    assert alert.triggered_at == _FIXED_NOW
    assert alert.triggered_at is not None
    assert alert.triggered_at.utcoffset() is not None
    assert len(records) == 1
    assert records[0]["level"].name == "INFO"
    assert records[0]["extra"]["alert_id"] == 11
    assert records[0]["extra"]["product_id"] == 7
    assert records[0]["extra"]["user_id"] == 3


@pytest.mark.asyncio
async def test_send_alert_blocked_user_is_skipped_with_one_warning_and_no_mark() -> (
    None
):
    """Mutation: let ``TelegramForbiddenError`` fall into the generic branch
    (it would raise) or mark ``triggered_at`` anyway (section 2.5)."""
    bot = _failing_bot(
        lambda method: TelegramForbiddenError(
            method=method, message="Forbidden: bot was blocked by the user"
        )
    )
    alert = _alert(AlertDirection.above)
    records, sink_id = _capture_logs()

    try:
        await NotificationService(bot).send_alert(alert, _product(), _user())
    finally:
        logger.remove(sink_id)

    assert alert.triggered_at is None
    assert len(records) == 1
    assert records[0]["level"].name == "WARNING"
    assert records[0]["extra"]["alert_id"] == 11
    assert records[0]["extra"]["product_id"] == 7
    assert records[0]["extra"]["user_id"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc_factory", "error_type"),
    [
        (
            lambda method: TelegramNetworkError(method=method, message=_SECRET_MARKER),
            "TelegramNetworkError",
        ),
        (lambda method: RuntimeError(_SECRET_MARKER), "RuntimeError"),
    ],
    ids=["telegram-network-error", "runtime-error"],
)
async def test_send_alert_other_failure_raises_unchained_error_and_logs_type_only(
    exc_factory: Any, error_type: str
) -> None:
    """Mutations: ``from exc`` / bare ``raise`` (chain carries the token text),
    ``str(exc)`` in the log call, ``logger.exception`` (traceback text)."""
    bot = _failing_bot(exc_factory)
    alert = _alert(AlertDirection.below)
    records, sink_id = _capture_logs()

    try:
        with pytest.raises(AlertDeliveryError) as exc_info:
            await NotificationService(bot).send_alert(alert, _product(), _user())
    finally:
        logger.remove(sink_id)

    raised = exc_info.value
    assert raised.__cause__ is None
    assert raised.__suppress_context__ is True
    assert _SECRET_MARKER not in str(raised)
    assert alert.triggered_at is None

    assert len(records) == 1
    record = records[0]
    assert record["level"].name == "ERROR"
    assert record["extra"]["alert_id"] == 11
    assert record["extra"]["product_id"] == 7
    assert record["extra"]["user_id"] == 3
    assert record["extra"]["error_type"] == error_type
    assert record["exception"] is None
    assert _SECRET_MARKER not in record["message"]
    assert _SECRET_MARKER not in repr(record["extra"])


@pytest.mark.asyncio
async def test_send_alert_rejects_an_unknown_direction_before_sending() -> None:
    """The ``match`` has no default branch: a direction added to the enum
    without a text must fail loudly, not send a half-built message."""
    bot = build_fake_bot(responder=send_message_responder())
    alert = _alert(AlertDirection.below)
    alert.direction = cast(AlertDirection, "sideways")

    with pytest.raises(AssertionError):
        await NotificationService(bot).send_alert(alert, _product(), _user())

    assert _sent(bot) == []
    assert alert.triggered_at is None


def test_threshold_label_has_no_default_branch_for_an_unknown_direction() -> None:
    """``_headline`` runs first inside ``send_alert`` and already rejects an
    unknown direction, so this second ``assert_never`` is unreachable through the
    public method; it is exercised directly to keep the branch honest."""
    with pytest.raises(AssertionError):
        notification_module._threshold_label(cast(AlertDirection, "sideways"))
