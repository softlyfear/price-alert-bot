"""PostToolUse hook: format Python files changed by apply_patch, report lint.

Unused-import and unused-variable rules are skipped here: they fire on the
intermediate states of a multi-step edit and are caught by the quality gate.
Exit code 2 feeds the lint output back to the agent; the edit itself stands.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from project_paths import project_root

SKIPPED_RULES = "F401,F841"


def edited_paths(tool_input: dict[str, str]) -> list[str]:
    """Read patch headers; a move replaces the preceding update's old path."""
    paths = []
    current = None
    for line in tool_input.get("command", "").splitlines():
        if line.startswith(("*** Add File: ", "*** Update File: ")):
            if current is not None:
                paths.append(current)
            current = line.split(": ", 1)[1]
        elif line.startswith("*** Move to: "):
            current = line.removeprefix("*** Move to: ")
        elif line.startswith(("*** Delete File: ", "*** End Patch")):
            if current is not None:
                paths.append(current)
            current = None
    if current is not None:
        paths.append(current)
    if tool_input.get("file_path"):
        paths.append(tool_input["file_path"])
    return paths


def main() -> int:
    payload = json.load(sys.stdin)
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    project = project_root(str(cwd))
    paths = sorted(
        {
            str(path)
            for name in edited_paths(payload.get("tool_input") or {})
            if (path := (cwd / name).resolve()).suffix == ".py"
            and path.is_file()
            and path.is_relative_to(project)
        }
    )
    if not paths:
        return 0

    formatted = subprocess.run(
        ["uv", "run", "--quiet", "ruff", "format", "--", *paths],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    if formatted.returncode != 0:
        sys.stderr.write((formatted.stdout.strip() or formatted.stderr.strip()) + "\n")
        return 2
    check = subprocess.run(
        [
            "uv",
            "run",
            "--quiet",
            "ruff",
            "check",
            "--output-format=concise",
            "--ignore",
            SKIPPED_RULES,
            "--",
            *paths,
        ],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    if check.returncode != 0:
        sys.stderr.write((check.stdout.strip() or check.stderr.strip()) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
