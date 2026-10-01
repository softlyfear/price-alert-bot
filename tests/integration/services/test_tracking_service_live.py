"""TrackingService against a real PostgreSQL (PAB-068 AC8, live-database part).

The SAVEPOINT-per-insert design only means something on a real database:
whether the transaction stays usable after a caught ``IntegrityError`` and
whether the constraints really fire cannot be shown on mocks. Isolation comes
from the ``db_session`` fixture (``tests/integration/conftest.py``): every
test runs in an outer transaction that is rolled back afterwards.

The WB client is replaced by a recording factory: these tests are about the
database, and the marketplace boundary is covered by the client's own tests.
"""

from __future__ import annotations

import itertools
from collections.abc import Awaitable
from collections.abc import Callable
from typing import Any
from typing import cast

import httpx
import pytest
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import ProductLimitExceededError
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import MarketplaceProductData
from app.services.base_client import BaseMarketplaceClient
from app.services.tracking import TrackingService

pytestmark = pytest.mark.asyncio

_WB = Marketplace.wb
_tg_ids = itertools.count(700_000_001)
_articles = itertools.count(5_000_001)


class _CountingClient(BaseMarketplaceClient):
    def __init__(self) -> None:
        self.fetches = 0

    async def get_product_data(self, article: int) -> MarketplaceProductData:
        self.fetches += 1
        return MarketplaceProductData(name="Live item", price=15_000)


def _service(
    session: AsyncSession, *, limit: int = 50, client: _CountingClient | None = None
) -> TrackingService:
    fake = client or _CountingClient()
    return TrackingService(
        session=session,
        http_client=httpx.AsyncClient(),
        client_factory=lambda _marketplace, _http: fake,
        max_products_per_user=limit,
    )


async def _counts_for_user(session: AsyncSession, tg_user_id: int) -> tuple[int, int]:
    user_id = (
        await session.execute(select(User.id).where(User.tg_user_id == tg_user_id))
    ).scalar_one()
    products = (
        await session.execute(
            select(func.count()).select_from(Product).where(Product.user_id == user_id)
        )
    ).scalar_one()
    alerts = (
        await session.execute(
            select(func.count()).select_from(Alert).where(Alert.user_id == user_id)
        )
    ).scalar_one()
    return products, alerts


async def _add(
    service: TrackingService,
    tg_user_id: int,
    article: int,
    *,
    target: int = 9_000,
    name: str = "Live item",
    price: int = 15_000,
) -> Alert:
    return await service.add_tracking(tg_user_id, _WB, article, name, price, target)


async def test_add_tracking_persists_product_and_active_below_alert(
    db_session: AsyncSession,
) -> None:
    tg, article = next(_tg_ids), next(_articles)

    alert = await _add(
        _service(db_session), tg, article, target=9_000, name="Кроссовки", price=15_000
    )

    assert alert.is_active is True
    assert alert.direction is AlertDirection.below
    assert alert.target_price == 9_000
    assert alert.triggered_at is None
    product = (
        await db_session.execute(select(Product).where(Product.id == alert.product_id))
    ).scalar_one()
    assert (product.marketplace, product.article) == (_WB, article)
    assert (product.product_name, product.current_price) == ("Кроссовки", 15_000)
    user = (
        await db_session.execute(select(User).where(User.tg_user_id == tg))
    ).scalar_one()
    assert product.user_id == user.id
    assert alert.user_id == user.id


async def test_second_threshold_reuses_product_without_overwriting_it(
    db_session: AsyncSession,
) -> None:
    tg, article = next(_tg_ids), next(_articles)
    service = _service(db_session)
    first = await _add(service, tg, article, target=9_000, name="First", price=15_000)

    second = await _add(service, tg, article, target=8_000, name="Other", price=1)

    assert second.product_id == first.product_id
    assert await _counts_for_user(db_session, tg) == (1, 2)
    product = (
        await db_session.execute(select(Product).where(Product.id == first.product_id))
    ).scalar_one()
    assert (product.product_name, product.current_price) == ("First", 15_000)


async def test_duplicate_alert_raises_and_the_transaction_stays_usable(
    db_session: AsyncSession,
) -> None:
    tg, article = next(_tg_ids), next(_articles)
    service = _service(db_session)
    await _add(service, tg, article, target=9_000)

    with pytest.raises(DuplicateAlertError):
        await _add(service, tg, article, target=9_000)

    # The same session still executes statements and commits ...
    assert await _counts_for_user(db_session, tg) == (1, 1)
    await db_session.commit()
    # ... and the user row registered before the duplicate survived.
    users = (
        await db_session.execute(select(func.count()).where(User.tg_user_id == tg))
    ).scalar_one()
    assert users == 1


async def _first_call_sees_nothing(
    monkeypatch: pytest.MonkeyPatch, owner: type, method: str, empty: Any
) -> None:
    """Make the first call of ``owner.method`` return ``empty``, later ones real."""
    real = cast(Callable[..., Awaitable[Any]], getattr(owner, method))
    calls: list[None] = []

    async def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(None)
        if len(calls) == 1:
            return empty
        return await real(self, *args, **kwargs)

    monkeypatch.setattr(owner, method, wrapper)


async def test_alert_race_is_translated_to_duplicate_error_not_integrity_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pre-check blind (a concurrent request committed in the gap): the
    unique constraint fires inside the SAVEPOINT and is reported as a duplicate."""
    tg, article = next(_tg_ids), next(_articles)
    service = _service(db_session)
    await _add(service, tg, article, target=9_000)
    await _first_call_sees_nothing(
        monkeypatch, AlertRepository, "get_by_user_and_product", []
    )

    with pytest.raises(DuplicateAlertError) as excinfo:
        await _add(service, tg, article, target=9_000)

    assert isinstance(excinfo.value.__cause__, IntegrityError)
    assert await _counts_for_user(db_session, tg) == (1, 1)
    await db_session.commit()


async def test_product_race_reuses_the_winning_row(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tg, article = next(_tg_ids), next(_articles)
    service = _service(db_session)
    first = await _add(service, tg, article, target=9_000, name="Winner", price=15_000)
    await _first_call_sees_nothing(
        monkeypatch, ProductRepository, "get_by_article_and_user", None
    )

    second = await _add(service, tg, article, target=8_000, name="Loser", price=2)

    assert second.product_id == first.product_id
    assert await _counts_for_user(db_session, tg) == (1, 2)
    product = (
        await db_session.execute(select(Product).where(Product.id == first.product_id))
    ).scalar_one()
    assert product.product_name == "Winner"
    await db_session.commit()


async def test_foreign_key_violation_on_alert_is_not_reported_as_duplicate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A product deleted by a concurrent transaction after the lookup: the
    alert insert hits the FK, which is not a duplicate and must surface as is."""
    tg, article = next(_tg_ids), next(_articles)
    service = _service(db_session)
    vanished = Product(id=2_000_000_000, user_id=1, marketplace=_WB, article=article)

    async def lookup(self: Any, *args: Any, **kwargs: Any) -> Product:
        return vanished

    monkeypatch.setattr(ProductRepository, "get_by_article_and_user", lookup)

    with pytest.raises(IntegrityError) as excinfo:
        await _add(service, tg, article)

    assert "foreign key" in str(excinfo.value).lower()
    # The SAVEPOINT was rolled back: the session is usable, the user row stays.
    assert await _counts_for_user(db_session, tg) == (0, 0)
    await db_session.commit()


async def test_foreign_key_violation_on_product_is_not_masked_as_a_race(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    tg, article = next(_tg_ids), next(_articles)
    service = _service(db_session)

    async def ghost_user(self: Any, tg_user_id: int) -> tuple[User, bool]:
        return User(id=2_000_000_000, tg_user_id=tg_user_id), True

    monkeypatch.setattr(UserRepository, "get_or_create_by_tg_id", ghost_user)

    with pytest.raises(IntegrityError) as excinfo:
        await _add(service, tg, article)

    assert "foreign key" in str(excinfo.value).lower()
    leftovers = (
        await db_session.execute(
            select(func.count()).select_from(Product).where(Product.article == article)
        )
    ).scalar_one()
    assert leftovers == 0
    await db_session.commit()


async def test_another_user_adding_the_same_article_gets_separate_rows(
    db_session: AsyncSession,
) -> None:
    tg_a, tg_b, article = next(_tg_ids), next(_tg_ids), next(_articles)
    service = _service(db_session)
    alert_a = await _add(service, tg_a, article, target=9_000)

    alert_b = await _add(service, tg_b, article, target=9_000)

    assert alert_b.product_id != alert_a.product_id
    assert alert_b.user_id != alert_a.user_id
    assert await _counts_for_user(db_session, tg_a) == (1, 1)
    assert await _counts_for_user(db_session, tg_b) == (1, 1)


async def test_limit_counts_only_the_callers_products(
    db_session: AsyncSession,
) -> None:
    tg_a, tg_b = next(_tg_ids), next(_tg_ids)
    service = _service(db_session, limit=2)
    await _add(service, tg_a, next(_articles))
    await _add(service, tg_a, next(_articles))

    # Another user is not affected by A reaching the limit.
    await _add(service, tg_b, next(_articles))

    with pytest.raises(ProductLimitExceededError) as excinfo:
        await _add(service, tg_a, next(_articles))

    assert excinfo.value.limit == 2
    assert await _counts_for_user(db_session, tg_a) == (2, 2)


async def test_at_limit_another_threshold_on_a_counted_product_is_allowed(
    db_session: AsyncSession,
) -> None:
    tg, article = next(_tg_ids), next(_articles)
    service = _service(db_session, limit=1)
    await _add(service, tg, article, target=9_000)

    await _add(service, tg, article, target=8_000)

    assert await _counts_for_user(db_session, tg) == (1, 2)


async def test_preview_at_limit_refuses_before_any_marketplace_request(
    db_session: AsyncSession,
) -> None:
    tg = next(_tg_ids)
    client = _CountingClient()
    service = _service(db_session, limit=1, client=client)
    await _add(service, tg, next(_articles))

    with pytest.raises(ProductLimitExceededError):
        await service.preview(tg, _WB, next(_articles))

    assert client.fetches == 0


async def test_preview_registers_the_user_and_returns_the_client_result(
    db_session: AsyncSession,
) -> None:
    tg = next(_tg_ids)
    client = _CountingClient()
    service = _service(db_session, client=client)

    result = await service.preview(tg, _WB, next(_articles))

    assert result == MarketplaceProductData(name="Live item", price=15_000)
    assert client.fetches == 1
    users = (
        await db_session.execute(select(func.count()).where(User.tg_user_id == tg))
    ).scalar_one()
    assert users == 1
