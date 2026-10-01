"""Unit tests for app.services.base_client.read_body_bounded (PAB-063).

Bodies are fed as async generators through a bare `httpx.Response`: no
network, and a chunk counter in the generator measures how much the
"transport" was asked to produce.
"""

from __future__ import annotations

import gzip
from collections.abc import AsyncIterator

import httpx
import pytest

from app.schemas.marketplace import FetchFailureReason
from app.schemas.marketplace import MarketplaceFetchFailure
from app.services.base_client import read_body_bounded

_CHUNK = 65_536
_LIMIT = 1_048_576


class _Counter:
    def __init__(self) -> None:
        self.yielded = 0


def _chunked(chunks: list[bytes], counter: _Counter) -> AsyncIterator[bytes]:
    async def gen() -> AsyncIterator[bytes]:
        for chunk in chunks:
            counter.yielded += 1
            yield chunk

    return gen()


@pytest.mark.asyncio
async def test_body_of_exactly_max_bytes_is_returned_whole() -> None:
    """AC1: the boundary is inclusive - `max_bytes` bytes pass intact."""
    response = httpx.Response(200, content=_chunked([b"a" * 10], _Counter()))

    result = await read_body_bounded(response, 10)

    assert result == b"a" * 10


@pytest.mark.asyncio
async def test_body_of_max_bytes_plus_one_is_bad_payload_naming_threshold() -> None:
    """AC1: one byte over the threshold is refused; `detail` names the
    threshold and carries no body bytes."""
    response = httpx.Response(200, content=_chunked([b"SECRET" * 2], _Counter()))

    result = await read_body_bounded(response, 11)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail="body exceeds 11 bytes"
    )
    assert isinstance(result, MarketplaceFetchFailure)
    assert result.detail is not None
    assert "SECRET" not in result.detail


@pytest.mark.asyncio
async def test_empty_body_is_returned_as_empty_bytes() -> None:
    """Zero-length body is within any threshold."""
    response = httpx.Response(200, content=_chunked([], _Counter()))

    assert await read_body_bounded(response, 10) == b""


@pytest.mark.asyncio
async def test_oversized_body_aborts_after_at_most_17_chunks_of_64() -> None:
    """AC2: 64 chunks of 64 KiB against a 1 MiB limit - reading stops at the
    first chunk crossing the threshold (the 17th), never pulling the rest.
    Reading the whole body (`aread`) would give 64 and fail here."""
    counter = _Counter()
    response = httpx.Response(200, content=_chunked([b"x" * _CHUNK] * 64, counter))

    result = await read_body_bounded(response, _LIMIT)

    assert isinstance(result, MarketplaceFetchFailure)
    assert result.reason is FetchFailureReason.bad_payload
    assert counter.yielded == 17


@pytest.mark.asyncio
async def test_threshold_applies_to_decoded_bytes_not_compressed_size() -> None:
    """AC3: a gzip body far below the limit on the wire that inflates past
    it is refused - the cap is on decoded bytes."""
    raw = b"\x00" * (_LIMIT + 1)
    compressed = gzip.compress(raw)
    assert len(compressed) < _LIMIT // 100
    counter = _Counter()
    response = httpx.Response(
        200,
        headers={"Content-Encoding": "gzip"},
        content=_chunked([compressed], counter),
    )

    result = await read_body_bounded(response, _LIMIT)

    assert result == MarketplaceFetchFailure(
        reason=FetchFailureReason.bad_payload, detail=f"body exceeds {_LIMIT} bytes"
    )


@pytest.mark.asyncio
async def test_httpx_error_during_read_is_not_swallowed() -> None:
    """AC1: errors raised while reading propagate to the calling client."""

    async def gen() -> AsyncIterator[bytes]:
        yield b"abc"
        raise httpx.ReadError("reset")

    response = httpx.Response(200, content=gen())

    with pytest.raises(httpx.ReadError):
        await read_body_bounded(response, 100)
