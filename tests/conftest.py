"""Shared pytest fixtures.

Fixtures for the database, HTTP clients, and settings are introduced in
later tickets (PAB-007, PAB-011) once there is a concrete need for them.
The one fixture here guards process-global stdlib ``logging`` state, which
both ``tests/unit/core/test_logging.py`` and ``tests/unit/test_main.py``
(through the real ``setup_logging`` called by ``lifespan``) mutate.
"""

import logging
from collections.abc import Iterator

import pytest

from app.core.logging import _InterceptHandler

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


@pytest.fixture(autouse=True)
def _restore_stdlib_logging_state() -> Iterator[None]:
    """Undo what ``setup_logging`` does to stdlib ``logging`` (PAB-083 AC7).

    Root: interceptors added during the test are removed; foreign handlers
    (pytest's own capture handlers) are never touched, because restoring the
    whole list would resurrect handlers pytest removes by itself between its
    setup, call and teardown phases. ``uvicorn`` loggers: handlers and
    ``propagate`` are put back exactly.
    """
    root = logging.getLogger()
    before_root = list(root.handlers)
    saved = {
        name: (list(lg.handlers), lg.propagate)
        for name in _UVICORN_LOGGERS
        for lg in [logging.getLogger(name)]
    }
    yield
    for handler in list(root.handlers):
        if handler not in before_root and isinstance(handler, _InterceptHandler):
            root.removeHandler(handler)
    for name, (handlers, propagate) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers = handlers
        lg.propagate = propagate
