"""Tests for app.core.logging.setup_logging.

Companion coverage ticket to PAB-006 (convention 9): after this file,
app/core/logging.py must reach 100% line and branch coverage. The file
also carries a regression guard for the secret-leak channel PAB-006
closes (loguru's ``diagnose=True`` printing call-argument values into
extended tracebacks) and a regression guard for convention 20 - both
``logger.bind(...)`` and kwargs passed straight to ``logger.exception``
must reach the printed line through ``{extra}``.
"""

import logging
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import pytest
from loguru import logger

from app.core.logging import _InterceptHandler
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


def test_restore_loguru_state_fixture_rebuilds_handler_on_ordinary_run() -> None:
    """Witness for ``_restore_loguru_state``'s ordinary branch (convention 22).

    ``test_fixture_restores_no_handler_when_loguru_autoinit_disabled``
    below only exercises the fixture's *rare* branch, where
    ``LOGURU_AUTOINIT=0`` and ``had_handlers`` is ``False`` -- the branch
    that never re-adds a handler. Nothing in this file previously checked
    the branch every ordinary test run actually takes: ``had_handlers``
    is ``True`` under the process' real (enabled) ``LOGURU_AUTOINIT``,
    the one this whole suite runs under by default, so this is the
    direction that matters for every other test in this file.

    Drives the fixture directly through ``__wrapped__`` -- the plain
    generator function ``pytest.fixture`` preserves there for
    introspection; calling the fixture object itself is rejected
    ("Fixture ... called directly"). No nested ``pytest.main()``
    subprocess is needed here, unlike the ``LOGURU_AUTOINIT=0`` guard
    below: this process already runs under the ordinary, enabled
    ``LOGURU_AUTOINIT``, so the handler count read directly below is
    already the value of interest.

    The assertion is ``after == before`` rather than a hard-coded
    ``before == 1``, because this test is also collected (and run) by
    the nested ``pytest.main()`` inside
    ``test_fixture_restores_no_handler_when_loguru_autoinit_disabled``
    below, where the environment starts with zero handlers instead; the
    invariant checked here holds identically in both regimes. On an
    ordinary top-level run, measured directly: ``before=1``, ``after=1``.

    Mutation ``if had_handlers:`` -> ``if False:`` is caught here: on an
    ordinary run this discards the re-add step, so ``after`` becomes 0
    while ``before`` stays 1.
    """
    before = len(logger._core.handlers)  # type: ignore[attr-defined]

    gen = _restore_loguru_state.__wrapped__()  # type: ignore[attr-defined]
    next(gen)
    with pytest.raises(StopIteration):
        next(gen)

    after = len(logger._core.handlers)  # type: ignore[attr-defined]
    assert after == before


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

    Positive control (convention 22): before setup_logging() ever runs,
    a log call must actually reach the probe sink -- proving the sink is
    registered and this logger is not, some other way, already muted.
    Without this, a combined mutation that both turns ``logger.remove()``
    into a no-op *and* silences this test's own logger (e.g. a stray
    ``logger.disable(__name__)``) would leave ``records`` empty for the
    wrong reason and pass regardless of what setup_logging() does --
    measured on a copy: that exact combination gives ``1 passed`` against
    the pre-fix version of this test, a false green on a fully broken
    guard.

    This guard's limit: it only proves setup_logging removes handlers
    that existed *before* it runs. It does not, by itself, prove two
    consecutive calls to setup_logging() leave exactly one handler -
    that is the narrower guarantee ``test_setup_logging_called_twice_...``
    below covers. Mutating away ``logger.remove()`` inside setup_logging
    now turns *both* tests red, not just that one -- see its docstring.
    """
    records: list[str] = []
    logger.add(records.append, format="{message}")
    logger.info("probe alive marker")
    assert records == ["probe alive marker\n"]
    records.clear()

    setup_logging()
    logger.info("single line marker")
    assert records == []


def test_setup_logging_called_twice_still_produces_a_single_handler(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Idempotency: calling setup_logging() twice must not double output
    and must not leave the process without a working handler.

    Mutating away the ``logger.remove()`` call inside setup_logging
    (PAB-054 review finding 1) turns this test red too, not only this
    one: both handlers added across the two calls are attached to the
    same capsys-visible ``sys.stderr``, so a duplicate handler surviving
    the second call doubles the captured line here. What this test
    exercises and ``test_setup_logging_removes_preexisting_handlers``
    above does not is specifically duplication *across two calls* - the
    other test only ever calls setup_logging() once.
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
    that whole nested run completes: they must be equal, and both zero -
    the assertions below check both directly, on the values parsed out
    of the nested run's own output.

    Run as a subprocess, not called in-process from this test:
    ``LOGURU_AUTOINIT`` is read once at loguru's import time, so it has
    to be set before the interpreter that imports loguru starts, and it
    must not touch the already-initialised loguru state of the outer
    pytest run executing this guard itself.

    ``-k "not test_fixture_restores_no_handler"`` excludes this test
    itself from the nested run - without it, the nested ``pytest.main()`` would
    collect and re-run this same test, which would spawn another
    subprocess, recursively without bound. The reentry-guard env var
    checked first is a second, unconditional line of defence against
    that exact runaway recursion in case the ``-k`` expression above
    ever stops matching this test's name (e.g. after a rename): a
    process that inherits the guard env var returns immediately instead
    of spawning a child.
    """
    if os.environ.get(_LOGURU_AUTOINIT_REENTRY_GUARD_ENV) == "1":
        pytest.skip(
            "reentry guard tripped: this process is itself the nested "
            "pytest.main() run spawned by an outer invocation of this "
            "same test, reached only if the -k filter above stops "
            "matching this test's name"
        )
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


# --- PAB-083: one redacting sink for loguru, stdlib logging and uvicorn ----

_MARKER = "secret-part-marker-pab083"
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LINE = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} \| (?P<level>\S+ *) \| "
    r"(?P<name>[\w.]+):(?P<func>\w+):(?P<line>\d+) \| (?P<message>.*) \| \{.*\}$"
)


def _emit_message() -> None:
    logger.info(f"around {_MARKER} text")


def _emit_bound_extra() -> None:
    logger.bind(token=_MARKER).info("around text")


def _emit_exception_object_in_extra() -> None:
    exc = RuntimeError(f"failure {_MARKER}")
    logger.bind(error_type="RuntimeError").error(
        "task died: {exc}", exc=exc
    )  # the _log_task_failure shape
    logger.bind(exc=exc).info("around text")


def _emit_exception_text_in_traceback() -> None:
    try:
        raise RuntimeError(f"failure {_MARKER}")
    except RuntimeError:
        logger.exception("around text")


def _emit_cause_in_traceback() -> None:
    try:
        try:
            raise ValueError(f"cause {_MARKER}")
        except ValueError as cause:
            raise RuntimeError("outer failure") from cause
    except RuntimeError:
        logger.exception("around text")


# (id, emitter, text outside the marker that must survive the cut)
_FORMS: list[tuple[str, Callable[[], None], str]] = [
    ("message", _emit_message, "around"),
    ("bound_extra", _emit_bound_extra, "'token': '***'"),
    ("exception_object_in_extra", _emit_exception_object_in_extra, "task died"),
    ("exception_text", _emit_exception_text_in_traceback, "RuntimeError: failure ***"),
    ("exception_cause", _emit_cause_in_traceback, "ValueError: cause ***"),
]
_FORM_PARAMS = [pytest.param(emit, keep, id=name) for name, emit, keep in _FORMS]


@pytest.mark.parametrize(("emit", "keep"), _FORM_PARAMS)
def test_secret_is_cut_from_every_part_of_the_record(
    capsys: pytest.CaptureFixture[str], emit: Callable[[], None], keep: str
) -> None:
    setup_logging(redact=[_MARKER])
    emit()
    err = capsys.readouterr().err
    # Positive controls: the record was written, the mask and the text around
    # the secret survived, and there is no ANSI colouring on a non-tty.
    assert keep in err
    assert _ANSI.search(err) is None
    assert _MARKER not in err


@pytest.mark.parametrize(("emit", "keep"), _FORM_PARAMS)
def test_control_cell_without_secrets_keeps_the_marker_in_output(
    capsys: pytest.CaptureFixture[str], emit: Callable[[], None], keep: str
) -> None:
    """Proves the probe forms really carry the marker (no vacuous 'absent')."""
    setup_logging()
    emit()
    err = capsys.readouterr().err
    assert keep.replace("***", _MARKER) in err
    assert "***" not in err


def test_empty_redact_value_inserts_no_mask(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(redact=[""])
    logger.info("plain message text")
    err = capsys.readouterr().err
    assert "plain message text" in err
    assert "***" not in err


def test_empty_value_does_not_disable_a_real_secret_next_to_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(redact=["", _MARKER])
    logger.info(f"around {_MARKER} text")
    err = capsys.readouterr().err
    assert "around *** text" in err
    assert _MARKER not in err


def _root_interceptors() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, _InterceptHandler)]


def test_two_calls_leave_one_loguru_handler_one_interceptor_and_one_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    root_before = list(logging.getLogger().handlers)
    setup_logging()
    setup_logging(redact=[_MARKER])
    assert len(logger._core.handlers) == 1  # type: ignore[attr-defined]
    assert len(_root_interceptors()) == 1
    assert len(logging.getLogger().handlers) == len(root_before) + 1

    logging.getLogger("aiogram.dispatcher").error("stdlib %s", _MARKER)
    logger.error(f"loguru {_MARKER}")
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert len(lines) == 2
    assert "stdlib ***" in lines[0]
    assert "loguru ***" in lines[1]
    assert _MARKER not in err


def test_last_call_defines_the_state_a_call_without_secrets_cuts_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(redact=[_MARKER])
    setup_logging()
    logger.info(f"around {_MARKER} text")
    err = capsys.readouterr().err
    assert f"around {_MARKER} text" in err
    assert len(_root_interceptors()) == 1


def test_interceptor_keeps_foreign_root_handlers_and_root_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = logging.getLogger()
    level_before = root.level
    foreign = logging.NullHandler()
    root.addHandler(foreign)
    try:
        assert caplog.handler in root.handlers  # positive control
        setup_logging(redact=[_MARKER])
        logging.getLogger("aiogram.dispatcher").error("canary %s", "visible")
        assert foreign in root.handlers
        assert caplog.handler in root.handlers
        assert "canary visible" in caplog.text
        assert root.level == level_before
    finally:
        root.removeHandler(foreign)


def test_stdlib_error_is_one_line_in_the_loguru_format_without_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(redact=[_MARKER])
    logging.getLogger("aiogram.dispatcher").error(
        "Failed to fetch updates - %s: %s", "TelegramNetworkError", f"url {_MARKER}"
    )
    err = capsys.readouterr().err
    lines = err.splitlines()
    assert len(lines) == 1
    match = _LINE.match(lines[0])
    assert match is not None, lines[0]
    assert match["level"].strip() == "ERROR"
    assert match["name"] == __name__
    assert match["func"] == (
        "test_stdlib_error_is_one_line_in_the_loguru_format_without_secret"
    )
    assert match["message"] == "Failed to fetch updates - TelegramNetworkError: url ***"
    assert _MARKER not in err


def test_stdlib_warning_passes_but_info_and_debug_are_dropped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    probe = logging.getLogger("pab083.level-probe")
    probe.setLevel(logging.DEBUG)  # the logger lets all through: the handler decides
    try:
        setup_logging()
        probe.debug("debug text")
        probe.info("info text")
        probe.warning("warning text")
    finally:
        probe.setLevel(logging.NOTSET)
    err = capsys.readouterr().err
    assert "warning text" in err
    assert "info text" not in err
    assert "debug text" not in err


def test_stdlib_exc_info_traceback_is_cut(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(redact=[_MARKER])
    try:
        raise RuntimeError(f"failure {_MARKER}")
    except RuntimeError:
        logging.getLogger("aiogram.dispatcher").error("handler failed", exc_info=True)
    err = capsys.readouterr().err
    assert "handler failed" in err
    assert "RuntimeError: failure ***" in err
    assert "Traceback" in err
    assert _MARKER not in err


def test_stdlib_braces_in_external_text_are_printed_as_is(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging()
    logging.getLogger("aiogram.dispatcher").error("bad {x} {0} {} %s", "{y}")
    err = capsys.readouterr().err
    assert "bad {x} {0} {} {y}" in err


def test_stdlib_record_with_a_level_unknown_to_loguru_is_logged_by_number(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Level 35 is named ``Level 35`` by stdlib, loguru has no such name."""
    setup_logging()
    logging.getLogger("pab083.custom").log(35, "custom level text")
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 1
    assert "Level 35" in lines[0]
    assert "custom level text" in lines[0]


def test_uvicorn_logger_has_one_interceptor_instead_of_its_own_handlers() -> None:
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.addHandler(logging.StreamHandler())
    uvicorn_logger.propagate = False
    setup_logging()
    assert len(uvicorn_logger.handlers) == 1
    assert isinstance(uvicorn_logger.handlers[0], _InterceptHandler)
    assert uvicorn_logger.propagate is False
    setup_logging()
    assert len(uvicorn_logger.handlers) == 1


def test_uvicorn_propagate_is_forced_off() -> None:
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_logger.propagate = True
    setup_logging()
    assert uvicorn_logger.propagate is False


def test_uvicorn_error_traceback_text_and_exc_info_are_cut_one_line_each(
    capsys: pytest.CaptureFixture[str],
) -> None:
    setup_logging(redact=[_MARKER])
    error_logger = logging.getLogger("uvicorn.error")
    error_logger.error(f"Traceback (most recent call last): boom {_MARKER}")
    try:
        raise RuntimeError(f"startup {_MARKER}")
    except RuntimeError as exc:
        error_logger.error("Exception in 'lifespan' protocol", exc_info=exc)
    err = capsys.readouterr().err
    assert len(re.findall(r"^\d{4}-", err, flags=re.MULTILINE)) == 2
    assert "boom ***" in err
    assert "Exception in 'lifespan' protocol" in err
    assert "RuntimeError: startup ***" in err
    assert _MARKER not in err


def test_uvicorn_access_logger_is_left_untouched() -> None:
    access = logging.getLogger("uvicorn.access")
    sentinel = logging.NullHandler()
    access.addHandler(sentinel)
    access.propagate = False
    before = (list(access.handlers), access.propagate, access.level)
    setup_logging(redact=[_MARKER])
    after = (list(access.handlers), access.propagate, access.level)
    assert sentinel in access.handlers  # positive control: state is non-trivial
    assert after == before


def test_stdlib_state_fixture_removes_interceptors_and_restores_uvicorn() -> None:
    """Witness for the conftest fixture (AC7): drives it through ``__wrapped__``."""
    from tests.conftest import _restore_stdlib_logging_state

    uvicorn_logger = logging.getLogger("uvicorn")
    handlers_before = list(uvicorn_logger.handlers)
    propagate_before = uvicorn_logger.propagate
    root_before = list(logging.getLogger().handlers)

    gen = _restore_stdlib_logging_state.__wrapped__()  # type: ignore[attr-defined]
    next(gen)
    setup_logging()
    assert len(_root_interceptors()) == 1  # positive control: state was mutated
    uvicorn_logger.propagate = True
    with pytest.raises(StopIteration):
        next(gen)

    assert _root_interceptors() == []
    assert logging.getLogger().handlers == root_before
    assert uvicorn_logger.handlers == handlers_before
    assert uvicorn_logger.propagate is propagate_before


# --- PAB-084: a malformed stdlib record must not raise into the caller -----

_FALLBACK_TEXT = "Unformattable stdlib log record from"


@pytest.fixture
def _bare_root_logger() -> Iterator[Callable[[], None]]:
    """Give tests a callable that detaches pytest's own root handlers.

    pytest attaches its capture handlers at the start of the call phase, after
    fixtures ran, so the detaching has to happen inside the test body. Those
    handlers format records too and would themselves raise on a malformed one,
    hiding what the interceptor does. Teardown restores the root handler list.
    """
    root = logging.getLogger()
    saved = list(root.handlers)

    def detach() -> None:
        root.handlers[:] = []

    try:
        yield detach
    finally:
        root.handlers[:] = saved


def _record_starts(err: str) -> list[str]:
    return re.findall(r"^\d{4}-.*$", err, flags=re.MULTILINE)


@pytest.mark.parametrize("logger_name", ["x", "uvicorn.error"])
def test_malformed_stdlib_record_gives_one_fallback_error_without_secret(
    _bare_root_logger: Callable[[], None],
    capsys: pytest.CaptureFixture[str],
    logger_name: str,
) -> None:
    _bare_root_logger()
    setup_logging(redact=[_MARKER])
    stdlib_logger = logging.getLogger(logger_name)

    stdlib_logger.error("%s %s", _MARKER)  # two placeholders, one argument
    stdlib_logger.error("next record %s", "fine")

    err = capsys.readouterr().err
    starts = _record_starts(err)
    assert len(starts) == 2
    match = _LINE.match(starts[0])
    assert match is not None, starts[0]
    assert match["level"].strip() == "ERROR"
    assert match["message"] == f"{_FALLBACK_TEXT} {logger_name}: TypeError"
    assert "TypeError" in err
    assert "next record fine" in starts[1]  # positive control: output still works
    assert _MARKER not in err
    assert "Logging error" not in err  # Handler.handleError output is absent


def test_fallback_record_does_not_echo_the_message_or_parse_braces_in_names(
    _bare_root_logger: Callable[[], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _bare_root_logger()
    setup_logging()
    odd_name = "pab084.{0}{x}{}"

    logging.getLogger(odd_name).error("leak_{x} {} {0} %d", "not-a-number")

    err = capsys.readouterr().err
    starts = _record_starts(err)
    assert len(starts) == 1
    assert f"{_FALLBACK_TEXT} {odd_name}: TypeError" in starts[0]
    assert "leak_" not in err
    assert "not-a-number" not in err


class _StopNotAnException(BaseException):
    """Stands for ``KeyboardInterrupt``-like exceptions outside ``Exception``."""


class _ExplodingMessage:
    def __str__(self) -> str:
        raise _StopNotAnException


def test_base_exception_outside_exception_reaches_the_caller_without_fallback(
    _bare_root_logger: Callable[[], None],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _bare_root_logger()
    setup_logging()
    stdlib_logger = logging.getLogger("x")

    with pytest.raises(_StopNotAnException):
        stdlib_logger.error(_ExplodingMessage())

    assert capsys.readouterr().err == ""
    stdlib_logger.error("alive after")  # positive control: the sink still works
    assert "alive after" in capsys.readouterr().err
