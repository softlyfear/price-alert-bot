import asyncio
from collections.abc import Iterator
from itertools import batched

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.enums import Marketplace
from app.repositories.product import ProductRepository
from app.schemas.marketplace import MarketplaceFetchResult
from app.services.price_service import PriceService

type _Batch = tuple[Marketplace, list[tuple[int, list[int]]]]
"""One unit of work: marketplace and ``(article, product ids)`` pairs."""


async def _load_products(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> list[tuple[int, Marketplace, int]] | None:
    """Return product scalars, or None on failure, releasing the session before HTTP."""
    try:
        async with sessionmaker() as session:
            products = await ProductRepository(session).get_all_with_active_alerts()
            return [
                (product.id, product.marketplace, product.article)
                for product in products
            ]
    except Exception:
        logger.exception("Price check pass aborted: could not select products")
        return None


def _requested_cancel() -> bool:
    """Distinguish requested cancellation from a child error ignored by TaskGroup."""
    task = asyncio.current_task()
    return task is None or task.cancelling() > 0


async def _apply_one(
    price_service: PriceService,
    sessionmaker: async_sessionmaker[AsyncSession],
    product_id: int,
    results: dict[int, MarketplaceFetchResult],
    article: int,
) -> bool:
    """Apply in a separate transaction; propagate requested cancellation.

    A failed lookup or unrequested cancellation affects only this product.
    """
    try:
        async with sessionmaker() as session:
            await price_service.apply_result(product_id, results[article], session)
            await session.commit()
    except Exception:
        logger.bind(product_id=product_id).exception("Product check failed")
        return False
    except asyncio.CancelledError:
        if _requested_cancel():
            raise
        logger.bind(product_id=product_id).exception("Product check failed")
        return False
    return True


def _build_batches(
    price_service: PriceService,
    products: list[tuple[int, Marketplace, int]],
) -> tuple[list[_Batch], int]:
    """Return batches and the number of products without a usable client."""
    grouped: dict[Marketplace, dict[int, list[int]]] = {}
    for product_id, marketplace, article in products:
        grouped.setdefault(marketplace, {}).setdefault(article, []).append(product_id)

    batches: list[_Batch] = []
    unbatched = 0
    for marketplace, articles in grouped.items():
        try:
            size = price_service.batch_size(marketplace)
        except Exception:
            logger.bind(marketplace=str(marketplace)).exception(
                "Batch size unavailable, marketplace skipped"
            )
            unbatched += sum(len(ids) for ids in articles.values())
            continue
        batches.extend(
            (marketplace, list(part))
            for part in batched(articles.items(), size, strict=False)
        )
    return batches, unbatched


async def run_price_check_pass(
    price_service: PriceService,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    concurrency: int,
    stop_event: asyncio.Event,
) -> None:
    """Fetch batches concurrently and apply each product in its own session.

    The shared iterator needs no lock: taking a batch does not await.
    Stop prevents starting new batches and products. Summary counts products,
    with failures <= checked <= products; an incomplete pass is an error.
    """
    products = await _load_products(sessionmaker)
    if products is None:
        return

    batches, unbatched = _build_batches(price_service, products)
    pending: Iterator[_Batch] = iter(batches)
    checked = unbatched
    failures = unbatched

    async def fetch(
        marketplace: Marketplace, articles: list[int]
    ) -> dict[int, MarketplaceFetchResult] | None:
        """Fetch one batch; ``None`` if it failed (logged once per batch)."""
        log = logger.bind(marketplace=str(marketplace), batch_size=len(articles))
        try:
            return await price_service.fetch_prices(marketplace, articles)
        except Exception:
            log.exception("Batch fetch failed")
        except asyncio.CancelledError:
            if _requested_cancel():
                raise
            log.exception("Batch fetch failed")
        return None

    async def worker() -> None:
        nonlocal checked, failures
        while not stop_event.is_set():
            batch = next(pending, None)
            if batch is None:
                return
            marketplace, groups = batch
            results = await fetch(marketplace, [article for article, _ in groups])
            for article, ids in groups:
                for product_id in ids:
                    if stop_event.is_set():
                        return
                    if results is None or not await _apply_one(
                        price_service, sessionmaker, product_id, results, article
                    ):
                        failures += 1
                    checked += 1

    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(batches))):
            group.create_task(worker())

    complete = checked == len(products) or stop_event.is_set()
    logger.log(
        "INFO" if complete else "ERROR",
        "Price check pass finished: products={products} checked={checked} "
        "failures={failures}",
        products=len(products),
        checked=checked,
        failures=failures,
    )


async def price_check_loop(
    price_service: PriceService,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    interval_seconds: int,
    concurrency: int,
    stop_event: asyncio.Event,
) -> None:
    """Run immediately, then pause after each pass to avoid overlap or a tight loop."""
    while not stop_event.is_set():
        await run_price_check_pass(
            price_service,
            sessionmaker,
            concurrency=concurrency,
            stop_event=stop_event,
        )
        try:
            async with asyncio.timeout(interval_seconds):
                await stop_event.wait()
        except TimeoutError:
            continue
