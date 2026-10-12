"""Background price-check loop (PROJECT.md §7.2).

Everything the loop needs -- the service, the sessionmaker, the interval and the
concurrency ceiling -- is passed in by ``app.main.lifespan``; this module reads
no settings and builds no services.
"""

import asyncio
from collections.abc import Iterator

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.repositories.product import ProductRepository
from app.services.price_service import PriceService


async def _load_product_ids(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> list[int] | None:
    """Select ids of products with active alerts; ``None`` if the query failed.

    The session is closed on return, i.e. before the first marketplace HTTP
    request, so a pass never holds a pooled connection while waiting on the
    network. Only ids leave this function (O(P) memory).
    """
    try:
        async with sessionmaker() as session:
            products = await ProductRepository(session).get_all_with_active_alerts()
            return [product.id for product in products]
    except Exception:
        logger.exception("Price check pass aborted: could not select products")
        return None


async def _check_one(
    price_service: PriceService,
    sessionmaker: async_sessionmaker[AsyncSession],
    product_id: int,
) -> bool:
    """Check one product in its own session and transaction; ``True`` on success.

    ``begin()`` commits on a clean exit and rolls back on an exception; the
    handlers sit outside the ``async with``, so the transaction is rolled back
    and the session closed before they run. Any ``Exception`` is a product
    failure. A ``CancelledError`` is a product failure too when nobody asked the
    current task to cancel (``Task.cancelling() == 0``): it came from code below
    and, left alone, would silently kill the worker, because ``TaskGroup`` does
    not treat a cancelled child as an error. When ``cancelling() >= 1`` the
    cancellation was requested and propagates unchanged.
    """
    try:
        async with sessionmaker() as session, session.begin():
            await price_service.check_product(product_id, session)
    except Exception:
        logger.bind(product_id=product_id).exception("Product check failed")
        return False
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is None or task.cancelling():
            raise
        logger.bind(product_id=product_id).exception("Product check failed")
        return False
    return True


async def run_price_check_pass(
    price_service: PriceService,
    sessionmaker: async_sessionmaker[AsyncSession],
    *,
    concurrency: int,
    stop_event: asyncio.Event,
) -> None:
    """Run one pass over every product with an active alert.

    ``concurrency`` workers pull ids from one shared iterator, so at most that
    many ``check_product`` calls run at once and the pass creates O(concurrency)
    tasks rather than one per product. Taking the next id involves no ``await``,
    so the shared iterator needs no lock. Once ``stop_event`` is set no new
    product is started.

    Writes exactly one summary record per completed pass (none when the pass is
    cancelled or the id query fails) with three fields: ``products`` -- ids
    loaded; ``checked`` -- checks that ended with an outcome, success or a
    failure counted in ``failures``; ``failures`` -- checks that failed with an
    ``Exception`` or an unrequested ``CancelledError``. Always
    ``failures <= checked <= products``. The level is ``INFO`` when
    ``checked == products`` or ``stop_event`` is set at summary time, otherwise
    ``ERROR``: a worker vanished (for example a cancellation requested from
    inside a check, which is propagated and not counted) and the pass is
    incomplete.
    """
    product_ids = await _load_product_ids(sessionmaker)
    if product_ids is None:
        return

    pending: Iterator[int] = iter(product_ids)
    checked = 0
    failures = 0

    async def worker() -> None:
        nonlocal checked, failures
        while not stop_event.is_set():
            product_id = next(pending, None)
            if product_id is None:
                return
            if not await _check_one(price_service, sessionmaker, product_id):
                failures += 1
            checked += 1

    async with asyncio.TaskGroup() as group:
        for _ in range(min(concurrency, len(product_ids))):
            group.create_task(worker())

    complete = checked == len(product_ids) or stop_event.is_set()
    logger.log(
        "INFO" if complete else "ERROR",
        "Price check pass finished: products={products} checked={checked} "
        "failures={failures}",
        products=len(product_ids),
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
