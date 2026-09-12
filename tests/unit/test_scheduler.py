"""Tests for app.scheduler.

This is a behavioral test, not a coverage test: it guards against a
regression where the ``NotImplementedError`` placeholder body of
``price_price_check_loop`` is silently replaced with ``pass`` or ``return``,
which would make the scheduler exit quietly instead of failing loudly.
"""

from typing import cast
from unittest.mock import MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.scheduler import price_price_check_loop


@pytest.mark.asyncio
async def test_price_price_check_loop_raises_not_implemented_error() -> None:
    fake_session = cast(AsyncSession, MagicMock(spec=AsyncSession))

    with pytest.raises(NotImplementedError):
        await price_price_check_loop(fake_session)
