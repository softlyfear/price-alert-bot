"""Ozon short-link expansion (PAB-114, PROJECT.md section 2.15 R22).

Two boundaries are replaced: the `curl_cffi` session under `OzonSession`
(`get_redirect` tests) and `OzonSession.get_redirect` under `OzonClient`
(`resolve_short_link` tests). No network. The statuses and the expected
categories come from the status map of R22 item 2, not from the code. The
`Location` strings are synthetic, shaped like the live fact (PAB-114 F7:
`301`, `Location` to `/product/<slug>-<article>/?...&short=<code>`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING
from typing import Any

import pytest
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import ProxyError
from loguru import logger

if TYPE_CHECKING:
    from loguru import Message
    from loguru import Record

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.base_client import BaseMarketplaceClient
from app.services.ozon_client import OzonClient
from app.services.ozon_client import OzonRedirect
from app.services.ozon_client import OzonSession
from tests.unit.ozon_stubs import HANG
from tests.unit.ozon_stubs import StubOzonAccess

_CODE = "7GeNMx2"
_LOCATION = "https://www.ozon.ru/product/some-slug-2622563074/?at=TOKEN&short=7GeNMx2"


# --- OzonSession.get_redirect on a fake curl_cffi session -------------------


class _FakeResponse:
    def __init__(
        self,
        status_code: int,
        headers: Mapping[str, Any] | None = None,
        *,
        quit_now: asyncio.Event | None = None,
        broken_status: bool = False,
    ) -> None:
        self._status = status_code
        self.headers = dict(headers or {})
        self.quit_now = quit_now
        self._broken_status = broken_status
        self.closed = 0
        self.read = False

    @property
    def status_code(self) -> int:
        if self._broken_status:
            raise RuntimeError("status unavailable")
        return self._status

    async def aiter_content(self) -> Any:
        self.read = True
        yield b"body"

    async def aclose(self) -> None:
        self.closed += 1


class _FakeCurlSession:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def request(self, *args: Any, **kwargs: Any) -> _FakeResponse:
        self.calls.append((args, kwargs))
        return self._response

    async def close(self) -> None:
        return None


def _session_with(
    monkeypatch: pytest.MonkeyPatch, response: _FakeResponse
) -> tuple[OzonSession, _FakeCurlSession]:
    session = OzonSession()
    fake = _FakeCurlSession(response)
    monkeypatch.setattr(session, "_session", fake)
    return session, fake


@pytest.mark.asyncio
async def test_get_redirect_returns_status_and_location_of_a_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(301, {"Location": _LOCATION}, quit_now=asyncio.Event())
    session, _ = _session_with(monkeypatch, response)

    redirect = await session.get_redirect("https://ozon.ru/t/x", timeout_seconds=3.0)

    assert redirect == OzonRedirect(301, _LOCATION)


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Location": ""}])
async def test_get_redirect_3xx_without_a_usable_location_has_none(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str]
) -> None:
    session, _ = _session_with(monkeypatch, _FakeResponse(302, headers))

    redirect = await session.get_redirect("https://ozon.ru/t/x", timeout_seconds=3.0)

    assert redirect == OzonRedirect(302, None)


@pytest.mark.asyncio
async def test_get_redirect_ignores_location_of_a_non_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _ = _session_with(monkeypatch, _FakeResponse(200, {"Location": _LOCATION}))

    redirect = await session.get_redirect("https://ozon.ru/t/x", timeout_seconds=3.0)

    assert redirect == OzonRedirect(200, None)


@pytest.mark.asyncio
async def test_get_redirect_sends_a_bare_streamed_get_with_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, fake = _session_with(monkeypatch, _FakeResponse(301, {}))

    await session.get_redirect("https://ozon.ru/t/x", timeout_seconds=3.5)

    ((args, kwargs),) = fake.calls
    assert args == ("GET", "https://ozon.ru/t/x")
    assert kwargs == {"timeout": 3.5, "stream": True}


@pytest.mark.asyncio
async def test_get_redirect_aborts_the_stream_closes_once_and_reads_no_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abort = asyncio.Event()
    response = _FakeResponse(301, {"Location": _LOCATION}, quit_now=abort)
    session, _ = _session_with(monkeypatch, response)

    await session.get_redirect("https://ozon.ru/t/x", timeout_seconds=3.0)

    assert abort.is_set()
    assert response.closed == 1
    assert not response.read


@pytest.mark.asyncio
async def test_get_redirect_without_an_abort_event_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _FakeResponse(200, quit_now=None)
    session, _ = _session_with(monkeypatch, response)

    await session.get_redirect("https://ozon.ru/t/x", timeout_seconds=3.0)

    assert response.closed == 1


@pytest.mark.asyncio
async def test_get_redirect_closes_the_response_when_reading_it_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    abort = asyncio.Event()
    response = _FakeResponse(301, quit_now=abort, broken_status=True)
    session, _ = _session_with(monkeypatch, response)

    with pytest.raises(RuntimeError):
        await session.get_redirect("https://ozon.ru/t/x", timeout_seconds=3.0)

    assert response.closed == 1
    assert abort.is_set()


# --- OzonClient.resolve_short_link on a planned get_redirect ----------------


class _RedirectSession(OzonSession):
    """Answers each `get_redirect` with the next plan (the last one repeats)."""

    def __init__(self, *plans: OzonRedirect | BaseException | object) -> None:
        self._plans = list(plans)
        self.requests: list[tuple[str, float]] = []
        self.started = asyncio.Event()

    async def get_redirect(self, url: str, *, timeout_seconds: float) -> OzonRedirect:
        plan = self._plans[min(len(self.requests), len(self._plans) - 1)]
        self.requests.append((url, timeout_seconds))
        self.started.set()
        if plan is HANG:
            await asyncio.Event().wait()
        if isinstance(plan, BaseException):
            raise plan
        assert isinstance(plan, OzonRedirect)
        return plan


def _capture() -> tuple[list[Record], int]:
    records: list[Record] = []

    def _sink(message: Message) -> None:
        records.append(message.record)

    return records, logger.add(_sink, level=0)


async def _resolve(
    *plans: OzonRedirect | BaseException | object, code: str = _CODE
) -> tuple[
    str | MarketplaceFetchFailure, _RedirectSession, StubOzonAccess, list[Record]
]:
    session = _RedirectSession(*plans)
    access = StubOzonAccess()
    records, sink_id = _capture()
    try:
        result = await OzonClient(session, access).resolve_short_link(code)
    finally:
        logger.remove(sink_id)
    return result, session, access, records


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_redirect_with_location_returns_the_location_as_is(status: int) -> None:
    result, _, _, _ = await _resolve(OzonRedirect(status, _LOCATION))

    assert result == _LOCATION


@pytest.mark.asyncio
async def test_one_request_to_the_canonical_url_with_the_client_deadline() -> None:
    _, session, access, _ = await _resolve(OzonRedirect(301, _LOCATION))

    assert session.requests == [("https://ozon.ru/t/7GeNMx2", 10.0)]
    assert access.get_calls == 0
    assert access.invalidated == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "reason"),
    [
        (OzonRedirect(301, None), FetchFailureReason.bad_payload),
        (OzonRedirect(302, None), FetchFailureReason.bad_payload),
        (OzonRedirect(200, None), FetchFailureReason.bad_payload),
        (OzonRedirect(403, None), FetchFailureReason.blocked),
        (OzonRedirect(429, None), FetchFailureReason.blocked),
        (OzonRedirect(404, None), FetchFailureReason.not_found),
        (OzonRedirect(500, None), FetchFailureReason.transport_error),
        (OzonRedirect(503, None), FetchFailureReason.transport_error),
        (OzonRedirect(418, None), FetchFailureReason.transport_error),
    ],
)
async def test_status_map_gives_the_category_of_r22(
    plan: OzonRedirect, reason: FetchFailureReason
) -> None:
    result, _, _, _ = await _resolve(plan)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is reason
    assert result.detail == f"HTTP {plan.status}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [CurlConnectionError("boom"), ProxyError("proxy down")]
)
async def test_curl_errors_are_transport_error_named_by_type(
    error: Exception,
) -> None:
    result, _, _, _ = await _resolve(error)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.transport_error, detail=type(error).__name__
    )


@pytest.mark.asyncio
async def test_missed_deadline_is_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(OzonClient, "TIMEOUT_SECONDS", 0.05)

    result, _, _, _ = await asyncio.wait_for(_resolve(HANG), timeout=5)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.transport_error, detail="TimeoutError"
    )


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed() -> None:
    session = _RedirectSession(HANG)
    task = asyncio.create_task(
        OzonClient(session, StubOzonAccess()).resolve_short_link(_CODE)
    )
    await asyncio.wait_for(session.started.wait(), timeout=5)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["", "a" * 33, "a/b", "a-b", "код", "a?b", "a_b", "١٢٣"]
)
async def test_invalid_code_is_bad_payload_without_a_request(code: str) -> None:
    result, session, access, _ = await _resolve(OzonRedirect(301, _LOCATION), code=code)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is FetchFailureReason.bad_payload
    assert session.requests == []
    assert access.get_calls == 0


@pytest.mark.asyncio
async def test_code_of_32_chars_is_requested_and_33_is_not() -> None:
    _, ok_session, _, _ = await _resolve(OzonRedirect(301, _LOCATION), code="a" * 32)
    _, bad_session, _, _ = await _resolve(OzonRedirect(301, _LOCATION), code="a" * 33)

    assert ok_session.requests == [("https://ozon.ru/t/" + "a" * 32, 10.0)]
    assert bad_session.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "plan",
    [
        OzonRedirect(404, None),
        OzonRedirect(403, None),
        OzonRedirect(500, None),
        OzonRedirect(200, None),
        CurlConnectionError("boom"),
    ],
)
async def test_neither_code_nor_location_reach_the_log_or_the_detail(
    plan: OzonRedirect | Exception,
) -> None:
    code = "SecretCode9"
    result, _, _, records = await _resolve(plan, code=code)

    assert records, "positive control: the scenario must log something"
    for record in records:
        assert code not in record["message"]
        assert "2622563074" not in record["message"]
        assert code not in str(record["extra"])
    if isinstance(result, MarketplaceFetchFailure):
        assert code not in (result.detail or "")


class _Stub(BaseMarketplaceClient):
    async def get_product_data(self, article: int) -> Any:
        raise NotImplementedError


@pytest.mark.asyncio
async def test_base_client_does_not_support_short_links_by_default() -> None:
    with pytest.raises(NotImplementedError):
        await _Stub().resolve_short_link(_CODE)
