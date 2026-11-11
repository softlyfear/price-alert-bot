import fcntl
import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.sync_upstream import _git
from scripts.sync_upstream import sync_repo


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.remote = self.base / "remote.git"
        self.source = self.base / "source"
        self.local = self.base / "local"
        self.run_git(
            self.base, "init", "--bare", "--initial-branch=main", str(self.remote)
        )
        self.run_git(self.base, "clone", str(self.remote), str(self.source))
        self.run_git(self.source, "config", "user.name", "Test")
        self.run_git(self.source, "config", "user.email", "test@example.com")
        self.commit(self.source, "file", "first")
        self.run_git(self.source, "push", "-u", "origin", "main")
        self.run_git(self.base, "clone", str(self.remote), str(self.local))
        self.run_git(self.local, "config", "user.name", "Test")
        self.run_git(self.local, "config", "user.email", "test@example.com")

    def run_git(self, root, *args):
        return subprocess.run(  # noqa: S603
            [shutil.which("git"), "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def commit(self, root, name, value):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        self.run_git(root, "add", name)
        self.run_git(root, "commit", "-m", value)
        return self.run_git(root, "rev-parse", "HEAD")

    def advance(self, name="file"):
        target = self.commit(self.source, name, "second")
        self.run_git(self.source, "push")
        return target

    def rewrite(self, name="file"):
        self.run_git(self.source, "checkout", "--orphan", "replacement")
        self.run_git(self.source, "rm", "-rf", ".")
        target = self.commit(self.source, name, "rewritten")
        self.run_git(self.source, "push", "--force", "origin", "HEAD:main")
        return target

    def test_fast_forward_isolates_fetch_head_and_tracking(self):
        target = self.advance()
        fetch_head = Path(
            self.run_git(self.local, "rev-parse", "--git-path", "FETCH_HEAD")
        )
        if not fetch_head.is_absolute():
            fetch_head = self.local / fetch_head
        fetch_head.write_text("sentinel\n")
        result = sync_repo(self.local)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["head"], target)
        self.assertEqual(fetch_head.read_text(), "sentinel\n")
        self.assertEqual(self.run_git(self.local, "rev-parse", "origin/main"), target)
        self.assertFalse(
            self.run_git(self.local, "for-each-ref", "refs/agent-sync/fetch/")
        )
        self.assertEqual(
            self.run_git(self.source, "rev-parse", "refs/remotes/origin/main"), target
        )

    def test_rewrite_requires_opt_in_and_retains_backup(self):
        old = self.run_git(self.local, "rev-parse", "HEAD")
        target = self.rewrite()
        self.assertFalse(sync_repo(self.local)["ok"])
        self.run_git(self.local, "config", "agentSync.allowRewrittenUpstream", "true")
        result = sync_repo(self.local)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["rewritten"])
        self.assertEqual(result["head"], target)
        self.assertEqual(
            self.run_git(self.local, "rev-parse", result["backup_ref"]), old
        )

    def test_commit_after_history_check_is_not_discarded(self):
        self.advance()
        created = []

        def racing_git(root, *args, **kwargs):
            result = _git(root, *args, **kwargs)
            if args[0] == "rev-list" and not created:
                created.append(self.commit(self.local, "local", "new local commit"))
            return result

        result = sync_repo(self.local, git=racing_git)
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), created[0])
        self.assertEqual((self.local / "local").read_text(), "new local commit")

    def test_branch_switch_after_history_check_is_not_updated(self):
        initial = self.run_git(self.local, "rev-parse", "HEAD")
        self.rewrite()
        self.run_git(self.local, "config", "agentSync.allowRewrittenUpstream", "true")
        switched = []

        def racing_git(root, *args, **kwargs):
            result = _git(root, *args, **kwargs)
            if args[0] == "rev-list" and not switched:
                self.run_git(self.local, "switch", "-c", "other")
                switched.append(True)
            return result

        result = sync_repo(self.local, git=racing_git)
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.run_git(self.local, "branch", "--show-current"), "other")
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), initial)
        self.assertEqual(self.run_git(self.local, "rev-parse", "main"), initial)

    def test_git_commit_and_switch_are_locked_during_checkout(self):
        target = self.advance()
        blocked = []

        def locked_git(root, *args, **kwargs):
            if args[0] == "read-tree" and not blocked:
                for command in (
                    ("commit", "--allow-empty", "-m", "race"),
                    ("switch", "-c", "other"),
                    ("symbolic-ref", "HEAD", "refs/heads/other"),
                ):
                    with self.assertRaises(subprocess.CalledProcessError):
                        self.run_git(self.local, *command)
                    blocked.append(command[0])
            return _git(root, *args, **kwargs)

        result = sync_repo(self.local, git=locked_git)
        self.assertTrue(result["ok"], result)
        self.assertEqual(blocked, ["commit", "switch", "symbolic-ref"])
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), target)

    def test_ignored_path_with_leading_space_is_preserved(self):
        self.rewrite(" ignored")
        (self.local / ".git/info/exclude").write_text(" ignored\n")
        (self.local / " ignored").write_text("private ignored data")
        self.run_git(self.local, "config", "agentSync.allowRewrittenUpstream", "true")
        result = sync_repo(self.local)
        self.assertFalse(result["ok"], result)
        self.assertEqual((self.local / " ignored").read_text(), "private ignored data")
        self.assertFalse(
            self.run_git(self.local, "for-each-ref", "refs/agent-sync/backups/")
        )

    def test_signature_policy_is_not_bypassed(self):
        initial = self.run_git(self.local, "rev-parse", "HEAD")
        self.advance()
        self.run_git(
            self.local, "config", "branch.main.mergeOptions", "--verify-signatures"
        )
        result = sync_repo(self.local)
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), initial)

    def test_post_merge_hook_runs_after_index_lock_is_released(self):
        target = self.advance()
        hook = self.local / ".git/hooks/post-merge"
        hook.write_text(
            "#!/bin/sh\ntest ! -e .git/index.lock || exit 1\n"
            "git diff --quiet || exit 1\nprintf done > .git/hook-ran\n"
        )
        hook.chmod(0o700)
        result = sync_repo(self.local)
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), target)
        self.assertEqual((self.local / ".git/hook-ran").read_text(), "done")

    def test_partial_checkout_failure_preserves_recovery_and_edits(self):
        initial = self.run_git(self.local, "rev-parse", "HEAD")
        self.advance()
        interrupted = []

        def failing_git(root, *args, **kwargs):
            if args[0] == "read-tree" and not interrupted:
                interrupted.append(True)
                (self.local / "file").write_text("partial checkout or concurrent edit")
                raise RuntimeError("simulated checkout failure")
            return _git(root, *args, **kwargs)

        result = sync_repo(self.local, git=failing_git)
        self.assertFalse(result["ok"], result)
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), initial)
        self.assertEqual(
            (self.local / "file").read_text(), "partial checkout or concurrent edit"
        )
        self.assertTrue(
            list((self.local / ".git/agent-sync-recovery").glob("*/before.index"))
        )
        self.assertFalse((self.local / ".git/index.lock").exists())

    def test_index_install_failure_does_not_rewind_advanced_reference(self):
        target = self.advance()
        with patch(
            "scripts.sync_upstream.os.replace", side_effect=OSError("index install")
        ):
            result = sync_repo(self.local)
        self.assertFalse(result["ok"], result)
        self.assertIn("Reference advanced", result["message"])
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), target)
        self.assertEqual((self.local / "file").read_text(), "second")
        self.assertTrue(
            list((self.local / ".git/agent-sync-recovery").glob("*/next.index"))
        )
        self.assertFalse((self.local / ".git/index.lock").exists())

    def test_rewind_is_a_rewrite(self):
        self.advance()
        self.run_git(self.local, "fetch", "origin")
        self.run_git(self.local, "merge", "--ff-only", "origin/main")
        old = self.run_git(self.local, "rev-parse", "HEAD")
        self.run_git(self.source, "push", "--force", "origin", "HEAD~1:main")
        self.assertFalse(sync_repo(self.local)["ok"])
        self.run_git(self.local, "config", "agentSync.allowRewrittenUpstream", "true")
        result = sync_repo(self.local)
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            self.run_git(self.local, "rev-parse", result["backup_ref"]), old
        )

    def test_local_commits_preserved_and_divergence_blocked(self):
        local = self.commit(self.local, "local", "local")
        self.assertTrue(sync_repo(self.local)["ok"])
        self.advance()
        self.run_git(self.local, "config", "agentSync.allowRewrittenUpstream", "true")
        self.assertFalse(sync_repo(self.local)["ok"])
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), local)

    def test_dirty_and_fetch_races(self):
        self.advance()
        (self.local / "file").write_text("dirty")
        self.assertFalse(sync_repo(self.local)["ok"])
        self.run_git(self.local, "checkout", "--", "file")

        def dirty_during_fetch(root, *args, **kwargs):
            result = _git(root, *args, **kwargs)
            if args[0] == "fetch":
                (root / "file").write_text("race")
            return result

        self.assertFalse(sync_repo(self.local, git=dirty_during_fetch)["ok"])
        self.assertEqual((self.local / "file").read_text(), "race")
        self.run_git(self.local, "checkout", "--", "file")

        def branch_during_fetch(root, *args, **kwargs):
            result = _git(root, *args, **kwargs)
            if args[0] == "fetch":
                _git(root, "checkout", "-b", "other")
            return result

        self.assertFalse(sync_repo(self.local, git=branch_during_fetch)["ok"])
        self.assertEqual(self.run_git(self.local, "branch", "--show-current"), "other")

    def test_ignored_collisions_and_symlinked_ancestor(self):
        self.advance("ignored/target")
        (self.local / ".git/info/exclude").write_text("ignored\n")
        (self.local / "ignored").mkdir()
        (self.local / "ignored/target").write_text("keep")
        self.assertFalse(sync_repo(self.local)["ok"])
        self.assertEqual((self.local / "ignored/target").read_text(), "keep")
        (self.local / "ignored/target").unlink()
        (self.local / "ignored").rmdir()
        (self.local / "ignored").symlink_to(self.base, target_is_directory=True)
        self.assertFalse(sync_repo(self.local)["ok"])
        self.assertTrue((self.local / "ignored").is_symlink())

    def test_rewrite_ignored_collision(self):
        self.rewrite("ignored")
        (self.local / ".git/info/exclude").write_text("ignored\n")
        (self.local / "ignored").write_text("keep")
        self.run_git(self.local, "config", "agentSync.allowRewrittenUpstream", "true")
        self.assertFalse(sync_repo(self.local)["ok"])
        self.assertEqual((self.local / "ignored").read_text(), "keep")
        self.assertFalse(
            self.run_git(self.local, "for-each-ref", "refs/agent-sync/backups/")
        )

    def test_missing_upstream_and_lock(self):
        lock_dir = self.local / ".git/agent-sync-locks"
        lock_dir.mkdir()
        lock_path = lock_dir / (
            hashlib.sha256(os.fsencode(self.local)).hexdigest() + ".lock"
        )
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = sync_repo(self.local)
            self.assertFalse(result["ok"])
        self.run_git(self.local, "branch", "--unset-upstream")
        self.assertFalse(sync_repo(self.local)["ok"])

    def test_upstream_without_tracking_mapping_allows_only_fast_forward(self):
        self.run_git(
            self.local,
            "config",
            "remote.origin.fetch",
            "+refs/heads/other:refs/remotes/origin/other",
        )
        target = self.advance()
        result = sync_repo(self.local)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["head"], target)
        self.rewrite()
        self.run_git(self.local, "config", "agentSync.allowRewrittenUpstream", "true")
        self.assertFalse(sync_repo(self.local)["ok"])
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), target)

    def test_history_comparison_failure_never_allows_rewrite(self):
        old = self.run_git(self.local, "rev-parse", "HEAD")
        self.rewrite()
        self.run_git(self.local, "config", "agentSync.allowRewrittenUpstream", "true")

        def broken_history(root, *args, **kwargs):
            if args[0] == "rev-list":
                raise RuntimeError("secret url and credentials")
            return _git(root, *args, **kwargs)

        result = sync_repo(self.local, git=broken_history)
        self.assertFalse(result["ok"])
        self.assertIn("history comparison", result["message"])
        self.assertNotIn("secret", result["message"])
        self.assertEqual(self.run_git(self.local, "rev-parse", "HEAD"), old)
        self.assertFalse(
            self.run_git(self.local, "for-each-ref", "refs/agent-sync/backups/")
        )

    def test_fetch_error_is_phase_specific_and_redacted(self):
        def broken_fetch(root, *args, **kwargs):
            if args[0] == "fetch":
                raise RuntimeError("secret url and credentials")
            return _git(root, *args, **kwargs)

        result = sync_repo(self.local, git=broken_fetch)
        self.assertFalse(result["ok"])
        self.assertIn("fetch", result["message"])
        self.assertNotIn("secret", result["message"])

    def test_two_syncs_do_not_overlap(self):
        self.advance()
        entered, release = threading.Event(), threading.Event()
        result = []

        def paused_git(root, *args, **kwargs):
            if args[0] == "fetch":
                entered.set()
                release.wait(5)
            return _git(root, *args, **kwargs)

        thread = threading.Thread(
            target=lambda: result.append(sync_repo(self.local, git=paused_git))
        )
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertFalse(sync_repo(self.local)["ok"])
        finally:
            release.set()
            thread.join()
        self.assertTrue(result[0]["ok"], result)


if __name__ == "__main__":
    unittest.main()
