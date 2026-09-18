"""PAB-058 conditions (в)/(г): the field-set invariant guard, on mocks.

``_required_non_nullable_fields ⊆ _required_fields ⊆ _create_fields`` is a
property of three plain ``set`` literals returned by each repository's
properties -- no session, no SQL, no PostgreSQL involved, which is exactly
why PAB-058's Description places this guard at unit level rather than next
to the rest of the repository suite on the live database.

The regression this guards against is PAB-008: ``AlertRepository`` once
listed ``is_active`` in ``_required_non_nullable_fields`` while omitting it
from ``_required_fields`` and ``_create_fields`` entirely, which made every
call to ``AlertRepository.create()`` raise
``RequiredFieldCannotBeNoneError`` unconditionally (``obj_in.get(f) is
None`` is true for a key that is never even accepted). Condition (г) adds
the requirement that this guard is parametrized one class per case, rather
than one test iterating a list internally, precisely so that a broken
class is named in the failing test ID and not buried in a loop.

Mutation demonstrated on a copy outside the repository (convention 7):
restoring ``is_active`` to ``AlertRepository._required_non_nullable_fields``
reproduces the PAB-008 regression and is required, by PAB-058 AC2, to fail
only the ``AlertRepository`` case below -- see the report for the actual
command and output.
"""

from typing import Any
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.alert import AlertRepository
from app.repositories.product import ProductRepository
from app.repositories.sqlalchemy_repository import SQLAlchemyRepository
from app.repositories.user import UserRepository

#: One class per parametrization case (condition (г)) -- never a single
#: test looping over this list -- so a failing case's pytest ID names the
#: repository class directly.
_REPOSITORY_CLASSES: tuple[type[SQLAlchemyRepository[Any, Any, Any]], ...] = (
    AlertRepository,
    ProductRepository,
    UserRepository,
)

#: The constructor only ever assigns ``session`` to an attribute this test
#: never exercises -- every property under test here is a literal ``set``
#: with no dependency on a real session, so a real ``AsyncSession`` would
#: add nothing but an unused connection.
_NO_SESSION = cast(AsyncSession, None)


@pytest.mark.parametrize(
    "repository_cls",
    _REPOSITORY_CLASSES,
    ids=[cls.__name__ for cls in _REPOSITORY_CLASSES],
)
def test_required_non_nullable_fields_is_a_subset_of_required_is_a_subset_of_create(
    repository_cls: type[SQLAlchemyRepository[Any, Any, Any]],
) -> None:
    """One assertion per class (condition (г)): a violation names the class."""
    repository = repository_cls(_NO_SESSION)

    assert (
        repository._required_non_nullable_fields
        <= repository._required_fields
        <= repository._create_fields
    ), (
        f"{repository_cls.__name__}: "
        "_required_non_nullable_fields must be a subset of _required_fields, "
        "which must be a subset of _create_fields"
    )
