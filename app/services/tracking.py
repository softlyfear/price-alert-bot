"""Tracking service: preview a product and start tracking its price."""

from collections.abc import Callable
from typing import assert_never

import httpx
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.services.base_client import BaseMarketplaceClient
from app.services.client_factory import UnsupportedMarketplaceError


def _failure_level(reason: FetchFailureReason) -> str:
    match reason:
        case FetchFailureReason.bad_payload:
            return "ERROR"
        case FetchFailureReason.out_of_stock:
            return "INFO"
        case (
            FetchFailureReason.blocked
            | FetchFailureReason.transport_error
            | FetchFailureReason.not_found
        ):
            return "WARNING"
        case _:
            assert_never(reason)


class TrackingService:
    """Orchestrates adding a product to tracking.

    Does not commit or roll back: the transaction belongs to the caller
    (bot middleware). Duplicates are resolved with a SAVEPOINT per insert.
    """

    def __init__(
        self,
        session: AsyncSession,
        http_client: httpx.AsyncClient,
        client_factory: Callable[
            [Marketplace, httpx.AsyncClient], BaseMarketplaceClient
        ],
        max_products_per_user: int,
    ) -> None:
        self._session = session
        self._http_client = http_client
        self._client_factory = client_factory
        self._max_products = max_products_per_user
        self._users = UserRepository(session)
        self._products = ProductRepository(session)
        self._alerts = AlertRepository(session)

    async def preview(
        self, tg_user_id: int, marketplace: Marketplace, article: int
    ) -> MarketplaceFetchResult:
        """Fetch current product data for the dialog; failures are values."""
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        await self._ensure_within_limit(user.id, marketplace, article)
        try:
            client = self._client_factory(marketplace, self._http_client)
        except UnsupportedMarketplaceError as exc:
            raise MarketplaceNotSupportedError(str(marketplace)) from exc
        result = await client.get_product_data(article)
        if isinstance(result, MarketplaceFetchFailure):
            logger.bind(
                reason=str(result.reason),
                marketplace=str(marketplace),
                article=article,
            ).log(_failure_level(result.reason), "Marketplace fetch failed")
        return result

    async def add_tracking(
        self,
        tg_user_id: int,
        marketplace: Marketplace,
        article: int,
        product_name: str,
        current_price: int,
        target_price: int,
    ) -> Alert:
        """Create (or reuse) the product and add a `below` alert for it."""
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        await self._ensure_within_limit(user.id, marketplace, article)

        product = await self._products.get_by_article_and_user(
            article, user.id, marketplace
        )
        if product is None:
            product = await self._create_product(
                user.id, marketplace, article, product_name, current_price
            )
        return await self._create_alert(user.id, product.id, target_price)

    async def _ensure_within_limit(
        self, user_id: int, marketplace: Marketplace, article: int
    ) -> None:
        """Raise when at the limit, unless the product is already counted."""
        if await self._products.count_active_by_user(user_id) < self._max_products:
            return
        product = await self._products.get_by_article_and_user(
            article, user_id, marketplace
        )
        if product is not None:
            alerts = await self._alerts.get_by_user_and_product(user_id, product.id)
            if any(alert.is_active for alert in alerts):
                return
        raise ProductLimitExceededError(self._max_products)

    async def _create_product(
        self,
        user_id: int,
        marketplace: Marketplace,
        article: int,
        product_name: str,
        current_price: int,
    ) -> Product:
        try:
            async with self._session.begin_nested():
                return await self._products.create(
                    {
                        "user_id": user_id,
                        "marketplace": marketplace,
                        "article": article,
                        "product_name": product_name,
                        "current_price": current_price,
                    }
                )
        except IntegrityError:
            existing = await self._products.get_by_article_and_user(
                article, user_id, marketplace
            )
            if existing is None:
                raise
            return existing

    async def _create_alert(
        self, user_id: int, product_id: int, target_price: int
    ) -> Alert:
        if await self._find_duplicate(user_id, product_id, target_price):
            raise DuplicateAlertError
        try:
            async with self._session.begin_nested():
                return await self._alerts.create(
                    {
                        "user_id": user_id,
                        "product_id": product_id,
                        "target_price": target_price,
                        "direction": AlertDirection.below,
                    }
                )
        except IntegrityError as exc:
            if await self._find_duplicate(user_id, product_id, target_price):
                raise DuplicateAlertError from exc
            raise

    async def _find_duplicate(
        self, user_id: int, product_id: int, target_price: int
    ) -> bool:
        alerts = await self._alerts.get_by_user_and_product(user_id, product_id)
        return any(
            alert.direction is AlertDirection.below
            and alert.target_price == target_price
            for alert in alerts
        )
