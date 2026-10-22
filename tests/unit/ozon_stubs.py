"""Hand-written fakes at the two boundaries of `OzonClient` (PAB-110).

- `StubOzonAccess` is an `OzonAccessProvider`: it hands out the current access,
  makes a new one after `invalidate`, and records every call.
- `FakeOzonSession` replaces the transport (`OzonSession.get`): it answers on a
  plan and records every request, so a test asserts on the observable effects
  (how many requests, with which cookies) and not on how the client is built.
  The real `curl_cffi` transport is covered against a local server in
  `tests/integration/test_ozon_transport_live.py`.
"""

import asyncio

from app.services.ozon_access import OzonAccess
from app.services.ozon_access import OzonAccessUnavailable
from app.services.ozon_client import OzonReply
from app.services.ozon_client import OzonSession

HANG = object()

Plan = OzonReply | BaseException | object


def make_access(n: int) -> OzonAccess:
    """A synthetic access; the values are recognisable in a request, not real."""
    return OzonAccess(
        cookies=(
            ("__Secure-access-token", f"OZON-TOKEN-MARKER-{n}"),
            ("abt_data", f"OZON-ABT-MARKER-{n}"),
        ),
        user_agent=f"Mozilla/5.0 UA-MARKER-{n}",
    )


class StubOzonAccess:
    def __init__(self, *refusals: OzonAccessUnavailable | None) -> None:
        """`refusals[i]` (if not `None`) is the answer of the i-th `get`;
        past the list, accesses are handed out."""
        self._refusals = list(refusals)
        self._current: OzonAccess | None = None
        self._issued = 0
        self.get_calls = 0
        self.invalidated: list[OzonAccess] = []
        self.issued: list[OzonAccess] = []

    async def get(self) -> OzonAccess | OzonAccessUnavailable:
        index = self.get_calls
        self.get_calls += 1
        if index < len(self._refusals):
            refusal = self._refusals[index]
            if refusal is not None:
                return refusal
        if self._current is None:
            self._issued += 1
            self._current = make_access(self._issued)
            self.issued.append(self._current)
        return self._current

    async def invalidate(self, access: OzonAccess) -> None:
        self.invalidated.append(access)
        if self._current == access:
            self._current = None


class FakeOzonSession(OzonSession):
    """Answers each `get` with the next plan (the last one repeats): an
    `OzonReply` is returned, an exception raised, `HANG` waits forever."""

    def __init__(self, *plans: Plan) -> None:
        # No `curl_cffi` session: the transport is replaced entirely.
        self._plans = list(plans)
        self.requests: list[tuple[str, dict[str, str], OzonAccess, float, int]] = []
        self.closed = False
        self.started = asyncio.Event()

    async def get(
        self,
        url: str,
        *,
        params: dict[str, str],
        access: OzonAccess,
        timeout_seconds: float,
        max_body_bytes: int,
    ) -> OzonReply:
        plan = self._plans[min(len(self.requests), len(self._plans) - 1)]
        self.requests.append((url, params, access, timeout_seconds, max_body_bytes))
        self.started.set()
        if plan is HANG:
            await asyncio.Event().wait()
        if isinstance(plan, BaseException):
            raise plan
        assert isinstance(plan, OzonReply)
        return plan

    async def aclose(self) -> None:
        self.closed = True
