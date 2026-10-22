"""Tracking service: preview a product and start tracking its price."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
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
from app.models.user import User
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import MarketplaceFetchFailure
from app.schemas.marketplace import MarketplaceFetchResult
from app.services.base_client import BaseMarketplaceClient
from app.services.client_factory import UnsupportedMarketplaceError
from app.services.failure_levels import failure_log_level

LIST_PAGE_SIZE = 10
"""Products per page of the "My products" list (PROJECT.md 2.13 R20)."""


@dataclass(frozen=True, slots=True)
class ProductCard:
    """A product with its alerts (sorted by `target_price`) and its list page."""

    product: Product
    alerts: tuple[Alert, ...]
    list_page: int


@dataclass(frozen=True, slots=True)
class ProductPage:
    """One page of the caller's products; `page` is already clamped."""

    products: tuple[Product, ...]
    total: int
    page: int
    pages: int


class AlertRemoval(StrEnum):
    """Outcome of removing a single alert."""

    not_found = "not_found"
    removed = "removed"
    removed_with_product = "removed_with_product"


class AlertChange(StrEnum):
    """Outcome of changing the threshold of a single alert."""

    changed = "changed"
    unchanged = "unchanged"
    not_found = "not_found"


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
        await self._ensure_within_limit(user, marketplace, article)
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
        *,
        price_fetched_at: datetime | None,
    ) -> Alert:
        """Create (or reuse) the product and add an alert for it.

        The direction follows from the threshold versus `current_price`;
        an equal threshold raises `TargetEqualsCurrentPriceError` before
        any database access. `price_fetched_at` is the moment the price was
        fetched (the dialog's `preview`); it is stored only for a new product.
        """
        direction = choose_direction(current_price, target_price)
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        await self._ensure_within_limit(user, marketplace, article)

        product = await self._products.get_by_article_and_user(
            article, user.id, marketplace
        )
        if product is None:
            product = await self._create_product(
                user.id,
                marketplace,
                article,
                product_name,
                current_price,
                price_fetched_at,
            )
        return await self._create_alert(user.id, product.id, target_price, direction)

    async def product_limit(self, tg_user_id: int) -> int:
        """Return the caller's effective product limit (personal or global)."""
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        return self._effective_limit(user)

    async def list_page(self, tg_user_id: int, page: int) -> ProductPage:
        """Return one page of the caller's products ordered by `id`.

        `page` is user input: it is clamped to `[1, pages]`. The number of
        queries is constant and does not depend on the number of products.
        """
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        total = await self._products.count_by_user(user.id)
        pages = self._page_count(total)
        page = min(max(page, 1), pages)
        products = await self._products.get_page_by_user(
            user.id, LIST_PAGE_SIZE, (page - 1) * LIST_PAGE_SIZE
        )
        return ProductPage(
            products=tuple(products), total=total, page=page, pages=pages
        )

    async def list_page_of(self, tg_user_id: int, product_id: int) -> int:
        """Return the list page holding `product_id`, clamped to `[1, pages]`.

        Valid for an already deleted product: the page is computed from the
        number of the caller's products with a lower `id`.
        """
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        total = await self._products.count_by_user(user.id)
        before = await self._products.count_before(user.id, product_id)
        return min(before // LIST_PAGE_SIZE + 1, self._page_count(total))

    @staticmethod
    def _page_count(total: int) -> int:
        return max(1, -(-total // LIST_PAGE_SIZE))

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
        return await self._build_card(user.id, product)

    async def find_tracked_product(
        self, tg_user_id: int, marketplace: Marketplace, article: int
    ) -> ProductCard | None:
        """Return the caller's product by (marketplace, article), or None.

        Reads the database only: no marketplace request, no writes besides
        the existing `get_or_create_by_tg_id`.
        """
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        product = await self._products.get_by_article_and_user(
            article, user.id, marketplace
        )
        if product is None:
            return None
        return await self._build_card(user.id, product)

    async def _build_card(self, user_id: int, product: Product) -> ProductCard:
        alerts = await self._alerts.get_by_user_and_product(user_id, product.id)
        before = await self._products.count_before(user_id, product.id)
        return ProductCard(
            product=product,
            alerts=tuple(sorted(alerts, key=lambda alert: alert.target_price)),
            list_page=before // LIST_PAGE_SIZE + 1,
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

    async def change_alert_target(
        self, tg_user_id: int, alert_id: int, target_price: int
    ) -> AlertChange:
        """Change the caller's alert threshold; direction follows the price.

        Raises `TargetEqualsCurrentPriceError` for a threshold equal to the
        product's current price and `DuplicateAlertError` when another alert
        of the product already has the same (target, direction). The same
        (target, direction) as the alert's own is `unchanged`: nothing is
        written and `triggered_at` (cooldown) stays. Runs in the caller's
        transaction; the write is wrapped in a SAVEPOINT.
        """
        user, _ = await self._users.get_or_create_by_tg_id(tg_user_id)
        alert = await self._alerts.get_by_id_for_user(alert_id, user.id)
        if alert is None:
            return AlertChange.not_found
        product = await self._products.get_by_id_for_user(alert.product_id, user.id)
        if product is None:
            return AlertChange.not_found
        direction = choose_direction(product.current_price, target_price)
        if alert.direction is direction and alert.target_price == target_price:
            return AlertChange.unchanged
        if await self._find_duplicate(
            user.id, product.id, target_price, direction, exclude_alert_id=alert.id
        ):
            raise DuplicateAlertError
        try:
            async with self._session.begin_nested():
                updated = await self._alerts.update_target_for_user(
                    alert_id, user.id, target_price, direction
                )
        except IntegrityError as exc:
            if await self._find_duplicate(
                user.id, product.id, target_price, direction, exclude_alert_id=alert.id
            ):
                raise DuplicateAlertError from exc
            raise
        return AlertChange.changed if updated else AlertChange.not_found

    def _effective_limit(self, user: User) -> int:
        """Personal limit when set (`is not None`), else the global default."""
        return self._max_products if user.product_limit is None else user.product_limit

    async def _ensure_within_limit(
        self, user: User, marketplace: Marketplace, article: int
    ) -> None:
        """Raise when at the limit, unless the product is already counted."""
        limit = self._effective_limit(user)
        user_id = user.id
        if await self._products.count_active_by_user(user_id) < limit:
            return
        product = await self._products.get_by_article_and_user(
            article, user_id, marketplace
        )
        if product is not None:
            alerts = await self._alerts.get_by_user_and_product(user_id, product.id)
            if any(alert.is_active for alert in alerts):
                return
        raise ProductLimitExceededError(limit)

    async def _create_product(
        self,
        user_id: int,
        marketplace: Marketplace,
        article: int,
        product_name: str,
        current_price: int,
        price_fetched_at: datetime | None,
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
                        "initial_price": current_price,
                        "price_fetched_at": price_fetched_at,
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
        exclude_alert_id: int | None = None,
    ) -> bool:
        alerts = await self._alerts.get_by_user_and_product(user_id, product_id)
        return any(
            alert.id != exclude_alert_id
            and alert.direction is direction
            and alert.target_price == target_price
            for alert in alerts
        )
