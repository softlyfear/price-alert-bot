"""Background price-check loop (PROJECT.md §7.2).

Everything the loop needs -- the service, the sessionmaker, the interval and the
concurrency ceiling -- is passed in by ``app.main.lifespan``; this module reads
no settings and builds no services.
"""

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
    """Select ``(id, marketplace, article)`` of products with active alerts.

    ``None`` if the query failed. The session is closed on return, i.e. before
    the first marketplace HTTP request, so a pass never holds a pooled
    connection while waiting on the network. Only scalars leave this function
    (O(P) memory).
    """
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
    """Tell whether the current task was asked to cancel (``cancelling() >= 1``).

    A ``CancelledError`` nobody asked for came from code below and, left alone,
    would silently kill the worker, because ``TaskGroup`` does not treat a
    cancelled child as an error.
    """
    task = asyncio.current_task()
    return task is None or task.cancelling() > 0


async def _apply_one(
    price_service: PriceService,
    sessionmaker: async_sessionmaker[AsyncSession],
    product_id: int,
    results: dict[int, MarketplaceFetchResult],
    article: int,
) -> bool:
    """Apply a fetched result in its own session and transaction; ``True`` on success.

    ``begin()`` commits on a clean exit and rolls back on an exception; the
    handlers sit outside the ``async with``, so the transaction is rolled back
    and the session closed before they run. Any ``Exception`` is a product
    failure. A ``CancelledError`` is a product failure too unless cancellation
    was requested (see ``_requested_cancel``), in which case it propagates.
    The lookup sits inside the ``try``: a missing batch entry fails only this
    product.
    """
    try:
        async with sessionmaker() as session, session.begin():
            await price_service.apply_result(product_id, results[article], session)
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
    """Group products by marketplace and article, cut into client-sized batches.

    Returns the batches and the number of products whose marketplace has no
    usable client (``batch_size`` raised): those are failures of the pass.
    """
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
    """Run one pass over every product with an active alert.

    Products are grouped by marketplace, equal articles are merged, and the
    articles are cut into batches of ``PriceService.batch_size``. A batch is a
    unit of work: one ``fetch_prices`` outside any session, then
    ``apply_result`` for each product in its own session and transaction.
    ``concurrency`` workers pull batches from one shared iterator, so at most
    that many fetches run at once and the pass creates O(concurrency) tasks.
    Taking the next batch involves no ``await``, so the iterator needs no lock.
    Once ``stop_event`` is set no new batch and no new product is started.

    Writes exactly one summary record per completed pass (none when the pass is
    cancelled or the product query fails) with three fields: ``products`` --
    products loaded (not articles); ``checked`` -- products that ended with an
    outcome, success or a failure counted in ``failures``; ``failures`` --
    products whose apply failed, whose batch fetch failed, or whose marketplace
    has no client, with an ``Exception`` or an unrequested ``CancelledError``.
    Always ``failures <= checked <= products``. The level is ``INFO`` when
    ``checked == products`` or ``stop_event`` is set at summary time, otherwise
    ``ERROR``: a worker vanished (for example a cancellation requested from
    inside a call, which is propagated and not counted) and the pass is
    incomplete.
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
    """Run passes until ``stop_event`` is set; the first pass starts at once.

    The pause is measured from the end of a pass, so passes never overlap and a
    long pass cannot trigger an immediate re-run. ``stop_event`` interrupts the
    pause, and the function then returns normally.
    """
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
