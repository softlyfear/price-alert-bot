class InvalidCreateFieldsError(Exception):
    """Raised when create payload contains fields not in _create_fields."""

    def __init__(self, unknown_fields: set[str]) -> None:
        self.unknown_fields = unknown_fields
        super().__init__(f"Unknown fields for creation: {unknown_fields}")


class InvalidPatchFieldsError(Exception):
    """Raised when patch payload contains fields not in _patch_fields."""

    def __init__(self, unknown_fields: set[str]) -> None:
        self.unknown_fields = unknown_fields
        super().__init__(f"Unknown fields for patch: {unknown_fields}")


class GetOrCreateUserError(Exception):
    """Raised when cannot locate the user after ON CONFLICT DO NOTHING."""

    def __init__(self, tg_user_id: int) -> None:
        self.tg_user_id = tg_user_id
        super().__init__(
            f"User with tg_user_id={tg_user_id} not found after insert attempt"
        )


class InvalidPaginationError(Exception):
    """Raised when list_paginated receives invalid offset or limit values."""

    def __init__(self, offset: int, limit: int, max_page_size: int) -> None:
        self.offset = offset
        self.limit = limit
        super().__init__(
            f"Invalid pagination: offset={offset} (must be >= 0), "
            f"limit={limit} (must be between 1 and {max_page_size})"
        )


class MissingRequiredCreateFieldsError(Exception):
    """Raised when create payload misses fields required by _required_fields."""

    def __init__(self, missing_fields: set[str]) -> None:
        self.missing_fields = missing_fields
        super().__init__(f"Missing required fields for creation: {missing_fields}")


class EmptyPatchError(Exception):
    """Raised when patch payload is empty (no-op patch)."""

    def __init__(self) -> None:
        super().__init__("Patch data cannot be empty")


class RequiredFieldCannotBeNoneError(Exception):
    """Raised when a required non-nullable field."""

    def __init__(self, fields: set[str]) -> None:
        self.fields = fields
        super().__init__(f"Required fields cannot be None: {fields}")
