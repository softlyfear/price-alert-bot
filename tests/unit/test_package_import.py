"""Regression test for import isolation of the ``app`` package.

``app.core.database`` reads settings from the environment at import time and
fails with a pydantic ``ValidationError`` when no ``.env`` is present. As
long as plain ``import app`` does not pull in ``app.core`` (and therefore
``app.core.database``), the test suite can run without a ``.env`` file.

The check has to happen in a fresh interpreter: once any other test in the
same process has imported ``app.core``, inspecting ``sys.modules`` from
within that process would give a false negative, and the side effects of an
earlier import cannot be undone by restoring the ``sys.modules`` mapping.
"""

import subprocess
import sys
from pathlib import Path

_CHECK_SCRIPT = (
    "import sys\n"
    "import app\n"
    "assert 'app.core' not in sys.modules, 'app.core' \n"
    "assert 'app.core.database' not in sys.modules, 'app.core.database'\n"
    "print('OK')\n"
)

# Resolve the repository root from this file's own location, not from the
# process' current working directory: the child interpreter locates the
# ``app`` package via its own ``cwd``, and pytest can be launched from
# anywhere (repo root, a subdirectory, an IDE runner, a CI job with its own
# ``working-directory``).
_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_importing_app_does_not_import_app_core_database() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CHECK_SCRIPT],
        capture_output=True,
        text=True,
        check=False,
        cwd=_REPO_ROOT,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"
