#!/usr/bin/env python3
"""Refresh the project agent models from the Codex app-server catalog."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from typing import Any
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
ROLES = {
    "python-architect": "sol",
    "code-reviewer": "sol",
    "bot-agent": "terra",
    "core-agent": "terra",
    "db-models-agent": "terra",
    "devops-agent": "terra",
    "integrations-agent": "terra",
    "repository-agent": "terra",
    "schemas-agent": "terra",
    "service-agent": "terra",
    "tests-agent": "terra",
}
MODEL = re.compile(r"gpt-(\d+(?:\.\d+)*?)-(sol|terra)\Z")
MODEL_LINE = re.compile(r'^(model\s*=\s*)"([^"\n]*)"(\s*(?:#.*)?)$', re.MULTILINE)


def version(model: str, family: str) -> tuple[int, ...] | None:
    match = MODEL.fullmatch(model)
    if match is None or match.group(2) != family:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def select_models(entries: list[dict[str, Any]]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for entry in entries:
        model = entry.get("model")
        if not isinstance(model, str) or entry.get("hidden") is not False:
            continue
        efforts = entry.get("supportedReasoningEfforts")
        if not isinstance(efforts, list) or not any(
            isinstance(effort, dict) and effort.get("reasoningEffort") == "high"
            for effort in efforts
        ):
            continue
        for family in ("sol", "terra"):
            candidate = version(model, family)
            previous = version(selected[family], family) if family in selected else None
            if candidate is not None and (previous is None or candidate > previous):
                selected[family] = model
    return selected


def catalog(timeout: float = 18) -> list[dict[str, Any]]:
    process = subprocess.Popen(
        ["codex", "app-server", "--listen", "stdio://"],  # noqa: S607
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + timeout
    buffer = b""
    selector = selectors.DefaultSelector()
    assert process.stdin is not None and process.stdout is not None
    stdin, stdout = process.stdin, process.stdout
    selector.register(stdout, selectors.EVENT_READ)

    def send(message: dict[str, Any]) -> None:
        stdin.write((json.dumps(message) + "\n").encode())
        stdin.flush()

    def response(request_id: int) -> dict[str, Any]:
        nonlocal buffer
        while True:
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                if isinstance(message, dict) and message.get("id") == request_id:
                    result = message.get("result")
                    if "error" in message or not isinstance(result, dict):
                        raise ValueError("app-server returned an error")
                    return cast(dict[str, Any], result)
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise TimeoutError("app-server timed out")
            chunk = os.read(stdout.fileno(), 65536)
            if not chunk:
                raise ValueError("app-server closed its output")
            buffer += chunk

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "price-alert-bot-models", "version": "1.0.0"}
                },
            }
        )
        response(1)
        send({"method": "initialized", "params": {}})
        entries: list[dict[str, Any]] = []
        cursor = None
        seen: set[str] = set()
        request_id = 2
        while True:
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            send({"id": request_id, "method": "model/list", "params": params})
            result = response(request_id)
            data = result.get("data")
            if not isinstance(data, list) or not all(
                isinstance(item, dict) for item in data
            ):
                raise ValueError("invalid catalog data")
            entries.extend(data)
            cursor = result.get("nextCursor")
            if cursor is None:
                return entries
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("invalid catalog cursor")
            seen.add(cursor)
            request_id += 1
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        stdin.close()
        stdout.close()


def plan_updates(agent_dir: Path, selected: dict[str, str]) -> list[tuple[Path, str]]:
    if not all(family in selected for family in set(ROLES.values())):
        raise ValueError(
            "catalog does not contain both model families with high reasoning"
        )
    updates = []
    for role, family in ROLES.items():
        path = agent_dir / f"{role}.toml"
        original = path.read_text(encoding="utf-8")
        parsed = tomllib.loads(original)
        current = parsed.get("model")
        current_version = version(current, family) if isinstance(current, str) else None
        candidate = selected[family]
        candidate_version = version(candidate, family)
        if (
            current_version is None
            or candidate_version is None
            or parsed.get("model_reasoning_effort") != "high"
        ):
            raise ValueError(f"invalid model configuration in {path.name}")
        matches = list(MODEL_LINE.finditer(original))
        if len(matches) != 1 or matches[0].group(2) != current:
            raise ValueError(f"cannot safely update model in {path.name}")
        if candidate_version <= current_version:
            continue
        match = matches[0]
        updated = original[: match.start(2)] + candidate + original[match.end(2) :]
        expected = dict(parsed)
        expected["model"] = candidate
        if tomllib.loads(updated) != expected:
            raise ValueError(f"unexpected changes in {path.name}")
        updates.append((path, updated))
    return updates


def apply_updates(updates: list[tuple[Path, str]]) -> None:
    for path, content in updates:
        temp = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as file:
                temp = Path(file.name)
                file.write(content)
            temp.chmod(path.stat().st_mode & 0o7777)
            os.replace(temp, path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)


def main() -> None:
    try:
        updates = plan_updates(ROOT / ".codex" / "agents", select_models(catalog()))
        apply_updates(updates)
        for path, _ in updates:
            sys.stderr.write(f"Updated {path.name}\n")
    except (OSError, ValueError, TimeoutError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"Warning: Codex agent models were not refreshed: {exc}\n")


if __name__ == "__main__":
    main()
