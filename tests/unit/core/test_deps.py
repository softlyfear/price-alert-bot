"""Tests for app.core.deps: ``SessionDep`` is wired to ``get_async_session``.

The alias is only a declaration, so the behaviour worth pinning is the wiring:
a route that asks for ``SessionDep`` must receive whatever FastAPI resolves
for ``get_async_session``. The provider is replaced through
``app.dependency_overrides`` with a stub yielding a marker object, so no
database, network or real session is involved.
"""

from typing import Any
from typing import cast

import httpx
import pytest
from fastapi import FastAPI

from app.core.database import get_async_session
from app.core.deps import SessionDep


class _SessionMarker:
    """Stand-in for ``AsyncSession`` that identifies itself by token."""

    token = "session-marker-7f3a"


@pytest.mark.asyncio
async def test_session_dep_injects_object_provided_by_get_async_session() -> None:
    app = FastAPI()

    @app.get("/probe")
    async def probe(session: SessionDep) -> dict[str, str]:
        return {"token": cast("Any", session).token}

    async def fake_session() -> _SessionMarker:
        return _SessionMarker()

    app.dependency_overrides[get_async_session] = cast("Any", fake_session)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/probe")

    assert response.status_code == 200
    assert response.json() == {"token": "session-marker-7f3a"}
