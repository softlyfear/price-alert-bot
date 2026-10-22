"""Proxy credentials stay out of logs, `detail`, `repr` and test output (PAB-111 AC8).

In-process: a `loguru` sink collects everything written while the real relay,
the pre-check and `OzonSession` fail against a stand-in. Out of process: a
child `pytest` runs throw-away tests that fail through the two real paths (the
`curl_cffi` request refused by the proxy, the pre-check refused by the proxy)
and its output is read under `-q` and under `--showlocals --tb=long`. The
positive controls are in the output that is checked for the absence: the
traceback passes the real frames (`ozon_client.py` + `curl_cffi`, or
`socks_relay.py`) and an open local of a control test is printed. Markers are
assembled at run time, so the source lines a traceback echoes do not hold them.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
import traceback
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from app.services.ozon_client import OzonSession
from app.services.socks_relay import SocksError
from app.services.socks_relay import SocksProxy
from app.services.socks_relay import SocksRelay
from app.services.socks_relay import check_proxy
from tests.integration.socks_stub import SocksStub
from tests.unit.ozon_stubs import make_access

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOGIN = "LOGIN-MARKER-7c1e"
_PASSWORD = "PASS-MARKER-5b90"
_HOST = "hostmark-3d8a.invalid"
_CONTROL_MARKER = "CONTROL-MARKER-4d2b"
_FUSE = 30.0


@pytest.fixture
def journal() -> Iterator[list[str]]:
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(str(m)), level=0, diagnose=False)
    try:
        yield lines
    finally:
        logger.remove(sink)


def _assert_clean(text: str) -> None:
    for marker in (_LOGIN, _PASSWORD, "hostmark-3d8a"):
        assert marker not in text, marker


def _named_host_proxy() -> SocksProxy:
    return SocksProxy.from_url(f"socks5://{_LOGIN}:{_PASSWORD}@{_HOST}:1080")


@pytest.mark.asyncio
async def test_precheck_failure_text_cause_and_repr_carry_no_marker(
    journal: list[str],
) -> None:
    proxy = _named_host_proxy()

    with pytest.raises(SocksError) as caught:
        await asyncio.wait_for(check_proxy(proxy, "www.ozon.test"), _FUSE)

    failure = caught.value
    assert failure.detail == "proxy_unreachable"  # positive control: the path ran
    assert isinstance(failure.__cause__, OSError)  # ... with a real cause
    for text in (
        failure.detail,
        str(failure),
        repr(failure),
        repr(proxy),
        "".join(traceback.format_exception(failure)),
        "".join(journal),
    ):
        _assert_clean(text)


@pytest.mark.asyncio
async def test_relay_failure_is_logged_without_marker(journal: list[str]) -> None:
    proxy = _named_host_proxy()
    async with SocksRelay(proxy) as relay:
        host, _, port = relay.url.removeprefix("http://").partition(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(b"CONNECT www.ozon.test:443 HTTP/1.1\r\n\r\n")
        await writer.drain()
        answer = await asyncio.wait_for(reader.read(), _FUSE)
        writer.close()
        description = repr(relay)

    assert answer == b"HTTP/1.1 502 Bad Gateway\r\n\r\n"
    assert any("upstream refused: proxy_unreachable" in line for line in journal)
    _assert_clean("".join(journal))
    _assert_clean(description)


@pytest.mark.asyncio
async def test_session_refused_by_the_proxy_leaves_no_marker_in_the_journal(
    journal: list[str],
) -> None:
    with SocksStub(mode="reject_auth", username=_LOGIN, password=_PASSWORD) as stub:
        proxy = SocksProxy.from_url(
            f"socks5://{_LOGIN}:{_PASSWORD}@127.0.0.1:{stub.port}"
        )
        session = OzonSession(proxy)
        try:
            with pytest.raises(Exception) as caught:  # noqa: PT011 - curl_cffi's own type
                await session.get(
                    "http://ozon-api.test/",
                    params={},
                    access=make_access(1),
                    timeout_seconds=5.0,
                    max_body_bytes=1000,
                )
        finally:
            await session.aclose()

    assert type(caught.value).__name__ == "ProxyError"  # positive control
    assert [c.username for c in stub.connections] == [_LOGIN]  # it was really sent
    _assert_clean("".join(journal))
    _assert_clean(repr(session))


# --- child pytest: the output of failing tests ------------------------------------

_PROBE_COMMON = """
import asyncio

from app.services.ozon_client import OzonSession
from app.services.socks_relay import SocksProxy
from app.services.socks_relay import check_proxy
from tests.integration.socks_stub import SocksStub
from tests.unit.ozon_stubs import make_access

_USER = "LOGIN-" + "MARKER-7c1e"
_PASS = "PASS-" + "MARKER-5b90"


def _proxy(port):
    url = "socks5://" + _USER + ":" + _PASS + "@127.0.0.1:" + str(port)
    return SocksProxy.from_url(url)


def test_positive_control_open_local_is_visible():
    value = "CONTROL-" + "MARKER-4d2b"
    assert False, "control"
"""

_PROBE_CLIENT = (
    _PROBE_COMMON
    + """

def test_session_get_refused_by_the_proxy():
    with SocksStub(mode="reject_auth", username=_USER, password=_PASS) as stub:
        proxy = _proxy(stub.port)

        async def call():
            session = OzonSession(proxy)
            try:
                await session.get(
                    "http://ozon-api.test/",
                    params={},
                    access=make_access(1),
                    timeout_seconds=5.0,
                    max_body_bytes=1000,
                )
            finally:
                await session.aclose()

        asyncio.run(call())
"""
)

_PROBE_PRECHECK = (
    _PROBE_COMMON
    + """

def test_precheck_refused_by_the_proxy():
    with SocksStub(mode="reject_auth", username=_USER, password=_PASS) as stub:
        proxy = _proxy(stub.port)
        asyncio.run(check_proxy(proxy, "www.ozon.test"))
"""
)


def _run_probe(tmp_path: Path, source: str, *flags: str) -> str:
    probe = tmp_path / "test_probe.py"
    probe.write_text(textwrap.dedent(source))
    done = subprocess.run(  # noqa: S603 - fixed argv, child pytest on a temp file
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--rootdir",
            str(tmp_path),
            "-c",
            "/dev/null",
            *flags,
            str(probe),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
    )
    output = done.stdout + done.stderr
    assert done.returncode == 1, output
    assert "2 failed" in output, output  # both probe tests failed, as designed
    return output


_FLAGS = pytest.mark.parametrize(
    "flags",
    [("-q",), ("-q", "--showlocals", "--tb=long")],
    ids=["q", "showlocals-tb-long"],
)


def _assert_controls(output: str, flags: tuple[str, ...]) -> None:
    if "--showlocals" in flags:
        assert _CONTROL_MARKER in output  # locals are printed at all


@_FLAGS
def test_session_failure_output_never_shows_login_or_password(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    output = _run_probe(tmp_path, _PROBE_CLIENT, *flags)

    assert "ProxyError" in output  # the real transport raised and it escaped
    assert "ozon_client.py" in output  # through our frame ...
    assert "curl_cffi" in output  # ... and the real library frames
    _assert_controls(output, flags)
    assert _LOGIN not in output
    assert _PASSWORD not in output


@_FLAGS
def test_precheck_failure_output_never_shows_login_or_password(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    output = _run_probe(tmp_path, _PROBE_PRECHECK, *flags)

    assert "SocksError" in output  # the real pre-check raised and it escaped
    assert "socks_relay.py" in output  # through the frames of the module
    _assert_controls(output, flags)
    assert _LOGIN not in output
    assert _PASSWORD not in output
