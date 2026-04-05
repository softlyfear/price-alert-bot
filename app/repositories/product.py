"""Product repository."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import exists
from sqlalchemy import select
from sqlalchemy.sql.functions import func

from app.models.alert import Alert
from app.models.enums import Marketplace
from app.models.product import Product
from app.repositories.sqlalchemy_repository import SQLAlchemyRepository


class ProductRepository(
    SQLAlchemyRepository[Product, Mapping[str, Any], Mapping[str, Any]]
):
    model = Product

    @property
    def _create_fields(self) -> set[str]:
        return {
            "user_id",
            "marketplace",
            "article",
            "product_name",
            "current_price",
            "previous_price",
        }

    @property
    def _required_fields(self) -> set[str]:
        return {
            "user_id",
            "marketplace",
            "article",
            "product_name",
            "current_price",
        }

    @property
    def _required_non_nullable_fields(self) -> set[str]:
        return {
            "user_id",
            "marketplace",
            "article",
            "product_name",
            "current_price",
        }

    @property
    def _patch_fields(self) -> set[str]:
        return {
            "product_name",
            "current_price",
            "previous_price",
            "last_checked_at",
        }

    async def get_by_user_id(self, user_id: int) -> list[Product]:
        """Get list of products by user id."""
        stmt = select(Product).where(Product.user_id == user_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_article_and_user(
        self,
        article: int,
        user_id: int,
        marketplace: Marketplace,
    ) -> Product | None:
        """Get product by user id and article."""
        stmt = select(Product).where(
            Product.user_id == user_id,
            Product.article == article,
            Product.marketplace == marketplace,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def count_active_by_user(self, user_id: int) -> int:
        """Count active product by user."""
        active_alerts_exist = exists().where(
            Alert.product_id == Product.id, Alert.is_active
        )
        stmt = select(func.count(Product.id)).where(
            Product.user_id == user_id, active_alerts_exist
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()

    async def get_all_with_active_alerts(self) -> list[Product]:
        """Get all product with active alerts."""
        stmt = select(Product).join(Product.alerts).where(Alert.is_active).distinct()
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
