"""Observation of the Chromium processes a test started (Linux `/proc`).

Processes are found by `TMPDIR`: a test points it at a private directory (an
allow-listed variable, passed to the browser), Playwright puts the browser
profile there, so a process whose command line holds that directory is one the
test started - orphans included.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


def processes_started_by_test(marker: str) -> dict[int, str]:
    """pid -> command line of every live process whose command line holds `marker`."""
    found: dict[int, str] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        cmdline = raw.replace(b"\0", b" ").decode(errors="replace")
        if marker in cmdline:
            found[int(entry.name)] = cmdline
    return found


def _environ_of(pid: int) -> dict[str, str] | None:
    """The environment of `pid`, or `None` if the process is gone or unreadable."""
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return None
    pairs = (item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
    return {k.decode(errors="replace"): v.decode(errors="replace") for k, v in pairs}


async def observe_chromium_environments(
    marker: str, *, deadline_seconds: float = 20.0
) -> list[dict[str, str]]:
    """Environments of the live Chromium processes of the test, waited for.

    The moment a test enumerates processes is not the moment the browser has
    started or its environment is readable, so the wait is bounded by a
    deadline and ends when at least one Chromium process shows the test's
    private `TMPDIR` (a process that exits between listing and reading is
    skipped). Fails if none does.
    """
    async with asyncio.timeout(deadline_seconds):
        while True:
            environments = [
                env
                for pid, cmd in processes_started_by_test(marker).items()
                if "chrom" in cmd and (env := _environ_of(pid)) is not None
            ]
            if any(env.get("TMPDIR") == marker for env in environments):
                return environments
            await asyncio.sleep(0.05)
