"""Cookie values stay out of `pytest --showlocals` output (PAB-110 AC13).

The values of an Ozon access are local variables of the transport and the
client; a failing test prints them under `--showlocals --tb=long`. The check
is by observation: a throw-away test file is run by a child pytest, once with
the cookies as `request_cookies()` returns them and once, as a positive
control, with plain strings - the marker must be absent from the first output
and present in the second. The probe assembles the marker at run time so that
the source lines echoed by a traceback do not hold it, only the values do.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MARKER = "COOKIE-MARKER-7c1e"

_MASKED = """
from app.services.ozon_access import OzonAccess

def test_fails_with_masked_locals():
    access = OzonAccess(cookies=(("__Secure-access-token", "COOKIE-" + "MARKER-7c1e"),),
                        user_agent="UA")
    cookies = access.request_cookies()
    value = cookies["__Secure-access-token"]
    assert False, "boom"
"""

_PLAIN = """
def test_fails_with_plain_locals():
    cookies = {"__Secure-access-token": "COOKIE-" + "MARKER-7c1e"}
    value = cookies["__Secure-access-token"]
    assert False, "boom"
"""


def _run(tmp_path: Path, source: str, *flags: str) -> str:
    (tmp_path / "test_probe.py").write_text(textwrap.dedent(source))
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
            str(tmp_path / "test_probe.py"),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=120,
        check=False,
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
    )
    assert done.returncode == 1, done.stdout + done.stderr  # the probe test fails
    return done.stdout + done.stderr


def test_plain_locals_show_the_marker_positive_control(tmp_path: Path) -> None:
    output = _run(tmp_path, _PLAIN, "-q", "--showlocals", "--tb=long")

    assert _MARKER in output


def test_masked_cookie_locals_never_show_the_marker(tmp_path: Path) -> None:
    output = _run(tmp_path, _MASKED, "-q", "--showlocals", "--tb=long")

    assert "test_fails_with_masked_locals" in output  # the probe really failed
    assert "cookies" in output  # and its locals were printed
    assert _MARKER not in output


def test_masked_cookie_failure_under_plain_q_never_shows_the_marker(
    tmp_path: Path,
) -> None:
    assert _MARKER not in _run(tmp_path, _MASKED, "-q")
