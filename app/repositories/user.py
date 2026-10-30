"""User repository."""

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.domain.exceptions import GetOrCreateUserError
from app.models import User
from app.repositories.sqlalchemy_repository import SQLAlchemyRepository


class UserRepository(SQLAlchemyRepository[User, Mapping[str, Any], Mapping[str, Any]]):
    """User repository."""

    model = User

    @property
    def _create_fields(self) -> set[str]:
        return {
            "tg_user_id",
            "tg_username",
        }

    @property
    def _required_fields(self) -> set[str]:
        return {
            "tg_user_id",
        }

    @property
    def _required_non_nullable_fields(self) -> set[str]:
        return {
            "tg_user_id",
        }

    @property
    def _patch_fields(self) -> set[str]:
        return {
            "tg_username",
        }

    async def get_by_tg_id(self, tg_user_id: int) -> User | None:
        """Get a user by Telegram ID without creating one."""
        stmt = select(User).where(User.tg_user_id == tg_user_id)
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create_by_tg_id(self, tg_user_id: int) -> tuple[User, bool]:
        """Get a user by Telegram ID or create a new one if it doesn't exist.

        Acquires the write transaction before the first read so concurrent
        creation cannot race with a stale SQLite snapshot.
        """
        await self.begin_write()
        user = await self.get_by_tg_id(tg_user_id)
        if user is not None:
            return user, False

        insert_stmt = (
            insert(User)
            .values(tg_user_id=tg_user_id)
            .on_conflict_do_nothing(index_elements=["tg_user_id"])
            .returning(User)
        )

        result = await self._session.execute(insert_stmt)
        user = result.scalar_one_or_none()

        if user:
            return user, True

        user = await self.get_by_tg_id(tg_user_id)

        if user is None:
            raise GetOrCreateUserError(tg_user_id)

        return user, False
