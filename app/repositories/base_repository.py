"""Base repository."""

from abc import ABC
from abc import abstractmethod
from collections.abc import Mapping
from typing import Any


class BaseRepository[ModelT, CreateT, PatchT: Mapping[str, Any]](ABC):
    """Abstract repository — defines the interface for all repositories."""

    @abstractmethod
    async def create(self, obj_in: CreateT) -> ModelT:
        """Add a new object."""
        raise NotImplementedError

    @abstractmethod
    async def get_by_id(self, obj_id: int) -> ModelT | None:
        """Get an object by ID."""
        raise NotImplementedError

    @abstractmethod
    async def list_paginated(
        self,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> list[ModelT]:
        """Get all objects with filters."""
        raise NotImplementedError

    @abstractmethod
    async def patch(self, obj_id: int, patch_data: PatchT) -> ModelT | None:
        """Partial  updating of the object."""
        raise NotImplementedError

    @abstractmethod
    async def delete(self, obj_id: int) -> bool:
        """Delete an object by ID."""
        raise NotImplementedError
