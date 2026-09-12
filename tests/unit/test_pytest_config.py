"""Regression tests for the *effective* pytest configuration.

``strict_markers``, ``strict_config``, and the promotion of
``pytest.PytestConfigWarning`` to a hard error via ``filterwarnings`` are the
gate that catches broken quality checks elsewhere in the project. Until this
module existed, their effectiveness had been proven only by one-off manual
experiments during review -- nothing in the suite would notice if a future
edit silently removed, renamed, or relocated any of these keys.

This is not a hypothetical risk: it already happened once, silently.
``addopts = "--strict-markers"`` was accepted during PAB-002's review,
present in ``pyproject.toml``, and inert -- ``--strict-markers`` on the
command line is consumed by argparse and never reaches the ini-option
lookup that ``strict_markers`` actually uses. The regression went unnoticed
for two tickets, surfacing only at PAB-003.

Every assertion below reads the configuration pytest actually applied for
the current run via ``config.getini(...)`` on the ``pytestconfig`` fixture,
not by parsing ``pyproject.toml`` as text. Text parsing would only prove a
line is present in the file -- exactly the failure mode above, where the
line was present and inert.
"""

import pytest


def test_strict_markers_is_enabled(pytestconfig: pytest.Config) -> None:
    """A marker not registered in the ``markers`` section must raise, not
    warn -- otherwise a typo'd or forgotten marker on a future test would be
    silently ignored instead of failing collection."""
    assert pytestconfig.getini("strict_markers") is True


def test_strict_config_is_enabled(pytestconfig: pytest.Config) -> None:
    """An unknown or malformed key under ``[tool.pytest.ini_options]`` must
    raise, not warn -- otherwise a typo'd ini key would be silently
    ignored, exactly like the ``addopts`` regression this ticket exists to
    guard against."""
    assert pytestconfig.getini("strict_config") is True


def test_filterwarnings_promotes_pytest_config_warning_to_error(
    pytestconfig: pytest.Config,
) -> None:
    """``PytestConfigWarning`` (e.g. raised for an unrecognised ini key
    when ``strict_config`` were ever off) must fail the run rather than be
    printed and ignored.

    Membership of ``"error::pytest.PytestConfigWarning"`` in the list is
    not sufficient: the ``warnings`` module applies filters so that the
    *last* matching entry wins. A later ``"ignore::pytest.PytestConfigWarning"``
    would silently cancel the earlier ``error`` entry while both strings
    stay present in ``filterwarnings`` -- a plain "is this string in the
    list" check would stay green through that regression. This test
    isolates the entries that name ``PytestConfigWarning`` explicitly and
    requires the one that takes effect, the last one, to be an ``error``
    action.

    Known limit: this only covers entries that mention
    ``PytestConfigWarning`` by name. ``warnings`` matches categories via
    ``issubclass``, so an entry silencing a broader ancestor category --
    ``ignore::pytest.PytestWarning``, ``ignore::UserWarning``, or a bare
    ``ignore`` with no category at all -- cancels the same protection
    without naming ``PytestConfigWarning`` and therefore slips past this
    string-based selection undetected. This residual gap is recorded in
    ``TASKS.md`` and left to the frozen ticket PAB-050, which replaces the
    selection with a behavioural oracle.
    """
    filters = pytestconfig.getini("filterwarnings")
    relevant_actions = [
        entry.split(":", 1)[0] for entry in filters if "PytestConfigWarning" in entry
    ]
    assert relevant_actions, "no filterwarnings entry targets PytestConfigWarning"
    assert relevant_actions[-1] == "error"


def test_addopts_is_empty(pytestconfig: pytest.Config) -> None:
    """``addopts`` is forbidden project-wide: command-line-only flags such
    as ``--strict-markers`` placed here look like they configure the
    matching ini option but are parsed and discarded before ever reaching
    it -- the exact mechanism behind the PAB-002/PAB-003 regression."""
    addopts = pytestconfig.getini("addopts")
    assert addopts == []


def test_strict_aggregate_key_is_not_enabled(pytestconfig: pytest.Config) -> None:
    """The aggregate ``strict`` key would also turn on ``strict_xfail`` and
    ``strict_parametrization_ids``, which nobody asked for. Only the two
    specific keys this project wants, ``strict_markers`` and
    ``strict_config``, are allowed to be set."""
    assert pytestconfig.getini("strict") is False
