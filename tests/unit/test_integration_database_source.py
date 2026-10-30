"""Regression tests for secret-safe subprocess environments."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import uuid
from collections.abc import Callable
from collections.abc import MutableMapping
from pathlib import Path

import pytest
from _pytest._io.saferepr import saferepr

from tests.integration.database_source import SubprocessEnvironment

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _markers() -> tuple[str, str]:
    return f"val-{uuid.uuid4().hex}", f"key-{uuid.uuid4().hex}"


@pytest.mark.parametrize(
    "render",
    [
        pytest.param(repr, id="repr"),
        pytest.param(str, id="str"),
        pytest.param(lambda m: f"{m}", id="fstring"),
        pytest.param(lambda m: repr({"env": m}), id="repr_in_dict"),
        pytest.param(saferepr, id="pytest_saferepr"),
    ],
)
def test_subprocess_environment_rendering_hides_values_and_keys(
    render: Callable[[SubprocessEnvironment], str],
) -> None:
    value, key = _markers()
    env = SubprocessEnvironment({key: value})

    rendered = render(env)

    assert value not in rendered
    assert key not in rendered


def test_subprocess_environment_derived_dict_is_not_masked_positive_control() -> None:
    """The markers are real: a ``dict`` derived from the type prints them."""
    value, key = _markers()
    env = SubprocessEnvironment({key: value})

    assert value in repr(dict(env))


def test_subprocess_environment_is_a_snapshot_of_its_sources() -> None:
    source = {"A": "1"}
    env = SubprocessEnvironment(source)

    source["A"] = "changed"
    source["B"] = "new"

    assert dict(env) == {"A": "1"}


def test_subprocess_environment_later_layer_overrides_earlier() -> None:
    env = SubprocessEnvironment({"A": "first", "B": "kept"}, {"A": "second"})

    assert dict(env) == {"A": "second", "B": "kept"}


def test_subprocess_environment_is_not_mutable() -> None:
    env = SubprocessEnvironment({"A": "1"})

    assert not isinstance(env, MutableMapping)
    assert not hasattr(env, "__setitem__")
    assert not hasattr(env, "__delitem__")


def test_subprocess_environment_without_layers_is_empty() -> None:
    env = SubprocessEnvironment()

    assert len(env) == 0
    assert list(env) == []


def test_subprocess_child_sees_the_same_environment_as_with_an_equal_dict() -> None:
    marker = f"child-{uuid.uuid4().hex}"
    program = "import os; print(os.environ['PAB085_MARKER']); print(len(os.environ))"

    def run(env: SubprocessEnvironment | dict[str, str]) -> list[str]:
        result = subprocess.run(
            [sys.executable, "-c", program],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        return result.stdout.split()

    layers = ({"PATH": os.environ["PATH"], "A": "1"}, {"PAB085_MARKER": marker})
    layered = SubprocessEnvironment(*layers)
    plain = {**layers[0], **layers[1]}

    assert run(layered) == run(plain)
    assert run(layered)[0] == marker


_TIMEOUT_PROBE = """
import os
import subprocess
import sys

from tests.integration.database_source import SubprocessEnvironment


def test_probe():
    subprocess.run(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env=__ENV__,
        timeout=0.5,
    )
"""
_NEW_FORM = 'SubprocessEnvironment(os.environ, {"0_PROBE": os.environ["PROBE_MARKER"]})'
# ``_OLD_FORM`` is the control: a plain ``dict`` literal, which pytest prints
# in full; its base is synthetic - the child prints this dict, and the output
# must never hold the real environment.
_OLD_FORM = '{**{"PATH": os.environ["PATH"]}, "0_PROBE": os.environ["PROBE_MARKER"]}'


@pytest.mark.parametrize(
    ("env_expr", "flags", "marker_expected", "expect_locals"),
    [
        pytest.param(_NEW_FORM, ["-q"], False, False, id="new_type_quiet"),
        pytest.param(
            _NEW_FORM,
            ["--showlocals", "--tb=long"],
            False,
            True,
            id="new_type_showlocals",
        ),
        pytest.param(
            _OLD_FORM,
            ["--showlocals", "--tb=long"],
            True,
            True,
            id="control_old_form_showlocals",
        ),
    ],
)
def test_timeout_in_subprocess_run_does_not_print_environment_values(
    tmp_path: Path,
    env_expr: str,
    flags: list[str],
    marker_expected: bool,
    expect_locals: bool,
) -> None:
    """Behavioural guard: ``pytest`` prints ``kwargs['env']`` of ``subprocess.run``.

    A throwaway module in ``tmp_path`` raises ``TimeoutExpired`` inside the
    stdlib ``subprocess.run``. The control cell uses the former form
    (a ``{**base, ...}`` dict) and must show the marker, and the cells with
    ``expect_locals`` must show the ``kwargs`` *local* line of that frame
    (name padded with spaces, unlike the frame-argument line of ``--tb=long``)
    -- otherwise "marker absent" would be measured on output that could not
    have shown it. The child gets a minimal environment, never the real one.
    """
    marker = f"env-{uuid.uuid4().hex}"
    probe = tmp_path / "test_probe_env_timeout.py"
    probe.write_text(_TIMEOUT_PROBE.replace("__ENV__", env_expr))

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-p", "no:cacheprovider", *flags],
        cwd=str(_REPO_ROOT),
        env=SubprocessEnvironment(
            {"PATH": os.environ["PATH"]}, {"PROBE_MARKER": marker}
        ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "TimeoutExpired" in output
    if expect_locals:
        assert "subprocess.py" in output
        assert re.search(r"^kwargs {5}= \{", output, re.M)
    assert (marker in output) is marker_expected
