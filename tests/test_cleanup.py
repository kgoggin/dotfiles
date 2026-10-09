import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "git-cleanup"


class CleanupTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="git-cleanup-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
        self.git("init", "--bare", str(self.root / "origin"), cwd=self.root)
        self.git("init", "-b", "main", str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Cleanup Test")
        self.git("config", "user.email", "cleanup@example.com")
        self.git("config", "core.hooksPath", os.devnull)
        self.git("config", "commit.gpgsign", "false")
        self.git("commit", "--allow-empty", "-m", "Initial")
        self.git("remote", "add", "origin", str(self.root / "origin"))
        self.git("push", "-u", "origin", "main")
        # An installed git-cleanup on PATH can take precedence over its alias.
        self.git("config", "alias.cleanup-test", "!" + shlex.quote(str(SCRIPT)))

    def git(self, *args, cwd=None, check=True, input=None):
        return subprocess.run(["git", *args], cwd=cwd or self.repo,
                              env=self.env, input=input, text=True,
                              capture_output=True, check=check)

    def worktree(self, branch="topic", name="linked worktree"):
        path = self.root / name
        self.git("worktree", "add", "-b", branch, str(path))
        return path

    def has_branch(self, branch):
        return self.git("show-ref", "--verify", "--quiet",
                        "refs/heads/" + branch, check=False).returncode == 0

    def cleanup(self, answer="", cwd=None):
        return self.git("cleanup-test", input=answer, check=False, cwd=cwd)

    def squash_worktree(self):
        path = self.worktree()
        (path / "feature.txt").write_text("Merged feature\n")
        self.git("add", "feature.txt", cwd=path)
        self.git("commit", "-m", "Feature", cwd=path)
        tip = self.git("rev-parse", "topic").stdout.strip()
        self.git("merge", "--squash", "topic")
        self.git("commit", "-m", "Squash feature")
        merged = self.git("rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(self.git("merge-base", "--is-ancestor", "topic", "HEAD",
                                     check=False).returncode, 0)
        self.git("config", "remote.origin.url", "https://github.com/example/repo.git")
        self.git("config", "url." + str(self.root / "origin") + ".insteadOf",
                 "https://github.com/example/repo.git")
        fake_bin = self.root / "bin"
        fake_bin.mkdir()
        gh = fake_bin / "gh"
        gh.write_text('#!/bin/sh\nprintf "%s" "$CLEANUP_TEST_PRS"\n'
                      'exit "${CLEANUP_TEST_GH_STATUS:-0}"\n')
        gh.chmod(0o755)
        self.env["PATH"] = str(fake_bin) + os.pathsep + self.env["PATH"]
        self.env["CLEANUP_TEST_PRS"] = f"topic\t{tip}\t{merged}\t123\n"
        return path

    def test_squash_merged_worktree_is_offered_and_removed(self):
        path = self.squash_worktree()
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("merged PR #123", result.stdout)
        self.assertFalse(path.exists())
        self.assertFalse(self.has_branch("topic"))

    def test_squash_merged_worktree_no_keeps_both(self):
        path = self.squash_worktree()
        result = self.cleanup("n\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Remove worktree", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_dirty_squash_worktree_is_preserved(self):
        path = self.squash_worktree()
        (path / "feature.txt").write_text("New uncommitted work\n")
        result = self.cleanup("y\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((path / "feature.txt").read_text(), "New uncommitted work\n")
        self.assertTrue(self.has_branch("topic"))

    def test_commits_after_squash_merge_are_preserved(self):
        path = self.squash_worktree()
        self.git("commit", "--allow-empty", "-m", "New work", cwd=path)
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Remove worktree", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_squash_merge_absent_from_checkout_is_not_offered(self):
        path = self.squash_worktree()
        self.git("reset", "--hard", "HEAD^")
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Remove worktree", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_github_failure_keeps_squash_worktree(self):
        path = self.squash_worktree()
        self.env["CLEANUP_TEST_GH_STATUS"] = "1"
        result = self.cleanup("y\n")
        self.assertIn("Could not check GitHub", result.stderr)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_dry_run_keeps_branches_and_worktrees(self):
        path = self.squash_worktree()
        self.git("branch", "ordinary")
        result = self.git("cleanup-test", "--dry-run", input="y\n", check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Would remove worktree", result.stdout)
        self.assertIn("Would delete merged branch ordinary", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))
        self.assertTrue(self.has_branch("ordinary"))

    def test_failed_remote_prune_still_prompts_for_worktrees(self):
        path = self.squash_worktree()
        fake_bin = self.root / "bin"
        real_git = shutil.which("git")
        git = fake_bin / "git"
        git.write_text('#!/bin/sh\n'
                       'if [ "$1" = remote ] && [ "$2" = prune ]; then\n'
                       '  echo "error: could not delete references" >&2\n'
                       '  echo " * [pruned] origin/example"\n'
                       '  exit 1\n'
                       'fi\n'
                       'exec ' + shlex.quote(real_git) + ' "$@"\n')
        git.chmod(0o755)
        self.env["GIT_EXEC_PATH"] = str(fake_bin)
        result = self.cleanup("n\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Remote ref pruning failed; continuing", result.stderr)
        self.assertIn("Remove worktree", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_yes_removes_worktree_then_branch(self):
        path = self.worktree(name='linked "worktree"\nwith newline')
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertFalse(self.has_branch("topic"))

    def test_no_blank_and_eof_keep_both(self):
        path = self.worktree()
        for answer in ("n\n", "\n", ""):
            with self.subTest(answer=answer):
                self.assertEqual(self.cleanup(answer).returncode, 0)
                self.assertTrue(path.exists())
                self.assertTrue(self.has_branch("topic"))

    def test_invalid_answer_reprompts(self):
        path = self.worktree()
        result = self.cleanup("maybe\ny\n")
        self.assertIn("Please answer y or n", result.stdout)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(path.exists())
        self.assertFalse(self.has_branch("topic"))

    def test_separate_answers_for_multiple_worktrees(self):
        first = self.worktree("a", "first")
        second = self.worktree("b", "second")
        result = self.cleanup("n\ny\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(first.exists())
        self.assertTrue(self.has_branch("a"))
        self.assertFalse(second.exists())
        self.assertFalse(self.has_branch("b"))

    def test_dirty_worktree_is_preserved(self):
        path = self.worktree()
        (path / "untracked.txt").write_text("Keep this work")
        result = self.cleanup("y\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((path / "untracked.txt").read_text(), "Keep this work")
        self.assertTrue(self.has_branch("topic"))

    def test_locked_worktree_is_preserved(self):
        path = self.worktree()
        self.git("worktree", "lock", str(path))
        self.assertNotEqual(self.cleanup("y\n").returncode, 0)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_unmerged_worktree_is_not_offered(self):
        path = self.worktree()
        self.git("commit", "--allow-empty", "-m", "Unmerged", cwd=path)
        result = self.cleanup("y\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Remove worktree", result.stdout)
        self.assertTrue(path.exists())
        self.assertTrue(self.has_branch("topic"))

    def test_main_current_and_develop_are_protected(self):
        self.git("switch", "-c", "primary-topic")
        current = self.worktree("current-topic", "current")
        develop = self.worktree("develop", "develop")
        result = self.cleanup("y\ny\ny\n", cwd=current)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Remove worktree", result.stdout)
        for branch in ("main", "develop", "primary-topic", "current-topic"):
            self.assertTrue(self.has_branch(branch))
        for path in (self.repo, current, develop):
            self.assertTrue(path.exists())

    def test_plain_merged_branch_and_remote_refs_are_pruned(self):
        self.git("branch", "topic")
        self.git("tag", "topic")  # Ambiguous short ref names must still work.
        self.git("push", "origin", "refs/heads/topic:refs/heads/topic")
        self.git("update-ref", "-d", "refs/heads/topic", cwd=self.root / "origin")
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.has_branch("topic"))
        self.assertNotEqual(self.git("show-ref", "--verify", "--quiet",
                                    "refs/remotes/origin/topic", check=False).returncode, 0)

    def test_missing_worktree_registration_is_pruned(self):
        path = self.worktree()
        shutil.rmtree(path)
        result = self.cleanup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Remove worktree", result.stdout)
        self.assertFalse(self.has_branch("topic"))
        self.assertNotIn(str(path), self.git("worktree", "list", "--porcelain").stdout)


if __name__ == "__main__":
    unittest.main()
