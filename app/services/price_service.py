"""Price servise."""

import asyncio
import math
from collections import Counter
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from enum import StrEnum
from itertools import batched

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

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
from app.services.failure_levels import failure_log_level
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
        alert_cooldown_seconds: int,
    ) -> None:
        self._notification = notification_service
        self._product_repo_factory = product_repo_factory
        self._alert_repo_factory = alert_repo_factory
        self._user_repo_factory = user_repo_factory
        self._client_factory = client_factory
        self._http_client = http_client
        self._alert_cooldown = timedelta(seconds=alert_cooldown_seconds)

    def batch_size(self, marketplace: Marketplace) -> int:
        """Return how many articles one batch request of the marketplace holds."""

        return self._client_factory(marketplace, self._http_client).batch_size

    async def fetch_prices(
        self, marketplace: Marketplace, articles: Sequence[int]
    ) -> dict[int, MarketplaceFetchResult]:
        """Fetch prices for articles in one batch; network only, no session.

        `UnsupportedMarketplaceError` from the factory is not caught: it is a
        configuration error, isolated per batch by the scheduler.
        """

        client = self._client_factory(marketplace, self._http_client)
        return await client.get_products_data(articles)

    async def apply_result(
        self,
        product_id: int,
        result: MarketplaceFetchResult,
        session: AsyncSession,
    ) -> None:
        """Apply an already fetched result to a product and send alerts.

        A product deleted since the batch was loaded is a no-op.
        """

        product_repo = self._product_repo_factory(session)
        await product_repo.begin_write()
        product = await product_repo.get_by_id(product_id)
        if product is None:
            return
        await self._apply(product, result, session)

    async def _apply(
        self,
        product: Product,
        market_data: MarketplaceFetchResult,
        session: AsyncSession,
    ) -> None:
        """Write a fetch result to the loaded product and notify (no commit)."""

        alert_repo = self._alert_repo_factory(session)
        user_repo = self._user_repo_factory(session)
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
                failure_log_level(market_data.reason), "Marketplace fetch failed"
            )
            await session.flush()
            return

        previous_price = product.current_price
        current_price = market_data.price

        product.previous_price = previous_price
        product.current_price = current_price
        product.last_checked_at = now_utc
        product.price_fetched_at = now_utc

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


REFRESH_WINDOW_SECONDS = 60.0
"""Default per-user budget window of a manual refresh (PROJECT.md 2.13 R15 p.2)."""

REFRESH_DEADLINE_SECONDS = 20.0
"""Default deadline of one manual refresh (PROJECT.md 2.13 R15 p.5)."""


class RefreshFailure(StrEnum):
    """Why a product of a manual refresh did not get a fresh price."""

    not_found = "not_found"
    blocked = "blocked"
    transport_error = "transport_error"
    bad_payload = "bad_payload"
    out_of_stock = "out_of_stock"
    timed_out = "timed_out"
    error = "error"


@dataclass(frozen=True)
class RefreshReport:
    """Outcome of a manual refresh.

    `failures` holds non-zero counters only;
    `updated + sum(failures.values()) == total`.
    """

    total: int
    updated: int
    failures: Mapping[RefreshFailure, int]


@dataclass(frozen=True)
class RefreshThrottled:
    """The per-user budget is spent; `retry_after_seconds` is rounded up, >= 1."""

    retry_after_seconds: int


@dataclass(frozen=True)
class RefreshGone:
    """The product of the card does not exist for the caller."""


class RefreshLimiter:
    """In-memory per-key budget: one accepted `reserve` per window.

    `reserve` is synchronous: check and mark happen with no `await` between
    them, so in one event loop it is atomic without a lock. The window runs
    from the accepted mark. Stale entries are purged on every call, so the size
    never exceeds the number of keys accepted within the last window.
    """

    def __init__(self, window_seconds: float, clock: Callable[[], float]) -> None:
        self._window = window_seconds
        self._clock = clock
        self._marks: dict[int, float] = {}

    def reserve(self, key: int) -> RefreshThrottled | None:
        """Accept and mark the key, or return the remaining wait."""

        now = self._clock()
        self._marks = {
            k: mark for k, mark in self._marks.items() if now - mark < self._window
        }
        mark = self._marks.get(key)
        if mark is not None:
            remaining = self._window - (now - mark)
            return RefreshThrottled(max(1, math.ceil(remaining)))
        self._marks[key] = now
        return None

    def __len__(self) -> int:
        return len(self._marks)


class PriceRefresher:
    """Manual price refresh of one user's products (PROJECT.md 2.13 R15).

    One instance per process. Fetches happen outside any session; results are
    written through `PriceService.apply_result`, one session and transaction
    per product, so thresholds and notifications follow the scheduler's rules.
    """

    def __init__(
        self,
        price_service: PriceService,
        sessionmaker: async_sessionmaker[AsyncSession],
        product_repo_factory: Callable[[AsyncSession], ProductRepository],
        user_repo_factory: Callable[[AsyncSession], UserRepository],
        *,
        concurrency: int,
        limiter: RefreshLimiter,
        deadline_seconds: float,
    ) -> None:
        self._price_service = price_service
        self._sessionmaker = sessionmaker
        self._product_repo_factory = product_repo_factory
        self._user_repo_factory = user_repo_factory
        self._semaphore = asyncio.Semaphore(concurrency)
        self._limiter = limiter
        self._deadline = deadline_seconds

    def reserve(self, tg_user_id: int) -> RefreshThrottled | None:
        """Take the user's refresh budget (shared by list and card)."""

        return self._limiter.reserve(tg_user_id)

    async def refresh_list(self, tg_user_id: int) -> RefreshReport:
        """Refresh every product of the user."""

        async with self._sessionmaker() as session, session.begin():
            user = await self._user_repo_factory(session).get_by_tg_id(tg_user_id)
            if user is None:
                items = []
            else:
                products = await self._product_repo_factory(session).get_by_user_id(
                    user.id
                )
                items = [(p.id, p.marketplace, p.article) for p in products]
        return await self._refresh(items, tg_user_id, "list")

    async def refresh_product(
        self, tg_user_id: int, product_id: int
    ) -> RefreshReport | RefreshGone:
        """Refresh one product; `RefreshGone` if it is not the caller's."""

        async with self._sessionmaker() as session, session.begin():
            user = await self._user_repo_factory(session).get_by_tg_id(tg_user_id)
            if user is None:
                return RefreshGone()
            product = await self._product_repo_factory(session).get_by_id_for_user(
                product_id, user.id
            )
            if product is None:
                return RefreshGone()
            items = [(product.id, product.marketplace, product.article)]
        return await self._refresh(items, tg_user_id, "card")

    async def _refresh(
        self,
        items: list[tuple[int, Marketplace, int]],
        tg_user_id: int,
        scope: str,
    ) -> RefreshReport:
        """Fetch and apply `(id, marketplace, article)` items within the deadline.

        The sessions of the selection are closed before this runs. The deadline
        bounds only the semaphore wait and `fetch_prices`; a product write that
        has started is never interrupted (the notification precedes the commit,
        so a rollback would repeat it past the cooldown), and no write starts
        after the deadline. Products with no outcome are `timed_out`; an
        external cancellation propagates.
        """

        deadline_at = asyncio.get_running_loop().time() + self._deadline

        outcomes: dict[int, RefreshFailure | None] = {}
        blocked: set[Marketplace] = set()

        grouped: dict[Marketplace, dict[int, list[int]]] = {}
        for product_id, marketplace, article in items:
            grouped.setdefault(marketplace, {}).setdefault(article, []).append(
                product_id
            )

        async def run_batch(
            marketplace: Marketplace, groups: list[tuple[int, list[int]]]
        ) -> None:
            try:
                async with asyncio.timeout_at(deadline_at):
                    await self._semaphore.acquire()
            except TimeoutError:
                return
            try:
                if marketplace in blocked:
                    # No request was made: `last_checked_at` stays untouched.
                    for _, ids in groups:
                        for product_id in ids:
                            outcomes[product_id] = RefreshFailure.blocked
                    return
                results: dict[int, MarketplaceFetchResult] | None
                fetch = asyncio.timeout_at(deadline_at)
                try:
                    async with fetch:
                        results = await self._price_service.fetch_prices(
                            marketplace, [article for article, _ in groups]
                        )
                except Exception:
                    if fetch.expired():
                        return
                    logger.bind(
                        marketplace=str(marketplace), batch_size=len(groups)
                    ).exception("Manual refresh batch fetch failed")
                    results = None
                else:
                    if any(
                        isinstance(r, MarketplaceFetchFailure)
                        and r.reason == FetchFailureReason.blocked
                        for r in results.values()
                    ):
                        blocked.add(marketplace)
                for article, ids in groups:
                    for product_id in ids:
                        if asyncio.get_running_loop().time() >= deadline_at:
                            return
                        outcomes[product_id] = await self._apply_one(
                            product_id, results, article
                        )
            finally:
                self._semaphore.release()

        batches: list[tuple[Marketplace, list[tuple[int, list[int]]]]] = []
        for marketplace, articles in grouped.items():
            try:
                size = self._price_service.batch_size(marketplace)
            except Exception:
                logger.bind(marketplace=str(marketplace)).exception(
                    "Batch size unavailable, marketplace skipped"
                )
                for ids in articles.values():
                    for product_id in ids:
                        outcomes[product_id] = RefreshFailure.error
                continue
            batches.extend(
                (marketplace, list(part))
                for part in batched(articles.items(), size, strict=False)
            )

        async with asyncio.TaskGroup() as group:
            for marketplace, groups in batches:
                group.create_task(run_batch(marketplace, groups))

        failures: Counter[RefreshFailure] = Counter()
        updated = 0
        for product_id, _, _ in items:
            outcome = outcomes.get(product_id, RefreshFailure.timed_out)
            if outcome is None:
                updated += 1
            else:
                failures[outcome] += 1
        report = RefreshReport(
            total=len(items), updated=updated, failures=dict(failures)
        )
        log = logger.bind(tg_user_id=tg_user_id, scope=scope)
        if failures[RefreshFailure.timed_out]:
            log.warning("Manual refresh hit the deadline")
        log.info(
            "Manual refresh finished: total={total} updated={updated} "
            "failures={failures}",
            total=report.total,
            updated=report.updated,
            failures={str(k): v for k, v in report.failures.items()},
        )
        return report

    async def _apply_one(
        self,
        product_id: int,
        results: Mapping[int, MarketplaceFetchResult] | None,
        article: int,
    ) -> RefreshFailure | None:
        """Apply one result in its own transaction; `None` means a fresh price."""

        try:
            if results is None:
                return RefreshFailure.error
            result = results[article]
            async with self._sessionmaker() as session:
                await self._price_service.apply_result(product_id, result, session)
                await session.commit()
        except Exception:
            logger.bind(product_id=product_id).exception("Manual refresh apply failed")
            return RefreshFailure.error
        if isinstance(result, MarketplaceFetchFailure):
            return RefreshFailure(str(result.reason))
        return None
