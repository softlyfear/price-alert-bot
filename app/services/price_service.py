"""Price servise."""

from collections.abc import Callable
from datetime import UTC
from datetime import datetime

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.services.base_client import BaseMarketplaceClient
from app.services.notification import NotificationService


class PriceService:
    """Price service logic."""

    def __init__(
        self,
        notification_service: NotificationService,
        product_repo_factory: Callable[[AsyncSession], ProductRepository],
        alert_repo_factory: Callable[[AsyncSession], AlertRepository],
        user_repo_factory: Callable[[AsyncSession], UserRepository],
        client_factory: Callable[
            [Marketplace, httpx.AsyncClient], BaseMarketplaceClient
        ],
        http_client: httpx.AsyncClient,
    ) -> None:
        self._notification = notification_service
        self._product_repo_factory = product_repo_factory
        self._alert_repo_factory = alert_repo_factory
        self._user_repo_factory = user_repo_factory
        self._client_factory = client_factory
        self._http_client = http_client

    async def check_product(self, product_id: int, session: AsyncSession) -> None:
        """Check product and send alerts."""

        product_repo = self._product_repo_factory(session)
        alert_repo = self._alert_repo_factory(session)
        user_repo = self._user_repo_factory(session)

        product = await product_repo.get_by_id(product_id)
        if product is None:
            return

        client = self._client_factory(product.marketplace, self._http_client)
        market_data = await client.get_product_data(product.article)
        now_utc = datetime.now(UTC)

        if market_data is None:
            product.last_checked_at = now_utc
            logger.bind(
                product_id=product.id,
                article=product.article,
                marketplace=str(product.marketplace),
            ).warning("Marketplace returned no data")
            await session.flush()
            return

        previous_price = product.current_price
        current_price = market_data.price

        product.previous_price = previous_price
        product.current_price = current_price
        product.last_checked_at = now_utc

        alerts = await alert_repo.get_active_by_product(product.id)
        if not alerts:
            await session.flush()
            return

        user = await user_repo.get_by_id(product.user_id)
        if user is None:
            await session.flush()
            return

        for alert in alerts:
            if alert.direction == AlertDirection.below:
                triggered = previous_price > alert.target_price >= current_price
            else:
                triggered = previous_price < alert.target_price <= current_price

            if not triggered:
                continue

            try:
                await self._notification.send_alert(
                    alert=alert,
                    product=product,
                    user=user,
                )
            except Exception as exc:
                logger.exception(
                    "Failed to send alert in price check loop",
                    alert_id=alert.id,
                    product_id=product.id,
                    user_id=user.id,
                    error=str(exc),
                )

        await session.flush()
