"""SQLAlchemy repository."""

from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.exceptions import EmptyPatchError
from app.domain.exceptions import InvalidCreateFieldsError
from app.domain.exceptions import InvalidPaginationError
from app.domain.exceptions import InvalidPatchFieldsError
from app.domain.exceptions import MissingRequiredCreateFieldsError
from app.domain.exceptions import RequiredFieldCannotBeNoneError
from app.models.base import Base
from app.repositories.base_repository import BaseRepository


class SQLAlchemyRepository[
    ModelT: Base,
    CreateT: Mapping[str, Any],
    PatchT: Mapping[str, Any],
](BaseRepository[ModelT, CreateT, PatchT], ABC):
    model: type[ModelT]
    MAX_PAGE_SIZE: int = 100

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    @abstractmethod
    def _create_fields(self) -> set[str]:
        """The list of fields allowed during creation."""
        raise NotImplementedError

    @property
    @abstractmethod
    def _required_fields(self) -> set[str]:
        """The list of fields that must be present during creation."""
        raise NotImplementedError

    @property
    @abstractmethod
    def _required_non_nullable_fields(self) -> set[str]:
        """Fields that must be present AND not None during creation."""
        raise NotImplementedError

    @property
    @abstractmethod
    def _patch_fields(self) -> set[str]:
        """The list of fields allowed during the update."""
        raise NotImplementedError

    async def create(self, obj_in: CreateT) -> ModelT:
        unknown = set(obj_in) - self._create_fields
        if unknown:
            raise InvalidCreateFieldsError(unknown)

        missing = self._required_fields - set(obj_in)
        if missing:
            raise MissingRequiredCreateFieldsError(missing)

        none_values = {
            f for f in self._required_non_nullable_fields if obj_in.get(f) is None
        }
        if none_values:
            raise RequiredFieldCannotBeNoneError(none_values)

        validated_data = {k: v for k, v in obj_in.items() if k in self._create_fields}

        obj = self.model(**validated_data)
        self._session.add(obj)
        await self._session.flush()
        await self._session.refresh(obj)
        return obj

    async def get_by_id(self, obj_id: int) -> ModelT | None:
        return await self._session.get(self.model, obj_id)

    async def list_paginated(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[ModelT]:
        if offset < 0 or limit < 1 or limit > self.MAX_PAGE_SIZE:
            raise InvalidPaginationError(offset, limit, self.MAX_PAGE_SIZE)

        stmt = select(self.model).order_by(self.model.id).offset(offset).limit(limit)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def patch(self, obj_id: int, patch_data: PatchT) -> ModelT | None:
        obj = await self.get_by_id(obj_id)
        if obj is None:
            return None

        unknown = set(patch_data) - self._patch_fields
        if unknown:
            raise InvalidPatchFieldsError(unknown)

        if not patch_data:
            raise EmptyPatchError()

        for field in self._patch_fields:
            if field in patch_data:
                setattr(obj, field, patch_data[field])

        await self._session.flush()
        await self._session.refresh(obj)
        return obj

    async def delete(self, obj_id: int) -> bool:
        obj = await self.get_by_id(obj_id)
        if obj is None:
            return False

        await self._session.delete(obj)
        await self._session.flush()
        return True
