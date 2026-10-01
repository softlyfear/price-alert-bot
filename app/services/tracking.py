"""Tracking service: preview a product and start tracking its price."""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import httpx
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import DuplicateAlertError
from app.domain.exceptions import MarketplaceNotSupportedError
from app.domain.exceptions import ProductLimitExceededError
from app.domain.thresholds import choose_direction
from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.services.base_client import BaseMarketplaceClient
from app.services.client_factory import UnsupportedMarketplaceError
from app.services.failure_levels import failure_log_level


@dataclass(frozen=True, slots=True)
class ProductCard:
    """A product together with its alerts, sorted by `target_price`."""

    product: Product
    alerts: tuple[Alert, ...]


class AlertRemoval(StrEnum):
    """Outcome of removing a single alert."""

    not_found = "not_found"
    removed = "removed"
    removed_with_product = "removed_with_product"


class TrackingService:
    """Orchestrates adding a product to tracking and managing the list.

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
            ).log(failure_log_level(result.reason), "Marketplace fetch failed")
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
        """Create (or reuse) the product and add an alert for it.

        The direction follows from the threshold versus `current_price`;
        an equal threshold raises `TargetEqualsCurrentPriceError` before
        any database access.
        """
        direction = choose_direction(current_price, target_price)
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        await self._ensure_within_limit(user.id, marketplace, article)

        product = await self._products.get_by_article_and_user(
            article, user.id, marketplace
        )
        if product is None:
            product = await self._create_product(
                user.id, marketplace, article, product_name, current_price
            )
        return await self._create_alert(user.id, product.id, target_price, direction)

    async def list_products(self, tg_user_id: int) -> list[Product]:
        """Return the caller's products ordered by `id` ascending."""
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        products = await self._products.get_by_user_id(user.id)
        return sorted(products, key=lambda product: product.id)

    async def get_product_card(
        self, tg_user_id: int, product_id: int
    ) -> ProductCard | None:
        """Return the caller's product with its alerts, or None.

        A foreign and a missing `product_id` are indistinguishable.
        """
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        product = await self._products.get_by_id_for_user(product_id, user.id)
        if product is None:
            return None
        alerts = await self._alerts.get_by_user_and_product(user.id, product.id)
        return ProductCard(
            product=product,
            alerts=tuple(sorted(alerts, key=lambda alert: alert.target_price)),
        )

    async def remove_product(self, tg_user_id: int, product_id: int) -> bool:
        """Delete the caller's product; its alerts go with it (DB cascade)."""
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        return await self._products.delete_for_user(product_id, user.id)

    async def remove_alert(self, tg_user_id: int, alert_id: int) -> AlertRemoval:
        """Delete the caller's alert; drop the product with its last alert.

        Runs in the caller's transaction, so the alert and product removal
        commit or roll back together.
        """
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        alert = await self._alerts.get_by_id_for_user(alert_id, user.id)
        if alert is None:
            return AlertRemoval.not_found
        product_id = alert.product_id
        if not await self._alerts.delete_for_user(alert_id, user.id):
            return AlertRemoval.not_found
        remaining = await self._alerts.get_by_user_and_product(user.id, product_id)
        if remaining:
            return AlertRemoval.removed
        await self._products.delete_for_user(product_id, user.id)
        return AlertRemoval.removed_with_product

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
        self,
        user_id: int,
        product_id: int,
        target_price: int,
        direction: AlertDirection,
    ) -> Alert:
        if await self._find_duplicate(user_id, product_id, target_price, direction):
            raise DuplicateAlertError
        try:
            async with self._session.begin_nested():
                return await self._alerts.create(
                    {
                        "user_id": user_id,
                        "product_id": product_id,
                        "target_price": target_price,
                        "direction": direction,
                    }
                )
        except IntegrityError as exc:
            if await self._find_duplicate(user_id, product_id, target_price, direction):
                raise DuplicateAlertError from exc
            raise

    async def _find_duplicate(
        self,
        user_id: int,
        product_id: int,
        target_price: int,
        direction: AlertDirection,
    ) -> bool:
        alerts = await self._alerts.get_by_user_and_product(user_id, product_id)
        return any(
            alert.direction is direction and alert.target_price == target_price
            for alert in alerts
        )
