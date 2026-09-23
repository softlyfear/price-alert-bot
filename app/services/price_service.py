"""Price servise."""

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.user import UserRepository
from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.base_client import BaseMarketplaceClient
from app.services.notification import NotificationService

_FAILURE_LOG_LEVELS: dict[FetchFailureReason, str] = {
    FetchFailureReason.bad_payload: "ERROR",
    FetchFailureReason.blocked: "WARNING",
    FetchFailureReason.transport_error: "WARNING",
    FetchFailureReason.not_found: "WARNING",
    FetchFailureReason.out_of_stock: "INFO",
}


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
        alert_cooldown_seconds: int,
    ) -> None:
        self._notification = notification_service
        self._product_repo_factory = product_repo_factory
        self._alert_repo_factory = alert_repo_factory
        self._user_repo_factory = user_repo_factory
        self._client_factory = client_factory
        self._http_client = http_client
        self._alert_cooldown = timedelta(seconds=alert_cooldown_seconds)

    async def check_product(self, product_id: int, session: AsyncSession) -> None:
        """Check product and send alerts.

        `triggered_at` is never assigned here: `NotificationService.send_alert`
        is the sole writer, and only after a confirmed delivery
        (PROJECT.md section 2.5). This method only reads it for the cooldown
        gate below.
        """

        product_repo = self._product_repo_factory(session)
        alert_repo = self._alert_repo_factory(session)
        user_repo = self._user_repo_factory(session)

        product = await product_repo.get_by_id(product_id)
        if product is None:
            return

        # `self._client_factory` may raise `UnsupportedMarketplaceError` for a
        # marketplace with no registered client. Deliberately not caught here:
        # this is a configuration/programming error, not a per-product
        # marketplace failure (`MarketplaceFetchFailure` does not apply), and
        # letting it propagate relies on the scheduler loop (PAB-032) isolating
        # one product's failure from the rest of the pass.
        client = self._client_factory(product.marketplace, self._http_client)
        market_data = await client.get_product_data(product.article)
        now_utc = datetime.now(UTC)

        if isinstance(market_data, MarketplaceFetchFailure):
            # Any failure category updates `last_checked_at` only; the known
            # price is never overwritten on a failed fetch (PROJECT.md
            # section 2.6). `isinstance` narrows `market_data` to
            # `MarketplaceFetchFailure` here and, by exclusion on the
            # two-member `MarketplaceFetchResult` alias, to
            # `MarketplaceProductData` past this block - a copy of
            # `app/schemas/marketplace.py` with the alias widened to
            # `object` breaks that exclusion and `mypy --strict` rejects the
            # `.price` access below (see the ticket report).
            product.last_checked_at = now_utc
            log_context = logger.bind(
                product_id=product.id,
                article=product.article,
                marketplace=str(product.marketplace),
                reason=str(market_data.reason),
                detail=market_data.detail,
            )
            log_context.log(
                _FAILURE_LOG_LEVELS[market_data.reason], "Marketplace fetch failed"
            )
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
                triggered = current_price <= alert.target_price
            else:
                triggered = current_price >= alert.target_price

            if not triggered:
                continue

            # `triggered_at` is timezone-aware UTC by contract (DateTime(timezone=True)
            # column, always written from `datetime.now(UTC)`); a naive value here
            # signals a violated invariant and is allowed to raise `TypeError`
            # rather than be silently coerced.
            if (
                alert.triggered_at is not None
                and now_utc - alert.triggered_at < self._alert_cooldown
            ):
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
                    error_type=type(exc).__name__,
                )

        await session.flush()
