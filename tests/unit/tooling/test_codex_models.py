"""Behavioural tests for project-local Codex model refresh."""

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import codex_models as models

ROOT = Path(__file__).resolve().parents[3]


def entry(name: str, *, hidden: bool = False, high: bool = True) -> dict[str, Any]:
    return {
        "model": name,
        "hidden": hidden,
        "supportedReasoningEfforts": [{"reasoningEffort": "high" if high else "low"}],
    }


class ModelTests(unittest.TestCase):
    def make_roles(self, directory: Path) -> None:
        for role, family in models.ROLES.items():
            (directory / f"{role}.toml").write_text(
                f'model = "gpt-6.1-{family}" # keep\nmodel_reasoning_effort = "high"\n'
                'developer_instructions = """\nmodel = \'leave me\'\n"""\n',
                encoding="utf-8",
            )

    def test_selects_latest_visible_stable_versions_with_high(self) -> None:
        chosen = models.select_models(
            [
                entry("gpt-6.9-sol"),
                entry("gpt-6.10-sol"),
                entry("gpt-7-sol", hidden=True),
                entry("gpt-8-sol", high=False),
                entry("gpt-9-sol-preview"),
                entry("gpt-7-luna"),
                entry("gpt-7-terra"),
                entry("gpt-6.20-terra"),
            ]
        )
        self.assertEqual(chosen, {"sol": "gpt-6.10-sol", "terra": "gpt-7-terra"})

    def test_preserves_fields_permissions_and_never_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.make_roles(directory)
            architect = directory / "python-architect.toml"
            architect.chmod(0o640)
            updates = models.plan_updates(
                directory, {"sol": "gpt-6.10-sol", "terra": "gpt-6-terra"}
            )
            self.assertEqual(len(updates), 2)
            models.apply_updates(updates)
            self.assertIn('model = "gpt-6.10-sol" # keep', architect.read_text())
            self.assertIn("model = 'leave me'", architect.read_text())
            self.assertIn('model_reasoning_effort = "high"', architect.read_text())
            self.assertEqual(architect.stat().st_mode & 0o777, 0o640)
            self.assertIn(
                'model = "gpt-6.1-terra"', (directory / "bot-agent.toml").read_text()
            )
            self.assertEqual(
                models.plan_updates(
                    directory, {"sol": "gpt-6.10-sol", "terra": "gpt-6-terra"}
                ),
                [],
            )

    def test_corrupted_role_aborts_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.make_roles(directory)
            (directory / "tests-agent.toml").write_text("broken = [")
            before = {p: p.read_bytes() for p in directory.iterdir()}
            with self.assertRaises(ValueError):
                models.plan_updates(
                    directory, {"sol": "gpt-7-sol", "terra": "gpt-7-terra"}
                )
            self.assertEqual(before, {p: p.read_bytes() for p in directory.iterdir()})

    def test_offline_catalog_leaves_files_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".codex/agents"
            directory.mkdir(parents=True)
            self.make_roles(directory)
            before = {p: p.read_bytes() for p in directory.iterdir()}
            with (
                patch.object(models, "ROOT", Path(tmp)),
                patch.object(models, "catalog", side_effect=TimeoutError("offline")),
                patch("sys.stderr") as stderr,
            ):
                models.main()
            self.assertEqual(before, {p: p.read_bytes() for p in directory.iterdir()})
            self.assertIn("Warning", stderr.write.call_args.args[0])

    def test_incomplete_catalog_leaves_files_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / ".codex/agents"
            directory.mkdir(parents=True)
            self.make_roles(directory)
            before = {p: p.read_bytes() for p in directory.iterdir()}
            with (
                patch.object(models, "ROOT", Path(tmp)),
                patch.object(models, "catalog", return_value=[entry("gpt-7-sol")]),
                patch("sys.stderr") as stderr,
            ):
                models.main()
            self.assertEqual(before, {p: p.read_bytes() for p in directory.iterdir()})
            self.assertIn("Warning", stderr.write.call_args.args[0])

    def test_wrapper_forwards_arguments_offline_from_another_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "codex"
            fake.write_text(
                "#!/usr/bin/env python3\nimport json, os, sys\n"
                'if sys.argv[1:2] == ["app-server"]: sys.exit(1)\n'
                'open(os.environ["CAPTURE"], "w").write(json.dumps(sys.argv[1:]))\n'
            )
            fake.chmod(0o755)
            capture = Path(tmp) / "args"
            env = dict(
                os.environ, PATH=f"{tmp}:{os.environ['PATH']}", CAPTURE=str(capture)
            )
            before = {
                p: p.read_bytes() for p in (ROOT / ".codex/agents").glob("*.toml")
            }
            result = subprocess.run(
                [str(ROOT / "scripts/codex"), "one two", "--flag"],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(capture.read_text()), ["-C", str(ROOT), "one two", "--flag"]
            )
            self.assertIn("Warning", result.stderr)
            self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_wrapper_updates_roles_before_starting_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            scripts = root / "scripts"
            agents = root / ".codex/agents"
            scripts.mkdir(parents=True)
            agents.mkdir(parents=True)
            self.make_roles(agents)
            for name in ("codex", "codex_models.py"):
                shutil.copy2(ROOT / "scripts" / name, scripts / name)

            fake_dir = Path(tmp) / "bin"
            fake_dir.mkdir()
            fake = fake_dir / "codex"
            fake.write_text("""#!/usr/bin/env python3
import json, os, sys, tomllib
from pathlib import Path
if sys.argv[1:2] != ["app-server"]:
    root = Path(sys.argv[2])
    roles = {}
    for path in (root / ".codex/agents").glob("*.toml"):
        data = tomllib.loads(path.read_text())
        roles[path.stem] = [data["model"], data["model_reasoning_effort"]]
    capture = {"args": sys.argv[1:], "roles": roles}
    Path(os.environ["CAPTURE"]).write_text(json.dumps(capture))
    sys.exit(0)
for line in sys.stdin:
    msg = json.loads(line)
    if msg["method"] == "initialize":
        print(json.dumps({"id": msg["id"], "result": {}}), flush=True)
    elif msg["method"] == "model/list":
        def entry(model):
            return {"model": model, "hidden": False,
                    "supportedReasoningEfforts": [{"reasoningEffort": "high"}]}
        data = [entry("gpt-7-sol"), entry("gpt-7-terra")]
        result = {"data": data, "nextCursor": None}
        print(json.dumps({"id": msg["id"], "result": result}), flush=True)
""")
            fake.chmod(0o755)
            capture = Path(tmp) / "capture"
            env = dict(
                os.environ,
                PATH=f"{fake_dir}:{os.environ['PATH']}",
                CAPTURE=str(capture),
            )
            result = subprocess.run(
                [str(scripts / "codex"), "one two", "--flag"],
                cwd=tmp,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = json.loads(capture.read_text())
            self.assertEqual(recorded["args"], ["-C", str(root), "one two", "--flag"])
            sol_roles = ("python-architect", "code-reviewer")
            terra_roles = (
                "bot-agent",
                "core-agent",
                "db-models-agent",
                "devops-agent",
                "integrations-agent",
                "repository-agent",
                "schemas-agent",
                "service-agent",
                "tests-agent",
            )
            self.assertEqual(
                recorded["roles"],
                {
                    **{role: ["gpt-7-sol", "high"] for role in sol_roles},
                    **{role: ["gpt-7-terra", "high"] for role in terra_roles},
                },
            )

    def test_catalog_handles_notifications_and_paginates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "codex"
            fake.write_text("""#!/usr/bin/env python3
import json, sys
expected = ["initialize", "initialized", "model/list", "model/list"]
for line in sys.stdin:
    msg = json.loads(line)
    if not expected or msg.get("method") != expected.pop(0):
        sys.exit(2)
    if msg["method"] == "initialize":
        print(json.dumps({"method": "notice", "params": {}}), flush=True)
        print(json.dumps({"id": msg["id"], "result": {}}), flush=True)
    elif msg["method"] == "initialized":
        if msg.get("params") != {} or "id" in msg:
            sys.exit(3)
    elif msg["method"] == "model/list":
        cursor = msg["params"].get("cursor")
        result = {"data": [{"model": "second" if cursor else "first"}],
                  "nextCursor": None if cursor else "next"}
        print(json.dumps({"id": msg["id"], "result": result}), flush=True)
""")
            fake.chmod(0o755)
            with patch.dict(os.environ, {"PATH": f"{tmp}:{os.environ['PATH']}"}):
                self.assertEqual(
                    [x["model"] for x in models.catalog(timeout=3)], ["first", "second"]
                )

    def test_catalog_timeout_terminates_child(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "codex"
            pid_file = Path(tmp) / "pid"
            fake.write_text(
                "#!/usr/bin/env python3\nimport os, sys\n"
                'open(os.environ["PID_FILE"], "w").write(str(os.getpid()))\n'
                "for line in sys.stdin: pass\n"
            )
            fake.chmod(0o755)
            env = {"PATH": f"{tmp}:{os.environ['PATH']}", "PID_FILE": str(pid_file)}
            start = time.monotonic()
            with patch.dict(os.environ, env), self.assertRaises(TimeoutError):
                models.catalog(timeout=0.5)
            self.assertLess(time.monotonic() - start, 3)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
