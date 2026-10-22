"""Cookie values stay out of a failing test's output on the real path (PAB-110 AC13).

A child pytest runs a throw-away test that calls the real `OzonSession.get`
against a local port that was bound and then released, so the connection is
refused and the `curl_cffi` exception propagates through the transport frames.
Those frames' locals are printed by `--showlocals --tb=long`. The marker of
the access cookie must be absent from the output; a positive control in the
same run puts a different marker in an open local variable and it must be
visible, so a silent output (no locals printed) cannot pass the check. Both
markers are assembled at run time: the source lines echoed by a traceback do
not hold them, only values do.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COOKIE_MARKER = "COOKIE-MARKER-9f3a"
_CONTROL_MARKER = "CONTROL-MARKER-4d2b"

_PROBE = """
import asyncio
import socket

from app.services.ozon_access import OzonAccess
from app.services.ozon_client import OzonClient
from app.services.ozon_client import OzonSession


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def test_real_transport_failure_masks_cookie_values():
    access = OzonAccess(
        cookies=(("__Secure-access-token", "COOKIE-" + "MARKER-9f3a"),),
        user_agent="UA",
    )
    url = f"http://127.0.0.1:{_closed_port()}/"

    async def call():
        session = OzonSession()
        try:
            await session.get(
                url,
                params={},
                access=access,
                timeout_seconds=5.0,
                max_body_bytes=OzonClient.MAX_BODY_BYTES,
            )
        finally:
            await session.aclose()

    asyncio.run(call())


def test_positive_control_open_local_is_visible():
    cookies = {"__Secure-access-token": "CONTROL-" + "MARKER-4d2b"}
    value = cookies["__Secure-access-token"]
    assert False, "control"
"""


def _run_probe(tmp_path: Path, *flags: str) -> str:
    probe = tmp_path / "test_probe.py"
    probe.write_text(textwrap.dedent(_PROBE))
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


@pytest.mark.parametrize(
    "flags",
    [("-q",), ("-q", "--showlocals", "--tb=long")],
    ids=["q", "showlocals-tb-long"],
)
def test_transport_exception_never_shows_the_cookie_value(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    output = _run_probe(tmp_path, *flags)

    assert "ConnectionError" in output  # the real transport raised and it escaped
    assert _COOKIE_MARKER not in output


def test_positive_control_shows_the_open_marker_under_showlocals(
    tmp_path: Path,
) -> None:
    output = _run_probe(tmp_path, "-q", "--showlocals", "--tb=long")

    assert _CONTROL_MARKER in output
