"""Alert repository."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from app.models.alert import Alert
from app.repositories.sqlalchemy_repository import SQLAlchemyRepository


class AlertRepository(
    SQLAlchemyRepository[Alert, Mapping[str, Any], Mapping[str, Any]]
):
    model = Alert

    @property
    def _create_fields(self) -> set[str]:
        return {
            "user_id",
            "product_id",
            "target_price",
            "direction",
        }

    @property
    def _required_fields(self) -> set[str]:
        return {
            "user_id",
            "product_id",
            "target_price",
            "direction",
        }

    @property
    def _required_non_nullable_fields(self) -> set[str]:
        return {
            "user_id",
            "product_id",
            "target_price",
            "direction",
            "is_active",
        }

    @property
    def _patch_fields(self) -> set[str]:
        return {
            "target_price",
            "is_active",
            "triggered_at",
        }

    async def get_by_user_and_product(
        self, user_id: int, product_id: int
    ) -> list[Alert]:
        """Get Alert by user and product."""
        stmt = select(Alert).where(
            Alert.user_id == user_id, Alert.product_id == product_id
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_active_by_product(self, product_id: int) -> list[Alert]:
        """Get alerts with active products."""
        stmt = select(Alert).where(Alert.product_id == product_id, Alert.is_active)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def deactivate(self, alert_id: int) -> None:
        """Deactivate active alert."""
        alert = await self._session.get(Alert, alert_id)
        if alert:
            alert.is_active = False
        await self._session.flush()
