#!/usr/bin/env python3
"""Synchronize a clean checkout with its configured upstream safely."""

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import select
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

GIT_ERRORS = (OSError, RuntimeError, subprocess.SubprocessError)


class SyncBlocked(Exception):
    pass


def _git(root, *args, timeout=5, env=None):
    try:
        result = subprocess.run(  # noqa: S603
            [
                shutil.which("git") or "git",
                "--no-optional-locks",
                "-C",
                str(root),
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND")
                or "ssh -oBatchMode=yes -oConnectTimeout=8",
                **(env or {}),
            },
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Git command failed") from exc
    if result.returncode:
        raise RuntimeError("Git command failed")
    return result.stdout if "-z" in args else result.stdout.strip()


class _RefTransaction:
    def __init__(self, root, branch, initial, target):
        self.root, self.branch, self.initial, self.target = (
            root,
            branch,
            initial,
            target,
        )
        self.process = None
        self.buffer = b""
        self.committed = False

    def command(self, value, expected):
        process = self.process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("Reference transaction is unavailable")
        process.stdin.write(value.encode())
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([process.stdout], [], [], max(0, remaining))
            if not ready:
                raise RuntimeError("Reference transaction timed out")
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                raise RuntimeError("Reference transaction rejected the expected state")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        if line.decode() != expected:
            raise RuntimeError("Reference transaction failed")

    def __enter__(self):
        self.process = subprocess.Popen(  # noqa: S603
            [
                shutil.which("git") or "git",
                "-C",
                str(self.root),
                "update-ref",
                "--stdin",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        try:
            self.command("start\n", "start: ok")
            ref = json.dumps("refs/heads/" + self.branch, ensure_ascii=False)
            self.command(
                f"update {ref} {self.target} {self.initial}\nprepare\n",
                "prepare: ok",
            )
            return self
        except BaseException:
            self.close()
            raise

    def commit(self):
        self.command("commit\n", "commit: ok")
        self.committed = True

    def close(self):
        process = self.process
        if process is None:
            return
        if not self.committed:
            with contextlib.suppress(BrokenPipeError, OSError, RuntimeError):
                self.command("abort\n", "abort: ok")
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()

    def __exit__(self, *unused):
        self.close()


def _update_checkout(root, git, branch, initial, target):
    """Hold HEAD, branch, and index locks throughout the checked tree transition."""
    index = Path(
        git(root, "rev-parse", "--path-format=absolute", "--git-path", "index")
    )
    lock_path = Path(str(index) + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    recovery = index.parent / "agent-sync-recovery" / uuid.uuid4().hex
    committed = False
    try:
        recovery.mkdir(parents=True, mode=0o700)
        before = recovery / "before.index"
        pending = recovery / "next.index"
        shutil.copyfile(index, before)
        shutil.copyfile(index, pending)
        state = {"branch": branch, "previous_head": initial, "target": target}
        (recovery / "state.json").write_text(json.dumps(state) + "\n")
        env = {"GIT_INDEX_FILE": str(pending)}
        if (
            git(root, "branch", "--show-current") != branch
            or git(root, "rev-parse", "HEAD") != initial
            or git(root, "status", "--porcelain=v1")
        ):
            raise SyncBlocked("Checkout changed before the locked update.")
        with _RefTransaction(root, branch, initial, target) as transaction:
            head_lock = Path(
                git(
                    root,
                    "rev-parse",
                    "--path-format=absolute",
                    "--git-path",
                    "HEAD.lock",
                )
            )
            if (
                git(root, "branch", "--show-current") != branch
                or git(root, "rev-parse", "HEAD") != initial
                or git(root, "status", "--porcelain=v1")
                or not head_lock.is_file()
            ):
                raise SyncBlocked("Checkout changed before the locked update.")
            _protect_untracked(git, root, target)
            started = False
            try:
                started = True
                git(root, "read-tree", "-m", "-u", initial, target, env=env)
                git(root, "diff-files", "--quiet", env=env)
                transaction.commit()
                committed = True
                os.replace(pending, index)
            except BaseException:
                if started and not committed:
                    # Two-tree rollback refuses to overwrite new working edits.
                    with contextlib.suppress(*GIT_ERRORS):
                        git(root, "read-tree", "-m", "-u", target, initial, env=env)
                raise
    except GIT_ERRORS as exc:
        stage = (
            "Reference advanced; index recovery is required"
            if committed
            else "Update stopped"
        )
        raise SyncBlocked(
            stage
            + "; inspect preserved recovery metadata under .git/agent-sync-recovery/."
        ) from exc
    else:
        shutil.rmtree(recovery)
    finally:
        os.close(descriptor)
        lock_path.unlink()


def _check_merge_policy(git, root, branch):
    try:
        options = git(root, "config", "--get", "branch." + branch + ".mergeOptions")
    except RuntimeError:
        options = ""
    if options:
        raise SyncBlocked(
            "Configured branch merge options require manual synchronization."
        )
    try:
        verify = git(root, "config", "--get", "merge.verifySignatures").lower()
    except RuntimeError:
        return
    if verify not in {"false", "no", "off", "0"}:
        raise SyncBlocked(
            "Configured signature verification requires manual synchronization."
        )


def _history_counts(git, root, initial, target):
    counts = git(
        root, "rev-list", "--left-right", "--count", initial + "..." + target
    ).split()
    if len(counts) != 2 or any(
        not value.isascii() or not value.isdecimal() for value in counts
    ):
        raise SyncBlocked("Could not determine the history relationship.")
    return int(counts[0]), int(counts[1])


def _protect_untracked(git, root, target):
    changed = git(
        root,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        "--diff-filter=ACMT",
        "HEAD",
        target,
    ).split("\0")
    tracked = set(git(root, "ls-files", "-z").split("\0"))
    for name in filter(None, changed):
        parts = Path(name).parts
        for index in range(1, len(parts) + 1):
            relative = "/".join(parts[:index])
            path = root.joinpath(*parts[:index])
            if (
                relative not in tracked
                and os.path.lexists(path)
                and (index == len(parts) or path.is_symlink() or not path.is_dir())
            ):
                raise SyncBlocked("Untracked or ignored path obstructs the update.")


def sync_repo(root, git=None, project_roots=(), ref_prefix="refs/agent-sync/fetch/"):
    """Fetch to a private ref and update only when local state permits it."""
    git = git or _git
    root = Path(root).resolve()
    private_ref = ref_prefix + uuid.uuid4().hex
    phase = "checkout inspection"
    try:
        lock_dir = Path(
            git(
                root,
                "rev-parse",
                "--path-format=absolute",
                "--git-path",
                "agent-sync-locks",
            )
        )
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / (hashlib.sha256(os.fsencode(root)).hexdigest() + ".lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {
                    "ok": False,
                    "message": "Another synchronization is in progress.",
                }
            try:
                branch = git(root, "branch", "--show-current")
                if not branch:
                    raise SyncBlocked("A checked-out branch is required.")
                if (
                    root in {Path(path).resolve() for path in project_roots}
                    and branch != "main"
                ):
                    raise SyncBlocked("This project must remain on main.")
                for marker in (
                    "rebase-merge",
                    "rebase-apply",
                    "MERGE_HEAD",
                    "CHERRY_PICK_HEAD",
                    "REVERT_HEAD",
                    "BISECT_START",
                    "sequencer",
                ):
                    path = Path(
                        git(
                            root,
                            "rev-parse",
                            "--path-format=absolute",
                            "--git-path",
                            marker,
                        )
                    )
                    if path.exists():
                        raise SyncBlocked(
                            "An unfinished Git operation blocks synchronization."
                        )
                phase = "upstream configuration"
                try:
                    remote = git(
                        root, "config", "--get", "branch." + branch + ".remote"
                    )
                    source = git(root, "config", "--get", "branch." + branch + ".merge")
                except RuntimeError as exc:
                    raise SyncBlocked("No configured upstream is available.") from exc
                if not remote or not source.startswith("refs/heads/"):
                    raise SyncBlocked("No configured upstream is available.")
                tracking = git(
                    root, "for-each-ref", "--format=%(upstream)", "refs/heads/" + branch
                )
                phase = "checkout inspection"
                initial = git(root, "rev-parse", "--verify", "HEAD^{commit}")
                if git(root, "status", "--porcelain=v1"):
                    raise SyncBlocked("The working tree is dirty.")
                old_tracking = None
                if tracking:
                    with contextlib.suppress(RuntimeError):
                        old_tracking = git(
                            root, "rev-parse", "--verify", tracking + "^{commit}"
                        )
                phase = "fetch"
                git(
                    root,
                    "fetch",
                    "--quiet",
                    "--no-tags",
                    "--no-write-fetch-head",
                    "--no-recurse-submodules",
                    "--refmap=",
                    "--",
                    remote,
                    source + ":" + private_ref,
                    timeout=15,
                )
                phase = "fetched ref verification"
                target = git(root, "rev-parse", "--verify", private_ref + "^{commit}")
                if (
                    git(root, "branch", "--show-current") != branch
                    or git(root, "rev-parse", "HEAD") != initial
                    or git(root, "status", "--porcelain=v1")
                ):
                    raise SyncBlocked("Checkout changed during fetch.")
                phase = "history comparison"
                left, right = _history_counts(git, root, initial, target)
                rewritten = False
                backup_ref = None
                phase = "checkout update"
                if left == 0:
                    if initial != target:
                        _check_merge_policy(git, root, branch)
                        _update_checkout(root, git, branch, initial, target)
                        git(
                            root,
                            "hook",
                            "run",
                            "--ignore-missing",
                            "post-merge",
                            "--",
                            "0",
                        )
                    expected = target
                elif old_tracking != initial and right == 0:
                    expected = initial
                else:
                    try:
                        enabled = (
                            git(
                                root,
                                "config",
                                "--local",
                                "--bool",
                                "--get",
                                "agentSync.allowRewrittenUpstream",
                            )
                            == "true"
                        )
                    except RuntimeError:
                        enabled = False
                    if not enabled or old_tracking != initial or target == initial:
                        raise SyncBlocked(
                            "Diverged history requires manual resolution."
                        )
                    _check_merge_policy(git, root, branch)
                    _protect_untracked(git, root, target)
                    backup_ref = "refs/agent-sync/backups/" + uuid.uuid4().hex
                    git(root, "update-ref", backup_ref, initial, "")
                    _update_checkout(root, git, branch, initial, target)
                    expected = target
                    rewritten = True
                phase = "post-update verification"
                head = git(root, "rev-parse", "HEAD")
                if (
                    head != expected
                    or git(root, "branch", "--show-current") != branch
                    or git(root, "status", "--porcelain=v1")
                ):
                    raise SyncBlocked("Post-update verification failed.")
                if (
                    tracking.startswith("refs/remotes/")
                    and old_tracking
                    and head == target
                ):
                    phase = "tracking ref update"
                    git(root, "update-ref", tracking, target, old_tracking)
                return {
                    "ok": True,
                    "branch": branch,
                    "previous_head": initial,
                    "head": head,
                    "rewritten": rewritten,
                    "backup_ref": backup_ref,
                    "message": "Synchronization completed.",
                }
            finally:
                with contextlib.suppress(
                    OSError, RuntimeError, subprocess.SubprocessError
                ):
                    git(root, "update-ref", "-d", private_ref)
                fcntl.flock(lock, fcntl.LOCK_UN)
    except SyncBlocked as exc:
        return {"ok": False, "message": str(exc)}
    except GIT_ERRORS:
        return {"ok": False, "message": f"Git synchronization failed during {phase}."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    result = sync_repo(root, project_roots=(root,))
    output = json.dumps(result, ensure_ascii=False) if args.json else result["message"]
    stream = sys.stdout if result["ok"] or args.json else sys.stderr
    stream.write(str(output) + "\n")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
