"""Test-data factories shared by the live-PostgreSQL repository test modules.

Plain async functions rather than pytest fixtures: every one of them needs
a per-test ``db_session`` plus caller-chosen relationships (a specific
owning user, a specific product), which regular arguments express more
directly than fixture injection would here. Each inserted row is flushed
(not committed) through the ``db_session`` the caller already holds, so it
lives and dies with that same test's transaction -- see
``tests/integration/conftest.py`` for the rollback that cleans it up.

Every identifier defaults to a private, per-process counter rather than a
fixed constant, so a test creating more than one row of the same model
never collides with itself inside its own transaction. Cross-test
collisions on these same counters are not a concern: PAB-011's per-test
rollback means no row created here is ever visible to another test.
"""

from __future__ import annotations

import itertools

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.alert import Alert
from app.models.enums import AlertDirection
from app.models.enums import Marketplace
from app.models.product import Product
from app.models.user import User

_tg_user_ids = itertools.count(900_000_001)
_articles = itertools.count(1_000_001)


async def make_user(session: AsyncSession, *, tg_user_id: int | None = None) -> User:
    """Insert and flush a ``User``, returning it with its DB-assigned ``id``."""
    user = User(tg_user_id=tg_user_id if tg_user_id is not None else next(_tg_user_ids))
    session.add(user)
    await session.flush()
    return user


async def make_product(
    session: AsyncSession,
    *,
    user_id: int,
    marketplace: Marketplace = Marketplace.wb,
    article: int | None = None,
    product_name: str = "PAB-058 test product",
    current_price: int = 10_000,
) -> Product:
    """Insert and flush a ``Product`` owned by ``user_id``."""
    product = Product(
        user_id=user_id,
        marketplace=marketplace,
        article=article if article is not None else next(_articles),
        product_name=product_name,
        current_price=current_price,
    )
    session.add(product)
    await session.flush()
    return product


async def make_alert(
    session: AsyncSession,
    *,
    user_id: int,
    product_id: int,
    target_price: int = 9_000,
    direction: AlertDirection = AlertDirection.below,
) -> Alert:
    """Insert and flush an ``Alert`` owned by ``user_id`` for ``product_id``."""
    alert = Alert(
        user_id=user_id,
        product_id=product_id,
        target_price=target_price,
        direction=direction,
    )
    session.add(alert)
    await session.flush()
    return alert
