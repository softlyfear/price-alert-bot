"""Tests for app.core.logging.setup_logging.

Companion coverage ticket to PAB-006 (convention 9): after this file,
app/core/logging.py must reach 100% line and branch coverage. The file
also carries a regression guard for the secret-leak channel PAB-006
closes (loguru's ``diagnose=True`` printing call-argument values into
extended tracebacks) and a regression guard for convention 20 - both
``logger.bind(...)`` and kwargs passed straight to ``logger.exception``
must reach the printed line through ``{extra}``.
"""

import os
import re
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from app.core.logging import setup_logging


@pytest.fixture(autouse=True)
def _restore_loguru_state() -> Iterator[None]:
    """Restore loguru to its pre-test handler state after each test.

    ``setup_logging()`` mutates process-global state: it removes every
    handler, including the stock stderr handler loguru installs on
    import (id 0). Left mutated, this silently changes the output of
    unrelated tests running later in the same process.

    Restoration is conditional on what was actually there before this
    test, not unconditional (PAB-054 review finding 3): under
    ``LOGURU_AUTOINIT=0`` loguru installs no stock handler at import
    time at all, and unconditionally adding a stderr handler back in
    teardown would hand every later test in the process a handler that
    never existed originally.

    Handler presence is read from loguru's internal handler registry,
    not by logging a probe message and reading it back through output
    capture: the stock handler holds a reference to the ``sys.stderr``
    object that existed at loguru's *import* time, which predates any
    pytest capture fixture swapping ``sys.stderr`` for a given test -
    exactly the mechanism that made ``capsys``/``capfd`` unreliable for
    the guard in PAB-054 review finding 1. Reading handler presence
    through capture would carry the identical blind spot here.
    """
    # loguru exposes no public API to count handlers without removing
    # them; `_core.handlers` is the internal handler registry.
    had_handlers = bool(logger._core.handlers)  # type: ignore[attr-defined]
    yield
    logger.remove()
    if had_handlers:
        logger.add(sys.stderr)


def test_setup_logging_removes_preexisting_handlers() -> None:
    """setup_logging() must remove every handler already registered.

    Regression guard for mutating ``logger.remove()`` inside
    setup_logging into a no-op. Unlike a capsys-based check on
    setup_logging's own stderr handler (PAB-054 review finding 1), the
    probe sink registered here is added and owned entirely by this
    test, so its receipt of the marker cannot be masked by pytest's
    output capture swapping ``sys.stderr`` mid-test: under the mutation
    the probe sink stays registered alongside setup_logging's new
    handler and receives the marker; on correct code it does not,
    because setup_logging removed it before adding its own handler.

    This guard's limit: it only proves setup_logging removes handlers
    that existed *before* it runs. It does not, by itself, prove two
    consecutive calls to setup_logging() leave exactly one handler -
    that is the narrower guarantee ``test_setup_logging_called_twice_...``
    below covers, and it is the only guard that mutation actually
    exercises.
    """
    records: list[str] = []
    logger.add(records.append, format="{message}")
    setup_logging()
    logger.info("single line marker")
    assert records == []


def test_setup_logging_called_twice_still_produces_a_single_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Idempotency: calling setup_logging() twice must not double output
    and must not leave the process without a working handler.

    This is the guard that actually catches mutating away the
    ``logger.remove()`` call inside setup_logging (PAB-054 review
    finding 1): both handlers added across the two calls are attached
    to the same capsys-visible ``sys.stderr``, so a duplicate handler
    surviving the second call doubles the captured line here - the
    property ``test_setup_logging_removes_preexisting_handlers`` above
    does not exercise.
    """
    setup_logging()
    setup_logging()
    logger.info("idempotent marker")
    captured = capsys.readouterr()
    lines = [line for line in captured.err.splitlines() if "idempotent marker" in line]
    assert len(lines) == 1


def test_setup_logging_prints_info_and_suppresses_debug(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging()
    logger.debug("debug marker should be suppressed")
    logger.info("info marker should be printed")
    captured = capsys.readouterr()
    assert "debug marker should be suppressed" not in captured.err
    assert "info marker should be printed" in captured.err


def test_context_via_bind_reaches_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Regression guard for convention 20: context attached through
    logger.bind(...) must reach the printed line via {extra}.
    """
    setup_logging()
    value = uuid.uuid4().hex
    logger.bind(marker_field=value).info("bound context marker")
    captured = capsys.readouterr()
    assert f"'marker_field': '{value}'" in captured.err


def test_context_via_exception_kwargs_reaches_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression guard for convention 20: kwargs passed directly to
    logger.exception(...) - the form used in
    app/services/notification.py:50 - must reach {extra} too. Removing
    {extra} as apparently-dead formatting would silently strip context
    from every live logger.exception call in the codebase.
    """
    setup_logging()
    try:
        raise RuntimeError("delivery failed")
    except RuntimeError:
        logger.exception(
            "Failed to send alert notification",
            alert_id=7,
            product_id=11,
        )
    captured = capsys.readouterr()
    assert "'alert_id': 7" in captured.err
    assert "'product_id': 11" in captured.err


def _inner_raises(secret: str) -> None:
    """The name ``secret`` on this call-argument line is exactly what
    loguru's diagnose=True renders: values of names occurring on a
    frame's active source line, not arbitrary locals of that frame.
    """
    raise RuntimeError("boom")


def _trigger_leak_prone_log() -> str:
    """Zero-argument entry point.

    Called by the test with no arguments, so the marker's name never
    appears on the test function's own active source line - only on
    ``_inner_raises``'s call-argument line below. This keeps the marker
    unreachable through the caller's (test's) own frame, per AC2.
    """
    secret = uuid.uuid4().hex
    try:
        _inner_raises(secret)
    except RuntimeError:
        logger.exception("failed with secret argument")
    return secret


def test_setup_logging_does_not_leak_call_argument_via_diagnose(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Regression guard for the secret-leak channel PAB-006 closes.

    A marker that lives only in a local variable never mentioned on a
    rendered line gives ``absent`` even with the protection fully
    disabled, proving nothing (Ловушка 1 of PAB-054). This guard instead
    passes the marker as a call argument on a traceback line
    (``_inner_raises(secret)``), the shape diagnose actually renders.

    Positive control (PAB-054 review finding 2): assert the log line
    itself reached the output before asserting the marker's absence.
    Without this, a mutation that silences logger.exception entirely
    (e.g. dropping the level below the message's level) makes the
    "marker not in captured.err" assertion pass on an empty string,
    turning the guard into a tautology.
    """
    setup_logging()
    marker = _trigger_leak_prone_log()
    captured = capsys.readouterr()
    assert "failed with secret argument" in captured.err
    assert marker not in captured.err


_LOGURU_AUTOINIT_REENTRY_GUARD_ENV = "PAB054_LOGURU_AUTOINIT_PROBE_ACTIVE"

_LOGURU_AUTOINIT_DISABLED_DRIVER = """
import sys

from loguru import logger

before = len(logger._core.handlers)

import pytest

exit_code = pytest.main(
    [
        "tests/unit/core/test_logging.py",
        "-q",
        "-k",
        "not test_fixture_restores_no_handler",
    ]
)

after = len(logger._core.handlers)
print(f"RESULT before={before} after={after} exit_code={int(exit_code)}")
"""


def test_fixture_restores_no_handler_when_loguru_autoinit_disabled() -> None:
    """Regression guard for PAB-054 review finding 3.

    Under ``LOGURU_AUTOINIT=0`` loguru installs no stock stderr handler
    at import time. ``_restore_loguru_state`` must not hand a later
    test a handler the process never had. This runs the rest of this
    file's suite (every other test here, each exercised through the
    real ``_restore_loguru_state`` fixture) as a nested, in-process
    ``pytest.main()`` call inside a subprocess started with
    ``LOGURU_AUTOINIT=0``, then compares the handler count read
    immediately after importing loguru to the handler count read after
    that whole nested run completes: they must be equal, and (per the
    manual check above) both zero.

    Run as a subprocess, not called in-process from this test:
    ``LOGURU_AUTOINIT`` is read once at loguru's import time, so it has
    to be set before the interpreter that imports loguru starts, and it
    must not touch the already-initialised loguru state of the outer
    pytest run executing this guard itself.

    ``-k "not test_restore_fixture_matches"`` excludes this test itself
    from the nested run - without it, the nested ``pytest.main()`` would
    collect and re-run this same test, which would spawn another
    subprocess, recursively without bound. The reentry-guard env var
    checked first is a second, unconditional line of defence against
    that exact runaway recursion in case the ``-k`` expression above
    ever stops matching this test's name (e.g. after a rename): a
    process that inherits the guard env var returns immediately instead
    of spawning a child.
    """
    if os.environ.get(_LOGURU_AUTOINIT_REENTRY_GUARD_ENV) == "1":
        return
    repo_root = Path(__file__).resolve().parents[3]
    env = {
        **os.environ,
        "LOGURU_AUTOINIT": "0",
        _LOGURU_AUTOINIT_REENTRY_GUARD_ENV: "1",
    }
    result = subprocess.run(
        [sys.executable, "-c", _LOGURU_AUTOINIT_DISABLED_DRIVER],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    match = re.search(r"RESULT before=(\d+) after=(\d+) exit_code=(\d+)", result.stdout)
    assert match is not None, result.stdout + result.stderr
    before, after, exit_code = (int(group) for group in match.groups())
    assert exit_code == 0, result.stdout + result.stderr
    assert before == 0
    assert after == before
