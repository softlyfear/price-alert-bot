"""PostToolUse hook for Edit/Write: format the edited Python file, report lint.

Unused-import and unused-variable rules are skipped here: they fire on the
intermediate states of a multi-step edit and are caught by the quality gate.
Exit code 2 feeds the lint output back to the agent; the edit itself stands.
"""

import json
import os
import subprocess
import sys

SKIPPED_RULES = "F401,F841"


def main() -> int:
    payload = json.load(sys.stdin)
    path = payload.get("tool_input", {}).get("file_path", "")
    project = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    if not path.endswith(".py") or not os.path.isfile(path):
        return 0
    if os.path.commonpath([os.path.abspath(path), project]) != project:
        return 0

    subprocess.run(
        ["uv", "run", "--quiet", "ruff", "format", "--quiet", path],
        cwd=project,
        check=False,
    )
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
            path,
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
