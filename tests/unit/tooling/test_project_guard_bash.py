"""Recursive removal of project data is denied, not only the checkout root."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def test_cli_returns_explicit_allow_for_routine_status() -> None:
    root = Path(__file__).resolve().parents[3]
    script = root / ".opencode" / "plugins" / "project-guard" / "guard_bash.py"
    result = subprocess.run(
        [sys.executable, str(script)],
        input=json.dumps({"command": "git status", "cwd": str(root)}),
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == {"decision": "allow"}


def _load_check() -> Any:
    plugin = (
        Path(__file__).resolve().parents[3] / ".opencode" / "plugins" / "project-guard"
    )
    sys.path.insert(0, str(plugin))
    spec = importlib.util.spec_from_file_location(
        "guard_bash", plugin / "guard_bash.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("project guard could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


def test_recursive_rm_of_project_data_is_denied(tmp_path: Path) -> None:
    check = _load_check()
    project = tmp_path / "proj"
    (project / "data" / "backups").mkdir(parents=True)
    root = str(project)

    for command in (
        "rm -rf data",
        "rm -rf ./data",
        "rm -rf data/",
        "rm -rf data/backups",
        "rm -rf data/price-alert-bot.sqlite3",
    ):
        decision = check(command, cwd=root, project=root)
        assert decision is not None
        assert decision[0] == "deny", command

    assert check("rm -rf docs", cwd=root, project=root) == (
        "deny",
        "recursive rm of the project or agent settings docs",
    )
    assert check("rm data/notes.txt", cwd=root, project=root) is None
    assert check("rm -rf /tmp/unrelated", cwd=root, project=root) is None
