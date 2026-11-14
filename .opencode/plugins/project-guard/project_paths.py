"""Resolve the checkout root from the working directory."""

import subprocess
from pathlib import Path


def project_root(cwd: str) -> Path:
    """Find the checkout root even when the session starts in a subdirectory."""
    result = subprocess.run(
        ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    return Path(result.stdout.strip() if result.returncode == 0 else cwd).resolve()
