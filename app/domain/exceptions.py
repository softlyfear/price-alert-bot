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


class DomainError(Exception):
    """Base class for business-rule violations raised by the domain layer.

    Messages are for developers; user-facing text lives in the bot layer.
    """


class ProductRefParseError(DomainError):
    """Raised when user input is not a supported product link or article."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Cannot parse product reference: {reason}")


class InvalidPriceInputError(DomainError):
    """Raised when user input is not a valid price in rubles."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Invalid price input: {reason}")


class ProductLimitExceededError(DomainError):
    """Raised when a user would exceed the tracked products limit."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"Tracked products limit exceeded: limit={limit}")


class DuplicateAlertError(DomainError):
    """Raised when the same (user, product, direction, target) alert exists."""

    def __init__(self) -> None:
        super().__init__("Alert with the same product and target price exists")


class MarketplaceNotSupportedError(DomainError):
    """Raised when no client is implemented for the requested marketplace."""

    def __init__(self, marketplace: str) -> None:
        self.marketplace = marketplace
        super().__init__(f"Marketplace is not supported: {marketplace}")


class TargetEqualsCurrentPriceError(DomainError):
    """Raised when the threshold equals the current price: no direction."""

    def __init__(self) -> None:
        super().__init__("Target price equals the current price")
