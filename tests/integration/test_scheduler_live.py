"""Live-PostgreSQL witness for the per-product transaction of ``app.scheduler``.

PAB-073 AC5. The unit tests pin the scheduler's behaviour against a fake
sessionmaker that repeats an assumption about SQLAlchemy ("``begin()`` rolls back
on an exception"); this module checks that assumption on a real database: a
product whose check wrote a row and then failed leaves that row unchanged, while
the neighbours' writes stay.

**Named limit of the technique.** The sessionmaker is built on the connection
that backs the ``db_session`` fixture, with ``join_transaction_mode=
"create_savepoint"``. Every product check therefore runs in a SAVEPOINT on one
connection -- it is not three pooled connections. The witness proves the
semantics of ``AsyncSession.begin()`` on PostgreSQL (commit releases the
savepoint, an exception rolls back to it), but not isolation between
connections. For the same reason only ``concurrency=1`` is used: concurrent
statements on one connection are not a model of the production pool.

The id query is the real ``ProductRepository`` (no substitution). Expected values
are read with column queries, not from ORM objects loaded before the pass, whose
identity map knows nothing about writes made by the scheduler's sessions.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest
from loguru import logger
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.product import Product
from app.scheduler import run_price_check_pass
from app.services.price_service import PriceService
from tests.integration.repositories.conftest import make_alert
from tests.integration.repositories.conftest import make_product
from tests.integration.repositories.conftest import make_user

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

_WAIT = 5.0  # hang guard only, never a synchronisation delay
_ORIGINAL_NAME = "before the pass"
_WRITTEN_NAME = "written by the check"


class _WritingService:
    """``check_product`` that writes the product's row and fails on the 2nd call."""

    def __init__(self, failure: BaseException) -> None:
        self._failure = failure
        self.order: list[int] = []

    async def check_product(self, product_id: int, session: AsyncSession) -> None:
        self.order.append(product_id)
        await session.execute(
            update(Product)
            .where(Product.id == product_id)
            .values(product_name=_WRITTEN_NAME)
        )
        await session.flush()
        if len(self.order) == 2:
            raise self._failure

    def as_service(self) -> PriceService:
        return cast(PriceService, self)


async def _seed_three_products(db_session: AsyncSession) -> set[int]:
    user = await make_user(db_session)
    ids: set[int] = set()
    for _ in range(3):
        product = await make_product(
            db_session, user_id=user.id, product_name=_ORIGINAL_NAME
        )
        await make_alert(db_session, user_id=user.id, product_id=product.id)
        ids.add(product.id)
    return ids


async def _names(db_session: AsyncSession, ids: set[int]) -> dict[int, str]:
    rows = await db_session.execute(
        select(Product.id, Product.product_name).where(Product.id.in_(ids))
    )
    return {row.id: row.product_name for row in rows}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "exception_type"),
    [
        (RuntimeError("boom"), RuntimeError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ],
    ids=["exception", "unrequested_cancellation"],
)
async def test_failed_check_rolls_back_only_its_own_write_on_postgresql(
    db_session: AsyncSession,
    failure: BaseException,
    exception_type: type[BaseException],
) -> None:
    """Mutation: remove ``session.begin()`` from ``_check_one`` (the failed
    product's flushed write would stay, or the neighbours' would be lost)."""
    ids = await _seed_three_products(db_session)
    connection = db_session.bind
    assert isinstance(connection, AsyncConnection)
    sessionmaker = async_sessionmaker(
        bind=connection,
        join_transaction_mode="create_savepoint",
        autoflush=False,
        expire_on_commit=False,
    )
    service = _WritingService(failure)
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    sink_id = logger.add(_sink, level=0)
    try:
        await asyncio.wait_for(
            run_price_check_pass(
                service.as_service(),
                sessionmaker,
                concurrency=1,
                stop_event=asyncio.Event(),
            ),
            _WAIT,
        )
    finally:
        logger.remove(sink_id)

    assert set(service.order) == ids
    first, failed, third = service.order
    names = await _names(db_session, ids)
    # Positive control: the neighbours' writes are visible through ``db_session``,
    # so the unchanged row of the failed product is a rollback, not a blind channel.
    assert names[first] == _WRITTEN_NAME
    assert names[third] == _WRITTEN_NAME
    assert names[failed] == _ORIGINAL_NAME

    errors = [r for r in records if r["level"].name == "ERROR"]
    assert len(errors) == 1
    assert errors[0]["extra"]["product_id"] == failed
    exception = errors[0]["exception"]
    assert exception is not None
    assert exception.type is exception_type

    summaries = [r for r in records if "products" in r["extra"]]
    assert len(summaries) == 1
    assert summaries[0]["level"].name == "INFO"
    assert summaries[0]["extra"] == {"products": 3, "checked": 3, "failures": 1}
