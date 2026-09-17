"""Alert repository."""

from collections.abc import Mapping
from typing import Any
from typing import cast

from sqlalchemy import CursorResult
from sqlalchemy import delete
from sqlalchemy import select
from sqlalchemy import update

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

    async def deactivate(self, alert_id: int, user_id: int) -> bool:
        """Deactivate active alert, scoped to its owning user.

        ``alert_id`` reaches this method from a Telegram callback and is
        therefore untrusted user input (PROJECT.md §8.2), so the ownership
        predicate is part of the same UPDATE statement as the lookup rather
        than a check performed after loading the row. Returns whether a row
        matched and was updated, taken from that statement's rowcount, so
        the caller can tell an alert belonging to another user or missing
        entirely apart from one that was actually deactivated.
        """
        stmt = (
            update(Alert)
            .where(Alert.id == alert_id, Alert.user_id == user_id)
            .values(is_active=False)
            .execution_options(synchronize_session="evaluate")
        )
        result = await self._session.execute(stmt)
        # AsyncSession.execute() is typed to return the generic Result[Any]
        # for a plain Executable; at runtime a Core DELETE/UPDATE always
        # yields a CursorResult, which is the type that actually defines
        # `.rowcount` (see SQLAlchemy docs, CursorResult.rowcount).
        return bool(cast(CursorResult[Any], result).rowcount)

    async def get_by_id_for_user(self, alert_id: int, user_id: int) -> Alert | None:
        """Get an alert by primary key, scoped to its owning user.

        The ownership predicate is part of the same SELECT as the primary
        key lookup, so an alert belonging to another user is
        indistinguishable from one that does not exist at all.
        """
        stmt = select(Alert).where(Alert.id == alert_id, Alert.user_id == user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_for_user(self, alert_id: int, user_id: int) -> bool:
        """Delete an alert by primary key, scoped to its owning user.

        Issued as a single Core DELETE with the ownership predicate in the
        same statement. ``Alert`` has no dependent relationships, so unlike
        ``ProductRepository.delete_for_user`` there is no cascade cost to
        weigh. ``synchronize_session="evaluate"`` evicts a matching
        ``Alert`` already present in this session's identity map without an
        extra round trip.
        """
        stmt = (
            delete(Alert)
            .where(Alert.id == alert_id, Alert.user_id == user_id)
            .execution_options(synchronize_session="evaluate")
        )
        result = await self._session.execute(stmt)
        # AsyncSession.execute() is typed to return the generic Result[Any]
        # for a plain Executable; at runtime a Core DELETE/UPDATE always
        # yields a CursorResult, which is the type that actually defines
        # `.rowcount` (see SQLAlchemy docs, CursorResult.rowcount).
        return bool(cast(CursorResult[Any], result).rowcount)
